"""Recover individual beat PNGs from white-divided B2 image grids.

GPT may shift or widen its white separators. Identify the real separators near
their expected positions before cropping, rather than dividing the canvas at
exact mathematical thirds/halves. Never initiate any image API request here.
"""

from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from .image_grids import BLACK_PANEL_SOURCE
from .professional_image_state import finished_in_place_sha

_SHA256 = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)
_BEAT_ID = re.compile(r"[1-9][0-9]*[A-Z]+", flags=re.ASCII)
_ASSET_ID = re.compile(r"[1-9][0-9]*[A-Z]+(?:__[1-9][0-9]*)?", flags=re.ASCII)
_DEFAULT_IMAGE_SIZE = (1280, 720)
_CROP_VERSION = "b2-white-divider-crop-v1"


class GridCropError(RuntimeError):
    """The paid grid remains intact, but its panels need manual inspection."""


def _whiteness(image: Image.Image, axis: str, position: int) -> float:
    """Fraction of near-white samples along an entire prospective divider."""
    other = image.height if axis == "x" else image.width
    step = max(1, other // 120)
    samples = range(step // 2, other, step)
    white = 0
    total = 0
    for transverse in samples:
        pixel = image.getpixel(
            (position, transverse) if axis == "x" else (transverse, position)
        )
        white += min(pixel[:3]) >= 244
        total += 1
    return white / total if total else 0.0


def _separator(
    image: Image.Image, axis: str, nominal: int, cell_length: int
) -> tuple[int, int]:
    """Return the inclusive/exclusive white band nearest a nominal seam."""
    extent = image.width if axis == "x" else image.height
    radius = min(64, max(8, round(cell_length * 0.06)))
    start = max(1, nominal - radius)
    stop = min(extent - 1, nominal + radius + 1)
    good = [
        position for position in range(start, stop)
        if _whiteness(image, axis, position) >= 0.91
    ]
    runs: list[tuple[int, int]] = []
    for position in good:
        if runs and runs[-1][1] == position:
            runs[-1] = runs[-1][0], position + 1
        else:
            runs.append((position, position + 1))
    max_width = max(6, round(cell_length * 0.065))
    # GPT can render a valid divider as a single bright pixel, especially on
    # one axis, while other grids use a much thicker white band. A one-pixel
    # band is still safe because the crop is inset on both sides below.
    plausible = [
        band for band in runs
        if 1 <= band[1] - band[0] <= max_width
    ]
    if not plausible:
        # A missing separator is recoverable only if there is actual image content.
        # Do not silently convert a completely uniform failed grid into panels.
        if all(low == high for low, high in image.getextrema()):
            raise GridCropError(
                "No reliable divider in a uniform grid; preserve the paid image "
                "for manual inspection"
            )
        # The model may omit or soften a divider in an otherwise valid grid.
        # Fall back to the mathematical seam while keeping the paid PNG intact.
        return nominal, nominal + 1
    return min(
        plausible,
        key=lambda band: (
            abs((band[0] + band[1]) / 2 - nominal),
            -(band[1] - band[0]),
        ),
    )


def _outer_white_width(image: Image.Image, axis: str, side: str) -> int:
    """Trim an optional outer white frame, but never search deep into a panel."""
    extent = image.width if axis == "x" else image.height
    limit = min(32, max(4, extent // 80))
    count = 0
    for offset in range(limit):
        position = offset if side == "start" else extent - 1 - offset
        if _whiteness(image, axis, position) < 0.91:
            break
        count += 1
    if count == limit:
        raise GridCropError("Outer white frame is too wide to crop safely")
    return count if count >= 2 else 0


def panel_boxes(
    image: Image.Image, *, columns: int, rows: int
) -> list[tuple[int, int, int, int]]:
    """Derive ordered, separator-free interior boxes; no exact grid split."""
    if (columns, rows) not in {(2, 2), (3, 2), (3, 3)}:
        raise GridCropError("Unsupported grid geometry")
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width < columns * 200 or height < rows * 110:
        raise GridCropError("Grid is too small for reliable divider detection")
    vertical = [
        _separator(rgb, "x", round(width * index / columns), width // columns)
        for index in range(1, columns)
    ]
    horizontal = [
        _separator(rgb, "y", round(height * index / rows), height // rows)
        for index in range(1, rows)
    ]
    inset = max(2, min(7, round(min(width, height) / 360)))
    left = _outer_white_width(rgb, "x", "start") + inset
    right = width - _outer_white_width(rgb, "x", "end") - inset
    top = _outer_white_width(rgb, "y", "start") + inset
    bottom = height - _outer_white_width(rgb, "y", "end") - inset
    x_spans = [
        (begin + inset, end - inset)
        for begin, end in zip(
            [left - inset, *(band[1] for band in vertical)],
            [*(band[0] for band in vertical), right + inset],
            strict=True,
        )
    ]
    y_spans = [
        (begin + inset, end - inset)
        for begin, end in zip(
            [top - inset, *(band[1] for band in horizontal)],
            [*(band[0] for band in horizontal), bottom + inset],
            strict=True,
        )
    ]
    if any(end - begin < 120 for begin, end in x_spans):
        raise GridCropError("Divider positions leave an invalid panel width")
    if any(end - begin < 80 for begin, end in y_spans):
        raise GridCropError("Divider positions leave an invalid panel height")
    return [
        (x0, y0, x1, y1)
        for y0, y1 in y_spans for x0, x1 in x_spans
    ]


def _verified_grid(output: Path, item: dict[str, Any]) -> Image.Image:
    name, digest = item.get("file"), item.get("sha256")
    if (
        not isinstance(name, str) or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise GridCropError("Grid PNG has no valid file and SHA-256")
    relative = Path(name)
    if (
        relative.is_absolute() or ".." in relative.parts
        or relative.parts[:2] != ("images", "grids")
    ):
        raise GridCropError("Grid PNG path is outside images/grids")
    path = output / relative
    if (
        (output / "images").is_symlink()
        or path.is_symlink() or path.parent.is_symlink()
        or not path.is_file()
    ):
        raise GridCropError(f"Missing or linked generated grid: {name}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise GridCropError(f"Generated grid checksum differs: {name}")
    try:
        with Image.open(BytesIO(data)) as png:
            if png.format != "PNG":
                raise GridCropError(f"Grid asset is not a PNG: {name}")
            png.load()
            rgb = png.convert("RGB")
    except (OSError, ValueError) as exc:
        raise GridCropError(f"Invalid generated grid PNG: {name}") from exc
    meta = item.get("grid")
    if (
        not isinstance(meta, dict)
        or rgb.size != (meta.get("width"), meta.get("height"))
    ):
        raise GridCropError(f"Grid dimensions differ from saved metadata: {name}")
    return rgb


def _write_panel(output: Path, metadata: dict[str, Any], data: bytes) -> None:
    relative = Path(metadata["file"])
    target = output / relative
    folder = target.parent
    temporary = target.with_name(target.name + ".part")
    if any(path.is_symlink() for path in (
        output / "images", folder, target, temporary
    )):
        raise GridCropError(f"Refusing linked panel output: {target}")
    folder.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file():
            raise GridCropError(f"Panel output path is not a file: {target}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual == metadata["sha256"]:
            return
        recipe = (
            "image_x2" if metadata["visual_type"] in {"media_image", "avatar_media"}
            else "native_grid_video"
        )
        finished = finished_in_place_sha(
            output, file=metadata["file"], source_sha256=metadata["sha256"],
            block=metadata["block_id"],
            beat=metadata.get("asset_id", metadata["beat_id"]),
            recipes=(recipe,),
        )
        if actual == finished:
            return  # The derived panel has already been replaced and verified.
        raise GridCropError(
            f"Existing panel differs from the derived grid crop: {target}; "
            "manual reconciliation is required"
        )
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink() or path.parent.is_symlink():
        raise GridCropError("Refusing linked grid crop manifest")
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def crop_grid_assets(
    output: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Extract all grid panels offline, preserving original paid grid PNGs.

    Re-runs verify and reuse byte-identical derived panels. An unrecognizable
    divider or a changed prior crop fails closed; it never resubmits a batch.
    """
    grids = [item for item in manifest["items"] if item.get("grid") is not None]
    if not grids:
        return []
    if manifest.get("status") != "completed":
        raise GridCropError("Cannot crop an incomplete image manifest")
    derived: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for grid in grids:
        geometry = grid["grid"]
        columns, rows = geometry["columns"], geometry["rows"]
        panel_width = geometry.get("panel_width", _DEFAULT_IMAGE_SIZE[0])
        panel_height = geometry.get("panel_height", _DEFAULT_IMAGE_SIZE[1])
        if (
            not isinstance(panel_width, int) or isinstance(panel_width, bool)
            or not isinstance(panel_height, int) or isinstance(panel_height, bool)
            or panel_width < 1 or panel_height < 1
        ):
            raise GridCropError("Invalid grid panel dimensions")
        target_size = (panel_width, panel_height)
        panels = grid["panels"]
        if len(panels) != columns * rows:
            raise GridCropError("Grid panel map does not match its cell count")
        image = _verified_grid(output, grid)
        boxes = panel_boxes(image, columns=columns, rows=rows)
        for index, (panel, box) in enumerate(zip(panels, boxes, strict=True)):
            if not isinstance(panel, dict):
                raise GridCropError("Invalid grid panel record")
            if (panel.get("row"), panel.get("column")) != divmod(index, columns):
                raise GridCropError("Invalid grid panel ordering")
            if panel.get("source") == BLACK_PANEL_SOURCE:
                # Black cells are only structural padding for the paid grid;
                # they do not correspond to a B2 beat and are not compositor
                # image assets.
                continue
            block_id, beat_id = panel["block_id"], panel["beat_id"]
            asset_id = panel.get("asset_id", beat_id)
            if (
                not isinstance(block_id, int) or isinstance(block_id, bool)
                or block_id < 1 or not isinstance(beat_id, str)
                or _BEAT_ID.fullmatch(beat_id) is None
                or not beat_id.startswith(str(block_id))
                or not isinstance(asset_id, str)
                or _ASSET_ID.fullmatch(asset_id) is None
                or not asset_id.startswith(beat_id)
            ):
                raise GridCropError("Invalid grid panel beat/asset identifier")
            key = block_id, asset_id
            if key in seen:
                raise GridCropError(f"Duplicate crop panel: {asset_id}")
            seen.add(key)
            # Preserve the full photographic interior even if GPT's panel
            # ratio differs slightly from the requested geometry.
            source_panel = image.crop(box)
            if panel["visual_type"] == "avatar_media" and (
                panel_width, panel_height
            ) != (768, 864):
                # Support stills have an 8:9 compositor slot even when their
                # continuity grid mixes wide video frames and static inserts.
                width, height = source_panel.size
                central_width = min(width, round(height * 8 / 9))
                central_height = min(height, round(width * 9 / 8))
                if central_width < width:
                    offset = (width - central_width) // 2
                    source_panel = source_panel.crop((offset, 0, offset + central_width, height))
                elif central_height < height:
                    offset = (height - central_height) // 2
                    source_panel = source_panel.crop((0, offset, width, offset + central_height))
                target_size = (768, 864)
            result = source_panel.resize(
                target_size, resample=Image.Resampling.LANCZOS
            )
            buffer = BytesIO()
            result.save(buffer, format="PNG")
            data = buffer.getvalue()
            metadata = {
                "block_id": block_id,
                "beat_id": beat_id,
                "asset_id": asset_id,
                **(
                    {
                        "subshot_index": panel["subshot_index"],
                        "subshot_count": panel["subshot_count"],
                    }
                    if isinstance(panel.get("subshot_index"), int)
                    and isinstance(panel.get("subshot_count"), int)
                    else {}
                ),
                "visual_type": panel["visual_type"],
                **(
                    {"reference": panel["reference"]}
                    if isinstance(panel.get("reference"), bool) else {}
                ),
                "file": f"images/block_{block_id}/{asset_id}.png",
                "sha256": hashlib.sha256(data).hexdigest(),
                "provider": "grid_crop",
                "source_provider": grid["provider"],
                "grid_file": grid["file"],
                "grid_sha256": grid["sha256"],
                "row": panel["row"],
                "column": panel["column"],
                "crop_box": list(box),
                "crop_version": _CROP_VERSION,
                "width": target_size[0],
                "height": target_size[1],
            }
            _write_panel(output, metadata, data)
            derived.append(metadata)
    manifest["panel_assets"] = derived
    manifest["cropping_status"] = "completed"
    _atomic_manifest(output / "images" / "manifest.json", manifest)
    return derived
