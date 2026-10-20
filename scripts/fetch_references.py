"""Download the official reference documents behind DCDR's decoding rules into references/.

Why a script: the documents are not stored in git (size, and redistribution terms), so this
re-creates the local copies from their official sources, and prints each file's size and SHA-256
for docs/references.md. A changed checksum means the publisher replaced the file: re-read it.

Only official publishers are listed (WMO, ICAMS/OFCM, NOAA NWS). WMO-No. 306 (Manual on Codes)
cannot be fetched here: library.wmo.int requires a human check, so it is downloaded by hand.
The ICAMS host refused scripted requests (HTTP 403, 2026-10-20); if it still does, save those
PDFs from a browser under the same names and re-run: existing files are only checksummed.

Usage (from the repo root):  uv run python scripts/fetch_references.py
Existing files are kept; delete one to download it again.
"""

from __future__ import annotations

import hashlib
import sys
import time
import urllib.request
from pathlib import Path

ICAMS = "https://www.icams-portal.gov/resources/ofcm/fmh"
BUFR4 = "https://raw.githubusercontent.com/wmo-im/BUFR4/v46"  # WMO's official tables, release v46

# (local path under references/, official URL, expected content type)
DOCUMENTS = [
    ("icams/fmh1_2019.pdf", f"{ICAMS}/FMH1/fmh1_2019.pdf", "application/pdf"),
    ("icams/FCM-H2-1988.pdf", f"{ICAMS}/FMH2/FCM-H2-1988.pdf", "application/pdf"),
    ("icams/fmh2-chg1and2.pdf", f"{ICAMS}/FMH2/fmh2-chg1and2.pdf", "application/pdf"),
    ("icams/fmh2-chg3.pdf", f"{ICAMS}/FMH2/fmh2-chg3.pdf", "application/pdf"),
    ("icams/fmh2-chg4.pdf", f"{ICAMS}/FMH2/fmh2-chg4.pdf", "application/pdf"),
    ("icams/fmh12.pdf", f"{ICAMS}/FMH12/fmh12.pdf", "application/pdf"),
    ("nws/aum-toc.pdf", "https://www.weather.gov/media/asos/aum-toc.pdf", "application/pdf"),
    ("wmo/BUFRCREX_CodeFlag_en_20.csv", f"{BUFR4}/BUFRCREX_CodeFlag_en_20.csv", "text/"),
    (
        "wmo/codes-registry_bufr4_0-20-003.jsonld",
        "https://codes.wmo.int/bufr4/codeflag/0-20-003?_format=jsonld",
        "application/ld+json",
    ),
]


def fetch(url: str, expected_type: str) -> bytes:
    """Download one URL; refuse an unexpected content type (e.g. an HTML error or login page)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (DCDR reference fetch)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.startswith(expected_type):
            raise ValueError(f"{url}: got {ctype!r}, expected {expected_type!r}")
        return resp.read()


def main(root: Path) -> int:
    failed = 0
    for rel, url, ctype in DOCUMENTS:
        path = root / rel
        if not path.exists():
            time.sleep(5)  # be polite: the ICAMS host throttles bursts of requests (HTTP 403)
            try:
                data = fetch(url, ctype)
            except Exception as exc:  # report and continue with the other documents
                print(f"FAILED {rel}: {exc}", file=sys.stderr)
                failed += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        data = path.read_bytes()
        print(f"| `{rel}` | {len(data):,} | `{hashlib.sha256(data).hexdigest()}` |")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(__file__).resolve().parents[1] / "references"))
