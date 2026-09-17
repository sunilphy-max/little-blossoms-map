"""
MAP BACKEND — CLOUD/SCHEDULED VERSION

Designed to run as a short-lived scheduled job (e.g. GitHub Actions,
every 15 minutes) rather than a continuously-running process on your
own computer. Each run:

  1. Pulls the current state (already-placed guests, geocode cache,
     etc.) from jsonbin.io — since a scheduled job has no memory of
     its own between runs, jsonbin.io IS the memory.
  2. Refreshes a short-lived Dropbox access token from your long-lived
     refresh token, then lists/downloads any CSV files in your
     Dropbox survey-exports folder.
  3. Pulls the latest photos from fotoShare Cloud.
  4. Matches new survey rows to photos, geocodes new cities.
  5. Publishes the updated state back to jsonbin.io.
  6. Exits. (The next scheduled run picks up right where this left off.)

This file is meant to live in a GitHub repo and be run by the
workflow in .github/workflows/map-sync.yml — see that file for the
schedule. All credentials below are read from environment variables,
which the workflow supplies from GitHub repo secrets — never commit
real credentials into this file.
"""

import os
import re
import csv
import json
import time
import hashlib
import io
from datetime import datetime

import requests


# =====================================================================
# CONFIG — non-secret settings only. Credentials come from env vars,
# set as GitHub repo secrets (see setup steps).
# =====================================================================
GALLERY_URL = "https://fotoshare.co/e/jZNLU9GUK7uE22OTnPU3I"
DROPBOX_FOLDER_PATH = "/Littleblossoms-survey"   # the Dropbox folder you export CSVs into

# How close a survey answer's timestamp and a photo's capture time need
# to be to count as the same guest. Test against a couple of real
# sessions before the event and adjust if they land further apart.
MATCH_WINDOW_SECONDS = 90

# CSV column headers — these match the actual LumaBooth export format.
COL_TIMESTAMP = "Date"
COL_FILENAME = "Filename"
COL_OPT_IN = "Want to appear on our guest map?"
COL_CITY = "What city were you born in? (e.g. Boston, MA)"

YES_VALUES = {"yes", "y", "true"}

# Credentials — supplied as env vars by the GitHub Actions workflow.
# .strip() guards against stray leading/trailing whitespace from
# copy-pasting secret values, which silently breaks URLs and auth.
REQUIRED_ENV = (
    "JSONBIN_BIN_ID",
    "JSONBIN_MASTER_KEY",
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_REFRESH_TOKEN",
)

missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
if missing:
    raise RuntimeError(
        "Missing (or empty) required GitHub Actions secrets: " + ", ".join(missing)
        + " — add them under Settings > Secrets and variables > Actions."
    )

JSONBIN_BIN_ID = os.environ["JSONBIN_BIN_ID"].strip()
JSONBIN_MASTER_KEY = os.environ["JSONBIN_MASTER_KEY"].strip()
DROPBOX_APP_KEY = os.environ["DROPBOX_APP_KEY"].strip()
DROPBOX_APP_SECRET = os.environ["DROPBOX_APP_SECRET"].strip()
DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"].strip()


# =====================================================================
# State — jsonbin.io holds everything between runs
# =====================================================================
def load_state():
    resp = requests.get(
        f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest",
        headers={"X-Master-Key": JSONBIN_MASTER_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    record = resp.json().get("record", {})
    return {
        "entries": record.get("entries", []),
        "processedRowSignatures": record.get("processedRowSignatures", []),
        "usedPhotoIds": record.get("usedPhotoIds", []),
        "geocodeCache": record.get("geocodeCache", {}),
    }


def save_state(state):
    resp = requests.put(
        f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}",
        headers={"Content-Type": "application/json", "X-Master-Key": JSONBIN_MASTER_KEY},
        json=state,
        timeout=10,
    )
    resp.raise_for_status()


# =====================================================================
# Dropbox — refresh token -> short-lived access token -> list/download
# =====================================================================
def get_dropbox_access_token():
    resp = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
        },
        auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET),
        timeout=10,
    )
    if not resp.ok:
        print(f"[dropbox] token refresh failed ({resp.status_code}): {resp.text}")
    resp.raise_for_status()
    return resp.json()["access_token"]


def list_dropbox_csvs(access_token):
    resp = requests.post(
        "https://api.dropboxapi.com/2/files/list_folder",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"path": DROPBOX_FOLDER_PATH},
        timeout=15,
    )
    resp.raise_for_status()
    entries = resp.json().get("entries", [])
    return [e["path_lower"] for e in entries if e["name"].lower().endswith(".csv")]


