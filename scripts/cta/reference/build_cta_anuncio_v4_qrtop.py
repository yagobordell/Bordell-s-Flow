# -*- coding: utf-8 -*-
"""Prueba rapida (v4): como v3, pero con el QR arriba y el titulo/beneficios abajo (intercambio
de vuelta respecto a v3, manteniendo las mejoras: QR grande, caja azul solo alrededor del QR,
texto "SCAN WITH YOUR MOBILE PHONE" grande y fuera del rectangulo, flecha que vibra).
"""
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from build_cta_anuncio import (
    BASE, W, H, FPS, DURATION_S, N_FRAMES,
    NAVY_DARK, STEEL, GOLD, WHITE, MUTED,
    FONTS, f_eyebrow, f_title, f_qr_label, f_brand,
    clamp01, progress, progress_back,
    build_background, wrap_lines, paste_alpha_text, corner_brackets, tracked_text,
)

f_eyebrow2 = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 36)
f_qr_cta_big = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 34)
f_title2 = ImageFont.truetype(str(FONTS / "georgiab.ttf"), 98)
f_bullet = ImageFont.truetype(str(FONTS / "segoeui.ttf"), 40)
f_band_small = ImageFont.truetype(str(FONTS / "segoeui.ttf"), 26)

QR_BOX_H = 240
QR_TOP_MARGIN = 60
QR_SIZE = 190

# ---- timeline (segundos): orden = 1) libro, 2) titulo/texto, 3) QR al final ----
T_BOOK_START, T_BOOK_DUR = 0.25, 1.0
T_EYEBROW_START, T_EYEBROW_DUR = 1.4, 0.45
TITLE_LINE_START, TITLE_LINE_STAGGER, TITLE_LINE_DUR = 1.75, 0.18, 0.55
BULLET_START, BULLET_STAGGER, BULLET_DUR = 2.9, 0.28, 0.5
T_QR_START, T_QR_DUR = 4.1, 0.6           # el QR entra el ultimo, despues del titulo/beneficios
T_QR_LIVE_START = T_QR_START + T_QR_DUR


