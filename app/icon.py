"""The app's mark, drawn in one place.

No bitmap is committed for this: the tray draws it at runtime, and
packaging/make-icon.py renders the same function to the .ico that the exe, the
shortcuts and the installer use. One implementation, so the icon on disk and
the icon in the notification area cannot drift apart — the same reasoning as
app/avail.py, applied to a picture.

Everything is laid out on a 256-unit grid and drawn at 4x, then reduced. PIL
draws shapes without antialiasing, and an icon this size is nearly all curves,
so the supersampling is what makes the edges clean rather than ragged. Sizes of
24px and below get a coarser drawing deliberately: the binder rings land under
2px there and read as dirt rather than as detail, so they are dropped and the
bars that remain are made chunkier.
"""

from __future__ import annotations

from typing import Any

# U of T / Rotman blue, and the app's own accent, green and ochre (--ac, --gn
# and --og in web/calendar.html), lightened for the navy ground. At full
# strength the accent is barely separable from the body it sits on: #0d64f4
# against #002a5c is a contrast ratio of about 2.5:1, which is a smudge at
# 16px. These are the same three hues at roughly 4.5:1.
NAVY = (0, 42, 92, 255)
PAPER = (255, 255, 255, 255)
BAR_BLUE = (77, 142, 247, 255)
BAR_GREEN = (34, 170, 122, 255)
BAR_OCHRE = (214, 158, 38, 255)

_GRID = 256
_SUPERSAMPLE = 4


def paint(size: int = 64) -> Any:
    """The mark, `size` pixels square, as an RGBA image."""
    from PIL import Image, ImageChops, ImageDraw

    k = (size * _SUPERSAMPLE) / _GRID
    canvas = size * _SUPERSAMPLE

    def box(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0 * k, y0 * k, x1 * k, y1 * k]

    def units(n: float) -> int:
        return max(1, round(n * k))

    small = size <= 24
    if small:
        # 16px in mind. One unit here is 1/16 of a pixel, so the margins are
        # ~2px a side and the gaps ~1px: below that, LANCZOS turns the whole
        # thing into a grey smear. Two rows of bars, not three, for the same
        # reason.
        body = (8, 8, 248, 248)
        radius, header_to = 36, 40
        rings: list[tuple[float, float, float, float]] = []
        bars = [
            (40, 72, 120, 136, BAR_BLUE),
            (136, 72, 216, 136, BAR_GREEN),
            (40, 152, 184, 216, BAR_OCHRE),
        ]
    else:
        body = (24, 40, 232, 240)
        radius, header_to = 32, 84
        rings = [(68, 16, 92, 56), (164, 16, 188, 56)]
        bars = [
            (48, 108, 116, 152, BAR_BLUE),
            (140, 108, 208, 152, BAR_GREEN),
            (48, 176, 184, 220, BAR_OCHRE),
        ]

    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # The rings first: the body and then the header band are painted over their
    # lower half, so they emerge from the top of the calendar without a seam.
    for x0, y0, x1, y1 in rings:
        d.rounded_rectangle(box(x0, y0, x1, y1), radius=units((x1 - x0) / 2),
                            fill=PAPER)

    # The body's own shape, kept as a mask. The header band has to be *clipped*
    # to it: a plain white rectangle over a rounded rectangle leaves square
    # white corners sticking out past the curve, which is what the tray icon
    # used to look like.
    shape = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(shape).rounded_rectangle(box(*body), radius=units(radius),
                                            fill=255)
    img.paste(NAVY, (0, 0), shape)

    band = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(band).rectangle(box(body[0], body[1], body[2], header_to),
                                   fill=255)
    img.paste(PAPER, (0, 0), ImageChops.multiply(band, shape))

    for x0, y0, x1, y1, colour in bars:
        d.rounded_rectangle(box(x0, y0, x1, y1), radius=units(10), fill=colour)

    return img.resize((size, size), Image.Resampling.LANCZOS)
