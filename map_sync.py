"""
LITTLE BLOSSOMS — MAP SYNC
=====================================================================
Runs on GitHub Actions every 5 minutes. Nothing of yours needs to be
switched on: not a laptop, not anything but the iPad at the booth.

Each run:
  1. Reads the fotoShare Cloud gallery for photos
  2. Skips any photo it has already handled
  3. For each new photo: crops the corner where LumaBooth printed the
     guest's city, runs OCR on that crop, geocodes the result
  4. Writes the guest to Supabase, which pushes it live to the map

The one thing you must tune is CROP_BOX below — it tells the script
where on the photo the city text sits. See the notes there.
=====================================================================
"""

import io
import os
import re
import json
import time
import sys

import requests
from PIL import Image, ImageOps, ImageFilter
import pytesseract


# =====================================================================
# CONFIG
# =====================================================================
GALLERY_URL = "https://fotoshare.co/e/jZNLU9GUK7uE22OTnPU3I"

# WHERE THE CITY TEXT SITS ON THE PHOTO, as fractions of the full image.
# (left, top, right, bottom), each 0.0–1.0 measured from the top-left.
#
# The default below reads the bottom-left area. To measure yours: open
# a test photo, note where the printed city is as a proportion of the
# image, and adjust. Err on the generous side — a crop slightly larger
# than the text is fine; one that clips it is not.
#
# Set DEBUG_OCR below to see exactly what the script is reading.
CROP_BOX = (0.04, 0.82, 0.55, 0.96)

# Prints the raw OCR output for every photo into the Actions log, and
# saves the cropped images as a downloadable artifact. Leave this on
# until you're confident the crop and accuracy are right.
DEBUG_OCR = True
DEBUG_DIR = "ocr_debug"

# Upscaling the crop before OCR meaningfully improves accuracy on small
# text. 3x is a reasonable default.
OCR_UPSCALE = 3

# If OCR returns fewer than this many characters, treat it as "nothing
# readable" rather than trying to geocode noise.
MIN_TEXT_LENGTH = 3

SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"].strip()


# =====================================================================
# Supabase helpers — plain REST, no SDK needed
# =====================================================================
def sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def sb_get(table, params):
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=sb_headers(),
        params=params,
        timeout=20,
    )
    if not resp.ok:
        # Supabase explains itself in the body; a bare status code doesn't.
        raise RuntimeError(
            f"Supabase GET {table} failed: {resp.status_code} {resp.text}"
        )
    return resp.json()


def sb_insert(table, row, upsert=False):
    headers = sb_headers()
    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates"
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        json=row,
        timeout=20,
    )
    if resp.status_code not in (200, 201, 204):
        print(f"  [supabase] insert into {table} failed: {resp.status_code} {resp.text}")
        return False
    return True


# =====================================================================
# Gallery
# =====================================================================
def fetch_gallery_photos():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; LittleBlossomsMapSync/1.0; personal-event-use)"
    }
    resp = requests.get(GALLERY_URL, headers=headers, timeout=20)
    resp.raise_for_status()

    match = re.search(r"let sessionImages\s*=\s*(\{.*?\});", resp.text, re.S)
    if not match:
        print("[gallery] could not find photo data in the page — has the page changed?")
        return []

    try:
        sessions = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"[gallery] could not parse photo data: {e}")
        return []

    photos = []
    for session in sessions.values():
        for item in session.values():
            if item.get("fileType") != "jpg":
                continue          # skip videos and boomerangs
            photos.append({
                "id": item["hash"],
                "url": item["img"],
                "date_added": item.get("date_added", ""),
            })
    photos.sort(key=lambda p: p["date_added"])
    return photos


