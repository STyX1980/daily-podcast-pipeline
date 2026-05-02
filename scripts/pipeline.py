"""
Dropbox → GitHub Pages → Spotify Pipeline
------------------------------------------
1. Checks a shared Dropbox folder for new .mp3 files
2. Uses Claude API to generate episode title + description from filename
3. Commits the mp3 + updated RSS feed directly to the gh-pages branch
4. GitHub Pages serves them publicly
5. Spotify polls the RSS and picks up the new episode automatically

Expected filename convention from contributors:
    YYYY-MM-DD_AuthorName_Short-Topic-Description.mp3
    e.g.  2026-05-01_Paz_Water-Chemistry-Membrane-Fouling.mp3

Required GitHub Secrets:
    DROPBOX_APP_KEY
    DROPBOX_APP_SECRET
    DROPBOX_REFRESH_TOKEN
    ANTHROPIC_API_KEY
    PODCAST_TITLE         e.g. Water Research Lab Podcast
    PODCAST_DESCRIPTION   e.g. Research discussions from the lab
    PODCAST_AUTHOR        e.g. Your Name
    PODCAST_EMAIL         e.g. you@example.com
    GH_REPO               e.g. yourusername/daily-podcast-pipeline
    GH_PAGES_TOKEN        a GitHub Personal Access Token with repo scope
"""

import os
import base64
import json
import hashlib
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import dropbox
from dropbox.exceptions import ApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DROPBOX_APP_KEY       = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET    = os.environ["DROPBOX_APP_SECRET"]
DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]
DROPBOX_FOLDER        = os.environ.get("DROPBOX_FOLDER", "")
DROPBOX_FOLDER        = "" if DROPBOX_FOLDER in ("", "/") else DROPBOX_FOLDER

ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]

PODCAST_TITLE         = os.environ["PODCAST_TITLE"]
PODCAST_DESCRIPTION   = os.environ["PODCAST_DESCRIPTION"]
PODCAST_AUTHOR        = os.environ["PODCAST_AUTHOR"]
PODCAST_EMAIL         = os.environ["PODCAST_EMAIL"]

GH_REPO               = os.environ["GH_REPO"]        # e.g. "paz/daily-podcast-pipeline"
GH_PAGES_TOKEN        = os.environ["GH_PAGES_TOKEN"]
GH_BRANCH             = "gh-pages"

_owner, _reponame     = GH_REPO.split("/", 1)
GH_PAGES_BASE         = f"https://{_owner}.github.io/{_reponame}"
PODCAST_BASE_URL      = f"{GH_PAGES_BASE}/audio/"
RSS_FEED_URL          = f"{GH_PAGES_BASE}/feed.xml"

STATE_FILE            = Path("processed_files.json")

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()

def save_state(processed: set):
    STATE_FILE.write_text(json.dumps(list(processed), indent=2))

# ── Dropbox ───────────────────────────────────────────────────────────────────

def get_dropbox_client():
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )

def list_new_mp3s(dbx, processed: set) -> list:
    new_files = []
    try:
        result  = dbx.files_list_folder(DROPBOX_FOLDER)
        entries = result.entries
        while result.has_more:
            result   = dbx.files_list_folder_continue(result.cursor)
            entries += result.entries
    except ApiError as e:
        log.error(f"Dropbox error: {e}")
        return []
    for entry in entries:
        if (
            isinstance(entry, dropbox.files.FileMetadata)
            and entry.name.lower().endswith((".mp3", ".m4a"))
            and entry.id not in processed
        ):
            new_files.append({"id": entry.id, "name": entry.name,
                               "path": entry.path_lower, "size": entry.size})
    return new_files

def download_mp3(dbx, path: str) -> bytes:
    log.info(f"Downloading {path} ...")
    _, response = dbx.files_download(path)
    data = response.content
    log.info(f"  {len(data)//1024} KB downloaded")
    return data

# ── Claude ────────────────────────────────────────────────────────────────────

def parse_filename(filename: str) -> dict:
    stem   = Path(filename).stem
    parts  = stem.split("_", 2)
    result = {"date": "", "author": "", "topic_raw": stem}
    if len(parts) >= 1: result["date"]      = parts[0]
    if len(parts) >= 2: result["author"]    = parts[1]
    if len(parts) >= 3: result["topic_raw"] = parts[2].replace("-", " ")
    return result

def generate_episode_metadata(filename: str) -> dict:
    parsed = parse_filename(filename)
    prompt = f"""A podcast episode was uploaded with filename: "{filename}"
Date: {parsed['date']} | Author: {parsed['author']} | Topic: {parsed['topic_raw']}

This is an academic research podcast on water chemistry, geochemistry, and membrane science.

Return ONLY valid JSON (no markdown):
{{"title": "engaging title max 80 chars", "description": "2-3 sentence description mentioning author and topic"}}"""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": "claude-sonnet-4-5", "max_tokens": 300,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"title": parsed["topic_raw"].title(),
                "description": f"Episode by {parsed['author']} on {parsed['topic_raw']}."}

# ── GitHub Pages API ──────────────────────────────────────────────────────────

def gh_headers():
    return {"Authorization": f"Bearer {GH_PAGES_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}

def gh_get_file(remote_path: str):
    """Returns (content_str, sha) or (None, None)."""
    r = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{remote_path}",
        headers=gh_headers(), params={"ref": GH_BRANCH}
    )
    if r.status_code == 404:
        return None, None
    if not r.ok:
        raise Exception(f"GitHub API error {r.status_code}: {r.text}")
    d = r.json()
    return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]

