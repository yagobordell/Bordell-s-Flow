"""Backward-compatible LTX imports and test hooks."""

from ai_video_factory.workers.ltx25 import model as _model
from ai_video_factory.workers.ltx25.model import (
    LTX_GENERATION_PROFILE,
    LTX_VIDEO_TASK,
    LTXModelFiles,
    LTXVideoBackend,
    LTXVideoParameters,
    LTXVideoTaskRunner,
    ltx_num_frames_for_duration,
)

_LTXBindings = _model._LTXBindings
_load_ltx_bindings = _model._load_ltx_bindings


class DirectLTX25Backend(_model.DirectLTX25Backend):
    """Legacy facade preserving historical binding monkeypatch hooks."""

    def _get_bindings(self):
        if self._bindings is None:
            self._bindings = _load_ltx_bindings()
        return self._bindings


__all__ = [
    "DirectLTX25Backend",
    "LTX_GENERATION_PROFILE",
    "LTX_VIDEO_TASK",
    "LTXModelFiles",
    "LTXVideoBackend",
    "LTXVideoParameters",
    "LTXVideoTaskRunner",
    "ltx_num_frames_for_duration",
]
