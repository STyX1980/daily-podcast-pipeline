"""
One-time setup: Get your Dropbox refresh token
-----------------------------------------------
Run this ONCE on your local machine to get the refresh token
you'll store as a GitHub Secret.

Usage:
    pip install dropbox
    python scripts/get_dropbox_token.py

You'll need your Dropbox App Key and App Secret from:
    https://www.dropbox.com/developers/apps
    → Create App → Scoped access → Full Dropbox (or specific folder)
    → Permissions tab: enable  files.content.read  files.metadata.read
"""

import dropbox
from dropbox import DropboxOAuth2FlowNoRedirect

APP_KEY    = input("Enter your Dropbox App Key:    ").strip()
APP_SECRET = input("Enter your Dropbox App Secret: ").strip()

auth_flow = DropboxOAuth2FlowNoRedirect(
    APP_KEY,
    APP_SECRET,
    token_access_type="offline",  # 'offline' gives a refresh token (non-expiring)
)

authorize_url = auth_flow.start()
print("\n1. Go to this URL in your browser:")
print(f"   {authorize_url}")
print("\n2. Click 'Allow', then copy the authorization code shown.")
auth_code = input("\n3. Paste the authorization code here: ").strip()

oauth_result = auth_flow.finish(auth_code)

print("\n✓ Success! Add these to your GitHub Secrets:")
print(f"   DROPBOX_APP_KEY       = {APP_KEY}")
print(f"   DROPBOX_APP_SECRET    = {APP_SECRET}")
print(f"   DROPBOX_REFRESH_TOKEN = {oauth_result.refresh_token}")
print("\nThe refresh token does not expire — store it safely.")
