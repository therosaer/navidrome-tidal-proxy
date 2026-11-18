"""Stamp a small "T" badge in the bottom-right corner of Tidal cover art."""
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


def add_badge(data: bytes) -> bytes:
    img = Image.open(BytesIO(data)).convert("RGB")
    w, h = img.size
    side = max(14, round(min(w, h) * 0.24))
    margin = max(2, round(side * 0.15))
    x0, y0 = w - side - margin, h - side - margin
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([x0, y0, x0 + side, y0 + side], radius=side * 0.22, fill=(0, 0, 0, 215))
    font = ImageFont.load_default(size=round(side * 0.7))
    d.text((x0 + side / 2, y0 + side / 2), "T", fill=(255, 255, 255, 255), font=font, anchor="mm")
    out = BytesIO()
    Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()
