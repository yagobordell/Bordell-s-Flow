from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.domain import Scene, Shot, StoryboardKeyframe
from ai_video_factory.workflows.storyboard_grids import build_storyboard_grids


def _write_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (40, 60), color)
    image.save(path, format="PNG")
    image.close()


def _scenes() -> list[Scene]:
    return [
        Scene(id=1, beat_ids=[1, 2]),
        Scene(id=2, beat_ids=[3]),
    ]


def _shots() -> list[Shot]:
    return [
        Shot(id=1, scene_id=1, beat_ids=[1], action="First"),
        Shot(id=2, scene_id=1, beat_ids=[2], action="Second"),
        Shot(id=3, scene_id=2, beat_ids=[3], action="Third"),
    ]


def _keyframes() -> list[StoryboardKeyframe]:
    return [
        StoryboardKeyframe(shot_id=1, uri="storyboard_keyframes/shot_001.png"),
        StoryboardKeyframe(shot_id=2, uri="storyboard_keyframes/shot_002.png"),
        StoryboardKeyframe(shot_id=3, uri="storyboard_keyframes/shot_003.png"),
    ]


def _write_keyframes(root: Path) -> None:
    _write_png(root / "storyboard_keyframes" / "shot_001.png", (255, 0, 0))
    _write_png(root / "storyboard_keyframes" / "shot_002.png", (0, 255, 0))
    _write_png(root / "storyboard_keyframes" / "shot_003.png", (0, 0, 255))


def test_storyboard_grids_group_keyframes_by_scene_and_preserve_order(tmp_path: Path) -> None:
    keyframe_root = tmp_path / "phase6"
    _write_keyframes(keyframe_root)
    output_dir = keyframe_root / "storyboard_grids"

    grids = build_storyboard_grids(
        _scenes(),
        _shots(),
        _keyframes(),
        keyframe_root=keyframe_root,
        output_dir=output_dir,
        thumbnail_width=40,
        thumbnail_height=60,
        max_columns=3,
        gap=4,
        margin=8,
        header_height=20,
        label_height=12,
    )

    assert [grid.model_dump() for grid in grids] == [
        {"scene_id": 1, "uri": "storyboard_grids/scene_001.png"},
        {"scene_id": 2, "uri": "storyboard_grids/scene_002.png"},
    ]

    with Image.open(output_dir / "scene_001.png") as grid:
        assert grid.size == (100, 108)
        assert grid.getpixel((28, 58)) == (255, 0, 0)
        assert grid.getpixel((72, 58)) == (0, 255, 0)

    with Image.open(output_dir / "scene_002.png") as grid:
        assert grid.size == (56, 108)
        assert grid.getpixel((28, 58)) == (0, 0, 255)


def test_storyboard_grid_wraps_after_max_columns(tmp_path: Path) -> None:
    keyframe_root = tmp_path / "phase6"
    shots = [
        Shot(id=index, scene_id=1, beat_ids=[index], action=f"Shot {index}")
        for index in range(1, 5)
    ]
    keyframes = [
        StoryboardKeyframe(
            shot_id=index,
            uri=f"storyboard_keyframes/shot_{index:03d}.png",
        )
        for index in range(1, 5)
    ]
    for index in range(1, 5):
        _write_png(
            keyframe_root / "storyboard_keyframes" / f"shot_{index:03d}.png",
            (index * 20, index * 20, index * 20),
        )

    build_storyboard_grids(
        [Scene(id=1, beat_ids=[1, 2, 3, 4])],
        shots,
        keyframes,
        keyframe_root=keyframe_root,
        output_dir=keyframe_root / "storyboard_grids",
        thumbnail_width=40,
        thumbnail_height=60,
        max_columns=3,
        gap=4,
        margin=8,
        header_height=20,
        label_height=12,
    )

    with Image.open(keyframe_root / "storyboard_grids" / "scene_001.png") as grid:
        assert grid.size == (144, 184)


def test_storyboard_grid_missing_keyframe_writes_nothing(tmp_path: Path) -> None:
    keyframe_root = tmp_path / "phase6"
    output_dir = keyframe_root / "storyboard_grids"

    with pytest.raises(ValueError, match="keyframe file not found"):
        build_storyboard_grids(
            [Scene(id=1, beat_ids=[1])],
            [Shot(id=1, scene_id=1, beat_ids=[1], action="Missing")],
            [
                StoryboardKeyframe(
                    shot_id=1,
                    uri="storyboard_keyframes/missing.png",
                )
            ],
            keyframe_root=keyframe_root,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_storyboard_grid_corrupt_keyframe_writes_nothing(tmp_path: Path) -> None:
    keyframe_root = tmp_path / "phase6"
    bad_path = keyframe_root / "storyboard_keyframes" / "shot_001.png"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_bytes(b"not-a-png")
    output_dir = keyframe_root / "storyboard_grids"

    with pytest.raises(ValueError, match="could not be decoded"):
        build_storyboard_grids(
            [Scene(id=1, beat_ids=[1])],
            [Shot(id=1, scene_id=1, beat_ids=[1], action="Corrupt")],
            [StoryboardKeyframe(shot_id=1, uri="storyboard_keyframes/shot_001.png")],
            keyframe_root=keyframe_root,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_storyboard_grid_rejects_noncontiguous_scene_shots(tmp_path: Path) -> None:
    scenes = [Scene(id=1, beat_ids=[1, 3]), Scene(id=2, beat_ids=[2])]
    shots = [
        Shot(id=1, scene_id=1, beat_ids=[1], action="One"),
        Shot(id=2, scene_id=2, beat_ids=[2], action="Two"),
        Shot(id=3, scene_id=1, beat_ids=[3], action="Three"),
    ]
    keyframes = [
        StoryboardKeyframe(shot_id=index, uri=f"storyboard_keyframes/{index}.png")
        for index in range(1, 4)
    ]

    with pytest.raises(ValueError, match="scene order and remain contiguous"):
        build_storyboard_grids(
            scenes,
            shots,
            keyframes,
            keyframe_root=tmp_path / "phase6",
            output_dir=tmp_path / "phase6" / "storyboard_grids",
        )


def test_storyboard_grid_rejects_asset_uri_outside_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes its root directory"):
        build_storyboard_grids(
            [Scene(id=1, beat_ids=[1])],
            [Shot(id=1, scene_id=1, beat_ids=[1], action="Unsafe")],
            [StoryboardKeyframe(shot_id=1, uri="../outside.png")],
            keyframe_root=tmp_path / "phase6",
            output_dir=tmp_path / "phase6" / "storyboard_grids",
        )
