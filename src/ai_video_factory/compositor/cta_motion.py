"""Reusable casero CTA book/QR animation adapted from the supplied v4 QR-top layout.

The original demonstration hard-coded a book title, three unsupported benefits
and "NEW RELEASE". Production uses only the configured cover and real QR URL:
never substitute one avatar's product claims into another avatar's video.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
NAVY = (14, 26, 38)
STEEL = (46, 79, 110)
GOLD = (184, 134, 43)
WHITE = (255, 255, 255)


def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont:
    names = (
        ["C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["C:/Windows/Fonts/segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for name in names:
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    raise RuntimeError("CTA motion requires Segoe UI or DejaVu fonts")


def _ease(value: float) -> float:
    x = max(0.0, min(1.0, value))
    return 1 - (1 - x) ** 3


def _paste(base: Image.Image, layer: Image.Image, left: int, top: int) -> None:
    """Crop moving layers as they enter the screen from negative coordinates."""
    x, y = max(0, left), max(0, top)
    right, bottom = min(W, left + layer.width), min(H, top + layer.height)
    if x < right and y < bottom:
        cropped = layer.crop((x - left, y - top, right - left, bottom - top))
        base.alpha_composite(cropped, (x, y))


def _background() -> Image.Image:
    strip = Image.new("RGB", (1, H))
    pix = strip.load()
    for y in range(H):
        f = y / max(1, H - 1)
        pix[0, y] = tuple(round(a + (b - a) * f) for a, b in zip(NAVY, STEEL))
    bg = strip.resize((W, H)).convert("RGBA")
    halo = Image.new("RGBA", (900, 900), GOLD + (0,))
    alpha = Image.new("L", halo.size, 0)
    ad = ImageDraw.Draw(alpha)
    for r in range(430, 0, -8):
        ad.ellipse((450-r, 450-r, 450+r, 450+r), fill=int(45 * (1-r/430)**2))
    halo.putalpha(alpha.filter(ImageFilter.GaussianBlur(32)))
    _paste(bg, halo, -190, 90)
    return bg


def _render_frame(
    t: float, *, background: Image.Image, cover: Image.Image,
    qr: Image.Image, domain: str,
) -> Image.Image:
    bg = background.copy()
    d = ImageDraw.Draw(bg, "RGBA")
    # v4: large book slides from the left before the QR enters from above.
    cov_h = H - 140
    cov_w = round(cov_h * cover.width / cover.height)
    progress = _ease((t - 0.25) / 1.0)
    if progress > 0:
        image = cover.resize((cov_w, cov_h), Image.Resampling.LANCZOS).convert("RGBA")
        image.putalpha(round(255 * progress))
        x = round(70 - 420 * (1 - progress))
        shadow = Image.new("RGBA", (cov_w + 48, cov_h + 48))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (24, 24, cov_w + 24, cov_h + 24), radius=10,
            fill=(0, 0, 0, round(155 * progress)),
        )
        _paste(bg, shadow.filter(ImageFilter.GaussianBlur(20)), x-24, 46)
        _paste(bg, image, x, 70)
    text_x = min(W - 550, 70 + cov_w + 90)
    eye = _ease((t - 1.4) / 0.45)
    if eye:
        d.text((text_x, H//2 - 65), "THE BOOK", fill=GOLD + (round(255*eye),), font=_font(48))
        d.line((text_x, H//2 + 15, text_x + round(125*eye), H//2 + 15), fill=GOLD, width=4)
    body = _ease((t - 2.1) / 0.6)
    if body:
        d.text(
            (text_x, H//2 + 48), "SCAN FOR YOUR COPY",
            fill=WHITE + (round(255*body),), font=_font(38),
        )
    # Original v4 hierarchy: QR card at the top-right, explanatory text outside.
    pqr = _ease((t - 4.1) / 0.6)
    if pqr:
        box_w, box_h, size = 290, 240, 190
        bx, by = W-box_w-40, round(60-40*(1-pqr))
        d.rounded_rectangle((bx, by, bx+box_w, by+box_h), radius=8, fill=NAVY + (235,))
        d.rectangle((bx, by+box_h-4, bx+box_w, by+box_h), fill=GOLD)
        qx, qy = bx + (box_w-size)//2, by + (box_h-size)//2
        d.rounded_rectangle((qx-14,qy-14,qx+size+14,qy+size+14),radius=14,fill=WHITE + (255,))
        _paste(bg, qr.resize((size, size), Image.Resampling.NEAREST).convert("RGBA"), qx, qy)
        if t >= 4.7:
            wave = (math.sin((t-4.7)*math.pi/1.2)+1)/2
            for x,y,dx,dy in ((qx-8,qy-8,1,1),(qx+size+8,qy-8,-1,1),
                              (qx-8,qy+size+8,1,-1),(qx+size+8,qy+size+8,-1,-1)):
                d.line((x,y,x+dx*(19+round(5*wave)),y),fill=GOLD,width=4)
                d.line((x,y,x,y+dy*(19+round(5*wave))),fill=GOLD,width=4)
            yy = qy + round(size * (1-math.cos((t-4.7)*math.pi))/2)
            d.line((qx,yy,qx+size,yy),fill=(255,235,152,120),width=4)
        label_font = _font(32)
        for j, line in enumerate(("SCAN WITH YOUR", "MOBILE PHONE")):
            box = d.textbbox((0,0),line,font=label_font)
            d.text((bx-30-(box[2]-box[0]),by+53+44*j),line,
                   fill=WHITE + (round(255*pqr),),font=label_font)
        if domain:
            font = _font(24,bold=False)
            box = d.textbbox((0,0),domain,font=font)
            d.text((bx-30-(box[2]-box[0]),by+154),domain,
                   fill=(196,211,225,round(255*pqr)),font=font)
    d.rectangle((0,H-6,W,H),fill=GOLD + (210,))
    return bg.convert("RGB")


def build_motion(cover_path: Path, qr_path: Path, qr_url: str,
                 duration_seconds: float, destination: Path) -> None:
    if not 1.5 <= duration_seconds <= 60.0:
        raise ValueError("CTA motion duration must be between 1.5 and 60 seconds")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to build the CTA motion graphics")
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("Unsafe CTA motion destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".part.mp4")
    if tmp.is_symlink():
        raise ValueError("Unsafe CTA motion temporary file")
    with Image.open(cover_path) as source:
        cover = source.convert("RGB")
    with Image.open(qr_path) as source:
        qr = source.convert("RGB")
    domain = urlsplit(qr_url).hostname or ""
    background = _background()
    frames = math.ceil(duration_seconds * FPS)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{W}x{H}",
        "-framerate", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-frames:v", str(frames), str(tmp),
    ]
    tmp.unlink(missing_ok=True)
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdin is not None
        for frame in range(frames):
            proc.stdin.write(_render_frame(frame/FPS, background=background,
                                           cover=cover, qr=qr, domain=domain).tobytes())
        proc.stdin.close()
        assert proc.stderr is not None
        error = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise RuntimeError(f"CTA FFmpeg render failed: {error[-1000:]}")
        tmp.replace(destination)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        tmp.unlink(missing_ok=True)
