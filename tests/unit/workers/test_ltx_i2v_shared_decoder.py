"""CPU-only exact-seed/lazy-lifecycle parity for the isolated Full HD shared VAE."""

from __future__ import annotations

import random
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_video_factory.workers.ltx25.shared_i2v_decoder import SharedSequentialVideoDecoder


class Latent:
    def __init__(self, value: int) -> None:
        self.value = value
        self.casts: list[str] = []

    def to(self, *, dtype: str) -> "Latent":
        self.casts.append(dtype)
        return self


def make_decoder(events: list, *, fail_on_clip: int | None = None) -> SimpleNamespace:
    built = []

    class Model:
        def __init__(self) -> None:
            self.decode_count = 0

        def eval(self) -> "Model":
            return self

        def decode_video(self, latent, tiling, generator):
            self.decode_count += 1
            idx = self.decode_count
            events.append(("decode", idx, latent.value, tiling))
            if fail_on_clip == idx:
                raise RuntimeError("injected decode error")
            # A model re-used for B=1 + B=1 must never alter advanced
            # per-output generator sequence; deterministic mock bits.
            for frame in range(3):
                yield bytes([latent.value, frame, generator.randrange(0, 256)])

    def build(*, device, dtype):
        events.append(("build", device, dtype))
        obj = Model()
        built.append(obj)
        return obj

    return SimpleNamespace(
        _dtype="bf16",
        _device="cuda:0",
        _alloc_trim_strategy="trim",
        _prepared_builder=lambda: SimpleNamespace(build=build),
        models=built,
    )


@contextmanager
def gpu_context(model, *, alloc_trim_strategy):
    # Model is built just once and retained for both B=1 decodes.
    assert alloc_trim_strategy == "trim"
    yield model


def _inject_gpu_model():
    module = types.ModuleType("ltx_pipelines.utils.gpu_model")
    module.gpu_model = gpu_context
    return patch.dict(sys.modules, {"ltx_pipelines.utils.gpu_model": module})


def test_shared_vae_matches_two_independent_decodes_byte_for_byte() -> None:
    events: list = []
    source = make_decoder(events)
    seeds = (5243, 5244)
    expected: list[list[bytes]] = []
    for clip, seed in enumerate(seeds, start=1):
        # Exactly the old original model lifecycle, same mock model, input
        # latent/tiling and actual advanced RNG state per independent sample.
        baseline = make_decoder([])
        model = baseline._prepared_builder().build(device="cuda:0", dtype="bf16")
        expected.append(list(model.decode_video(Latent(clip), "tile", random.Random(seed))))
    assert len(source.models) == 0
    with _inject_gpu_model():
        with SharedSequentialVideoDecoder(source) as shared:
            a = shared(Latent(1), "tile", random.Random(seeds[0]), dtype="bf16")
            b = shared(Latent(2), "tile", random.Random(seeds[1]), dtype="bf16")
            assert not source.models, "VAE build must remain lazy"
            actual_a = list(a)
            assert len(source.models) == 1
            actual_b = list(b)
            assert actual_a == expected[0]
            assert actual_b == expected[1]
            assert shared.completed == 2
            assert [event[0] for event in events] == ["build", "decode", "decode"]
            with pytest.raises(RuntimeError, match="exactly two"):
                list(shared(Latent(3), "tile", random.Random(6)))
        assert shared._closed is True
        assert shared._model is None


def test_shared_vae_fails_closed_on_interleaved_lazy_iterators() -> None:
    source = make_decoder([])
    with _inject_gpu_model():
        with SharedSequentialVideoDecoder(source) as shared:
            first = shared(Latent(1), "tile", random.Random(1))
            second = shared(Latent(2), "tile", random.Random(2))
            next(first)
            with pytest.raises(RuntimeError, match="sequential"):
                next(second)
            # Misordered consumption fails rather than sharing live decoder
            # state or silently building an extra one.
            first.close()
        assert shared._closed
        assert len(source.models) == 1


def test_shared_vae_releases_model_on_second_decode_error() -> None:
    events: list = []
    source = make_decoder(events, fail_on_clip=2)
    exits = []

    @contextmanager
    def tracked_context(model, *, alloc_trim_strategy):
        try:
            yield model
        finally:
            exits.append(alloc_trim_strategy)

    module = types.ModuleType("ltx_pipelines.utils.gpu_model")
    module.gpu_model = tracked_context
    with patch.dict(sys.modules, {"ltx_pipelines.utils.gpu_model": module}):
        with SharedSequentialVideoDecoder(source) as shared:
            assert len(list(shared(Latent(1), None, random.Random(1)))) == 3
            with pytest.raises(RuntimeError, match="injected decode error"):
                list(shared(Latent(2), None, random.Random(2)))
        assert shared.completed == 1
        assert shared._closed is True
        assert exits == ["trim"]


def test_decoder_preserves_explicit_dtype_and_never_builds_for_empty_pair() -> None:
    source = make_decoder([])
    with _inject_gpu_model():
        with SharedSequentialVideoDecoder(source) as shared:
            assert len(source.models) == 0
            first = Latent(1)
            assert len(list(shared(first, None, random.Random(1), dtype="fp32"))) == 3
            assert first.casts == ["fp32"]
            second = Latent(2)
            assert len(list(shared(second, None, random.Random(2), dtype="fp32"))) == 3
            assert second.casts == ["fp32"]
        assert [x for x in source.models if x.decode_count == 2]
