"""Shared PDF branding for invoices & reports.

Three pieces:
- the company logo, embedded directly in the light PDF header;
- a rubber-stamp style round company seal (Pillow-drawn per ship, cached),
  laid over every page as a faint watermark;
- an authorized-signature block: signature image above the line. Drop a real
  scanned signature at assets/images/signature.png to replace the generated
  placeholder — no code change needed.

The seal is rendered at runtime from the ship's name — nothing single-ship is
baked into an asset.
"""

import io
import math
from functools import lru_cache

from django.conf import settings
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ASSETS_DIR = settings.BASE_DIR / "assets"
LOGO_PATH = ASSETS_DIR / "images" / "logo.png"
FONTS_DIR = ASSETS_DIR / "fonts"

# Blue rubber-stamp ink — deliberately not the exact brand navy: real stamp
# pads read a touch brighter than printed artwork.
INK = (27, 63, 122)

_CANVAS = 1200  # internal render size; downsampled before embedding


SIGNATURE_PATH = ASSETS_DIR / "images" / "signature.png"


def draw_header_logo(pdf, x, y, height):
    """Company logo, straight onto the (light) header background."""
    pdf.image(str(LOGO_PATH), x, y, h=height)


def draw_watermark(pdf, ship_name, width=125):
    """Stamp the company seal as a faint watermark across the middle of every
    page. Drawn after the content (fills would cover it otherwise) at very low
    opacity, so the text underneath stays perfectly readable."""
    x = (pdf.w - width) / 2
    y = (pdf.h - width) / 2
    last_page = pdf.page
    for page in range(1, last_page + 1):
        pdf.page = page
        pdf.image(io.BytesIO(_watermark_png(ship_name)), x, y, w=width)
    pdf.page = last_page


def draw_signature_block(pdf, x, width, ship_name):
    """Signature over a line with 'Authorized Signatory / For <company>'
    captions. Uses assets/images/signature.png when present, otherwise the
    generated placeholder."""
    top_y = pdf.get_y()
    line_y = top_y + 20
    sig = _signature_png()
    with Image.open(io.BytesIO(sig)) as im:
        aspect = im.width / im.height
    sig_h = 15
    sig_w = min(sig_h * aspect, width - 8)
    pdf.image(io.BytesIO(sig), x + (width - sig_w) / 2, line_y - sig_h - 1,
              w=sig_w)
    pdf.set_draw_color(120, 128, 138)
    pdf.line(x + 4, line_y, x + width - 4, line_y)
    pdf.set_font("NotoSans", "", 8)
    pdf.set_text_color(105, 115, 125)
    pdf.set_xy(x, line_y + 1.5)
    pdf.cell(width, 5, "Authorized Signatory", align="C")
    pdf.set_xy(x, line_y + 6.5)
    pdf.set_font("NotoSans", "", 7)
    pdf.cell(width, 4, f"For {ship_name}", align="C")
    pdf.set_y(line_y + 12)


@lru_cache(maxsize=8)
def company_seal_png(ship_name):
    """Round official-seal PNG (RGBA bytes): company name curved on top,
    'OFFICIAL SEAL' curved on the bottom, star separators, the logo as an
    ink silhouette in the middle — with ink texture and a slight tilt."""
    size = _CANVAS
    cx = cy = size / 2
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ink = (*INK, 255)

    def ring(radius, stroke):
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            outline=ink, width=stroke,
        )

    ring(560, 16)   # outer rim
    ring(534, 5)
    ring(408, 5)    # inner ring enclosing the centre

    font = ImageFont.truetype(str(FONTS_DIR / "NotoSans-Bold.ttf"), 86)
    _arc_text(img, cx, cy, 470, ship_name.upper(), font, mid_deg=-90, ink=ink)
    _arc_text(
        img, cx, cy, 470, "OFFICIAL SEAL", font, mid_deg=90, ink=ink, inward=True
    )
    for side_deg in (180, 0):  # star separators at 9 and 3 o'clock
        _star(draw, cx + 470 * math.cos(math.radians(side_deg)),
              cy + 470 * math.sin(math.radians(side_deg)), 34, ink)

    # Centre: logo as a solid ink silhouette (its alpha channel, ink-filled)
    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo_w = 560
    logo = logo.resize((logo_w, int(logo.height * logo_w / logo.width)),
                       Image.LANCZOS)
    silhouette = Image.new("RGBA", logo.size, ink)
    silhouette.putalpha(logo.getchannel("A"))
    img.alpha_composite(
        silhouette, (int(cx - logo.width / 2), int(cy - logo.height / 2))
    )

    # Ink realism: mottled alpha (uneven ink take-up), soft edges, slight tilt
    noise = Image.effect_noise((size, size), 60).point(
        lambda v: min(255, 120 + v)
    )
    img.putalpha(ImageChops.multiply(img.getchannel("A"), noise))
    img = img.filter(ImageFilter.GaussianBlur(1.4))
    img = img.rotate(-8, resample=Image.BICUBIC, expand=True)
    img.thumbnail((640, 640), Image.LANCZOS)

    # Overall stamp opacity — a pressed stamp is never 100% solid
    img.putalpha(img.getchannel("A").point(lambda v: int(v * 0.90)))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@lru_cache(maxsize=8)
