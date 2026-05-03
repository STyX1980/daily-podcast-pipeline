"""
PDF → Podcast Pipeline (multi-paper synthesis)
------------------------------------------------
1. Detects ALL new PDFs in Dropbox /pdfs/ folder
2. Downloads and extracts text from all of them
3. Claude writes ONE synthesized two-host podcast episode across all papers
4. Edge TTS converts script to audio (Jenny + Guy voices)
5. Merges audio into single mp3, uploads to Dropbox audio folder
6. Moves processed PDFs to /pdfs/old/
7. Updates RSS feed on GitHub Pages → Spotify picks it up

Required GitHub Secrets:
    DROPBOX_APP_KEY
    DROPBOX_APP_SECRET
    DROPBOX_REFRESH_TOKEN
    DROPBOX_AUDIO_FOLDER  e.g. /pEEL/Daily podcast/uploads/audio
    DROPBOX_PDF_FOLDER    e.g. /pEEL/Daily podcast/uploads/pdfs
    ANTHROPIC_API_KEY
    PODCAST_TITLE
    PODCAST_DESCRIPTION
    PODCAST_AUTHOR
    PODCAST_EMAIL
    GH_REPO
    GH_PAGES_TOKEN
"""

import os, base64, json, hashlib, logging, asyncio, tempfile, subprocess
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime
import requests
import xml.etree.ElementTree as ET
import edge_tts
import dropbox
from dropbox.exceptions import ApiError
from dropbox.sharing import CreateSharedLinkWithSettingsError, RequestedVisibility, SharedLinkSettings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DROPBOX_APP_KEY       = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET    = os.environ["DROPBOX_APP_SECRET"]
DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]
DROPBOX_AUDIO_FOLDER  = os.environ["DROPBOX_AUDIO_FOLDER"]
DROPBOX_PDF_FOLDER    = os.environ["DROPBOX_PDF_FOLDER"]
DROPBOX_OLD_FOLDER    = DROPBOX_PDF_FOLDER.rstrip("/") + "/old"

ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]
PODCAST_TITLE         = os.environ["PODCAST_TITLE"]
PODCAST_DESCRIPTION   = os.environ["PODCAST_DESCRIPTION"]
PODCAST_AUTHOR        = os.environ["PODCAST_AUTHOR"]
PODCAST_EMAIL         = os.environ["PODCAST_EMAIL"]

GH_REPO               = os.environ["GH_REPO"]
GH_PAGES_TOKEN        = os.environ["GH_PAGES_TOKEN"]
GH_BRANCH             = "gh-pages"

_owner, _reponame     = GH_REPO.split("/", 1)
GH_PAGES_BASE         = f"https://{_owner}.github.io/{_reponame}"
RSS_FEED_URL          = f"{GH_PAGES_BASE}/feed.xml"

PDF_STATE_FILE        = Path("processed_pdfs.json")

VOICE_FEMALE          = "en-US-JennyNeural"
VOICE_MALE            = "en-US-GuyNeural"

# ── State ─────────────────────────────────────────────────────────────────────

def load_pdf_state() -> set:
    if PDF_STATE_FILE.exists():
        return set(json.loads(PDF_STATE_FILE.read_text()))
    return set()

def save_pdf_state(processed: set):
    PDF_STATE_FILE.write_text(json.dumps(list(processed), indent=2))

# ── Dropbox ───────────────────────────────────────────────────────────────────

def get_dropbox_client():
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )

def list_new_pdfs(dbx, processed: set) -> list:
    new_files = []
    try:
        result  = dbx.files_list_folder(DROPBOX_PDF_FOLDER)
        entries = result.entries
        while result.has_more:
            result   = dbx.files_list_folder_continue(result.cursor)
            entries += result.entries
    except ApiError as e:
        log.error(f"Dropbox error listing PDFs: {e}")
        return []
    for entry in entries:
        if (
            isinstance(entry, dropbox.files.FileMetadata)
            and entry.name.lower().endswith(".pdf")
            and entry.id not in processed
        ):
            new_files.append({
                "id":   entry.id,
                "name": entry.name,
                "path": entry.path_lower,
                "size": entry.size,
            })
    return new_files

def download_file(dbx, path: str) -> bytes:
    log.info(f"  Downloading {path} ...")
    _, response = dbx.files_download(path)
    data = response.content
    log.info(f"    {len(data)//1024} KB")
    return data

def move_to_old(dbx, file_path: str, filename: str):
    """Move a processed PDF to the /old/ subfolder."""
    old_path = f"{DROPBOX_OLD_FOLDER}/{filename}"
    try:
        dbx.files_move_v2(file_path, old_path, autorename=True)
        log.info(f"  Moved to old: {filename}")
    except Exception as e:
        log.warning(f"  Could not move {filename} to old: {e}")

def upload_to_dropbox(dbx, data: bytes, dropbox_path: str):
    dbx.files_upload(data, dropbox_path, mode=dropbox.files.WriteMode.overwrite)
    log.info(f"Uploaded to Dropbox: {dropbox_path}")

