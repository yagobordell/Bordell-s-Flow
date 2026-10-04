"""Pure offline contracts for repeat-prompt cache and fail-closed A2V queue gate."""

from __future__ import annotations

import json
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_video_factory.workers.ltx25.a2v_prompt_cache import A2VBenchmarkPromptCache
from scripts.smoke.check_ltx_a2v_benchmark_jobs import (
    find_unsettled,
    validate_terminal_failed_attempt,
)

Context = namedtuple("Context", ("video_encoding", "audio_encoding", "attention_mask"))


class FakeTensor:
    """Minimal tensor contract so offline CI needs no actual GPU/PyTorch install."""

    def __init__(self, values: list[float], dtype: str) -> None:
        self.values = values
        self.dtype = dtype

    def detach(self) -> FakeTensor:
        return self

    def to(self, *, device: str, copy: bool = False) -> FakeTensor:
        assert device == "cpu"
        return FakeTensor(self.values.copy() if copy else self.values, self.dtype)

    def numel(self) -> int:
        return len(self.values)

    def element_size(self) -> int:
        return 2 if self.dtype == "bfloat16" else 8

    def data_ptr(self) -> int:
        return id(self.values)

    def zero_(self) -> None:
        self.values[:] = [0.0] * len(self.values)


def _context() -> Context:
    return Context(
        video_encoding=FakeTensor([1.125, -2.5], "bfloat16"),
        audio_encoding=FakeTensor([3.0, -4.25], "bfloat16"),
        attention_mask=FakeTensor([1.0, 0.0], "int64"),
    )


def test_prompt_cache_reuses_exact_fp16_bf16_embeddings_without_gpu_retention() -> None:
    cache = A2VBenchmarkPromptCache()
    original = [_context()]
    assert cache.restore(("prompt",), device="cpu") is None
    assert cache.store(("prompt",), original)
    reused = cache.restore(("prompt",), device="cpu")
    assert reused is not None and len(reused) == 1
    assert type(reused[0]) is Context
    for name in Context._fields:
        a, b = getattr(original[0], name), getattr(reused[0], name)
        assert a.dtype == b.dtype
        assert a.values == b.values
        assert a.data_ptr() != b.data_ptr()
    reused[0].video_encoding.zero_()
    still_valid = cache.restore(("prompt",), device="cpu")
    assert still_valid is not None
    assert still_valid[0].video_encoding.values == original[0].video_encoding.values
    assert cache.restore(("different prompt",), device="cpu") is None


def test_prompt_cache_is_bounded_and_keeps_none_audio() -> None:
    cache = A2VBenchmarkPromptCache()
    ctx = _context()._replace(audio_encoding=None)
    assert cache.store(("prompt", "negative"), [ctx])
    restored = cache.restore(("prompt", "negative"), device="cpu")
    assert restored is not None and restored[0].audio_encoding is None
    cache.MAX_BYTES = 1
    assert not cache.store(("new",), [_context()])
    assert cache.restore(("prompt", "negative"), device="cpu") is None


def test_a2v_queue_gate_blocks_even_expired_running_jobs() -> None:
    old = datetime.now(UTC) - timedelta(days=1)
    rows = [
        {"job_id": "a", "status": "succeeded", "lease_expires_at": None},
        {"job_id": "b", "status": "failed", "lease_expires_at": None},
        {"job_id": "c", "status": "pending", "lease_expires_at": None},
        {"job_id": "d", "status": "retryable_failed", "lease_expires_at": None},
        {"job_id": "e", "status": "running", "lease_expires_at": old},
    ]
    assert {row["job_id"] for row in find_unsettled(rows)} == {"c", "d", "e"}


def test_terminal_failure_file_identifies_only_own_submitted_job(tmp_path: Path) -> None:
    job = "ltx-a2v-a2v-fp8-20261003-reference-fp8none-012345678901"
    (tmp_path / ("job-request-" + job + ".json")).write_text(
        json.dumps({"job_id": job}),
        encoding="utf-8",
    )
    assert validate_terminal_failed_attempt(
        tmp_path, [], expected_segment="a2v-fp8-20261003-reference-fp8none-1"
    ) == job
    (tmp_path / "job-request-other.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exactly one"):
        validate_terminal_failed_attempt(tmp_path, [], expected_segment="sample")
