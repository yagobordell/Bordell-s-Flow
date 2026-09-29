"""Offline grid cropping contracts, including thick shifted white dividers."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from ai_video_factory.providers.grid_crops import (
    GridCropError,
    crop_grid_assets,
    panel_boxes,
)


def _panel_color(row: int, column: int) -> tuple[int, int, int]:
    return ((column + 1) * 48, (row + 1) * 57, 90)


def _sample_grid(
    columns: int, rows: int, *, divider: int = 22,
    offset_x: int = 14, offset_y: int = -9, outer: int = 8,
) -> Image.Image:
    """Leave real full-width white dividers like the supplied 3x2 example."""
    width, height = columns * 1280, rows * 720
    image = Image.new("RGB", (width, height), "white")
    x_bands = [
        (round(width * index / columns) + offset_x - divider // 2,
         round(width * index / columns) + offset_x + divider // 2)
        for index in range(1, columns)
    ]
    y_bands = [
        (round(height * index / rows) + offset_y - divider // 2,
         round(height * index / rows) + offset_y + divider // 2)
        for index in range(1, rows)
    ]
    x_spans = list(zip(
        [outer, *(band[1] for band in x_bands)],
        [*(band[0] for band in x_bands), width - outer],
        strict=True,
    ))
    y_spans = list(zip(
        [outer, *(band[1] for band in y_bands)],
        [*(band[0] for band in y_bands), height - outer],
        strict=True,
    ))
    drawing = ImageDraw.Draw(image)
    for row, (top, bottom) in enumerate(y_spans):
        for column, (left, right) in enumerate(x_spans):
            drawing.rectangle(
                (left, top, right - 1, bottom - 1),
                fill=_panel_color(row, column),
            )
    return image


def _source(
    output: Path, columns: int, rows: int, *, image: Image.Image | None = None,
) -> tuple[dict, bytes]:
    full = image if image is not None else _sample_grid(columns, rows)
    buffer = io.BytesIO()
    full.save(buffer, "PNG")
    data = buffer.getvalue()
    path = output / "images" / "grids" / "block_1_1B.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    panels = [
        {
            "block_id": 1,
            "beat_id": f"1{chr(ord('B') + index)}",
            "visual_type": "media_video" if index % 2 else "media_image",
            "description": f"{index + 1}. Test panel",
            "row": index // columns,
            "column": index % columns,
        }
        for index in range(columns * rows)
    ]
    item = {
        "block_id": 1,
        "beat_id": "1B",
        "provider": "openai_direct",
        "file": path.relative_to(output).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "grid": {
            "columns": columns,
            "rows": rows,
            "width": full.width,
            "height": full.height,
        },
        "panels": panels,
    }
    return {"status": "completed", "grid_count": 1, "items": [item]}, data


@pytest.mark.parametrize("columns,rows", [(2, 2), (3, 2), (3, 3)])
def test_thick_shifted_white_dividers_produce_clean_720p_panels(
    tmp_path: Path, columns: int, rows: int,
) -> None:
    manifest, original = _source(tmp_path, columns, rows)
    saved = manifest["items"][0]["file"]
    cropped = crop_grid_assets(tmp_path, manifest)
    assert len(cropped) == columns * rows
    assert manifest["cropping_status"] == "completed"
    assert (tmp_path / saved).read_bytes() == original
    assert len({
        (item["block_id"], item["beat_id"]) for item in cropped
    }) == columns * rows
    for index, asset in enumerate(cropped):
        row, col = divmod(index, columns)
        assert (asset["row"], asset["column"]) == (row, col)
        assert asset["crop_box"][0] < asset["crop_box"][2]
        assert asset["crop_box"][1] < asset["crop_box"][3]
        assert asset["grid_sha256"] == manifest["items"][0]["sha256"]
        with Image.open(tmp_path / asset["file"]) as panel:
            assert panel.size == (1280, 720)
            assert panel.getpixel((3, 3)) == _panel_color(row, col)
            assert panel.getpixel((1276, 716)) == _panel_color(row, col)
        assert hashlib.sha256(
            (tmp_path / asset["file"]).read_bytes()
        ).hexdigest() == asset["sha256"]
    persisted = json.loads(
        (tmp_path / "images" / "manifest.json").read_text(encoding="utf-8")
    )
    assert persisted["panel_assets"] == cropped
    assert crop_grid_assets(tmp_path, manifest) == cropped


def test_dividers_are_found_at_their_actual_positions_not_exact_thirds() -> None:
    image = _sample_grid(3, 2, divider=30, offset_x=22, offset_y=-15)
    boxes = panel_boxes(image, columns=3, rows=2)
    assert len(boxes) == 6
    assert 1280 < boxes[0][2] < 1320
    assert boxes[1][0] > 1280
    for index, box in enumerate(boxes):
        row, col = divmod(index, 3)
        sample_x = (box[0] + box[2]) // 2
        sample_y = (box[1] + box[3]) // 2
        assert image.getpixel((sample_x, sample_y)) == _panel_color(row, col)


def test_missing_separator_blocks_cropping_without_destroying_paid_grid(
    tmp_path: Path,
) -> None:
    image = Image.new("RGB", (2560, 1440), (20, 30, 50))
    manifest, original = _source(tmp_path, 2, 2, image=image)
    with pytest.raises(GridCropError, match="divider"):
        crop_grid_assets(tmp_path, manifest)
    assert not (tmp_path / "images/manifest.json").exists()
    assert not list((tmp_path / "images").glob("block_*/*.png"))
    assert (tmp_path / manifest["items"][0]["file"]).read_bytes() == original


def test_missing_white_separators_keep_usable_colored_panels(
    tmp_path: Path,
) -> None:
    # Genuine panel content can be cropped on nominal seams without white lines.
    image = Image.new("RGB", (2560, 1440))
    draw = ImageDraw.Draw(image)
    for row in range(2):
        for column in range(2):
            draw.rectangle(
                (column * 1280, row * 720,
                 (column + 1) * 1280 - 1, (row + 1) * 720 - 1),
                fill=_panel_color(row, column),
            )
    manifest, original = _source(tmp_path, 2, 2, image=image)
    crops = crop_grid_assets(tmp_path, manifest)

    assert len(crops) == 4
    assert (tmp_path / manifest["items"][0]["file"]).read_bytes() == original
    for index, panel in enumerate(crops):
        row, column = divmod(index, 2)
        with Image.open(tmp_path / panel["file"]) as generated:
            assert generated.getpixel((640, 360)) == _panel_color(row, column)


def test_modified_paid_grid_checksum_blocks_all_crops(tmp_path: Path) -> None:
    manifest, _original = _source(tmp_path, 2, 2)
    (tmp_path / manifest["items"][0]["file"]).write_bytes(b"modified")
    with pytest.raises(GridCropError, match="checksum"):
        crop_grid_assets(tmp_path, manifest)
    assert not list((tmp_path / "images").glob("block_*/*.png"))


def test_modified_derived_panel_requires_manual_reconciliation(tmp_path: Path) -> None:
    manifest, original = _source(tmp_path, 2, 2)
    cropped = crop_grid_assets(tmp_path, manifest)
    first = tmp_path / cropped[0]["file"]
    first.write_bytes(b"changed-by-user")
    with pytest.raises(GridCropError, match="manual reconciliation"):
        crop_grid_assets(tmp_path, manifest)
    assert first.read_bytes() == b"changed-by-user"
    assert (tmp_path / manifest["items"][0]["file"]).read_bytes() == original


def test_crop_rejects_linked_grid_source(tmp_path: Path) -> None:
    manifest, _original = _source(tmp_path, 2, 2)
    source = tmp_path / manifest["items"][0]["file"]
    actual = tmp_path / "backing.png"
    source.replace(actual)
    try:
        source.symlink_to(actual)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks the privilege to create file symlinks")
        raise
    with pytest.raises(GridCropError, match="linked"):
        crop_grid_assets(tmp_path, manifest)
    assert not list((tmp_path / "images").glob("block_*/*.png"))
