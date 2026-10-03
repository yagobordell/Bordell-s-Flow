"""Bounded CPU-only cache of *final* A2V prompt embeddings for repeat benchmarks.

Upstream frees Gemma after each request, even when the pipeline Python object
is reused. Keep only deterministic post-connector tensors (not Gemma weights)
for an identical prompt within one experimental pipeline. Production requests
never enable this cache. Do not share across different model/offload profiles.
"""

from __future__ import annotations

from typing import Any


class A2VBenchmarkPromptCache:
    """One-entry cache with a hard 64-MiB ceiling, no persistent GPU tensors."""

    MAX_BYTES = 64 * 1024 * 1024

    def __init__(self) -> None:
        self._key: tuple[str, ...] | None = None
        self._cpu_contexts: tuple[Any, ...] = ()

    @staticmethod
    def _move(context: Any, *, device: Any, copy: bool) -> Any:
        # EmbeddingsProcessorOutput is a NamedTuple in pinned upstream. Preserve
        # its exact tensor dtype, padding mask and connector results.
        return type(context)(
            video_encoding=context.video_encoding.detach().to(device=device, copy=copy),
            audio_encoding=(
                context.audio_encoding.detach().to(device=device, copy=copy)
                if context.audio_encoding is not None else None
            ),
            attention_mask=context.attention_mask.detach().to(device=device, copy=copy),
        )

    def restore(self, prompts: tuple[str, ...], *, device: Any) -> list[Any] | None:
        if prompts != self._key:
            return None
        # Every call receives distinct GPU tensors. Cached CPU storage remains
        # untouched and does not retain the previous generation's latents.
        return [self._move(item, device=device, copy=True) for item in self._cpu_contexts]

    def store(self, prompts: tuple[str, ...], contexts: list[Any]) -> bool:
        # Build the entire entry before publishing it: exceptions never leave a
        # partially populated cache that a later generation might silently use.
        cpu = tuple(self._move(item, device="cpu", copy=True) for item in contexts)
        total_bytes = sum(
            tensor.numel() * tensor.element_size()
            for item in cpu
            for tensor in (item.video_encoding, item.audio_encoding, item.attention_mask)
            if tensor is not None
        )
        if total_bytes > self.MAX_BYTES:
            self._key = None
            self._cpu_contexts = ()
            return False
        self._cpu_contexts = cpu
        self._key = prompts
        return True
