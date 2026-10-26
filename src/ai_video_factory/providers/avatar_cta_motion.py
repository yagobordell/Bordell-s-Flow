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


def _sha256(file: Path) -> str:
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe_manual_motion(file: Path) -> float:
    if (file.is_symlink() or file.is_junction()
            or not file.is_file() or file.suffix.lower() != ".mp4"):
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
        nominal_rate = Fraction(track.get("r_frame_rate", "30/1"))
        if (
            track.get("codec_name") != "h264"
            or track.get("width") != 1920
            or track.get("height") != 1080
            or track.get("pix_fmt") != "yuv420p"
            or rate != 30 or nominal_rate != 30
        ):
            raise ValueError("H.264, yuv420p, 1920x1080, 30 FPS")
        duration = float(info["format"]["duration"])
        # The maximum supported A2V first-half is 15 seconds. Preflight a
        # 16.5-second tail before ANY paid CTA request so that speech of any
        # supported length cannot make the manually supplied clip too short.
        if not 16.5 <= duration < 3600:
            raise ValueError(
                "mínimo 16.5 segundos (15.0 s A2V máximo + 1.5 s cola). "
                "El compositor recorta automáticamente el vídeo sobrante"
            )
        return duration
    except (OSError, subprocess.CalledProcessError, KeyError, TypeError,
            ValueError, ZeroDivisionError) as exc:
        raise SystemExit(
            f"Motion graphic manual inválido: {file}. "
            "Formato obligatorio: MP4 H.264, 1920x1080, 30 FPS, "
            f"yuv420p, sin audio. Detalle: {exc}"
        ) from exc


def _replace_json(path: Path, record: dict[str, object]) -> None:
    if path.is_symlink() or path.is_junction():
        raise SystemExit(f"Metadatos CTA inseguros: {path}")
    temp = path.with_name(path.name + ".part")
    if temp.exists() or temp.is_symlink() or temp.is_junction():
        raise SystemExit(f"Metadatos CTA pendientes de reconciliar: {temp}")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


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
        digest = _sha256(file)
        if record.get("sha256") != digest:
            if (
                (directory / "motion_manual_state.json").exists()
                or (directory / "completed.json").exists()
            ):
                raise SystemExit(
                    "Motion manual cambiado después de preparar el CTA. "
                    "Reconciliar el CTA antes de continuar."
                )
            # The clip can be corrected while onboarding is unfinished.
            # This is important when the first candidate was too short for
            # the actual, subsequently generated CTA narration.
            print(
                f"Motion manual actualizado antes de completar el CTA: {file}. "
                "Revalidando el nuevo MP4 sin regenerar voz, imagen ni A2V."
            )
            record = {**record, "sha256": digest}
            _replace_json(path, record)
        identity_path = directory / "identity.json"
        if identity_path.is_file() and not (directory / "motion_manual_state.json").exists():
            if (directory / "completed.json").exists():
                return record
            if identity_path.is_symlink() or identity_path.is_junction():
                raise SystemExit("Identidad CTA no segura")
            try:
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SystemExit("Identidad CTA ilegible") from exc
            if not isinstance(identity, dict) or "manual_motion_sha256" not in identity:
                raise SystemExit("Identidad CTA manual inválida")
            if identity["manual_motion_sha256"] != digest:
                _replace_json(
                    identity_path, {**identity, "manual_motion_sha256": digest},
                )
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
        record.update(file=file.name, sha256=_sha256(file))
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