def build_frame(t, title_lines, bullets, domain, cover, qr, bg):
    img = bg.copy()
    d = ImageDraw.Draw(img, "RGBA")

    COV_H = H - 140
    COV_W = int(COV_H * cover.width / cover.height)
    FINAL_CX = 70
    FINAL_CY = 70
    TX = FINAL_CX + COV_W + 130

    # --- portada del libro ---
    p_book = progress(t, T_BOOK_START, T_BOOK_DUR)
    if p_book > 0:
        cov_h, cov_w = COV_H, COV_W
        cov = cover.resize((cov_w, cov_h), Image.LANCZOS)

        shadow = Image.new("RGBA", (cov_w + 60, cov_h + 60), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle((30, 40, 30 + cov_w, 40 + cov_h), 8, fill=(0, 0, 0, int(140 * p_book)))
        shadow = shadow.filter(ImageFilter.GaussianBlur(28))

        final_cx, final_cy = FINAL_CX, FINAL_CY
        start_cx = final_cx - 420
        cx = int(start_cx + (final_cx - start_cx) * p_book)
        cy = final_cy

        cov_rgba = cov.convert("RGBA")
        cov_rgba.putalpha(int(255 * p_book))

        img.alpha_composite(shadow, (cx - 30, cy - 20))
        img.alpha_composite(cov_rgba, (cx, cy))
        d.rectangle((cx, cy, cx + cov_w, cy + cov_h), outline=(255, 255, 255, int(60 * p_book)), width=2)
        d.line((cx + cov_w, cy + 6, cx + cov_w, cy + cov_h - 6), fill=GOLD + (int(160 * p_book),), width=3)

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
            img.alpha_composite(ribbon, (cx + cov_w - ribbon.width + 66, cy - 18))

    tx = TX

    # --- ARRIBA: caja SOLO para el QR (grande) + texto "SCAN WITH YOUR MOBILE PHONE" fuera de
    # la caja, mas grande, a su izquierda ---
    BOX_W = QR_SIZE + 100
    box_x = W - BOX_W - 40
    p_qr = progress(t, T_QR_START, T_QR_DUR)
    if p_qr > 0:
        box_y = int(QR_TOP_MARGIN - 40 * (1 - p_qr))
        box = Image.new("RGBA", (BOX_W, QR_BOX_H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(box, "RGBA")
        box_bg = Image.new("RGBA", (BOX_W, QR_BOX_H), NAVY_DARK + (235,))
        box.alpha_composite(box_bg)
        bd.rectangle((0, QR_BOX_H - 4, BOX_W, QR_BOX_H), fill=GOLD + (255,))

        live = t >= T_QR_LIVE_START
        breathe = (math.sin((t - T_QR_LIVE_START) * 2 * math.pi / 2.4) + 1) / 2 if live else 0.5

        qx = (BOX_W - QR_SIZE) // 2
        qy = (QR_BOX_H - QR_SIZE) // 2
        if live:
            halo_r = 120 + int(16 * breathe)
            halo = Image.new("RGBA", (halo_r * 2, halo_r * 2), (0, 0, 0, 0))
            hqd = ImageDraw.Draw(halo)
            for rr in range(halo_r, 0, -3):
                a = int(55 * (1 - rr / halo_r) ** 2 * (0.6 + 0.4 * breathe))
                hqd.ellipse((halo_r - rr, halo_r - rr, halo_r + rr, halo_r + rr), fill=GOLD + (a,))
            halo = halo.filter(ImageFilter.GaussianBlur(16))
            box.alpha_composite(halo, (qx + QR_SIZE // 2 - halo_r, qy + QR_SIZE // 2 - halo_r))

        bd.rounded_rectangle((qx - 14, qy - 14, qx + QR_SIZE + 14, qy + QR_SIZE + 14), 14, fill=WHITE)
        qr_r = qr.resize((QR_SIZE, QR_SIZE), Image.NEAREST).convert("RGBA")
        box.alpha_composite(qr_r, (qx, qy))

        if live:
            pulse = (math.sin((t - T_QR_LIVE_START) * 2 * math.pi / 1.6) + 1) / 2
            calpha = int(140 + 115 * pulse)
            corner_brackets(bd, (qx - 8, qy - 8, qx + QR_SIZE + 8, qy + QR_SIZE + 8), 24, 4, GOLD, calpha)
            cyc = ((t - T_QR_LIVE_START) % 2.0) / 2.0
            tt = (1 - math.cos(cyc * 2 * math.pi)) / 2
            line_y = qy + tt * QR_SIZE
            glow = Image.new("RGBA", (QR_SIZE, 10), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            for k in range(5):
                a = int(150 * (1 - abs(k - 2) / 2))
                gd.line([(0, 5 + k - 2), (QR_SIZE, 5 + k - 2)], fill=GOLD + (a,), width=1)
            box.alpha_composite(glow, (qx, int(line_y - 5)))
        else:
            corner_brackets(bd, (qx - 8, qy - 8, qx + QR_SIZE + 8, qy + QR_SIZE + 8), 24, 4, GOLD, int(160 * p_qr))

        img.alpha_composite(box, (box_x, box_y))

        # texto FUERA de la caja, mas pequeno y mas cerca del QR (a su izquierda, pero pegado)
        text_y = box_y + QR_BOX_H // 2 - 60
        off = int((1 - p_qr) * 20)
        right_edge = box_x - 30
        for li, line in enumerate(("SCAN WITH YOUR", "MOBILE PHONE")):
            lbbox = d.textbbox((0, 0), line, font=f_qr_cta_big)
            llw = lbbox[2] - lbbox[0]
            paste_alpha_text(img, (right_edge - llw + off, text_y + li * 44), line, f_qr_cta_big, WHITE, 255 * p_qr)
        dbbox = d.textbbox((0, 0), domain, font=f_band_small)
        dlw = dbbox[2] - dbbox[0]
        paste_alpha_text(img, (right_edge - dlw + off, text_y + 92), domain, f_band_small, (190, 200, 210), 255 * p_qr)

    # --- "THE BOOK" + titulo + 3 beneficios: lo importante, centrado verticalmente en el
    # frame (ya no depende de lo grande que sea la caja del QR, que ahora es secundaria) ---
    TITLE_BLOCK_H = 80 + len(title_lines) * 96 + 70 + len(bullets) * 58
    ty = (H - TITLE_BLOCK_H) // 2
    p_eye = progress(t, T_EYEBROW_START, T_EYEBROW_DUR)
    if p_eye > 0:
        off = int((1 - p_eye) * 24)
        paste_alpha_text(img, (tx, ty + off), "THE BOOK", f_eyebrow2, GOLD, 255 * p_eye)
        d.line((tx, ty + 52, tx + int(52 * p_eye), ty + 52), fill=GOLD + (int(255 * p_eye),), width=3)
    ty += 80

    for i, line in enumerate(title_lines):
        p_line = progress_back(t, TITLE_LINE_START + i * TITLE_LINE_STAGGER, TITLE_LINE_DUR)
        p_line_alpha = progress(t, TITLE_LINE_START + i * TITLE_LINE_STAGGER, TITLE_LINE_DUR)
        if p_line_alpha > 0:
            off = int((1 - p_line) * 34)
            paste_alpha_text(img, (tx + off + 4, ty + 5), line, f_title2, (0, 0, 0), 160 * p_line_alpha)
            paste_alpha_text(img, (tx + off, ty), line, f_title2, WHITE, 255 * p_line_alpha)
        ty += 96
    ty += 70  # hueco de verdad entre el titulo y los 3 beneficios

    for i, bullet in enumerate(bullets):
        p_b = progress(t, BULLET_START + i * BULLET_STAGGER, BULLET_DUR)
        if p_b > 0:
            off = int((1 - p_b) * 26)
            bx, by = tx + off, ty + 6
            d.ellipse((bx, by, bx + 34, by + 34), outline=GOLD + (int(255 * p_b),), width=3)
            d.line((bx + 9, by + 18, bx + 15, by + 24), fill=GOLD + (int(255 * p_b),), width=4)
            d.line((bx + 15, by + 24, bx + 26, by + 10), fill=GOLD + (int(255 * p_b),), width=4)
            paste_alpha_text(img, (tx + off + 52, ty), bullet, f_bullet, (232, 236, 240), 255 * p_b)
        ty += 58

    return img.convert("RGB")


def build(title, bullets, domain, cover_path, out_stub, qr_path):
    cover = Image.open(cover_path).convert("RGB")
    qr = Image.open(qr_path).convert("RGB")

    probe = Image.new("RGBA", (1, 1))
    pd = ImageDraw.Draw(probe)
    cov_h_probe = H - 140
    tx_probe = 70 + int(cov_h_probe * cover.width / cover.height) + 130
    title_lines = wrap_lines(pd, title, f_title2, W - tx_probe - 90)

    bg = build_background()

    import os
    tmp = BASE / f"_frames_ad_v4_{os.getpid()}"
    tmp.mkdir(exist_ok=True)
    for i in range(N_FRAMES):
        t = i / FPS
        fr = build_frame(t, title_lines, bullets, domain, cover, qr, bg)
        fr.save(tmp / f"f{i:04d}.png")
        if i == int(6.0 * FPS):
            fr.save(BASE / f"{out_stub}_anuncio_v4.png")

    subprocess.run([
        "ffmpeg", "-y", "-framerate", str(FPS), "-i", str(tmp / "f%04d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        str(BASE / f"{out_stub}_anuncio_v4.mp4"),
    ], check=True, capture_output=True)

    for p in tmp.glob("*.png"):
        p.unlink()
    tmp.rmdir()
    print("listo:", out_stub, "anuncio v4 (QR arriba)")


if __name__ == "__main__":
    build(
        title="THE TORNADO-READY HOME MANUAL",
        bullets=[
            "Know what to do in the first 90 seconds",
            "Find the safest room, even without a basement",
            "A drill your whole family can actually remember",
        ],
        domain="wadecartertornado.com",
        cover_path=str(BASE / "portada_libro1_avatar.png"),
        out_stub="cta_tornado",
        qr_path=str(BASE / "qr_raw.png"),
    )