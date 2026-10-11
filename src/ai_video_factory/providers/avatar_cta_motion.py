"""Persistent casero CTA motion source, chosen once per avatar.

A manual motion is a silent, 1920x1080 H.264 MP4 at 30 fps. The selected
file is pinned by SHA-256; changing it requires explicit reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def probe_manual_motion(file: Path) -> float:
    if file.is_symlink() or not file.is_file() or file.suffix.lower() != ".mp4":
        raise SystemExit(f"Falta el motion graphic MP4 manual: {file}")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(file)],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(result.stdout)
        streams = info["streams"]
        video = [entry for entry in streams if entry.get("codec_type") == "video"]
        audio = [entry for entry in streams if entry.get("codec_type") == "audio"]
        if len(video) != 1 or audio:
            raise ValueError("un único canal de vídeo y ningún canal de audio")
        track = video[0]
        rate = Fraction(track.get("avg_frame_rate", "0/1"))
        if (
            track.get("codec_name") != "h264"
            or track.get("width") != 1920
            or track.get("height") != 1080
            or track.get("pix_fmt") != "yuv420p"
            or rate != 30
        ):
            raise ValueError("H.264, yuv420p, 1920x1080, 30 FPS")
        duration = float(info["format"]["duration"])
        if not 0 < duration < 3600:
            raise ValueError("duración MP4 positiva y válida")
        return duration
    except (OSError, subprocess.CalledProcessError, KeyError, TypeError,
            ValueError, ZeroDivisionError) as exc:
        raise SystemExit(
            f"Motion graphic manual inválido: {file}. "
            "Formato obligatorio: MP4 H.264, 1920x1080, 30 FPS, "
            f"yuv420p, sin audio. Detalle: {exc}"
        ) from exc


def motion_source(directory: Path) -> dict[str, object]:
    path = directory / "motion_source.json"
    if path.is_symlink() or path.is_junction():
        raise SystemExit(f"Configuración de motion graphic insegura: {path}")
    if not path.is_file():
        return {"mode": "automatic"}  # old CTA caches
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Configuración de motion graphic dañada: {path}") from exc
    if not isinstance(record, dict) or record.get("mode") not in {"automatic", "manual"}:
        raise SystemExit(f"Configuración de motion graphic inválida: {path}")
    if record["mode"] == "manual":
        if record.get("file") != "motion_manual.mp4":
            raise SystemExit("Motion manual debe llamarse motion_manual.mp4")
        file = directory / "motion_manual.mp4"
        probe_manual_motion(file)
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if record.get("sha256") != digest:
            raise SystemExit("Motion manual cambiado: revisar SHA-256 antes de continuar")
    return record


def ensure_avatar_cta_motion(
    avatar: Path, requested: str | None = None, *, interactive: bool | None = None,
) -> dict[str, object]:
    directory = avatar.parent / "meta" / "cta"
    if directory.is_symlink() or directory.is_junction():
        raise SystemExit(f"Carpeta CTA insegura: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    saved = directory / "motion_source.json"
    if saved.exists():
        record = motion_source(directory)
        if requested is not None and requested != record["mode"]:
            raise SystemExit("El motion graphic del avatar ya está configurado.")
        return record
    if requested is not None and requested not in {"automatic", "manual"}:
        raise SystemExit("--cta-motion admite automatic o manual")
    # Existing paid CTAs must retain their original automatic animation.
    existing = (directory / "identity.json").is_file() or (directory / "completed.json").is_file()
    if existing and requested == "manual":
        raise SystemExit("CTA ya generado: no se puede cambiar su motion sin reconciliación.")
    if interactive is None:
        interactive = sys.stdin.isatty()
    if existing:
        mode = "automatic"
    elif requested is not None:
        mode = requested
    elif not interactive:
        mode = "automatic"
    else:
        print("Motion graphic del CTA casero:\n"
              "  1. Generar automáticamente\n"
              "  2. Añadir un vídeo manual")
        while True:
            try:
                selection = input("Elige 1 o 2: ").strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise SystemExit("Selección de motion cancelada.") from exc
            if selection in {"1", "2"}:
                mode = "automatic" if selection == "1" else "manual"
                break
            print("Introduce 1 o 2.")
    record: dict[str, object] = {"schema_version": 1, "mode": mode}
    if mode == "manual":
        file = directory / "motion_manual.mp4"
        print(f"Coloca el motion graphic manual en: {file}\n"
              "Formato: MP4 H.264, 1920x1080, 30 FPS, yuv420p, sin audio.")
        while True:
            try:
                probe_manual_motion(file)
                break
            except SystemExit as exc:
                if not interactive:
                    raise
                print(str(exc))
                try:
                    answer = input(
                        "Copia/corrige el MP4 y pulsa Enter para validar (q cancela): "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt) as stop:
                    raise SystemExit("Configuración manual cancelada.") from stop
                if answer in {"q", "quit"}:
                    raise SystemExit("Configuración manual cancelada.") from exc
        record.update(file=file.name, sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    temp = saved.with_name("motion_source.json.part")
    if temp.exists() or temp.is_symlink():
        raise SystemExit("Configuración de motion incompleta; reconciliar .part")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if saved.exists():
            raise SystemExit("Configuración de motion creada concurrentemente")
        temp.replace(saved)
    finally:
        temp.unlink(missing_ok=True)
    return record
