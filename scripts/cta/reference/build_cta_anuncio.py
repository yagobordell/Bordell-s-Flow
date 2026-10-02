# -*- coding: utf-8 -*-
"""Anuncio completo animado (pantalla de venta): el libro aparece primero (desliza + escala +
fade), luego el titulo, luego la descripcion y el dominio, y por ultimo la tarjeta del QR con
barrido de luz en bucle. Todo con easing, como un anuncio real, no una tarjeta estatica.
"""
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = Path(__file__).parent
W, H = 1920, 1080
FPS = 30
DURATION_S = 15
N_FRAMES = FPS * DURATION_S

NAVY_DARK = (14, 26, 38)
STEEL = (46, 79, 110)
GOLD = (184, 134, 43)
WHITE = (255, 255, 255)
MUTED = (176, 190, 202)

FONTS = Path("C:/Windows/Fonts")
f_eyebrow = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 30)
f_title = ImageFont.truetype(str(FONTS / "georgiab.ttf"), 74)
f_desc = ImageFont.truetype(str(FONTS / "segoeui.ttf"), 34)
f_domain = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 40)
f_qr_label = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 22)


def clamp01(x):
    return max(0.0, min(1.0, x))


def ease_out_cubic(x):
    x = clamp01(x)
    return 1 - (1 - x) ** 3


def ease_out_back(x):
    x = clamp01(x)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def progress(t, start, dur):
    if dur <= 0:
        return 1.0 if t >= start else 0.0
    return ease_out_cubic((t - start) / dur)


def progress_back(t, start, dur):
    """Igual que progress pero con un pequeno rebote/overshoot (mas dinamico, look de anuncio)."""
    if dur <= 0:
        return 1.0 if t >= start else 0.0
    return ease_out_back((t - start) / dur)


def vertical_gradient(w, h, top, bottom):
    base = Image.new("RGB", (1, h), color=0)
    for y in range(h):
        tt = y / h
        r = int(top[0] + (bottom[0] - top[0]) * tt)
        g = int(top[1] + (bottom[1] - top[1]) * tt)
        b = int(top[2] + (bottom[2] - top[2]) * tt)
        base.putpixel((0, y), (r, g, b))
    return base.resize((w, h))


