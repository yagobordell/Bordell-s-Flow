"""Persistent, per-avatar GPT-generated QR artwork for the B pipeline."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

QR_SYSTEM_PROMPT = "crea un QR para el link que se te adjunte."
_MAX_PNG_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class AvatarQR:
    url: str
    png: bytes
    sha256: str
    model: str
    image_model: str


def validate_ebook_url(value: str) -> str:
    """Preserve the URL exactly as supplied, rejecting invalid or unsafe schemes."""
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
        raise ValueError("El QR generado tiene un tamaño de archivo no válido.")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or min(image.size) < 256:
                raise ValueError("La API no devolvió una imagen PNG de QR utilizable.")
            image.verify()
    except OSError as exc:
        raise ValueError("La API no devolvió un PNG válido.") from exc


def _no_links(*paths: Path) -> None:
    for path in paths:
        if path.is_symlink():
            raise SystemExit(f"No se permiten enlaces simbólicos en el almacén QR: {path}")


def _load_saved_qr(
    *, avatar_filename: str, png_path: Path, metadata_path: Path
) -> AvatarQR:
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("avatar_filename") != avatar_filename:
            raise ValueError("Los metadatos no corresponden al avatar seleccionado.")
        url = validate_ebook_url(payload["url"])
        data = png_path.read_bytes()
        _validate_png(data)
        digest = hashlib.sha256(data).hexdigest()
        if payload["sha256"] != digest:
            raise ValueError("La huella del QR guardado no coincide con su imagen.")
        model = payload["model"]
        image_model = payload["image_model"]
        if not isinstance(model, str) or not isinstance(image_model, str):
            raise ValueError("Faltan los modelos de generación del QR.")
        return AvatarQR(url=url, png=data, sha256=digest, model=model, image_model=image_model)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"QR guardado inválido para {avatar_filename}; revisa {metadata_path} "
            "antes de intentar otra generación de pago."
        ) from exc


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".part")
    _no_links(path, temporary)
    if temporary.exists() or path.exists():
        raise FileExistsError(f"Ya existe un artefacto QR que requiere revisión: {path}")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def ensure_avatar_qr(
    *,
    avatar_file: Path,
    avatars_dir: Path,
    api_key: str,
    model: str,
    image_model: str,
    input_url: str | None = None,
    interactive: bool | None = None,
    client: Any | None = None,
) -> AvatarQR:
    """Reuse a QR by avatar filename; request URL only on its first generation.

    Once a paid request begins, a pending marker prevents accidental paid retries
    after a timeout or interruption. It must be reconciled manually.
    """
    requested_url = validate_ebook_url(input_url) if input_url is not None else None
    store = avatars_dir / ".qr"
    slot = store / avatar_file.name
    png_path = slot / "qr.png"
    metadata_path = slot / "qr.json"
    pending_path = slot / "request_pending.json"
    _no_links(store, slot, png_path, metadata_path, pending_path)
    if metadata_path.exists() or png_path.exists():
        if not metadata_path.is_file() or not png_path.is_file():
            raise SystemExit(
                f"QR incompleto para {avatar_file.name}: revisa {slot} antes de continuar."
            )
        cached = _load_saved_qr(
            avatar_filename=avatar_file.name,
            png_path=png_path,
            metadata_path=metadata_path,
        )
        if requested_url is not None and requested_url != cached.url:
            raise SystemExit(
                f"Este avatar ya tiene otro enlace QR. Revisa {slot} antes de cambiarlo."
            )
        return cached
    if pending_path.exists():
        raise SystemExit(
            f"Hay una solicitud QR pendiente para {avatar_file.name} en {pending_path}. "
            "Comprueba su resultado antes de eliminar el marcador; no se repetirá el cobro."
        )

    if requested_url is None:
        if interactive is None:
            interactive = sys.stdin.isatty()
        if not interactive:
            raise SystemExit(
                f"El avatar {avatar_file.name} no tiene QR. "
                "Ejecuta en una terminal interactiva o indica --qr-url URL."
            )
        while requested_url is None:
            try:
                entered = input(
                    f"Enlace del ebook para el QR de {avatar_file.name} (q para cancelar): "
                ).strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise SystemExit("Generación de QR cancelada.") from exc
            if entered.lower() in {"q", "quit"}:
                raise SystemExit("Generación de QR cancelada.")
            try:
                requested_url = validate_ebook_url(entered)
            except ValueError as exc:
                print(exc, flush=True)

    if not api_key:
        raise SystemExit("OPENAI_API_KEY es necesaria para generar el QR.")
    slot.mkdir(parents=True, exist_ok=True)
    _no_links(store, slot, png_path, metadata_path, pending_path)
    pending = {
        "avatar_filename": avatar_file.name,
        "url": requested_url,
        "model": model,
        "image_model": image_model,
        "status": "submitted_or_outcome_unknown",
    }
    try:
        with pending_path.open("x", encoding="utf-8") as marker:
            json.dump(pending, marker, ensure_ascii=False, indent=2)
    except FileExistsError as exc:
        raise SystemExit(
            f"Ya existe una solicitud QR pendiente: {pending_path}. No se repetirá."
        ) from exc

    # The only user input sent to GPT is the URL; the system instructions are fixed.
    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, max_retries=0, timeout=180.0)
    try:
        response = await client.responses.create(
            model=model,
            instructions=QR_SYSTEM_PROMPT,
            input=requested_url,
            tools=[{"type": "image_generation", "model": image_model}],
            tool_choice={"type": "image_generation"},
        )
        generated = [
            item.result
            for item in response.output
            if item.type == "image_generation_call" and isinstance(item.result, str)
        ]
        if len(generated) != 1 or len(generated[0]) > (_MAX_PNG_BYTES * 4 // 3) + 4:
            raise ValueError("GPT no devolvió exactamente una imagen QR dentro del límite.")
        try:
            data = base64.b64decode(generated[0], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("La imagen QR de GPT contiene base64 inválido.") from exc
        _validate_png(data)
        digest = hashlib.sha256(data).hexdigest()
        _atomic_write(png_path, data)
        metadata = {
            "avatar_filename": avatar_file.name,
            "url": requested_url,
            "file": "qr.png",
            "sha256": digest,
            "model": model,
            "image_model": image_model,
            "verification": "png_validated_scan_pending",
        }
        _atomic_write(
            metadata_path,
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        pending_path.unlink()
        return AvatarQR(
            url=requested_url, png=data, sha256=digest, model=model, image_model=image_model
        )
    except Exception as exc:
        raise SystemExit(
            f"No se pudo completar el QR de {avatar_file.name}: {exc}. "
            f"Revisa {pending_path} antes de repetir la solicitud de pago."
        ) from exc
