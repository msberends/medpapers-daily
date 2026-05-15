#!/usr/bin/env python3
"""Download country flag SVGs from flagcdn.com into static/flags/.

Run from the project root:
    python scripts/fetch_flags.py

SVGs are sourced from https://flagcdn.com/ (public domain / CC BY 4.0).
Re-run annually or whenever app/flags.py gains new ISO codes.
"""
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# All ISO codes referenced in app/flags.py
CODES = [
    "us", "ca", "mx", "br", "ar", "cl", "co", "pe", "ve", "bo", "ec",
    "py", "uy", "cu", "ht", "do", "jm", "tt", "bb", "pa", "cr", "gt",
    "hn", "sv", "ni", "bz", "gy", "sr",
    "gb", "ie", "de", "fr", "it", "es", "pt", "nl", "be", "ch", "at",
    "se", "no", "dk", "fi", "is", "lu", "mt", "cy", "gr", "pl", "cz",
    "sk", "hu", "ro", "bg", "hr", "rs", "si", "ba", "mk", "al", "me",
    "md", "ua", "by", "ru", "ee", "lv", "lt", "xk",
    "cn", "hk", "tw", "jp", "kr", "kp", "in", "pk", "bd", "lk", "np",
    "bt", "mv", "af", "ir", "iq", "sa", "ae", "qa", "kw", "bh", "om",
    "ye", "il", "ps", "jo", "lb", "sy", "tr", "sg", "my", "id", "ph",
    "th", "vn", "kh", "mm", "la", "mn", "uz", "kz", "kg", "tj", "tm",
    "ge", "am", "az",
    "au", "nz", "pg", "fj",
    "za", "ng", "ke", "et", "gh", "tz", "ug", "rw", "zm", "zw", "na",
    "bw", "mz", "mw", "mg", "ls", "sz", "ao", "cm", "ci", "sn", "ml",
    "bf", "gn", "sl", "lr", "tg", "bj", "td", "ne", "mr", "dj", "er",
    "so", "sd", "ss", "eg", "ly", "tn", "dz", "ma", "cd", "cg", "ga",
    "gq", "cf", "cv", "mu", "sc", "km", "st", "gm", "gw",
]

BASE_URL = "https://flagcdn.com/{code}.svg"
OUT_DIR = Path(__file__).parent.parent / "static" / "flags"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch(code: str) -> bool:
    url = BASE_URL.format(code=code)
    dest = OUT_DIR / f"{code}.svg"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            dest.write_bytes(resp.read())
        return True
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {code}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  Error {code}: {e}", file=sys.stderr)
        return False


def patch_svg(path: Path) -> None:
    """Set preserveAspectRatio="none" so browsers render flags at exact pixel dimensions."""
    content = path.read_text()
    if 'preserveAspectRatio' in content:
        new = re.sub(r'preserveAspectRatio="[^"]*"', 'preserveAspectRatio="none"', content, count=1)
    else:
        new = re.sub(r'(<svg\b[^>]*?)(\/?>)', r'\1 preserveAspectRatio="none"\2', content, count=1)
    if new != content:
        path.write_text(new)


def main() -> None:
    existing = {p.stem for p in OUT_DIR.glob("*.svg")}
    missing = [c for c in CODES if c not in existing]
    targets = CODES if "--all" in sys.argv else missing

    if not targets:
        print(f"All {len(CODES)} flags already present in {OUT_DIR}.")
        return

    print(f"Downloading {len(targets)} flag(s) to {OUT_DIR} …")
    ok = fail = 0
    for code in targets:
        sys.stdout.write(f"  {code} … ")
        sys.stdout.flush()
        if fetch(code):
            patch_svg(OUT_DIR / f"{code}.svg")
            print("ok")
            ok += 1
        else:
            fail += 1
        time.sleep(0.05)  # be polite to the CDN

    print(f"\nDone: {ok} downloaded, {fail} failed.")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