def download_dropbox_file(access_token, path):
    resp = requests.post(
        "https://content.dropboxapi.com/2/files/download",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Dropbox-API-Arg": json.dumps({"path": path}),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


# =====================================================================
# fotoShare Cloud gallery
# =====================================================================
def fetch_gallery_photos():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MapBackend/1.0; personal-event-use)"}
    resp = requests.get(GALLERY_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    match = re.search(r"let sessionImages\s*=\s*(\{.*?\});", resp.text, re.S)
    if not match:
        return []
    sessions = json.loads(match.group(1))

    photos = []
    for session in sessions.values():
        for item in session.values():
            if item.get("fileType") != "jpg":
                continue
            raw_date = item.get("date_added", "")
            try:
                date_added = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except Exception:
                date_added = None
            photos.append({"id": item["hash"], "url": item["img"], "date_added": date_added})
    return photos


# =====================================================================
# Geocoding — free (Nominatim), cached in state, throttled to their
# usage policy (~1 request/second, light non-commercial use)
# =====================================================================
def geocode_city(city, cache):
    key = city.strip().lower()
    if key in cache:
        return cache[key]
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "limit": 1, "q": city},
            headers={"User-Agent": "little-blossoms-photo-map/1.0"},
            timeout=10,
        )
        results = resp.json()
        time.sleep(1.1)
        if results:
            coords = {"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])}
            cache[key] = coords
            return coords
    except Exception as e:
        print(f"[geocode] failed for '{city}': {e}")
    return None


# =====================================================================
# CSV processing
# =====================================================================
def row_signature(row):
    raw = f"{row.get(COL_TIMESTAMP,'')}|{row.get(COL_FILENAME,'')}|{row.get(COL_OPT_IN,'')}|{row.get(COL_CITY,'')}"
    return hashlib.sha1(raw.encode()).hexdigest()


def parse_filename_timestamp(filename):
    """LumaBooth filenames look like 20260916_201311523.jpg — that's
    YYYYMMDD_HHMMSSmmm, i.e. capture time down to the millisecond.
    Much more precise than the CSV's minute-resolution Date column."""
    m = re.match(r"(\d{8})_(\d{6})(\d*)", filename or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def parse_csv_date(value):
    """The CSV Date column is US-style local time, e.g. '9/16/2026 20:13'.
    Used only as a fallback when the filename can't be parsed."""
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%y %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def find_matching_photo(row, gallery_photos, used_ids):
    """Tries two strategies, best first:

    1. FILENAME MATCH — if the CSV's filename appears in the photo's URL,
       that's an exact identification, no guessing involved.
    2. TIMESTAMP MATCH — fall back to closest-in-time within the window.
       Note this is sensitive to timezone differences between the iPad's
       local time and the gallery's timestamps, which is exactly why the
       filename match is preferred.
    """
    filename = (row.get(COL_FILENAME) or "").strip()

    # Strategy 1: direct filename match against the photo URL
    if filename:
        stem = filename.rsplit(".", 1)[0]
        for photo in gallery_photos:
            if photo["id"] in used_ids:
                continue
            if stem and stem in photo["url"]:
                return photo, "filename"

    # Strategy 2: closest timestamp within the window
    row_time = parse_filename_timestamp(filename) or parse_csv_date(row.get(COL_TIMESTAMP, ""))
    if not row_time:
        return None, None

    best_photo, best_diff = None, float("inf")
    for photo in gallery_photos:
        if photo["id"] in used_ids or not photo["date_added"]:
            continue
        photo_time = photo["date_added"]
        if photo_time.tzinfo is not None:
            photo_time = photo_time.replace(tzinfo=None)
        diff = abs((photo_time - row_time).total_seconds())
        if diff < best_diff and diff <= MATCH_WINDOW_SECONDS:
            best_diff, best_photo = diff, photo

    return (best_photo, "timestamp") if best_photo else (None, None)


def process_csv_text(csv_text, state, gallery_photos):
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    for col in (COL_TIMESTAMP, COL_OPT_IN, COL_CITY):
        if col not in headers:
            print(f"[csv] column '{col}' not found. Actual columns: {headers}")
            print("[csv] update COL_TIMESTAMP / COL_OPT_IN / COL_CITY at the top of this script.")
            return 0

    processed = set(state["processedRowSignatures"])
    used_ids = set(state["usedPhotoIds"])
    new_count = 0

    for row in reader:
        sig = row_signature(row)
        if sig in processed:
            continue
        processed.add(sig)

        opt_in = (row.get(COL_OPT_IN) or "").strip().lower()
        city = (row.get(COL_CITY) or "").strip()
        if opt_in not in YES_VALUES:
            print(f"[skip] opt-in value '{row.get(COL_OPT_IN)}' is not a yes")
            continue
        if not city:
            print("[skip] opted in but left the city blank")
            continue

        best_photo, how = find_matching_photo(row, gallery_photos, used_ids)
        if best_photo:
            used_ids.add(best_photo["id"])
            print(f"[match] '{city}' matched to a photo by {how}")
        else:
            print(f"[match] '{city}' — no photo match found, will show as a plain pin")

        coords = geocode_city(city, state["geocodeCache"])
        if not coords:
            print(f"[skip] could not geocode '{city}' — not placed on the map")
            continue

        state["entries"].append({
            "city": city,
            "lat": coords["lat"],
            "lng": coords["lng"],
            "photoUrl": best_photo["url"] if best_photo else None,
            "photoId": best_photo["id"] if best_photo else None,
        })
        new_count += 1

    state["processedRowSignatures"] = list(processed)
    state["usedPhotoIds"] = list(used_ids)
    return new_count


# =====================================================================
# Main — one pass, then exit
# =====================================================================
def main():
    print("Loading state from jsonbin...")
    state = load_state()

    print("Fetching gallery photos...")
    gallery_photos = fetch_gallery_photos()
    print(f"  {len(gallery_photos)} photos available")

    print("Refreshing Dropbox access token...")
    access_token = get_dropbox_access_token()

    print(f"Listing CSVs in Dropbox {DROPBOX_FOLDER_PATH}...")
    csv_paths = list_dropbox_csvs(access_token)
    print(f"  {len(csv_paths)} CSV file(s) found")

    total_new = 0
    for path in csv_paths:
        csv_text = download_dropbox_file(access_token, path)
        total_new += process_csv_text(csv_text, state, gallery_photos)

    if total_new:
        print(f"Placed {total_new} new guest(s). Publishing state...")
        save_state(state)
    else:
        print("No new guests this run.")


if __name__ == "__main__":
    main()