def build_background():
    """Fondo cine-grado: degradado base + halo calido tras el libro + vinetado + grano fino."""
    bg = vertical_gradient(W, H, NAVY_DARK, STEEL).convert("RGBA")

    # halo/spotlight calido detras de donde aparece el libro (da profundidad, look de anuncio)
    halo_r = 620
    hx, hy = 260, H // 2
    halo = Image.new("L", (halo_r * 2, halo_r * 2), 0)
    hd = ImageDraw.Draw(halo)
    for rr in range(halo_r, 0, -4):
        a = int(70 * (1 - rr / halo_r) ** 2)
        hd.ellipse((halo_r - rr, halo_r - rr, halo_r + rr, halo_r + rr), fill=a)
    halo = halo.filter(ImageFilter.GaussianBlur(40))
    halo_rgba = Image.new("RGBA", halo.size, GOLD + (0,))
    halo_rgba.putalpha(halo)
    bg.alpha_composite(halo_rgba, (hx - halo_r, hy - halo_r))

    # vinetado suave real (por distancia al centro, sin artefactos de elipses concentricas)
    import numpy as np
    yy, xx = np.mgrid[0:H, 0:W]
    nx = (xx - W / 2) / (W / 2)
    ny = (yy - H / 2) / (H / 2)
    dist = np.sqrt(nx ** 2 + ny ** 2)
    vig = np.clip((dist - 0.55) / (1.15 - 0.55), 0, 1)
    alpha = (vig * 95).astype(np.uint8)
    dark_rgba = np.zeros((H, W, 4), dtype=np.uint8)
    dark_rgba[..., 3] = alpha
    dark_layer = Image.fromarray(dark_rgba, mode="RGBA")
    bg.alpha_composite(dark_layer)

    # grano fino (textura cine, muy sutil)
    noise = Image.effect_noise((W // 2, H // 2), 14).resize((W, H)).convert("L")
    grain = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    grain.putalpha(Image.eval(noise, lambda p: int(p * 0.03)))
    bg.alpha_composite(grain)

    return bg


def wrap_lines(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w_ in words:
        test = (cur + " " + w_).strip()
        if draw.textlength(test, font=font) > max_w and cur:
            lines.append(cur); cur = w_
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def paste_alpha_text(base, xy, text, font, fill_rgb, alpha):
    if alpha <= 0:
        return
    bbox = base.getbbox() if False else None
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text(xy, text, font=font, fill=fill_rgb + (int(alpha),))
    base.alpha_composite(layer)


def corner_brackets(draw, box, length, width, color, alpha=255):
    x0, y0, x1, y1 = box
    col = color + (int(alpha),)
    for cx, cy, dx, dy in [(x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)]:
        draw.line([(cx, cy), (cx + dx * length, cy)], fill=col, width=width)
        draw.line([(cx, cy), (cx, cy + dy * length)], fill=col, width=width)


# ---- timeline (segundos) ----
T_BOOK_START, T_BOOK_DUR = 0.25, 1.0
T_EYEBROW_START, T_EYEBROW_DUR = 1.15, 0.45
TITLE_LINE_START, TITLE_LINE_STAGGER, TITLE_LINE_DUR = 1.5, 0.18, 0.55
T_DESC_START, T_DESC_DUR = 2.6, 0.5
T_DOMAIN_START, T_DOMAIN_DUR = 3.1, 0.5
T_QR_START, T_QR_DUR = 3.5, 0.6
T_QR_ANIM_START = T_QR_START + T_QR_DUR  # a partir de aqui, barrido en bucle


f_brand = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 24)
f_cta_big = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 30)


def tracked_text(base, xy, text, font, fill_rgb, alpha, tracking=4):
    """Texto con leve espaciado entre letras (look editorial premium)."""
    x, y = xy
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for ch in text:
        ld.text((x, y), ch, font=font, fill=fill_rgb + (int(alpha),))
        x += ld.textlength(ch, font=font) + tracking
    base.alpha_composite(layer)


def build_frame(t, title_lines, desc, domain, cover, qr, bg):
    img = bg.copy()
    d = ImageDraw.Draw(img, "RGBA")

    # --- marca superior (look editorial de anuncio de marca) ---
    p_brand = progress(t, 0.0, 0.6)
    if p_brand > 0:
        tracked_text(img, (150, 70), "WADE CARTER  •  TORNADO READY", f_brand, GOLD, 210 * p_brand, tracking=3)
        d.line((150, 104, 150 + int(560 * p_brand), 104), fill=(255, 255, 255, int(40 * p_brand)), width=1)

    # --- portada del libro: desliza desde la izquierda + fade + ligero zoom continuo (Ken Burns) ---
    p_book = progress(t, T_BOOK_START, T_BOOK_DUR)
    if p_book > 0:
        kb = 1.0 + 0.02 * clamp01((t - T_BOOK_START) / max(DURATION_S - T_BOOK_START, 1))
        cov_h = int(820 * kb)
        cov_w = int(cov_h * cover.width / cover.height)
        cov = cover.resize((cov_w, cov_h), Image.LANCZOS)

        shadow = Image.new("RGBA", (cov_w + 60, cov_h + 60), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle((30, 40, 30 + cov_w, 40 + cov_h), 8, fill=(0, 0, 0, int(140 * p_book)))
        shadow = shadow.filter(ImageFilter.GaussianBlur(28))

        final_cx, final_cy = 150, (H - 820) // 2
        start_cx = final_cx - 420
        cx = int(start_cx + (final_cx - start_cx) * p_book)
        cy = final_cy - (cov_h - 820) // 2

        cov_layer = Image.new("RGBA", (cov_w, cov_h), (0, 0, 0, 0))
        cov_layer.paste(cov.convert("RGBA"), (0, 0))
        alpha_mask = Image.new("L", (cov_w, cov_h), int(255 * p_book))
        cov_rgba = cov.convert("RGBA")
        cov_rgba.putalpha(alpha_mask)

        # flotado sutil y continuo una vez que el libro ha terminado de entrar (da vida, look premium)
        bob = math.sin(t * 1.1) * 6 * p_book
        cy = int(cy + bob)

        img.alpha_composite(shadow, (cx - 30, cy - 20))
        img.alpha_composite(cov_rgba, (cx, cy))
        d.rectangle((cx, cy, cx + cov_w, cy + cov_h), outline=(255, 255, 255, int(60 * p_book)), width=2)
        # rim light dorado en el canto derecho de la portada (look de estudio/producto premium)
        d.line((cx + cov_w, cy + 6, cx + cov_w, cy + cov_h - 6), fill=GOLD + (int(160 * p_book),), width=3)

        # cinta "NEW RELEASE" en la esquina superior de la portada (refuerzo de deseo/urgencia)
        p_ribbon = progress_back(t, T_BOOK_START + T_BOOK_DUR - 0.1, 0.45)
        if p_ribbon > 0:
            rw, rh = 240, 46
            ribbon = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
            rd = ImageDraw.Draw(ribbon)
            rd.rectangle((0, 0, rw, rh), fill=GOLD + (int(255 * p_ribbon),))
            rtxt = "NEW RELEASE"
            rbbox = rd.textbbox((0, 0), rtxt, font=f_qr_label)
            rlw = rbbox[2] - rbbox[0]
            rd.text((rw / 2 - rlw / 2, rh / 2 - 12), rtxt, font=f_qr_label, fill=NAVY_DARK + (int(255 * p_ribbon),))
            ribbon = ribbon.rotate(-32, expand=True, resample=Image.BICUBIC)
            img.alpha_composite(ribbon, (cx - 66, cy - 18))

        tx = final_cx + int(820 * cover.width / cover.height) + 130
    else:
        tx = 150 + int(820 * cover.width / cover.height) + 130

    # --- columna de texto derecha ---
    ty = H // 2 - 220
    p_eye = progress(t, T_EYEBROW_START, T_EYEBROW_DUR)
    if p_eye > 0:
        off = int((1 - p_eye) * 24)
        paste_alpha_text(img, (tx, ty + off), "THE BOOK", f_eyebrow, GOLD, 255 * p_eye)
        d.line((tx, ty + 46, tx + int(46 * p_eye), ty + 46), fill=GOLD + (int(255 * p_eye),), width=3)
    ty += 80

    for i, line in enumerate(title_lines):
        p_line = progress_back(t, TITLE_LINE_START + i * TITLE_LINE_STAGGER, TITLE_LINE_DUR)
        p_line_alpha = progress(t, TITLE_LINE_START + i * TITLE_LINE_STAGGER, TITLE_LINE_DUR)
        if p_line_alpha > 0:
            off = int((1 - p_line) * 34)
            paste_alpha_text(img, (tx + off + 3, ty + 4), line, f_title, (0, 0, 0), 160 * p_line_alpha)
            paste_alpha_text(img, (tx + off, ty), line, f_title, WHITE, 255 * p_line_alpha)
        ty += 86
    ty += 22

    p_desc = progress(t, T_DESC_START, T_DESC_DUR)
    if p_desc > 0:
        d.line((tx, ty, tx + int(90 * p_desc), ty), fill=GOLD + (int(255 * p_desc),), width=3)
    ty += 30
    if p_desc > 0:
        dwrap = Image.new("RGBA", (1, 1))
        dd = ImageDraw.Draw(dwrap)
        for line in wrap_lines(dd, desc, f_desc, W - tx - 480):
            off = int((1 - p_desc) * 20)
            paste_alpha_text(img, (tx, ty + off), line, f_desc, MUTED, 255 * p_desc)
            ty += 46
    ty += 20

    p_dom = progress(t, T_DOMAIN_START, T_DOMAIN_DUR)
    if p_dom > 0:
        scale_pop = 0.85 + 0.15 * p_dom
        fdom = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), int(40 * scale_pop))
        paste_alpha_text(img, (tx, ty), domain, fdom, GOLD, 255 * p_dom)

    p_benefit = progress(t, T_DOMAIN_START + 0.35, 0.5)
    if p_benefit > 0:
        off = int((1 - p_benefit) * 14)
        paste_alpha_text(img, (tx, ty + 52 + off), "Instant access  •  Read it tonight", f_qr_label, MUTED, 220 * p_benefit)

    # --- tarjeta QR: sube desde abajo + fade, luego barrido en bucle ---
    p_qr = progress(t, T_QR_START, T_QR_DUR)
    if p_qr > 0:
        QR = 190
        final_qx, final_qy = W - 250, H - 260
        qy = int(final_qy + 90 * (1 - p_qr))
        qx = final_qx

        if t >= T_QR_ANIM_START:
            breathe = (math.sin((t - T_QR_ANIM_START) * 2 * math.pi / 2.4) + 1) / 2
            halo_r = 150 + int(20 * breathe)
            halo = Image.new("RGBA", (halo_r * 2, halo_r * 2), (0, 0, 0, 0))
            hd2 = ImageDraw.Draw(halo)
            for rr in range(halo_r, 0, -3):
                a = int(55 * (1 - rr / halo_r) ** 2 * (0.6 + 0.4 * breathe))
                hd2.ellipse((halo_r - rr, halo_r - rr, halo_r + rr, halo_r + rr), fill=GOLD + (a,))
            halo = halo.filter(ImageFilter.GaussianBlur(18))
            img.alpha_composite(halo, (qx + 95 - halo_r, qy + 95 - halo_r))

        card = Image.new("RGBA", (QR + 40, QR + 60), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(card)
        cdraw.rounded_rectangle((0, 0, QR + 40, QR + 60), 18, fill=(255, 255, 255, int(255 * p_qr)))
        img.alpha_composite(card, (qx - 20, qy - 20))

        qr_rgba = qr.convert("RGBA")
        qr_rgba.putalpha(int(255 * p_qr))
        img.alpha_composite(qr_rgba, (qx, qy))

        # esquinas + barrido: solo una vez que la tarjeta ha terminado de entrar
        if t >= T_QR_ANIM_START:
            tl = t - T_QR_ANIM_START
            pulse = (math.sin(tl * 2 * math.pi / 1.6) + 1) / 2
            calpha = int(140 + 115 * pulse)
            pad = 10
            corner_brackets(d, (qx - pad, qy - pad, qx + QR + pad, qy + QR + pad), 34, 5, GOLD, calpha)

            cyc = (tl % 2.0) / 2.0
            tt = (1 - math.cos(cyc * 2 * math.pi)) / 2
            line_y = qy + tt * QR
            glow = Image.new("RGBA", (QR, 14), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            for k in range(7):
                a = int(160 * (1 - abs(k - 3) / 3))
                gd.line([(0, 7 + k - 3), (QR, 7 + k - 3)], fill=GOLD + (a,), width=1)
            img.alpha_composite(glow, (qx, int(line_y - 7)))
        else:
            corner_brackets(d, (qx - 10, qy - 10, qx + QR + 10, qy + QR + 10), 34, 5, GOLD, int(140 * p_qr))

        bbox = d.textbbox((0, 0), "SCAN FOR THE BOOK", font=f_qr_label)
        lw = bbox[2] - bbox[0]
        paste_alpha_text(img, (qx + 95 - lw / 2, qy + 200), "SCAN FOR THE BOOK", f_qr_label, (29, 59, 87), 255 * p_qr)

        # llamada a la accion grande + flechas doradas animadas apuntando al QR (refuerzo de conversion)
        if t >= T_QR_ANIM_START:
            cta_txt = "SCAN TO GET YOUR COPY TONIGHT"
            cbbox = d.textbbox((0, 0), cta_txt, font=f_cta_big)
            cw = cbbox[2] - cbbox[0]
            cta_x = qx + 95 - cw / 2 - 40
            cta_y = qy - 74
            if cta_x + cw > W - 60:
                cta_x = W - 60 - cw
            paste_alpha_text(img, (cta_x, cta_y), cta_txt, f_cta_big, WHITE, 255)

            tl = t - T_QR_ANIM_START
            for a_i in range(3):
                ph = ((tl * 1.3 + a_i * 0.33) % 1.0)
                ax = qx + 95 + int(math.sin(ph * math.pi) * 0) - 130 + int(ph * 130)
                aalpha = int(255 * math.sin(ph * math.pi))
                ay = cta_y + 44
                d.line((ax, ay, ax + 14, ay + 8), fill=GOLD + (aalpha,), width=4)
                d.line((ax, ay + 16, ax + 14, ay + 8), fill=GOLD + (aalpha,), width=4)

    # --- barra dorada inferior (marco de anuncio, look premium) ---
    d.rectangle((0, H - 6, W, H), fill=GOLD + (200,))

    # --- push-in de camara sutil y continuo (da sensacion de anuncio filmado, no imagen fija) ---
    zoom = 1.0 + 0.035 * clamp01(t / DURATION_S)
    zw, zh = int(W * zoom), int(H * zoom)
    zimg = img.resize((zw, zh), Image.LANCZOS)
    left = (zw - W) // 2
    top = (zh - H) // 2
    img = zimg.crop((left, top, left + W, top + H))

    return img.convert("RGB")


def build(title, desc, domain, cover_path, out_stub, qr_path):
    cover = Image.open(cover_path).convert("RGB")
    qr = Image.open(qr_path).convert("RGB").resize((190, 190), Image.NEAREST)

    probe = Image.new("RGBA", (1, 1))
    pd = ImageDraw.Draw(probe)
    tx_probe = 150 + int(820 * cover.width / cover.height) + 130
    title_lines = wrap_lines(pd, title, f_title, W - tx_probe - 480)

    bg = build_background()

    tmp = BASE / "_frames_ad"
    tmp.mkdir(exist_ok=True)
    for i in range(N_FRAMES):
        t = i / FPS
        fr = build_frame(t, title_lines, desc, domain, cover, qr, bg)
        fr.save(tmp / f"f{i:04d}.png")
        if i == int(4.5 * FPS):
            fr.save(BASE / f"{out_stub}_anuncio.png")

    subprocess.run([
        "ffmpeg", "-y", "-framerate", str(FPS), "-i", str(tmp / "f%04d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        str(BASE / f"{out_stub}_anuncio.mp4"),
    ], check=True, capture_output=True)

    for p in tmp.glob("*.png"):
        p.unlink()
    tmp.rmdir()
    print("listo:", out_stub, "anuncio completo")


if __name__ == "__main__":
    build(
        title="THE TORNADO-READY HOME MANUAL",
        desc="What to build, buy and practice before the sky turns green.",
        domain="wadecartertornado.com",
        cover_path=str(BASE / "portada_libro1.png"),
        out_stub="cta_tornado",
        qr_path=str(BASE / "qr_raw.png"),
    )