# =====================================================================
# OCR
# =====================================================================
def read_city_from_photo(photo):
    """Download the photo, crop the region where LumaBooth printed the
    city, and OCR it. Returns the raw text (possibly empty)."""
    resp = requests.get(photo["url"], timeout=30)
    resp.raise_for_status()

    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    w, h = img.size
    left, top, right, bottom = CROP_BOX
    crop = img.crop((int(w * left), int(h * top), int(w * right), int(h * bottom)))

    # Preprocessing: greyscale, upscale, sharpen, then autocontrast.
    # This sequence is what makes small rendered text read reliably.
    crop = crop.convert("L")
    crop = crop.resize(
        (crop.width * OCR_UPSCALE, crop.height * OCR_UPSCALE),
        Image.LANCZOS,
    )
    crop = crop.filter(ImageFilter.SHARPEN)
    crop = ImageOps.autocontrast(crop)

    if DEBUG_OCR:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        crop.save(os.path.join(DEBUG_DIR, f"{photo['id']}.png"))

    # psm 7 = "treat the image as a single line of text", which is what
    # a printed city caption is. Much more accurate here than the
    # default page-segmentation mode.
    text = pytesseract.image_to_string(crop, config="--psm 7")
    return text.strip()


def clean_city_text(raw):
    """OCR output needs tidying before it's worth geocoding."""
    text = raw.replace("\n", " ").strip()
    # Drop characters that never appear in a place name but do appear
    # in OCR noise.
    text = re.sub(r"[^A-Za-z0-9,.'\- ]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")
    return text


# =====================================================================
# Geocoding — Nominatim, cached in Supabase so each city is looked up once
# =====================================================================
def geocode_city(city):
    key = city.strip().lower()

    cached = sb_get("geocode_cache", {"city_key": f"eq.{key}", "select": "lat,lng"})
    if cached:
        return {"lat": cached[0]["lat"], "lng": cached[0]["lng"]}

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "limit": 1, "q": city},
            headers={"User-Agent": "little-blossoms-guest-map/1.0"},
            timeout=15,
        )
        results = resp.json()
        time.sleep(1.1)   # Nominatim asks for no more than ~1 request/second
    except Exception as e:
        print(f"  [geocode] request failed for '{city}': {e}")
        return None

    if not results:
        return None

    coords = {"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])}
    sb_insert("geocode_cache", {"city_key": key, **coords}, upsert=True)
    return coords


# =====================================================================
# Main
# =====================================================================
def main():
    print("Fetching gallery...")
    photos = fetch_gallery_photos()
    print(f"  {len(photos)} photo(s) in the gallery")
    if not photos:
        return

    done = sb_get("processed_photos", {"select": "photo_id"})
    done_ids = {row["photo_id"] for row in done}
    new_photos = [p for p in photos if p["id"] not in done_ids]
    print(f"  {len(new_photos)} new photo(s) to process")

    placed = 0
    for photo in new_photos:
        print(f"\n[{photo['id']}]")
        try:
            raw = read_city_from_photo(photo)
        except Exception as e:
            print(f"  could not read photo: {e}")
            continue          # no processed_photos row, so it retries next run

        city = clean_city_text(raw)
        if DEBUG_OCR:
            print(f"  OCR raw:     {raw!r}")
            print(f"  OCR cleaned: {city!r}")

        if len(city) < MIN_TEXT_LENGTH:
            print("  nothing readable in the crop — skipping")
            sb_insert("processed_photos", {
                "photo_id": photo["id"], "outcome": "no_text", "ocr_text": raw,
            })
            continue

        coords = geocode_city(city)
        if not coords:
            print(f"  could not geocode '{city}' — skipping")
            sb_insert("processed_photos", {
                "photo_id": photo["id"], "outcome": "geocode_failed", "ocr_text": raw,
            })
            continue

        ok = sb_insert("guests", {
            "city": city,
            "lat": coords["lat"],
            "lng": coords["lng"],
            "photo_url": photo["url"],
            "photo_id": photo["id"],
        })
        sb_insert("processed_photos", {
            "photo_id": photo["id"],
            "outcome": "placed" if ok else "geocode_failed",
            "ocr_text": raw,
        })
        if ok:
            placed += 1
            print(f"  placed '{city}' at {coords['lat']:.4f}, {coords['lng']:.4f}")

    print(f"\nDone. {placed} guest(s) added to the map this run.")


if __name__ == "__main__":
    main()
