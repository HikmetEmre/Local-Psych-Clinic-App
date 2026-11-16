"""Build UI images from the clinic logo (run once, outputs are committed).

    uv run python scripts/make_icons.py

Produces the header mark, favicon and PWA icons from ``assets/nil_psikoloji_logo.jpeg``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "nil_psikoloji_logo.jpeg"
OUT = ROOT / "packages" / "shared" / "klinik_shared" / "ui" / "static" / "img"

# The circular emblem (leaf + N) inside the 1004x479 logo, as a square.
EMBLEM_BOX = (348, 8, 688, 348)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, OUT / "logo.jpeg")

    emblem = Image.open(SOURCE).convert("RGB").crop(EMBLEM_BOX)
    for name, size in {
        "favicon.png": 48,
        "mark-96.png": 96,
        "icon-192.png": 192,
        "icon-512.png": 512,
    }.items():
        emblem.resize((size, size), Image.Resampling.LANCZOS).save(OUT / name, optimize=True)
        print(f"wrote {name} ({size}px)")


if __name__ == "__main__":
    main()
