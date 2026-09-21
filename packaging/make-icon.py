"""Render app/icon.py to the .ico the exe, the shortcuts and the installer use.

Run from the repository root, with the venv:

    .venv\\Scripts\\python.exe packaging\\make-icon.py

The .ico is a build output that is committed, because PyInstaller and Inno
Setup both need a file on disk rather than a function. It is generated from
app/icon.py rather than drawn separately so the icon in the notification area
and the icon Explorer shows cannot drift apart; re-run this after changing the
mark.

Each size is rendered at that size rather than resampled down from 256, because
app/icon.py draws a coarser mark below 25px -- the binder rings are under 2px
there and turn to dirt. That is what append_images is for: Pillow would
otherwise scale one image down to every requested size and the small sizes
would get the detailed drawing, scaled.

Entries are PNG-compressed, which is Pillow's default and what it did here
(measured: all nine payloads start with the PNG signature; the 256px frame is
6.7 KB rather than 256 KB of raw BGRA). That is the modern shape for an .ico and
Windows itself has read it since Vista, but this file has two other readers --
PyInstaller's resource compile and Inno Setup's shortcut and Add/Remove
Programs icons -- so PNG is used on the strength of those two accepting it,
which was checked when the build and the installer were first run rather than
assumed. If either ever rejects it, pass bitmap_format="bmp" to save() below:
that produces the older, larger, universally-read form.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.icon import paint  # noqa: E402

# What Explorer, the taskbar, Alt-Tab and the installer ask for. 256 is the
# extra-large view; 16 is the notification area and the title bar; 20 and 40
# are the 125%-and-200%-scaled versions of those, which Windows picks itself
# from the nearest size rather than asking for.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

OUT = ROOT / "packaging" / "RotmanLSMCalendar.ico"


def main() -> int:
    frames = [paint(n) for n in SIZES]
    largest = frames[-1]
    largest.save(
        OUT,
        format="ICO",
        sizes=[(n, n) for n in SIZES],
        append_images=frames,
    )
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")

    # Read it back and check rather than assume: an .ico that Explorer renders
    # wrong is not something this script could otherwise tell you about. Every
    # size must be present, and each one must be the drawing for *its* size --
    # comparing against a resampled 256 would catch append_images being
    # ignored, which fails silently and ships nine sizes of one image.
    with Image.open(OUT) as check:
        present = sorted(check.ico.sizes())
        expected = sorted((n, n) for n in SIZES)
        if present != expected:
            print(f"  FAIL: sizes in the file are {present}, expected {expected}")
            return 1
        for n in SIZES:
            check.size = (n, n)
            check.load()
            drawn = paint(n)
            if check.convert("RGBA").tobytes() != drawn.tobytes():
                print(f"  FAIL: the {n}px frame is not paint({n})")
                return 1
    print(f"  verified: {len(SIZES)} frames, each one the drawing for its size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
