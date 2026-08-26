"""Bounded LLM transformations used by the production pipeline.

Production bots use narrow system prompts and Structured Outputs. Autonomous agents are
reserved for later verification and selective-regeneration stages.
"""

from .beat_timing import BeatTimingBot
from .beats import BeatExtractorBot
from .continuity import ContinuityBot
from .narrative_blocks import NarrativeBlockBot
from .scenes import ScenePlannerBot
from .shots import ShotPlannerBot
from .storyboard_frames import StoryboardFrameBot
from .visual_references import VisualReferenceBot

__all__ = [
    "BeatExtractorBot",
    "BeatTimingBot",
    "ContinuityBot",
    "NarrativeBlockBot",
    "ScenePlannerBot",
    "ShotPlannerBot",
    "StoryboardFrameBot",
    "VisualReferenceBot",
]
