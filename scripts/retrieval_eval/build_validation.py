"""Acquire a predeclared, open-license real-photo validation corpus from Commons.

Selection is independent of development retrieval outcomes. Raw API responses,
download log, chosen candidates, license metadata and SHA256 are retained.
No image editing or image generation is performed: thumbnails are provided by
the source itself, with original links retained. Human-readable review is separate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/eval/retrieval_validation"
UA = "PhotoAgentValidationResearch/1.0 (open-license educational retrieval benchmark)"
STRATA = [
    ("cat_portrait", 'intitle:cat portrait filetype:bitmap'),
    ("cat_sleep", 'intitle:cat sleeping filetype:bitmap'),
    ("dog_agility", 'intitle:dog agility filetype:bitmap'),
    ("dog_sleep", 'intitle:dog sleeping filetype:bitmap'),
    ("coffee_pour", 'intitle:pouring coffee filetype:bitmap'),
    ("coffee_latte", 'intitle:latte art filetype:bitmap'),
    ("reading_person", 'intitle:reading book person filetype:bitmap'),
    ("book_stack", 'intitle:stack books filetype:bitmap'),
    ("cycling_person", 'intitle:cyclist road filetype:bitmap'),
    ("bicycle_parked", 'intitle:bicycles parked filetype:bitmap'),
    ("apple_fruit", 'intitle:apples fruit basket filetype:bitmap'),
    ("orange_fruit", 'intitle:oranges fruit filetype:bitmap'),
    ("croissant", 'intitle:croissant filetype:bitmap'),
    ("doughnut", 'intitle:doughnut filetype:bitmap'),
    ("train_platform", 'intitle:train platform filetype:bitmap'),
    ("bus_stop", 'intitle:bus stop shelter filetype:bitmap'),
    ("stop_sign", 'intitle:stop sign street filetype:bitmap'),
    ("yield_sign", 'intitle:yield sign road filetype:bitmap'),
    ("speed_30", 'intitle:speed limit 30 sign filetype:bitmap'),
    ("speed_50", 'intitle:speed limit 50 sign filetype:bitmap'),
    ("sunflower", 'intitle:sunflower field filetype:bitmap'),
    ("rose", 'intitle:red rose flower filetype:bitmap'),
    ("waterfall", 'intitle:waterfall forest filetype:bitmap'),
    ("river_bridge", 'intitle:stone bridge river filetype:bitmap'),
    ("snow_mountain", 'intitle:snow mountain landscape filetype:bitmap'),
    ("beach", 'intitle:sandy beach sea filetype:bitmap'),
    ("guitar_player", 'intitle:guitar player filetype:bitmap'),
    ("piano_player", 'intitle:piano player filetype:bitmap'),
    ("cooking_person", 'intitle:cooking kitchen person filetype:bitmap'),
    ("washing_dishes", 'intitle:washing dishes filetype:bitmap'),
    ("umbrella_rain", 'intitle:umbrella rain street filetype:bitmap'),
    ("umbrella_beach", 'intitle:beach umbrella filetype:bitmap'),
    ("basketball", 'intitle:basketball game filetype:bitmap'),
    ("football", 'intitle:football soccer game filetype:bitmap'),
    ("laptop", 'intitle:laptop open filetype:bitmap'),
    ("keyboard", 'intitle:computer keyboard filetype:bitmap'),
    ("shopping_cart", 'intitle:shopping cart supermarket filetype:bitmap'),
    ("shopping_basket", 'intitle:shopping basket filetype:bitmap'),
    ("parking_sign", 'intitle:parking sign street filetype:bitmap'),
    ("no_parking_sign", 'intitle:no parking sign filetype:bitmap'),
]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_log(value):
    value["time_utc"] = datetime.now(timezone.utc).isoformat()
    with (OUT / "acquisition.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
            append_log({"url": url, "status": response.status, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
            time.sleep(1.5)
            return body
    except Exception as exc:
        append_log({"url": url, "error": type(exc).__name__, "message": str(exc)})
        raise


def plan():
    path = OUT / "sampling-plan.json"
    if path.exists():
        return
    write_json(path, {
        "version": "1.0.0", "created_utc": datetime.now(timezone.utc).isoformat(),
        "development_outcomes_observed": False,
        "target_images": 40, "target_queries": 80,
        "strata": [{"slot": n + 1, "concept": c, "commons_search": q} for n, (c, q) in enumerate(STRATA)],
        "selection": "Retrieve up to 5 ranked Commons bitmap candidates per predeclared concept; inspect visually. Choose first licensed, decodable real photograph clearly depicting concept. Skip diagrams, screenshots, unusably tiny or unintelligible images. Record every replacement and its visual reason before retrieval evaluation. If no candidate qualifies, use documented revised keyword search for same concept. Relevance queries are then written from visual evidence, without observing development or validation retrieval results.",
        "license_allowlist": ["CC0", "Public domain", "CC BY", "CC BY-SA"],
        "query_design": "40 image-specific queries, 12 multi-positive/generalization queries, 16 exclusions/OCR confusions, 12 semantically plausible empty queries; revise exact proportions only for visible evidence, record actual counts.",
        "annotation": "Codex AI visual inspection of all selected images and full closed-corpus relevance review; not independent human double annotation. No paid annotation model and no generated image.",
        "independence": "New real publicly available photographs, visually authored new Chinese queries, no development output access. Exact and perceptual hash comparison with development. Shared internet/foundation training exposure is unknown; this is not proof of training independence.",
    })


def acquire(only=None):
    plan()
    for index, (concept, search) in enumerate(STRATA, 1):
        if only and index not in only:
            continue
        path = OUT / "source_api" / f"{index:02d}-{concept}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": search,
                      "gsrnamespace": 6, "gsrlimit": 5, "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1280}
            url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
            try:
                payload = json.loads(fetch(url))
            except Exception as exc:
                print(f"SEARCH FAILED {concept}: {type(exc).__name__}", flush=True)
                continue
            write_json(path, payload)
        pages = sorted(payload.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 999))
        print(f"{index:02d} {concept}: {len(pages)} candidates", flush=True)
        for rank, page in enumerate(pages, 1):
            info = page.get("imageinfo", [{}])[0]
            meta = info.get("extmetadata", {})
            license_name = meta.get("LicenseShortName", {}).get("value", "")
            if not any(x in license_name for x in ("CC0", "Public domain", "CC BY", "CC BY-SA")):
                continue
            url = info.get("thumburl", info.get("url", "")).split("?")[0]
            suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            target = OUT / "candidates" / f"{index:02d}-{concept}-{rank}{suffix}"
            if not target.exists():
                try:
                    data = fetch(url)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    with Image.open(target) as image:
                        image.verify()
                except Exception as exc:
                    print(f"DOWNLOAD FAILED {index}:{rank} {type(exc).__name__}", flush=True)
                    continue
            write_json(target.with_suffix(target.suffix + ".json"), {"concept": concept, "slot": index, "rank": rank, "search": search, "download_url": url, "commons_page": page})
            print(f"  {rank} {page['title']} [{license_name}]", flush=True)
            # First three license-compatible candidates give visual choice with bounded bandwidth.
            if rank >= 3:
                break


def contact():
    files = sorted(p for p in (OUT / "candidates").glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 16) if font_path.exists() else ImageFont.load_default()
    for start in range(0, len(files), 16):
        sheet = Image.new("RGB", (1360, 1456), "white")
        draw = ImageDraw.Draw(sheet)
        for offset, path in enumerate(files[start:start + 16]):
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((332, 326))
                x, y = offset % 4 * 340, offset // 4 * 364
                sheet.paste(im, (x + (332 - im.width) // 2, y))
                draw.text((x + 4, y + 330), path.stem, font=font, fill="black")
        target = OUT / "review_sheets" / f"candidates-{start // 16 + 1:02d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(target, quality=92)
    print(f"{len(files)} candidates; {(len(files) + 15) // 16} review sheets")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "acquire", "contact"])
    parser.add_argument("--slots", help="Optional comma-separated original slot numbers to recover")
    args = parser.parse_args()
    if args.command == "acquire":
        acquire({int(x) for x in args.slots.split(",")} if args.slots else None)
    else:
        globals()[args.command]()
