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

from ai_video_factory.bots.cta_motion_copy import CTAMotionCopy

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
        pix[0, y] = tuple(round(a + (b - a) * f) for a, b in zip(NAVY, STEEL, strict=True))
    bg = strip.resize((W, H)).convert("RGBA")
    halo = Image.new("RGBA", (900, 900), GOLD + (0,))
    alpha = Image.new("L", halo.size, 0)
    ad = ImageDraw.Draw(alpha)
    for r in range(430, 0, -8):
        ad.ellipse((450-r, 450-r, 450+r, 450+r), fill=int(45 * (1-r/430)**2))
    halo.putalpha(alpha.filter(ImageFilter.GaussianBlur(32)))
    _paste(bg, halo, -190, 90)
    return bg


def _wrap_to_width(text: str, draw: ImageDraw.ImageDraw,
                   font: ImageFont.FreeTypeFont, limit: int) -> list[str]:
    """Wrap display copy at word boundaries (characters for unspaced scripts)."""
    parts = text.split() if " " in text else list(text)
    separator = " " if " " in text else ""
    lines: list[str] = []
    current = ""
    for part in parts:
        candidate = current + (separator if current else "") + part
        if draw.textbbox((0, 0), candidate, font=font)[2] <= limit:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = part
            if draw.textbbox((0, 0), current, font=font)[2] > limit:
                raise ValueError("CTA motion copy has an unbreakable word too wide for the layout")
    if current:
        lines.append(current)
    return lines


def _copy_layout(
    copy: CTAMotionCopy, text_width: int,
) -> tuple[list[str], ImageFont.FreeTypeFont, list[ImageFont.FreeTypeFont]]:
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in (62, 58, 54, 50, 46, 42, 38):
        title_font = _font(size)
        try:
            title_lines = _wrap_to_width(copy.title, probe, title_font, text_width)
        except ValueError:
            continue
        if len(title_lines) <= 2:
            break
    else:
        raise ValueError("CTA motion title cannot fit within two lines")
    bullet_fonts: list[ImageFont.FreeTypeFont] = []
    for line in copy.benefits:
        for size in (34, 32, 30, 28, 26, 24, 22):
            font = _font(size, bold=False)
            if probe.textbbox((0, 0), line, font=font)[2] <= text_width - 52:
                bullet_fonts.append(font)
                break
        else:
            raise ValueError("CTA motion benefit cannot fit on one line")
    return title_lines, title_font, bullet_fonts


def _render_frame(
    t: float, *, background: Image.Image, cover: Image.Image,
    qr: Image.Image, domain: str, copy: CTAMotionCopy | None = None,
    layout: tuple[list[str], ImageFont.FreeTypeFont, list[ImageFont.FreeTypeFont]] | None = None,
) -> Image.Image:
    bg = background.copy()
    d = ImageDraw.Draw(bg, "RGBA")
    # v4: large book slides from the left before the QR enters from above.
    cov_h = min(H - 140, round(670 * cover.height / cover.width))
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
    text_x = max(780, 70 + cov_w + 72)
    eye = _ease((t - 1.4) / 0.45)
    if eye:
        eye_y = 340 if copy is not None else H//2 - 65
        d.text((text_x, eye_y), "THE BOOK", fill=GOLD + (round(255*eye),), font=_font(48))
        d.line((text_x, eye_y + 75, text_x + round(125*eye), eye_y + 75),
               fill=GOLD, width=4)
    if copy is not None and layout is not None:
        title_lines, title_font, bullet_fonts = layout
        for i, line in enumerate(title_lines):
            reveal = _ease((t - 1.75 - 0.18*i) / 0.55)
            if reveal:
                d.text((text_x + round((1-reveal)*24), 430 + 74*i), line,
                       fill=WHITE + (round(255*reveal),), font=title_font)
        bullet_y = 615 if len(title_lines) == 1 else 680
        for i, (line, font) in enumerate(zip(copy.benefits, bullet_fonts, strict=True)):
            reveal = _ease((t - 2.9 - 0.28*i) / 0.50)
            if reveal:
                y = bullet_y + 62*i
                d.ellipse((text_x, y+4, text_x+24, y+28),
                          outline=GOLD + (round(255*reveal),), width=3)
                d.line((text_x+6,y+16,text_x+11,y+21),
                       fill=GOLD + (round(255*reveal),), width=3)
                d.line((text_x+11,y+21,text_x+19,y+9),
                       fill=GOLD + (round(255*reveal),), width=3)
                d.text((text_x+42, y), line,
                       fill=WHITE + (round(255*reveal),), font=font)
    body = _ease((t - 2.1) / 0.6)
    if body:
        d.text(
            (text_x, 925 if copy is not None else H//2 + 48),
            "SCAN FOR YOUR COPY",
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
                 duration_seconds: float, destination: Path, *,
                 copy: CTAMotionCopy | None = None) -> None:
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
    book_height = min(H - 140, round(670 * cover.height / cover.width))
    book_width = round(book_height * cover.width / cover.height)
    text_x = max(780, 70 + book_width + 72)
    layout = _copy_layout(copy, W - text_x - 45) if copy is not None else None
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
        if proc.stdin is None:
            raise RuntimeError("CTA FFmpeg stdin unavailable")
        for frame in range(frames):
            proc.stdin.write(_render_frame(frame/FPS, background=background,
                                           cover=cover, qr=qr, domain=domain,
                                           copy=copy, layout=layout).tobytes())
        proc.stdin.close()
        if proc.stderr is None:
            raise RuntimeError("CTA FFmpeg stderr unavailable")
        error = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise RuntimeError(f"CTA FFmpeg render failed: {error[-1000:]}")
        tmp.replace(destination)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        tmp.unlink(missing_ok=True)
