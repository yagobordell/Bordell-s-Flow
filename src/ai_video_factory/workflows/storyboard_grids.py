from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from ai_video_factory.domain import Scene, Shot, StoryboardGrid, StoryboardKeyframe

_BACKGROUND = (18, 18, 18)
_PANEL_BACKGROUND = (30, 30, 30)
_LABEL_BACKGROUND = (8, 8, 8)
_TEXT = (240, 240, 240)


def build_storyboard_grids(
    scenes: list[Scene],
    shots: list[Shot],
    keyframes: list[StoryboardKeyframe],
    *,
    keyframe_root: Path,
    output_dir: Path,
    thumbnail_width: int = 320,
    thumbnail_height: int = 480,
    max_columns: int = 3,
    gap: int = 16,
    margin: int = 16,
    header_height: int = 32,
    label_height: int = 24,
) -> list[StoryboardGrid]:
    """Compose deterministic scene-level contact sheets from canonical keyframes."""

    _validate_layout(
        thumbnail_width=thumbnail_width,
        thumbnail_height=thumbnail_height,
        max_columns=max_columns,
        gap=gap,
        margin=margin,
        header_height=header_height,
        label_height=label_height,
    )
    _validate_inputs(scenes, shots, keyframes)
    if not scenes:
        return []

    keyframes_by_shot = {keyframe.shot_id: keyframe for keyframe in keyframes}
    shots_by_scene: dict[int, list[Shot]] = {scene.id: [] for scene in scenes}
    for shot in shots:
        shots_by_scene[shot.scene_id].append(shot)

    loaded_by_shot = {
        shot.id: _load_keyframe(
            keyframes_by_shot[shot.id],
            keyframe_root=keyframe_root,
        )
        for shot in shots
    }

    composed: list[tuple[int, Image.Image]] = []
    try:
        for scene in scenes:
            scene_shots = shots_by_scene[scene.id]
            grid = _compose_scene_grid(
                scene.id,
                scene_shots,
                loaded_by_shot,
                thumbnail_width=thumbnail_width,
                thumbnail_height=thumbnail_height,
                max_columns=max_columns,
                gap=gap,
                margin=margin,
                header_height=header_height,
                label_height=label_height,
            )
            composed.append((scene.id, grid))
    finally:
        for image in loaded_by_shot.values():
            image.close()

    output_dir.mkdir(parents=True, exist_ok=True)
    grids: list[StoryboardGrid] = []
    try:
        for scene_id, grid in composed:
            path = output_dir / f"scene_{scene_id:03d}.png"
            grid.save(path, format="PNG")
            grids.append(
                StoryboardGrid(
                    scene_id=scene_id,
                    uri=path.relative_to(output_dir.parent).as_posix(),
                )
            )
    finally:
        for _, grid in composed:
            grid.close()

    return grids


def _load_keyframe(
    keyframe: StoryboardKeyframe,
    *,
    keyframe_root: Path,
) -> Image.Image:
    path = _resolve_asset_path(keyframe_root, keyframe.uri)
    if not path.is_file():
        raise ValueError(f"Storyboard keyframe file not found: {path}")
    if path.suffix.lower() != ".png":
        raise ValueError(f"Storyboard grid requires PNG keyframes: {path}")

    try:
        with Image.open(path) as source:
            source.load()
            if source.format != "PNG":
                raise ValueError(f"Storyboard keyframe is not a valid PNG: {path}")
            return source.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Storyboard keyframe could not be decoded: {path}") from exc