def gh_put_file(remote_path: str, content_bytes: bytes, message: str, sha=None):
    payload = {"message": message, "branch": GH_BRANCH,
               "content": base64.b64encode(content_bytes).decode("utf-8")}
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/contents/{remote_path}",
        headers=gh_headers(), json=payload
    )
    r.raise_for_status()
    log.info(f"  Committed: {remote_path}")

def ensure_gh_pages_branch():
    r = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/branches/gh-pages",
        headers=gh_headers()
    )
    if r.status_code == 200:
        log.info("gh-pages branch exists ✓")
        return
    log.info("Creating gh-pages branch...")
    repo_r = requests.get(f"https://api.github.com/repos/{GH_REPO}", headers=gh_headers())
    repo_r.raise_for_status()
    default = repo_r.json()["default_branch"]
    ref_r   = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/git/ref/heads/{default}",
        headers=gh_headers()
    )
    ref_r.raise_for_status()
    sha = ref_r.json()["object"]["sha"]
    requests.post(
        f"https://api.github.com/repos/{GH_REPO}/git/refs",
        headers=gh_headers(),
        json={"ref": "refs/heads/gh-pages", "sha": sha}
    ).raise_for_status()
    gh_put_file("index.html",
                b"<html><body><h1>Podcast Feed</h1><p>Subscribe in your podcast app.</p></body></html>",
                "chore: init gh-pages")
    log.info("gh-pages branch created ✓")

# ── RSS ───────────────────────────────────────────────────────────────────────

ITUNES_NS  = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

def make_fresh_rss():
    ET.register_namespace("itunes",  ITUNES_NS)
    ET.register_namespace("content", CONTENT_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    ch  = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text                         = PODCAST_TITLE
    ET.SubElement(ch, "description").text                   = PODCAST_DESCRIPTION
    ET.SubElement(ch, "link").text                          = RSS_FEED_URL
    ET.SubElement(ch, "language").text                      = "en-us"
    ET.SubElement(ch, f"{{{ITUNES_NS}}}author").text        = PODCAST_AUTHOR
    owner = ET.SubElement(ch, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text       = PODCAST_AUTHOR
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text      = PODCAST_EMAIL
    ET.SubElement(ch, f"{{{ITUNES_NS}}}explicit").text      = "no"
    ET.SubElement(ch, f"{{{ITUNES_NS}}}category", attrib={"text": "Science"})
    return rss

def get_or_create_rss():
    """Returns (rss_root, channel, sha_or_None)."""
    existing, sha = gh_get_file("feed.xml")
    if existing:
        try:
            ET.register_namespace("itunes",  ITUNES_NS)
            ET.register_namespace("content", CONTENT_NS)
            rss = ET.fromstring(existing)
            return rss, rss.find("channel"), sha
        except ET.ParseError:
            pass
    rss = make_fresh_rss()
    return rss, rss.find("channel"), None

def add_episode(channel, title, description, mp3_url, file_size, pub_date, guid):
    item = ET.Element("item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "description").text = description
    ET.SubElement(item, "pubDate").text     = format_datetime(pub_date)
    ET.SubElement(item, "guid", attrib={"isPermaLink": "false"}).text = guid
    ET.SubElement(item, "enclosure", attrib={"url": mp3_url, "length": str(file_size), "type": "audio/mpeg"})
    ET.SubElement(item, f"{{{ITUNES_NS}}}title").text    = title
    ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text  = description
    ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "no"
    children       = list(channel)
    first_item_idx = next((i for i, c in enumerate(children) if c.tag == "item"), len(children))
    channel.insert(first_item_idx, item)

def rss_to_bytes(rss):
    ET.indent(rss, space="  ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' +
            ET.tostring(rss, encoding="unicode", xml_declaration=False)).encode("utf-8")

# ── Main ──────────────────────────────────────────────────────────────────────

def process_file(dbx, file_info: dict, processed: set):
    filename  = file_info["name"]
    safe_name = filename.replace(" ", "_")
    log.info(f"━━━ {filename}")

    mp3_bytes = download_mp3(dbx, file_info["path"])

    log.info("Generating metadata with Claude...")
    meta = generate_episode_metadata(filename)
    log.info(f"  Title: {meta['title']}")

    log.info("Uploading mp3 to GitHub Pages...")
    _, existing_sha = gh_get_file(f"audio/{safe_name}")
    gh_put_file(f"audio/{safe_name}", mp3_bytes, f"feat: add {safe_name}", existing_sha)
    mp3_url = PODCAST_BASE_URL + safe_name

    log.info("Updating RSS feed...")
    rss, channel, rss_sha = get_or_create_rss()
    add_episode(
        channel=channel, title=meta["title"], description=meta["description"],
        mp3_url=mp3_url, file_size=file_info["size"],
        pub_date=datetime.now(timezone.utc),
        guid=hashlib.md5(file_info["id"].encode()).hexdigest(),
    )
    gh_put_file("feed.xml", rss_to_bytes(rss), f"feat: RSS — {meta['title']}", rss_sha)

    processed.add(file_info["id"])
    save_state(processed)
    log.info(f"✓ Done: {filename}")

def main():
    log.info("=== Dropbox → GitHub Pages → Spotify ===")
    processed = load_state()
    dbx       = get_dropbox_client()

    ensure_gh_pages_branch()

    new_files = list_new_mp3s(dbx, processed)
    if not new_files:
        log.info("No new files. Nothing to do.")
        return

    log.info(f"{len(new_files)} new file(s) found.")
    for f in new_files:
        try:
            process_file(dbx, f, processed)
        except Exception as e:
            log.error(f"Failed: {f['name']} — {e}", exc_info=True)

    log.info("=== Done ===")

if __name__ == "__main__":
    main()
