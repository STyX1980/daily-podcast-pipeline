"""
Dropbox → Podcast RSS → Spotify Pipeline
-----------------------------------------
1. Checks a shared Dropbox folder for new .mp3 files
2. Uses Claude API to generate episode title + description from filename
3. Uploads the mp3 to your web server via FTP/SFTP
4. Appends a new <item> to the RSS feed XML on your server
5. Spotify polls the RSS and picks up the new episode automatically

Expected filename convention from contributors:
    YYYY-MM-DD_AuthorName_Short-Topic-Description.mp3
    e.g.  2026-05-01_Paz_Water-Chemistry-Membrane-Fouling.mp3

Required GitHub Secrets (set in repo Settings → Secrets → Actions):
    DROPBOX_APP_KEY
    DROPBOX_APP_SECRET
    DROPBOX_REFRESH_TOKEN
    ANTHROPIC_API_KEY
    FTP_HOST
    FTP_USER
    FTP_PASS
    FTP_REMOTE_DIR        e.g. /public_html/podcast/audio/
    RSS_REMOTE_PATH       e.g. /public_html/podcast/feed.xml
    PODCAST_BASE_URL      e.g. https://yoursite.com/podcast/audio/
    RSS_FEED_URL          e.g. https://yoursite.com/podcast/feed.xml
    PODCAST_TITLE         e.g. Water Research Lab Podcast
    PODCAST_DESCRIPTION   e.g. Research discussions from the lab
    PODCAST_AUTHOR        e.g. Your Name
    PODCAST_EMAIL         e.g. you@example.com
"""

import os
import json
import ftplib
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

# ── Config from environment ───────────────────────────────────────────────────

DROPBOX_APP_KEY      = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET   = os.environ["DROPBOX_APP_SECRET"]
DROPBOX_REFRESH_TOKEN= os.environ["DROPBOX_REFRESH_TOKEN"]
DROPBOX_FOLDER       = os.environ.get("DROPBOX_FOLDER", "/podcast-uploads")

ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]

FTP_HOST             = os.environ["FTP_HOST"]
FTP_USER             = os.environ["FTP_USER"]
FTP_PASS             = os.environ["FTP_PASS"]
FTP_REMOTE_DIR       = os.environ["FTP_REMOTE_DIR"]   # remote dir for mp3s
RSS_REMOTE_PATH      = os.environ["RSS_REMOTE_PATH"]  # full remote path to feed.xml

PODCAST_BASE_URL     = os.environ["PODCAST_BASE_URL"].rstrip("/") + "/"
RSS_FEED_URL         = os.environ["RSS_FEED_URL"]
PODCAST_TITLE        = os.environ["PODCAST_TITLE"]
PODCAST_DESCRIPTION  = os.environ["PODCAST_DESCRIPTION"]
PODCAST_AUTHOR       = os.environ["PODCAST_AUTHOR"]
PODCAST_EMAIL        = os.environ["PODCAST_EMAIL"]

# Local state file — tracks which Dropbox files have already been processed
STATE_FILE           = Path("processed_files.json")


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> set:
    """Load the set of already-processed Dropbox file IDs."""
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()

def save_state(processed: set):
    STATE_FILE.write_text(json.dumps(list(processed), indent=2))


# ── Dropbox ───────────────────────────────────────────────────────────────────

def get_dropbox_client() -> dropbox.Dropbox:
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )

def list_new_mp3s(dbx: dropbox.Dropbox, processed: set) -> list[dict]:
    """Return list of new mp3 file metadata dicts not yet processed."""
    new_files = []
    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER)
        entries = result.entries
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries += result.entries
    except ApiError as e:
        log.error(f"Dropbox list_folder error: {e}")
        return []

    for entry in entries:
        if (
            isinstance(entry, dropbox.files.FileMetadata)
            and entry.name.lower().endswith(".mp3")
            and entry.id not in processed
        ):
            new_files.append({
                "id":   entry.id,
                "name": entry.name,
                "path": entry.path_lower,
                "size": entry.size,
            })
    return new_files