def _compose_scene_grid(
    scene_id: int,
    shots: list[Shot],
    images_by_shot: dict[int, Image.Image],
    *,
    thumbnail_width: int,
    thumbnail_height: int,
    max_columns: int,
    gap: int,
    margin: int,
    header_height: int,
    label_height: int,
) -> Image.Image:
    columns = min(max_columns, len(shots))
    rows = (len(shots) + columns - 1) // columns
    cell_height = thumbnail_height + label_height
    width = margin * 2 + columns * thumbnail_width + (columns - 1) * gap
    height = margin * 2 + header_height + rows * cell_height + (rows - 1) * gap

    canvas = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text(
        (margin, margin + 6),
        f"SCENE {scene_id:03d} | {len(shots)} SHOTS",
        fill=_TEXT,
        font=font,
    )

    grid_top = margin + header_height
    for index, shot in enumerate(shots):
        row, column = divmod(index, columns)
        x = margin + column * (thumbnail_width + gap)
        y = grid_top + row * (cell_height + gap)

        draw.rectangle(
            (x, y, x + thumbnail_width - 1, y + thumbnail_height - 1),
            fill=_PANEL_BACKGROUND,
        )
        thumbnail = ImageOps.contain(
            images_by_shot[shot.id],
            (thumbnail_width, thumbnail_height),
            method=Image.Resampling.LANCZOS,
        )
        paste_x = x + (thumbnail_width - thumbnail.width) // 2
        paste_y = y + (thumbnail_height - thumbnail.height) // 2
        canvas.paste(thumbnail, (paste_x, paste_y))
        thumbnail.close()

        label_y = y + thumbnail_height
        draw.rectangle(
            (x, label_y, x + thumbnail_width - 1, label_y + label_height - 1),
            fill=_LABEL_BACKGROUND,
        )
        draw.text(
            (x + 8, label_y + 6),
            f"SHOT {shot.id:03d}",
            fill=_TEXT,
            font=font,
        )

    return canvas


def _resolve_asset_path(root: Path, uri: str) -> Path:
    resolved_root = root.resolve()
    path = (root / uri).resolve()
    if not path.is_relative_to(resolved_root):
        raise ValueError(f"Storyboard keyframe URI escapes its root directory: {uri}")
    return path


def _validate_inputs(
    scenes: list[Scene],
    shots: list[Shot],
    keyframes: list[StoryboardKeyframe],
) -> None:
    scene_ids = [scene.id for scene in scenes]
    if len(scene_ids) != len(set(scene_ids)):
        raise ValueError("Storyboard scenes must have unique IDs")

    shot_ids = [shot.id for shot in shots]
    if len(shot_ids) != len(set(shot_ids)):
        raise ValueError("Storyboard shots must have unique IDs")

    keyframe_ids = [keyframe.shot_id for keyframe in keyframes]
    if keyframe_ids != shot_ids:
        raise ValueError("Storyboard keyframe IDs must match shot IDs exactly and preserve order")

    known_scene_ids = set(scene_ids)
    if any(shot.scene_id not in known_scene_ids for shot in shots):
        raise ValueError("Storyboard shots reference unknown scene IDs")

    represented_scene_ids = {shot.scene_id for shot in shots}
    if represented_scene_ids != known_scene_ids:
        raise ValueError("Every storyboard scene must contain at least one shot")

    encountered_scene_ids: list[int] = []
    for shot in shots:
        if not encountered_scene_ids or encountered_scene_ids[-1] != shot.scene_id:
            encountered_scene_ids.append(shot.scene_id)
    if encountered_scene_ids != scene_ids:
        raise ValueError("Storyboard shots must preserve scene order and remain contiguous by scene")


def _validate_layout(
    *,
    thumbnail_width: int,
    thumbnail_height: int,
    max_columns: int,
    gap: int,
    margin: int,
    header_height: int,
    label_height: int,
) -> None:
    if thumbnail_width <= 0 or thumbnail_height <= 0:
        raise ValueError("Storyboard grid thumbnail dimensions must be positive")
    if max_columns <= 0:
        raise ValueError("Storyboard grid max columns must be positive")
    if gap < 0 or margin < 0:
        raise ValueError("Storyboard grid spacing values cannot be negative")
    if header_height <= 0 or label_height <= 0:
        raise ValueError("Storyboard grid label dimensions must be positive")
