"""Deterministic, persistent ebook QR codes for the selected avatar."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import segno
from PIL import Image

_GENERATOR = "segno"
_MAX_PNG_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class AvatarQR:
    url: str
    png: bytes
    sha256: str
    generator: str = _GENERATOR


def validate_ebook_url(value: str) -> str:
    """Keep the exact destination string; only absolute HTTP(S) URLs are accepted."""
    if not value or value != value.strip() or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Introduce una URL completa sin espacios ni saltos de línea.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("La URL debe comenzar por https:// o http:// y tener un dominio.")
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("La URL debe ser un enlace web válido (http/https).") from exc
    return value


def _validate_png(data: bytes) -> None:
    if not data or len(data) > _MAX_PNG_BYTES:
        raise ValueError("El PNG del QR tiene un tamaño no válido.")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or min(image.size) < 256:
                raise ValueError("El QR no tiene un formato o una resolución válida.")
            image.verify()
    except OSError as exc:
        raise ValueError("El QR no contiene un PNG válido.") from exc


def _no_links(*paths: Path) -> None:
    for path in paths:
        if path.is_symlink():
            raise SystemExit(f"No se permiten enlaces simbólicos en el almacén QR: {path}")


def _read_record(path: Path, *, avatar_filename: str) -> dict[str, object]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("avatar_filename") != avatar_filename:
            raise ValueError("Los metadatos no corresponden al avatar.")
        record["url"] = validate_ebook_url(record["url"])
        return record
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"Metadatos de QR inválidos para {avatar_filename}: {path}. "
            "Revisa la URL guardada antes de continuar."
        ) from exc


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".part")
    _no_links(path, temporary)
    if temporary.exists():
        raise SystemExit(
            f"Hay un archivo QR temporal pendiente: {temporary}. Revísalo antes de continuar."
        )
    try:
        with temporary.open("xb") as target:
            target.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _generate_png(url: str) -> bytes:
    """Encode URL as a standard QR, with full quiet zone and integer-size modules."""
    try:
        code = segno.make_qr(url, error="m", encoding="utf-8")
        buffer = BytesIO()
        code.save(buffer, kind="png", scale=10, border=4, dark="black", light="white")
        data = buffer.getvalue()
        _validate_png(data)
        return data
    except (OSError, ValueError) as exc:
        raise SystemExit(f"No se pudo generar localmente el QR con Segno: {exc}") from exc


def ensure_avatar_qr(
    *,
    avatar_file: Path,
    avatars_dir: Path,
    input_url: str | None = None,
    interactive: bool | None = None,
) -> AvatarQR:
    """Reuse a locally generated QR, replacing legacy GPT images using their saved URL.

    The store is independent of script outputs. A previous GPT request marker can
    be resolved locally from its saved URL without any new API request.
    """
    requested_url = validate_ebook_url(input_url) if input_url is not None else None
    store = avatars_dir / ".qr"
    slot = store / avatar_file.name
    png_path = slot / "qr.png"
    metadata_path = slot / "qr.json"
    legacy_pending = slot / "request_pending.json"
    _no_links(store, slot, png_path, metadata_path, legacy_pending)

    record = (
        _read_record(metadata_path, avatar_filename=avatar_file.name)
        if metadata_path.exists()
        else (
            _read_record(legacy_pending, avatar_filename=avatar_file.name)
            if legacy_pending.exists()
            else None
        )
    )
    saved_url = record["url"] if record is not None else None
    if saved_url is not None and requested_url is not None and saved_url != requested_url:
        raise SystemExit(
            f"Este avatar ya tiene otro enlace QR. Revisa {slot} antes de cambiarlo."
        )
    url = saved_url or requested_url
    if url is None:
        if interactive is None:
            interactive = sys.stdin.isatty()
        if not interactive:
            raise SystemExit(
                f"El avatar {avatar_file.name} no tiene QR. "
                "Ejecuta en una terminal interactiva o indica --qr-url URL."
            )
        while url is None:
            try:
                entered = input(
                    f"Enlace del ebook para el QR de {avatar_file.name} (q para cancelar): "
                ).strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise SystemExit("Generación de QR cancelada.") from exc
            if entered.lower() in {"q", "quit"}:
                raise SystemExit("Generación de QR cancelada.")
            try:
                url = validate_ebook_url(entered)
            except ValueError as exc:
                print(exc, flush=True)

    # A well-formed PNG from a text-to-image model is NOT proof of a valid QR.
    # Only reuse assets explicitly marked as produced by the deterministic encoder.
    if record is not None and record.get("generator") == _GENERATOR and png_path.is_file():
        try:
            png = png_path.read_bytes()
            _validate_png(png)
            digest = hashlib.sha256(png).hexdigest()
            if record["sha256"] == digest:
                return AvatarQR(url=url, png=png, sha256=digest)
        except (OSError, KeyError, ValueError):
            pass  # Repair the local asset deterministically, without prompting or API calls.

    png = _generate_png(url)
    digest = hashlib.sha256(png).hexdigest()
    slot.mkdir(parents=True, exist_ok=True)
    _no_links(store, slot, png_path, metadata_path, legacy_pending)
    metadata = {
        "avatar_filename": avatar_file.name,
        "url": url,
        "file": "qr.png",
        "sha256": digest,
        "generator": _GENERATOR,
        "verification": "deterministic_qr_encoding",
    }
    # Image first: an interruption leaves either a legacy record to regenerate
    # or a Segno record with a mismatched hash to repair on the next run.
    _atomic_write(png_path, png)
    _atomic_write(
        metadata_path,
        (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    legacy_pending.unlink(missing_ok=True)
    return AvatarQR(url=url, png=png, sha256=digest)
