"""Bounded LLM transformations used by the production pipeline.

Production bots use narrow system prompts and Structured Outputs. Autonomous agents are
reserved for later verification and selective-regeneration stages.
"""

from .beats import BeatExtractorBot
from .continuity import ContinuityBot
from .narrative_blocks import NarrativeBlockBot
from .scenes import ScenePlannerBot

__all__ = ["BeatExtractorBot", "ContinuityBot", "NarrativeBlockBot", "ScenePlannerBot"]
