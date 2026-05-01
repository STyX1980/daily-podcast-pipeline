name: Podcast Pipeline

on:
  schedule:
    - cron: "0 * * * *"   # every hour
  workflow_dispatch:       # manual trigger from Actions tab

jobs:
  run-pipeline:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run podcast pipeline
        env:
          DROPBOX_APP_KEY:       ${{ secrets.DROPBOX_APP_KEY }}
          DROPBOX_APP_SECRET:    ${{ secrets.DROPBOX_APP_SECRET }}
          DROPBOX_REFRESH_TOKEN: ${{ secrets.DROPBOX_REFRESH_TOKEN }}
          DROPBOX_FOLDER:        ${{ secrets.DROPBOX_FOLDER }}
          ANTHROPIC_API_KEY:     ${{ secrets.ANTHROPIC_API_KEY }}
          PODCAST_TITLE:         ${{ secrets.PODCAST_TITLE }}
          PODCAST_DESCRIPTION:   ${{ secrets.PODCAST_DESCRIPTION }}
          PODCAST_AUTHOR:        ${{ secrets.PODCAST_AUTHOR }}
          PODCAST_EMAIL:         ${{ secrets.PODCAST_EMAIL }}
          GH_REPO:               ${{ secrets.GH_REPO }}
          GH_PAGES_TOKEN:        ${{ secrets.GH_PAGES_TOKEN }}
        run: python scripts/pipeline.py

      - name: Save processed files state
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add processed_files.json || true
          git diff --staged --quiet || git commit -m "chore: update state [skip ci]"
          git push || true