def download_mp3(dbx: dropbox.Dropbox, path: str, local_path: Path):
    """Download a file from Dropbox to a local path."""
    log.info(f"Downloading {path} from Dropbox...")
    metadata, response = dbx.files_download(path)
    local_path.write_bytes(response.content)
    log.info(f"Downloaded {local_path} ({local_path.stat().st_size // 1024} KB)")


# ── Claude API — title + description generation ───────────────────────────────

def parse_filename(filename: str) -> dict:
    """
    Parse filename convention: YYYY-MM-DD_Author_Topic-Words.mp3
    Returns dict with date, author, topic_raw.
    Falls back gracefully if convention not followed.
    """
    stem = Path(filename).stem  # remove .mp3
    parts = stem.split("_", 2)
    result = {"date": "", "author": "", "topic_raw": stem}
    if len(parts) >= 1:
        result["date"] = parts[0]
    if len(parts) >= 2:
        result["author"] = parts[1]
    if len(parts) >= 3:
        result["topic_raw"] = parts[2].replace("-", " ")
    return result

def generate_episode_metadata(filename: str) -> dict:
    """
    Call Claude API to generate a good episode title and description
    from the filename. Returns {"title": ..., "description": ...}.
    """
    parsed = parse_filename(filename)
    prompt = f"""A podcast episode audio file was uploaded with this filename:
"{filename}"

Parsed info:
- Date: {parsed['date']}
- Author/contributor: {parsed['author']}
- Topic hint: {parsed['topic_raw']}

This is an academic/research podcast about water chemistry, geochemistry, 
membrane science, and related environmental engineering topics.

Generate:
1. A concise, engaging episode TITLE (max 80 characters). Should sound like a 
   real podcast episode — not just the raw filename words.
2. A SHORT episode DESCRIPTION (2-3 sentences). Mention the contributor name 
   and topic. Sound professional but approachable.

Respond ONLY with valid JSON, no markdown, no extra text:
{{"title": "...", "description": "..."}}"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning(f"Claude returned non-JSON, using fallback. Response: {text}")
        parsed = parse_filename(filename)
        return {
            "title": parsed["topic_raw"].title(),
            "description": f"Episode by {parsed['author']} on {parsed['topic_raw']}.",
        }


# ── FTP upload ────────────────────────────────────────────────────────────────

def ftp_upload_mp3(local_path: Path, remote_filename: str) -> str:
    """Upload mp3 to web server via FTP. Returns the public URL."""
    log.info(f"Uploading {local_path.name} to FTP...")
    with ftplib.FTP(FTP_HOST, FTP_USER, FTP_PASS) as ftp:
        ftp.cwd(FTP_REMOTE_DIR)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_filename}", f)
    public_url = PODCAST_BASE_URL + remote_filename
    log.info(f"Uploaded → {public_url}")
    return public_url

def ftp_download_rss() -> str | None:
    """Download current RSS feed.xml from server. Returns content or None."""
    log.info("Fetching current RSS feed from server...")
    try:
        with ftplib.FTP(FTP_HOST, FTP_USER, FTP_PASS) as ftp:
            lines = []
            ftp.retrlines(f"RETR {RSS_REMOTE_PATH}", lines.append)
        return "\n".join(lines)
    except ftplib.error_perm:
        log.info("No existing RSS feed found — will create fresh one.")
        return None

def ftp_upload_rss(xml_content: str):
    """Upload updated RSS feed.xml to server."""
    log.info("Uploading updated RSS feed...")
    with ftplib.FTP(FTP_HOST, FTP_USER, FTP_PASS) as ftp:
        import io
        data = xml_content.encode("utf-8")
        ftp.storbinary(f"STOR {RSS_REMOTE_PATH}", io.BytesIO(data))
    log.info("RSS feed updated ✓")


# ── RSS feed management ───────────────────────────────────────────────────────

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

def make_fresh_rss() -> ET.Element:
    """Create a brand-new RSS feed root element."""
    ET.register_namespace("itunes", ITUNES_NS)
    ET.register_namespace("content", CONTENT_NS)

    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": ITUNES_NS,
        "xmlns:content": CONTENT_NS,
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text          = PODCAST_TITLE
    ET.SubElement(channel, "description").text    = PODCAST_DESCRIPTION
    ET.SubElement(channel, "link").text           = RSS_FEED_URL
    ET.SubElement(channel, "language").text       = "en-us"
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = PODCAST_AUTHOR
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text  = PODCAST_AUTHOR
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = PODCAST_EMAIL
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "no"
    ET.SubElement(channel, f"{{{ITUNES_NS}}}category", attrib={"text": "Science"})
    return rss

def get_or_create_rss() -> tuple[ET.Element, ET.Element]:
    """
    Returns (rss_root, channel_element).
    Loads existing feed from server or creates a fresh one.
    """
    existing = ftp_download_rss()
    if existing:
        try:
            ET.register_namespace("itunes", ITUNES_NS)
            ET.register_namespace("content", CONTENT_NS)
            rss = ET.fromstring(existing)
            channel = rss.find("channel")
            return rss, channel
        except ET.ParseError as e:
            log.warning(f"Could not parse existing RSS, creating fresh: {e}")
    rss = make_fresh_rss()
    channel = rss.find("channel")
    return rss, channel

def add_episode_to_rss(
    channel: ET.Element,
    title: str,
    description: str,
    mp3_url: str,
    file_size: int,
    pub_date: datetime,
    guid: str,
):
    """Insert a new <item> at the top of the channel (newest first)."""
    item = ET.Element("item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "description").text = description
    ET.SubElement(item, "pubDate").text     = format_datetime(pub_date)
    ET.SubElement(item, "guid", attrib={"isPermaLink": "false"}).text = guid
    ET.SubElement(item, "enclosure", attrib={
        "url":    mp3_url,
        "length": str(file_size),
        "type":   "audio/mpeg",
    })
    ET.SubElement(item, f"{{{ITUNES_NS}}}title").text   = title
    ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = description
    ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "no"

    # Insert after last existing <item> header tags but before other items
    # (i.e., prepend among items so newest is first)
    children = list(channel)
    # Find index of first existing <item>
    first_item_idx = next(
        (i for i, c in enumerate(children) if c.tag == "item"), len(children)
    )
    channel.insert(first_item_idx, item)

def rss_to_string(rss: ET.Element) -> str:
    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss, encoding="unicode", xml_declaration=False
    )


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_file(dbx, file_info: dict, processed: set):
    """Full pipeline for a single new mp3 file."""
    filename  = file_info["name"]
    file_id   = file_info["id"]
    file_size = file_info["size"]

    log.info(f"━━━ Processing: {filename}")

    # 1. Generate a stable remote filename (sanitize)
    safe_name = filename.replace(" ", "_")

    # 2. Download from Dropbox
    local_path = Path(f"/tmp/{safe_name}")
    download_mp3(dbx, file_info["path"], local_path)

    # 3. Generate title + description with Claude
    log.info("Generating episode metadata with Claude...")
    meta = generate_episode_metadata(filename)
    log.info(f"  Title: {meta['title']}")
    log.info(f"  Desc:  {meta['description']}")

    # 4. Upload mp3 to web server
    public_url = ftp_upload_mp3(local_path, safe_name)

    # 5. Update RSS feed
    rss, channel = get_or_create_rss()
    guid = hashlib.md5(file_id.encode()).hexdigest()
    add_episode_to_rss(
        channel=channel,
        title=meta["title"],
        description=meta["description"],
        mp3_url=public_url,
        file_size=file_size,
        pub_date=datetime.now(timezone.utc),
        guid=guid,
    )
    ftp_upload_rss(rss_to_string(rss))

    # 6. Mark as processed
    processed.add(file_id)
    save_state(processed)
    log.info(f"✓ Done: {filename}")

    # Cleanup local temp file
    local_path.unlink(missing_ok=True)


def main():
    log.info("=== Dropbox → Podcast Pipeline ===")
    processed = load_state()
    dbx = get_dropbox_client()
    new_files = list_new_mp3s(dbx, processed)

    if not new_files:
        log.info("No new files found. Nothing to do.")
        return

    log.info(f"Found {len(new_files)} new file(s).")
    for file_info in new_files:
        try:
            process_file(dbx, file_info, processed)
        except Exception as e:
            log.error(f"Failed to process {file_info['name']}: {e}", exc_info=True)
            # Continue with next file even if one fails

    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()
