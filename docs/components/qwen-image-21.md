# Qwen Image 2.1 worker

Qwen is the active text-to-image generator for Phase 4 references and Phase 6 storyboard
keyframes. The production artifact is a native **1280x736 PNG**, with no post-generation
crop. This near-16:9 size is divisible by 32 but is not exact 16:9. The primary request
contract is prompt-only; binary reference conditioning is not enabled in the current worker.

The pinned production profile keeps 40 inference steps, true CFG 1 and KV cache. Salad
uses the RTX 5090 with explicit `QWEN_IMAGE_21_MEMORY_MODE=int8_cuda`. Before applying the
updated Salad manifest, build and publish its new Docker image tag. A configuration or source
commit alone does not update a running worker.

Phase 8 accepts the 1280x736 keyframe without pre-cropping. LTX preserves all image content
by proportionally fitting it to 1280x720 and extending approximately 14 pixels on each side
with edge pixels; the LTX internal 1280x768 model-grid padding is separate. Final video
remains 1280x720 for the existing 2x upscale and compositor contracts.