def _watermark_png(ship_name):
    """The company seal at watermark opacity (~7%)."""
    img = Image.open(io.BytesIO(company_seal_png(ship_name))).convert("RGBA")
    img.putalpha(img.getchannel("A").point(lambda v: int(v * 0.08)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _signature_png():
    if SIGNATURE_PATH.exists():
        return SIGNATURE_PATH.read_bytes()
    return _placeholder_signature_png()


@lru_cache(maxsize=1)
def _placeholder_signature_png():
    """Hand-drawn-looking placeholder signature (blue ink scribble), used
    until a real scanned signature is dropped at assets/images/signature.png."""
    w, h = 900, 300
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ink = (24, 48, 96, 235)

    # Main flourish: tall initial loops flowing into a wavy tail
    stroke = _spline([
        (80, 235), (135, 70), (185, 225), (240, 90), (300, 230),
        (355, 150), (420, 205), (490, 140), (560, 200), (640, 150),
        (720, 190), (800, 160),
    ])
    draw.line(stroke, fill=ink, width=9, joint="curve")
    # Underline flourish with an end dot
    under = _spline([(190, 262), (420, 245), (650, 265), (790, 240)])
    draw.line(under, fill=ink, width=6, joint="curve")
    draw.ellipse([812, 232, 830, 250], fill=ink)

    img = img.filter(ImageFilter.GaussianBlur(1.1))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _spline(points, steps=24):
    """Catmull-Rom spline through `points` → list of line segment vertices."""
    pts = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for t in (j / steps for j in range(steps + 1)):
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t
                       + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                       + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3)
                for k in (0, 1)
            ))
    return out


def _arc_text(img, cx, cy, radius, text, font, mid_deg, ink, inward=False,
              tracking=1.28):
    """Write `text` along a circle. `inward=False`: top arc, letters upright
    tangent to the circle. `inward=True`: bottom arc, letter tops pointing at
    the centre (classic seal bottom line). Angles: 0°=right, 90°=bottom."""
    direction = 1 if not inward else -1
    widths = [font.getlength(ch) * tracking for ch in text]
    total_angle = sum(widths) / radius
    angle = math.radians(mid_deg) - direction * total_angle / 2
    for ch, w in zip(text, widths):
        mid = angle + direction * (w / 2) / radius
        deg = math.degrees(mid)
        rot = -(deg + 90) if not inward else 90 - deg
        tile_size = font.size * 2
        tile = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
        tile_draw = ImageDraw.Draw(tile)
        tile_draw.text((tile_size / 2, tile_size / 2), ch, font=font, fill=ink,
                       anchor="mm")
        tile = tile.rotate(rot, resample=Image.BICUBIC, expand=True)
        x = cx + radius * math.cos(mid) - tile.width / 2
        y = cy + radius * math.sin(mid) - tile.height / 2
        img.alpha_composite(tile, (int(x), int(y)))
        angle += direction * w / radius


def _star(draw, cx, cy, radius, ink):
    """Five-point star polygon (font-independent — no missing-glyph tofu)."""
    points = []
    for i in range(10):
        r = radius if i % 2 == 0 else radius * 0.42
        a = math.radians(-90 + i * 36)
        points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.polygon(points, fill=ink)