def get_or_create_public_link(dbx, dropbox_path: str) -> str:
    def to_direct(url):
        return url.replace("?dl=0", "?dl=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")
    try:
        result = dbx.sharing_list_shared_links(path=dropbox_path, direct_only=True)
        if result.links:
            return to_direct(result.links[0].url)
    except Exception:
        pass
    try:
        settings  = SharedLinkSettings(requested_visibility=RequestedVisibility.public)
        link_meta = dbx.sharing_create_shared_link_with_settings(dropbox_path, settings)
        return to_direct(link_meta.url)
    except Exception:
        result = dbx.sharing_list_shared_links(path=dropbox_path, direct_only=True)
        return to_direct(result.links[0].url)

# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 8000) -> str:
    import pdfplumber, io
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:20]:
            text = page.extract_text()
            if text:
                text_parts.append(text)
    return "\n\n".join(text_parts)[:max_chars]

# ── Claude — multi-paper podcast script ───────────────────────────────────────

SCRIPT_SYSTEM = """You are a podcast script writer for "Daily Science Intake" — 
an academic research podcast aimed at researchers and students in water chemistry, 
geochemistry, membrane science, and environmental engineering.

Write engaging, conversational two-host podcast scripts. The hosts are:
- JENNY: female host, warm and curious, asks great questions, connects concepts to real-world applications
- GUY: male host, analytical and enthusiastic, explains mechanisms clearly, uses good analogies

Style guidelines:
- Conversational and natural, like two smart friends discussing papers over coffee
- Synthesize ACROSS papers — find connections, contrasts, and common themes
- Mix technical depth with accessibility — explain jargon when it comes up
- Include moments of genuine surprise or excitement about the findings
- 10-15 minutes when read aloud (roughly 1500-2200 words of dialogue)
- Start with a hook that connects all the papers, end with unified key takeaways
- NO stage directions, NO [laughter], NO (pause) — just clean dialogue

Output format — strictly alternate lines, each starting with the speaker name:
JENNY: ...
GUY: ...
JENNY: ...
(etc.)"""

def generate_podcast_script(papers: list[dict]) -> tuple[str, str, str]:
    """
    papers: list of {"filename": str, "text": str}
    Returns (script, title, description).
    """
    log.info(f"Generating synthesized podcast script for {len(papers)} paper(s)...")

    papers_block = ""
    for i, p in enumerate(papers, 1):
        papers_block += f"\n\n--- PAPER {i}: {p['filename']} ---\n{p['text']}"

    prompt = f"""Here are {len(papers)} research paper(s) to synthesize into one podcast episode.
Find the common themes, interesting contrasts, and connections between them.
{papers_block}

Write the full synthesized podcast script covering all papers, then on a new line write:
TITLE: <one episode title that captures the common theme across all papers>
DESCRIPTION: <2-3 sentence show note mentioning all papers and their common thread>"""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": 5000,
            "system": SCRIPT_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    r.raise_for_status()
    full_response = r.json()["content"][0]["text"].strip()

    lines       = full_response.split("\n")
    title       = f"Research Digest — {datetime.now(timezone.utc).strftime('%B %d, %Y')}"
    description = ""
    script_lines = []

    for line in lines:
        if line.startswith("TITLE:"):
            title = line.replace("TITLE:", "").strip()
        elif line.startswith("DESCRIPTION:"):
            description = line.replace("DESCRIPTION:", "").strip()
        else:
            script_lines.append(line)

    script = "\n".join(script_lines).strip()
    if not description:
        names = ", ".join(p["filename"].replace(".pdf", "") for p in papers)
        description = f"This episode synthesizes recent research: {names}."

    log.info(f"  Script: {len(script.split())} words")
    log.info(f"  Title: {title}")
    return script, title, description

# ── Edge TTS ──────────────────────────────────────────────────────────────────

def parse_script(script: str) -> list[tuple[str, str]]:
    lines = []
    for line in script.split("\n"):
        line = line.strip()
        if line.startswith("JENNY:"):
            lines.append(("JENNY", line[6:].strip()))
        elif line.startswith("GUY:"):
            lines.append(("GUY", line[4:].strip()))
    return lines

async def text_to_speech_segment(text: str, voice: str, output_path: str):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)

async def generate_audio_segments(lines: list[tuple[str, str]], tmp_dir: str) -> list[str]:
    segment_paths = []
    for i, (speaker, text) in enumerate(lines):
        if not text.strip():
            continue
        voice    = VOICE_FEMALE if speaker == "JENNY" else VOICE_MALE
        out_path = os.path.join(tmp_dir, f"segment_{i:04d}.mp3")
        await text_to_speech_segment(text, voice, out_path)
        segment_paths.append(out_path)
        if i % 10 == 0:
            log.info(f"  TTS progress: {i}/{len(lines)} segments")
    return segment_paths

