#!/usr/bin/env python3
"""Harvest official Line-up photos into the checked-in static bundle.

The live site should never need to download a photo just to render a card.
Run this script from a trusted network after exporting match-detail JSON (or
point it at a running local site with ``--details-url``).  Existing files are
left untouched, so a subsequent deployment only contains newly discovered
registrations.  The browser uses ``/player-photos/<Reg>.jpg`` first and only
falls back to ``/api/player-photo`` for a registration that has not been
harvested yet.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

PHOTO_BASE = "https://results.asiangames2026.org/ag2026/photos/"
REGISTRATION = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def registrations(value):
    if isinstance(value, dict):
        reg = str(value.get("reg") or value.get("Reg") or "").strip()
        if REGISTRATION.fullmatch(reg):
            yield reg
        for item in value.values():
            yield from registrations(item)
    elif isinstance(value, list):
        for item in value:
            yield from registrations(item)


def load_json_files(paths):
    for path in paths:
        try:
            yield json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"skip {path}: {error}")


def fetch_details(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "AichiSchedulePhotoHarvester/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read(8 * 1024 * 1024))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", action="append", default=[], help="match/tournament JSON export (repeatable)")
    parser.add_argument("--details-url", action="append", default=[], help="local API URL returning match details (repeatable)")
    parser.add_argument("--output", default=str(Path(__file__).parents[1] / "static" / "player-photos"))
    parser.add_argument("--delay", type=float, default=0.2, help="seconds between official photo requests")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    values = list(load_json_files(args.details))
    for url in args.details_url:
        try:
            values.append(fetch_details(url))
        except (OSError, ValueError) as error:
            print(f"skip {url}: {error}")
    regs = sorted(set(reg for value in values for reg in registrations(value)))
    if not regs:
        print("No valid registrations found; export details first.")
        return 2
    downloaded = skipped = failed = 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for reg in regs:
        target = output / f"{reg}.jpg"
        if target.is_file() and target.stat().st_size:
            skipped += 1
            continue
        try:
            request = urllib.request.Request(PHOTO_BASE + reg + ".jpg", headers={"User-Agent": "AichiSchedulePhotoHarvester/1.0", "Accept": "image/jpeg,image/*"})
            with opener.open(request, timeout=30) as response:
                body = response.read(2 * 1024 * 1024 + 1)
                content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
            if not body or len(body) > 2 * 1024 * 1024 or not content_type.startswith("image/"):
                raise ValueError("not an image")
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)
            downloaded += 1
            print(f"cached {reg}")
        except (OSError, urllib.error.URLError, ValueError) as error:
            failed += 1
            print(f"failed {reg}: {error}")
        time.sleep(max(0, args.delay))
    print(f"registrations={len(regs)} downloaded={downloaded} existing={skipped} failed={failed}")
    return 0 if downloaded or skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
