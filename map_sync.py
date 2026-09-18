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

# FALLBACK LOCATION.
# When OCR reads text but no place can be found for it, the guest is
# pinned here instead of being dropped. These rows are flagged in the
# database (located = false) and labelled on the map, so they can be
# told apart from people genuinely from this city — and so you can
# correct them afterwards.
#
# This applies ONLY to readable-but-unlocatable text. Photos with no
# text at all (the untemplated originals) are still skipped, otherwise
# they would all pile up on this one spot.
FALLBACK_ENABLED = True
FALLBACK_CITY = "Lancaster, PA"
FALLBACK_LAT = 40.0379
FALLBACK_LNG = -76.3055

# HOW LONG TO KEEP RUNNING, in minutes, checking every POLL_SECONDS.
# 0 (the default) means a single pass, then exit — right for scheduled
# runs. Set RUN_FOR_MINUTES in the workflow to keep one job alive for a
# whole event, which gives ~1-minute latency without paying the
# Tesseract install cost on every check.
# GitHub caps a single job at 6 hours, so 330 is a safe maximum.
RUN_FOR_MINUTES = int(os.environ.get("RUN_FOR_MINUTES", "0"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))

SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
# Supabase's dashboard sometimes shows the full REST endpoint rather than
# the bare project URL. Accept either, since the script adds /rest/v1 itself.
if SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[: -len("/rest/v1")]
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
        "User-Agent": "Mozilla/5.0 (compatible; LittleBlossomsMapSync/1.0; personal-event-use)",
        # Without these, a CDN or proxy can hand back a cached copy of the
        # gallery page, which looks identical to a slow upload.
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    resp = requests.get(
        GALLERY_URL,
        headers=headers,
        params={"_": int(time.time())},   # cache-buster
        timeout=20,
    )
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
def _nominatim_lookup(query):
    """One raw lookup. Returns coords or None."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "limit": 1, "q": query},
            headers={"User-Agent": "little-blossoms-guest-map/1.0"},
            timeout=15,
        )
        results = resp.json()
        time.sleep(1.1)   # Nominatim asks for no more than ~1 request/second
    except Exception as e:
        print(f"  [geocode] request failed for '{query}': {e}")
        return None

    if not results:
        return None
    return {"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])}


def geocode_city(city):
    """OCR is rarely perfect, so don't bet everything on the full string
    being right. Try the whole thing first; if that fails, fall back to
    just the part before the comma — a single misread character in the
    state or country shouldn't lose the guest entirely.

    Example: OCR reads 'tatanagar, iharkhand' (a J misread as i).
    The full string fails; 'tatanagar' alone resolves correctly.
    """
    key = city.strip().lower()

    cached = sb_get("geocode_cache", {"city_key": f"eq.{key}", "select": "lat,lng"})
    if cached:
        return {"lat": cached[0]["lat"], "lng": cached[0]["lng"]}

    # Build the candidate queries, most specific first, no duplicates.
    candidates = [city.strip()]
    if "," in city:
        first_part = city.split(",")[0].strip()
        if first_part and first_part.lower() != city.strip().lower():
            candidates.append(first_part)

    for attempt, query in enumerate(candidates, start=1):
        coords = _nominatim_lookup(query)
        if coords:
            if attempt > 1:
                print(f"  [geocode] full string failed; matched on '{query}'")
            # Cache against the original OCR text, so the same misread
            # resolves instantly next time rather than retrying the chain.
            sb_insert("geocode_cache", {"city_key": key, **coords}, upsert=True)
            return coords

    return None


# =====================================================================
# Main
# =====================================================================
def run_once():
    print("Fetching gallery...")
    photos = fetch_gallery_photos()
    print(f"  {len(photos)} photo(s) in the gallery")
    if not photos:
        return

    # Only skip photos we've actually settled: ones placed on the map,
    # and ones where the crop genuinely had no text. Geocode failures
    # are left open deliberately, so improving the matching (or fixing
    # a misread) gives those guests another chance on the next run.
    done = sb_get("processed_photos", {
        "select": "photo_id",
        "outcome": "in.(placed,placed_fallback,no_text)",
    })
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
        located = coords is not None

        if not located:
            if not FALLBACK_ENABLED:
                print(f"  could not geocode '{city}' — skipping")
                sb_insert("processed_photos", {
                    "photo_id": photo["id"], "outcome": "geocode_failed", "ocr_text": raw,
                })
                continue
            coords = {"lat": FALLBACK_LAT, "lng": FALLBACK_LNG}
            print(f"  could not geocode '{city}' — pinning to {FALLBACK_CITY} instead")

        ok = sb_insert("guests", {
            "city": city,
            "lat": coords["lat"],
            "lng": coords["lng"],
            "photo_url": photo["url"],
            "photo_id": photo["id"],
            "located": located,
        })
        sb_insert("processed_photos", {
            "photo_id": photo["id"],
            "outcome": ("placed" if located else "placed_fallback") if ok else "geocode_failed",
            "ocr_text": raw,
        })
        if ok:
            placed += 1
            if located:
                print(f"  placed '{city}' at {coords['lat']:.4f}, {coords['lng']:.4f}")

    print(f"\nDone. {placed} guest(s) added to the map this run.")


def main():
    if RUN_FOR_MINUTES <= 0:
        run_once()
        return

    deadline = time.monotonic() + RUN_FOR_MINUTES * 60
    print(f"Watching for {RUN_FOR_MINUTES} minutes, checking every {POLL_SECONDS}s.\n")

    pass_no = 0
    while time.monotonic() < deadline:
        pass_no += 1
        started = time.monotonic()
        print(f"--- check #{pass_no} at {datetime.now().strftime('%H:%M:%S')} ---")
        try:
            run_once()
        except Exception as e:
            # One bad pass shouldn't end the event. Log it and carry on;
            # the next check picks up anything that was missed.
            print(f"[error] check #{pass_no} failed: {e}")

        # Sleep the remainder of the interval, so a slow pass (lots of
        # new photos) doesn't push every later check further behind.
        elapsed = time.monotonic() - started
        remaining_in_window = deadline - time.monotonic()
        if remaining_in_window <= 0:
            break
        time.sleep(max(0, min(POLL_SECONDS - elapsed, remaining_in_window)))

    print(f"\nWatch window finished after {pass_no} check(s).")


if __name__ == "__main__":
    main()
