# LTX-2.5 A2V: quality investigation handoff (2026-09-26)

**Status: quality NOT accepted. This document records evidence, not a fix or an approved generation recipe.** The control-plane-only branch `fix/ltx25-bootstrap-readiness-observability-20260926` starts from `main` at `5b583ca5902affbe0c59b0431140004f48f21d82` and does **not** port the abandoned `reference-compiled` transformer changes. The earlier mixed PR #231 was rejected for visual quality and cold latency. Do not base future quality changes on that experimental branch.

## Objective and reproducible inputs

Generate a five-second talking-head clip using the same avatar/speech with clear speech-driven lips and jaw, stable identity, no malformed final frames, and lower total time **without changing or silently lowering accepted visual quality**. A technically valid MP4 or good audio mux is not lip-sync acceptance. Keep quality and startup/performance experiments separated.

- Image SHA-256: `aaaefa0acd0dbf25b7526ccb349fb6ad4fd7a8511b9eadec66107244bd7bbe64`, original test object `ltx25-a2v/smoke/images/<sha>.png`.
- Audio SHA-256: `18d070d56289a7abb83abe1394bcbcc29d7cab09cf464220c38ac7297bc94553`, five-second mono PCM WAV / 24 kHz.
- Short prompt: `An elderly monk speaking calmly to the camera.`; seed `4242`; 1280 × 720, 24 fps, 121 frames. Preserve the full 120,000 speech samples; 1,000 samples of silence were added only to conditioning audio to cover the model temporal grid.
- Latest eager run: job `ltx-a2v-monje-verified-eager-20260926-153042-132d94b1e0a5`; MP4 SHA-256 `19e66b584846b86bcbc6877979b2994d277945402f1ec5300c255c46f9aca2cc`; metadata SHA-256 `08177a71cff87035e234ed66b729977a5662df5b7affd9ad609cf0d9f0735f24`. Sidecar/MP4 are retained in the operator's local `data/output/deployment-validation/ltx25-a2v/monje-verified-eager-20260926-153042/` and R2 job outputs. Do not commit private/binary inputs or generated videos to the repository.
- The original earlier video circulated as a WhatsApp export and lacks its authoritative job sidecar. It shows visibly greater lip movement in user comparison, but cannot establish an exact controlled A/B without original job metadata. Avoid claiming a root cause solely from these MP4s.

## Verified benchmark results

| Run | Generation profile | Inference (seconds) | Worker total (seconds) | User visual assessment |
| --- | --- | ---: | ---: | --- |
| Earlier reference, PR #228 | eager distilled reference | 157.360 | 175.876 | Better than fast; still insufficient |
| Compiled regression, old PR #231 | `reference-compiled` (blocks) | 403.733 | 425.529 | Mouth barely moved vs older video; rejected |
| Latest verified eager, 26 Sep 2026 | `ltx25-a2v-reference-distilled-a95ab856-model6c7e5e5-fp8cpu-eagersdpa-v1` | 153.973 | 172.463 | Still insufficient, even without compilation |

The latest eager sidecar records `transformer_compilation=eager`, `pipeline_reused=false`, FP8_CAST, CPU offload, 8 ancestral stage-one steps, 3 Euler stage-two steps, image strengths 0.7/1.0, both audio stages frozen, video modality/CFG 1.0, STG 0.0. This proves that reverting compilation alone does not solve mouth articulation; it does **not** prove which recipe parameter causes the quality gap.

Latest end-to-end timings: Salad capacity 309.6 s; actual worker bootstrap/ready 727.6 s; Postgres/R2 job phase 201.4 s (includes the 172.463 s worker generation); cleanup 22.7 s; total 1,261.3 s (~21 min 1.3 s). End state: LTX group `ai-video-factory-ltx25-worker-v3` stopped, `replicas=1`, `pending=False`; pinned image then deployed `docker.io/yagobordell/ai-video-factory@sha256:3a46d675c4ca93ea065c0540ba38efdb7a45cf447cecb841524aaee0d7342469`. That old image was built from the experimental branch, although the latest job selected eager. **Do not treat it as the new clean-branch image.**

## Code baseline and constraints for the next quality agent

- Start from `main` or this clean **control-plane-only** branch; do not cherry-pick old PR #231's `CompilationConfig`, `reference-compiled` profile, fake compilation test hooks or compiled smoke options. The clean branch has zero generation-recipe differences from the referenced `main` commit.
- `main` already contains `reference` and experimental `guided` A2V code predating PR #231; those profiles are **not** approved lip-sync solutions. Inspect `src/ai_video_factory/workers/ltx25/a2v.py`, `reference_a2v.py`, `reference_recipe.py`, upstream pinned `a95ab856bf29407b6b066ede0abe1846050db56c`, and model revision `6c7e5e573ac1667efc83407806fe9b0b93730e60` as evidence. Do not infer production acceptance from CI.
- LTX article: https://ltx.io/blog/how-to-build-talking-ai-avatars-from-audio . It describes **LTX-2.3**, not this pinned LTX-2.5 implementation. A separately merged experimental `guided` profile tried video CFG 3.0, modality 3.0, STG 1.0, rescale .7 and block [29] with full/dev transformer, 30 stage-one steps, stage-two LoRA and disk offload. Its prior real GPU test took ~530 s inference and deformed final frames. The pinned LTX-2.5 upstream A2Vid API does not expose the blog's audio-CFG-7 knob as a public argument. Check pinned upstream APIs before proposing any port.
- Keep image/audio hashes, prompt, seed, model revision, exact conditioning waveform, frame count, crop and encoders controlled in A/B. Compare specific mouth motion/phonemes and face identity *visually*; use metadata for technical contracts. Collect both original job sidecars if available.
- Keep Salad capacity allocation, model manifest readiness, Postgres dispatch and worker download diagnostics separate from denoising quality. The clean branch's per-start model marker and atomic receipt gate are not a quality change; the image needs an explicit new build/push and pinned `Prepare` before any future GPU smoke. Do not deploy from an unmerged draft by assumption.
- No additional paid GPU tests or merge were executed in producing this handoff. Any GPU test must be requested explicitly, and the controlled wrapper must stop Salad in `finally`.

## Open investigation, without a preselected fix

Determine whether the prior better-looking source clip used a different prompt, conditioning image, model/checkpoint, guidance, temporal recipe, seed, or renderer version. If an exact historical recipe is unavailable, define a reproducible quality baseline and assess candidate changes **one at a time**, preserving identity, phonetic mouth motion, 5 s audio fidelity and end-to-end cost. Document any visual regression and revert unaccepted variants rather than promoting technically valid MP4s.
