"""Download Pretendard Korean fonts into assets/fonts/.

Run once before first use of make_reel.py:
    python setup_fonts.py
"""

from __future__ import annotations

import sys
import zipfile
from io import BytesIO
from pathlib import Path

import requests


PRETENDARD_RELEASE = (
    "https://github.com/orioncactus/pretendard/releases/download/"
    "v1.3.9/Pretendard-1.3.9.zip"
)
FONT_DIR = Path(__file__).parent / "assets" / "fonts"
WANTED = {"Pretendard-Regular.otf", "Pretendard-Bold.otf", "Pretendard-Black.otf"}


def main() -> int:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    missing = [name for name in WANTED if not (FONT_DIR / name).exists()]
    if not missing:
        print(f"All fonts already present in {FONT_DIR}")
        return 0

    print(f"Downloading Pretendard from {PRETENDARD_RELEASE}")
    resp = requests.get(PRETENDARD_RELEASE, timeout=120)
    resp.raise_for_status()

    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if name in WANTED:
                target = FONT_DIR / name
                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                print(f"  -> {target}")

    still_missing = [name for name in WANTED if not (FONT_DIR / name).exists()]
    if still_missing:
        print(f"Failed to extract: {still_missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
