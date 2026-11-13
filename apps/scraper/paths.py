"""Repo-level paths shared by every script in apps/scraper.

The scripts live in <repo>/apps/scraper but read and write <repo>/data (scrapers write it, the site build copies
from it, workflows commit it), so `Path(__file__).parent / "data"` is no longer correct. Import DATA_DIR from here.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
