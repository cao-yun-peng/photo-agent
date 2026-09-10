"""Recover unavailable Commons search slots using documented exact public titles."""
import json
import hashlib
import urllib.parse
from pathlib import Path

from build_validation import OUT, STRATA, fetch, write_json

REQUESTS = [
    (2, "File:Cat sleeping on bed.jpg", "Original Commons search failed twice; public web search for the same sleeping-cat concept identified this real-photo source."),
    (4, "File:Sleeping dog (4100811373).jpg", "Original Commons search failed twice; same-concept public web search identified a photographic candidate."),
    (5, "File:Machine pouring coffee (Unsplash).jpg", "Original Commons search failed twice; same pouring-coffee concept, source describes real espresso preparation."),
    (17, "File:Stop sign (1).jpg", "Original Commons search failed twice; same-concept web search, exclude stop-sign icon results."),
    (20, "File:50 km speed limit road sign 2.jpg", "Original candidates showed speed 20 or a graphic sign; same-concept exact title identifies a photographic 50 sign."),
    (28, "File:Piano player.jpg", "All original search results concern automated player pianos, not a person playing; same-concept web search identified a real pianist photograph."),
    (37, "File:Shopping Cart Supermarket.jpg", "Recover first original search candidate after intermittent thumbnail connection failure; same original selection priority."),
]


def main():
    titles = "|".join(t for s, t, r in REQUESTS)
    revision = hashlib.sha256(titles.encode()).hexdigest()[:12]
    write_json(OUT / f"supplement-plan-{revision}.json", {"retrieval_outcomes_seen": False, "selection": "Exact public titles identified from same-concept web search, then subject to source-license and visual inspection", "requests": [{"slot": s, "title": t, "reason": r} for s, t, r in REQUESTS]})
    api = OUT / "source_api" / f"supplement-exact-titles-{revision}.json"
    if api.exists():
        payload = json.loads(api.read_text(encoding="utf-8"))
    else:
        url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({"action": "query", "format": "json", "titles": titles, "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1280})
        payload = json.loads(fetch(url))
        write_json(api, payload)
    pages = {p["title"]: p for p in payload.get("query", {}).get("pages", {}).values()}
    for slot, title, reason in REQUESTS:
        page = pages.get(title, {})
        if "imageinfo" not in page:
            print("MISSING", slot, title, flush=True)
            continue
        info = page["imageinfo"][0]
        license_name = info.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")
        assert any(x in license_name for x in ("CC0", "Public domain", "CC BY", "CC BY-SA")), license_name
        url = info.get("thumburl", info["url"]).split("?")[0]
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        target = OUT / "candidates" / f"{slot:02d}-{STRATA[slot - 1][0]}-supplement{suffix}"
        if not target.exists():
            try:
                target.write_bytes(fetch(url))
            except Exception as e:
                print("FAILED", slot, type(e).__name__, flush=True)
                continue
        write_json(target.with_suffix(target.suffix + ".json"), {"concept": STRATA[slot - 1][0], "slot": slot, "rank": "supplement", "search": title, "download_url": url, "commons_page": page, "selection_reason": reason})
        print("DOWNLOADED", slot, title, license_name, flush=True)


if __name__ == "__main__":
    main()
