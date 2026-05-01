# 🎙️ Dropbox → Podcast → Spotify Pipeline

Automated pipeline: shared Dropbox folder → RSS feed → Spotify.  
Contributors drop an `.mp3` into a shared Dropbox folder. Within ~1 hour it appears as a new episode on Spotify, with an AI-generated title and description. No manual steps needed.

---

## How it works

```
Someone drops an .mp3 into the shared Dropbox folder
              ↓
GitHub Actions runs every hour (free, no server needed)
              ↓
New files detected → Claude API generates title + description
              ↓
mp3 uploaded to your website via FTP
              ↓
RSS feed XML updated on your website
              ↓
Spotify polls RSS → episode appears automatically
```

---

## File naming convention

Ask contributors to name files like this:

```
YYYY-MM-DD_AuthorName_Short-Topic-Description.mp3
```

Examples:
```
2026-05-01_Paz_Water-Chemistry-Membrane-Fouling.mp3
2026-05-03_Sarah_Lithium-Brine-Geochemistry.mp3
2026-05-07_David_pH-Measurement-Hypersaline-Systems.mp3
```

Claude will turn `Water-Chemistry-Membrane-Fouling` into a proper episode title and description automatically. If someone doesn't follow the convention, it still works — Claude does its best with whatever name is given.

---

## One-time setup

### Step 1 — Create a Dropbox App

1. Go to [https://www.dropbox.com/developers/apps](https://www.dropbox.com/developers/apps)
2. Click **Create app**
3. Choose: **Scoped access** → **Full Dropbox** (or specific folder)
4. Give it any name (e.g. `podcast-pipeline`)
5. Go to **Permissions** tab → enable:
   - `files.content.read`
   - `files.metadata.read`
6. Note your **App key** and **App secret** from the Settings tab

### Step 2 — Get your Dropbox refresh token

Run this once on your local machine:

```bash
pip install dropbox
python scripts/get_dropbox_token.py
```

Follow the prompts. You'll get a `DROPBOX_REFRESH_TOKEN` that doesn't expire.

### Step 3 — Create a shared Dropbox folder

1. Create a folder in your Dropbox (e.g. `/podcast-uploads`)
2. Share it with your contributors (Dropbox sharing → can edit)
3. Note the folder path for the `DROPBOX_FOLDER` secret

### Step 4 — Prepare your website

You need a web server with FTP access (your group website works perfectly).

Create two things on your server:
- A folder for audio files, e.g. `/public_html/podcast/audio/`
- A path where the RSS feed will live, e.g. `/public_html/podcast/feed.xml`

Make sure both are publicly accessible via HTTP/HTTPS.

### Step 5 — Create a GitHub repo and add secrets

1. Create a new GitHub repository (can be private)
2. Push this code to it
3. Go to **Settings → Secrets and variables → Actions → New repository secret**

Add all of these secrets:

| Secret name | Example value | Description |
|---|---|---|
| `DROPBOX_APP_KEY` | `abc123` | From Dropbox App Console |
| `DROPBOX_APP_SECRET` | `xyz789` | From Dropbox App Console |
| `DROPBOX_REFRESH_TOKEN` | `sl.xxx...` | From the token helper script |
| `DROPBOX_FOLDER` | `/podcast-uploads` | Path in your Dropbox |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | From console.anthropic.com |
| `FTP_HOST` | `ftp.yoursite.com` | Your web server FTP host |
| `FTP_USER` | `youruser` | FTP username |
| `FTP_PASS` | `yourpassword` | FTP password |
| `FTP_REMOTE_DIR` | `/public_html/podcast/audio/` | Remote dir for mp3 files |
| `RSS_REMOTE_PATH` | `/public_html/podcast/feed.xml` | Full remote path to RSS file |
| `PODCAST_BASE_URL` | `https://yoursite.com/podcast/audio/` | Public URL prefix for mp3s |
| `RSS_FEED_URL` | `https://yoursite.com/podcast/feed.xml` | Public URL of the RSS feed |
| `PODCAST_TITLE` | `Water Research Lab Podcast` | Your podcast name |
| `PODCAST_DESCRIPTION` | `Research discussions from the lab` | Short podcast description |
| `PODCAST_AUTHOR` | `Paz Steinberg` | Your name |
| `PODCAST_EMAIL` | `paz@example.com` | Contact email (in RSS metadata) |

### Step 6 — Register with Spotify

1. Go to [https://creators.spotify.com](https://creators.spotify.com)
2. Sign in and click **Get started**
3. Choose **"I have a podcast somewhere else"**
4. Paste your RSS feed URL: `https://yoursite.com/podcast/feed.xml`

> ⚠️ The RSS feed must have at least one episode before Spotify will accept it.  
> Drop a test `.mp3` in your Dropbox folder and wait ~1 hour for the pipeline to run, then submit to Spotify.

### Step 7 — Test it

1. Drop a test `.mp3` file into the shared Dropbox folder
2. Go to your GitHub repo → **Actions** tab → **Podcast Pipeline** → **Run workflow** (manual trigger)
3. Watch the logs — you should see it download, generate metadata, upload, and update the RSS
4. Check your website: `https://yoursite.com/podcast/feed.xml` should show the new episode

---

## Running schedule

The pipeline runs **every hour** automatically via GitHub Actions (free tier, 2000 min/month — this uses ~2 min/run → well within limits).

You can also trigger it manually anytime from the Actions tab.

---

## Folder structure

```
dropbox-podcast/
├── .github/
│   └── workflows/
│       └── podcast-pipeline.yml   # GitHub Actions schedule + steps
├── scripts/
│   ├── pipeline.py                # Main pipeline logic
│   └── get_dropbox_token.py       # One-time token helper
├── processed_files.json           # Tracks which files were already processed
├── requirements.txt
└── README.md
```

---

## Troubleshooting

**Pipeline runs but no new episodes found**
- Check that the Dropbox folder path in the secret exactly matches where files are uploaded (case-sensitive)
- Confirm the Dropbox app has `files.content.read` and `files.metadata.read` permissions

**FTP upload fails**
- Test your FTP credentials manually with a tool like FileZilla
- Some hosts use SFTP instead of FTP — if so, let me know and the script can be updated to use `paramiko` for SFTP

**Spotify doesn't pick up new episodes**
- Spotify polls RSS feeds roughly every 1-4 hours. Wait a bit.
- Validate your RSS at [https://www.castfeedvalidator.com](https://www.castfeedvalidator.com)

**Claude API generates wrong metadata**
- The filename convention matters. The clearer the filename, the better the output.
- You can always edit episode metadata directly in Spotify for Creators after the fact.
