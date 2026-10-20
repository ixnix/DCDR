"""Download the official reference documents behind DCDR's decoding rules into references/.

Why a script: the documents are not stored in git (size, and redistribution terms), so this
re-creates the local copies from their official sources, and prints each file's size and SHA-256
for docs/references.md. A changed checksum means the publisher replaced the file: re-read it.

Only official publishers are listed (WMO, ICAMS/OFCM, NOAA NWS). WMO-No. 306 (Manual on Codes)
cannot be fetched here: library.wmo.int requires a human check, so it is downloaded by hand.
The ICAMS handbooks (FMH-1, FMH-2) are saved by hand too: the ICAMS host refuses scripted
requests (HTTP 403, 2026-10-20). docs/references.md lists the manual files with their checksums.

Usage (from the repo root):  uv run python scripts/fetch_references.py
Existing files are kept; delete one to download it again.
"""

from __future__ import annotations

import hashlib
import sys
import time
import urllib.request
from pathlib import Path

BUFR4 = "https://raw.githubusercontent.com/wmo-im/BUFR4/v46"  # WMO's official tables, release v46

# (local path under references/, official URL, expected content type)
DOCUMENTS = [
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
            time.sleep(5)  # be polite to the servers between downloads
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