def merge_audio_segments(segment_paths: list[str], output_path: str):
    log.info(f"Merging {len(segment_paths)} segments...")
    concat_file = output_path + ".txt"
    with open(concat_file, "w") as f:
        for path in segment_paths:
            f.write(f"file '{path}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", concat_file, "-c", "copy", output_path],
        check=True, capture_output=True
    )
    os.unlink(concat_file)
    log.info(f"  Merged: {os.path.getsize(output_path)//1024} KB")

def script_to_mp3(script: str, output_path: str):
    lines = parse_script(script)
    log.info(f"  Parsed {len(lines)} dialogue lines")
    with tempfile.TemporaryDirectory() as tmp_dir:
        segment_paths = asyncio.run(generate_audio_segments(lines, tmp_dir))
        merge_audio_segments(segment_paths, output_path)

# ── GitHub Pages RSS ──────────────────────────────────────────────────────────

ITUNES_NS  = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

def gh_headers():
    return {"Authorization": f"Bearer {GH_PAGES_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}

def gh_get_file(remote_path):
    r = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{remote_path}",
        headers=gh_headers(), params={"ref": GH_BRANCH}
    )
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    d = r.json()
    return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]

def gh_put_file(remote_path, content_bytes, message, sha=None):
    payload = {"message": message, "branch": GH_BRANCH,
               "content": base64.b64encode(content_bytes).decode("utf-8")}
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/contents/{remote_path}",
        headers=gh_headers(), json=payload
    )
    r.raise_for_status()
    log.info(f"  Committed to GitHub: {remote_path}")

def get_or_create_rss():
    existing, sha = gh_get_file("feed.xml")
    if existing:
        try:
            ET.register_namespace("itunes",  ITUNES_NS)
            ET.register_namespace("content", CONTENT_NS)
            rss = ET.fromstring(existing)
            return rss, rss.find("channel"), sha
        except ET.ParseError:
            pass
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
    return rss, rss.find("channel"), None

def add_episode(channel, title, description, audio_url, file_size, pub_date, guid):
    item = ET.Element("item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "description").text = description
    ET.SubElement(item, "pubDate").text     = format_datetime(pub_date)
    ET.SubElement(item, "guid", attrib={"isPermaLink": "false"}).text = guid
    ET.SubElement(item, "enclosure", attrib={
        "url": audio_url, "length": str(file_size), "type": "audio/mpeg"
    })
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

def main():
    log.info("=== PDF → Podcast Pipeline ===")
    processed = load_pdf_state()
    dbx       = get_dropbox_client()

    new_pdfs = list_new_pdfs(dbx, processed)
    if not new_pdfs:
        log.info("No new PDFs. Nothing to do.")
        return

    log.info(f"{len(new_pdfs)} new PDF(s) found — synthesizing into one episode.")

    # 1. Download + extract all PDFs
    papers = []
    for f in new_pdfs:
        try:
            pdf_bytes = download_file(dbx, f["path"])
            text      = extract_pdf_text(pdf_bytes)
            log.info(f"  Extracted {len(text)} chars from {f['name']}")
            papers.append({"filename": f["name"], "text": text, "info": f})
        except Exception as e:
            log.error(f"  Could not extract {f['name']}: {e}")

    if not papers:
        log.error("No papers could be extracted. Aborting.")
        return

    # 2. Generate one synthesized script
    script, title, description = generate_podcast_script(papers)

    # 3. Convert to audio
    date_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mp3_filename = f"{date_str}_digest_podcast.mp3"

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_mp3_path = tmp.name

    try:
        log.info("Converting script to audio with Edge TTS...")
        script_to_mp3(script, tmp_mp3_path)

        # 4. Upload mp3 to Dropbox
        dropbox_audio_path = f"{DROPBOX_AUDIO_FOLDER}/{mp3_filename}"
        with open(tmp_mp3_path, "rb") as f:
            mp3_bytes = f.read()
        upload_to_dropbox(dbx, mp3_bytes, dropbox_audio_path)
        file_size = len(mp3_bytes)

    finally:
        if os.path.exists(tmp_mp3_path):
            os.unlink(tmp_mp3_path)

    # 5. Get public link
    audio_url = get_or_create_public_link(dbx, dropbox_audio_path)
    log.info(f"  Audio URL: {audio_url}")

    # 6. Update RSS feed
    log.info("Updating RSS feed...")
    guid = hashlib.md5("|".join(p["info"]["id"] for p in papers).encode()).hexdigest()
    rss, channel, rss_sha = get_or_create_rss()
    add_episode(
        channel=channel,
        title=title,
        description=description,
        audio_url=audio_url,
        file_size=file_size,
        pub_date=datetime.now(timezone.utc),
        guid=guid,
    )
    gh_put_file("feed.xml", rss_to_bytes(rss), f"feat: RSS — {title}", rss_sha)

    # 7. Move PDFs to /old/ and mark as processed
    log.info("Moving PDFs to /old/ folder...")
    for p in papers:
        move_to_old(dbx, p["info"]["path"], p["info"]["name"])
        processed.add(p["info"]["id"])

    save_pdf_state(processed)
    log.info(f"✓ Done! Episode: {title}")
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
