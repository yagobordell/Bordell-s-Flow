import json

from ai_video_factory.providers.salad_ideogram import reference_caption_variants
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_KEYFRAME_TASK


def _photo_caption() -> str:
    return json.dumps(
        {
            "high_level_description": "Canonical location reference. Rocky plateau at noon.",
            "style_description": {
                "aesthetics": "documentary realism",
                "lighting": "neutral daylight",
                "photo": "realistic reference photography",
                "medium": "documentary photograph",
            },
            "compositional_deconstruction": {
                "background": "Rocky plateau under a clear sky.",
                "elements": [],
            },
        },
        separators=(",", ":"),
    )


def test_safety_variants_preserve_official_style_key_order() -> None:
    variants = dict(
        reference_caption_variants(
            _photo_caption(),
            task_name=IDEOGRAM4_KEYFRAME_TASK,
        )
    )
    simplified = json.loads(variants["safe_simplified"])["style_description"]
    minimal = json.loads(variants["safe_minimal_art"])["style_description"]

    assert list(simplified) == ["aesthetics", "lighting", "photo", "medium"]
    assert list(minimal) == ["aesthetics", "lighting", "medium", "art_style"]
