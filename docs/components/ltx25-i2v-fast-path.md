# LTX 2.5 I2V: 5-second fast-path experiment (RTX 5090)

## Goal and provenance

Based on the user-supplied `analisis_optimizacion_ltx25_i2v.docx` (2026-09-30).
The ~87-second reference is OptiumLabs **LTX 2.3** generating 5 seconds;
its effective GPU/offload settings were not recovered. It is a target, NOT
an established LTX 2.5 SLA. Exclude Salad bootstrapping and model download
from *warm inference* timings, but preserve separate first-job data.

The priority is reducing the dominant transformer work *before* micro-
optimizing the decoder: the current guided profile uses two logical
positive/negative stage-1 passes over an internal 1280x768 grid. The new
vanilla stage-1 has only one pass, and low-res vanilla uses a 1024x576 grid.
Neither changes the upstream official **8+3** distilled schedule or the
2.5 ancestral sampler. Both continue joint audio/video latent diffusion;
only the final discarded audio waveform decode is skipped.

## Exact profiles: all opt-in; production default unchanged

| CLI | Stage 1 | Internal | DiffVAE | Offload | Audio decode | Purpose |
| --- | --- | --- | --- | --- | --- | --- |
| default | negative CFG 1.25/B1 | 1280x768 | eager | FP8_CAST+CPU | no | Validated original baseline |
| cfg-b2 | negative CFG 1.25/B2 | 1280x768 | eager | FP8_CAST+CPU | yes | Existing same-math CFG batching control |
| fast-vanilla-reference | upstream SimpleDenoiser/B1 | 1280x768 | eager | FP8_CAST+CPU | yes | Direct pinned-upstream correctness reference |
| fast-vanilla | instrumented pinned vanilla/B1 | 1280x768 | eager | FP8_CAST+CPU | no | Isolate CFG removal |
| fast-lowres | instrumented pinned vanilla/B1 | 1024x576 | eager | FP8_CAST+CPU | no | Isolate reduced diffusion grid |
| fast-lowres-triton | same as fast-lowres | 1024x576 | triton | FP8_CAST+CPU | no | Decoder only |
| fast-lowres-none | same as fast-lowres | 1024x576 | eager | FP8_CAST+NONE | no | HIGH-RISK experimental CPU-offload removal |

Every recipe has a distinct `generation_profile`, Postgres job fingerprint
and R2 artifact key: changed algorithm/resolution/decoder/offload can NEVER
silently replay an unrelated old clip. The default production profile,
negative guidance, quantization, delivery and A2V are unchanged.

`fast_i2v.py` adapts the exact pinned upstream 2.5 vanilla sampler/stages
and adds phase timers. Compare `fast-vanilla-reference` against
`fast-vanilla` with the same seed on real RTX 5090 before trusting
the cloned adapter. If decoded MP4s differ, investigate and do NOT promote.
Changing from guided to vanilla can change camera movement/direct speech.
Neither matching hashes nor fast generation alone proves acceptable quality.

Low-res uses deterministic Lanczos full-frame fitting (without crop),
downsampling the **whole** input keyframe including Qwen 1280x736.
The internal 1024x576 raw MP4 is deterministically upscaled by FFmpeg
to the required silent **1280x720 H.264/24fps** delivery. It retains the
original 2560x1440 post-generation upscale as a separate unchanged stage.
A real smoke must verify the first frame, camera, hands/faces, no new
distortions, and correct codec/FPS/frame count. Postprocess cost is logged
as `delivery_postprocess_seconds`.

`fast-lowres-none` is NOT enabled automatically; a 32 GB 5090 may OOM
during FP8_CAST nonstreaming or other module residency. Run it only after
low-res CPU success, as the last profile in a controlled batch. A single
OOM/segfault disqualifies it and must be reported as failure, without
automatic retry or production fallback.

NATTEN remains disabled after its verified RTX 5090 segfault. Triton is
an optional control and must prove total latency improvement, not just
decoder latency. NVFP4 requires a separately pinned model checkpoint,
the appropriate SM120 ltx-kernels, different weight-builder path and
independent quality/VRAM testing; it has NOT been enabled by these changes.
Compile/capture also stays out until warm inference is characterized.

## Controlled 5-second batch

Prerequisites: checked-out feature branch, new Docker image built *from this
branch* and pinned by digest, A2V group and Capacity Controller stopped,
original-reviewed full keyframe PNG and exact original positive prompt.
The fast batch can be run with a single worker:

~~~powershell
$Profiles = @(
    "default", "cfg-b2", "fast-vanilla-reference",
    "fast-vanilla", "fast-lowres"
)
$Options = @{
    Image = $Image
    Prompt = $Prompt
    ExpectedPinnedImage = $PinnedImage
    DurationSeconds = 5
    SeedBase = 4242
    ProfileSelection = $Profiles
    RepeatCount = 2
    EnvFile = ".env"
    NonInteractive = $true
}
.\scripts\smoke\run_ltx25_i2v_fast_benchmark.ps1 @Options
~~~

For a cheap *smoke first*, choose DurationSeconds=2, RepeatCount=1,
ProfileSelection=@("fast-vanilla-reference","fast-vanilla","fast-lowres").
After all smoke clips are technically and visually accepted, run the
5-second benchmark above. This script pre-checks all image/prompt/timing
hashes, R2 account and immutable digest before paid GPU; starts **one**
worker; runs profiles sequentially; uses fresh round-specific seeds so
repeat 2 is NOT a replay; saves every MP4 and per-phase Salad log;
stops capacity in finally. Interruption outside PowerShell finally still
requires manual Salad verification.

To test offload separately, add `"fast-lowres-none"` **last** in
ProfileSelection and pass `AllowNoOffload=$true`. Never run the
unproven no-offload configuration as a normal production request.
To test Triton on already-validated lowres, add `fast-lowres-triton`
after `fast-lowres` and measure the separate decoder and total latency.

## Promotion gates

1. All offline CI must pass: syntax/Ruff, contracts, real static/vanilla
   selection, same-seed independent job IDs, atomic upscale, Qwen no-crop
   tests, and unchanged production/A2V regression tests.
2. No OOM/segfault, no cached replay, one proper Postgres success per trial.
   `summary.csv` contains real Salad phase timings, not fabricated values.
3. Confirm direct upstream vanilla reference equals instrumented vanilla
   when seed/keyframe/prompt/dimensions match, except the unused waveform
   decode. Visual review required even when the hashes match.
4. Compare guided vs vanilla at 2s first. Remove guided negative CFG
   from production ONLY if direct-to-camera/zoom and motion quality
   remain acceptable. If quality worsens, keep guided CFG B2.
5. Compare lowres vs fullres visually after deterministic delivery; do
   not infer no quality loss from technical ffprobe validity.
6. Repeat 5s (121 frames) at least twice with the same images and
   round-matched seeds. Distinguish first-load from subsequent runs,
   compare phase breakdown and peak allocated/reserved per trial,
   and review full 1280x720 output. The target is **near 90s warm 5s**
   if hardware, quality and stability allow it. The benchmark may
   show that a stronger NVFP4/Blackwell-specific build is necessary.
7. Do not merge the new feature branch or switch the normal run to FAST
   without the user's review and approval. Fall back to the exact
   pre-existing validated guided default if any gate fails.

There are deliberately **no Salad bootstrap/download modifications** here.


## Opt-in full-resolution FP8, Triton and native NVFP4 experiments

The first real 5s/121-frame A/B measured fast-vanilla at 109.18s and
95.74s versus default at 146.93s and 125.67s. Our instrumented
fast-vanilla and pinned upstream reference produced identical MP4s
in both rounds. Preserve 1280x768 native internal diffusion and silent
1280x720 delivery while evaluating remaining optimizations.

| CLI name | DiT precision | Offload | DiffVAE | Risk |
| --- | --- | --- | --- | --- |
| fast-vanilla | FP8_CAST | CPU | eager | Existing validated reference |
| fast-vanilla-triton | FP8_CAST | CPU | optional Triton | Changed decoder rounding |
| fast-vanilla-none | FP8_CAST | NONE | eager | 32GB RTX 5090 OOM possible |
| fast-vanilla-nvfp4 | Official prequant NVFP4 | NONE | eager | Separate native image + visual differences |

For the three new profiles, the instrumentation separates Gemma
loading+encoding and embeddings-builder time. There is deliberately
no repeated-prompt cache shortcut: normal beats may use unique prompts.

### FP8 comparison using the normal experimental image

Build and pin the normal Dockerfile from this branch as before.
Stop the Capacity Controller and avatar group; Prepare the stopped
I2V group with the matching immutable Docker digest. For the first
run, prefer a cheaper 2-second smoke.

~~~powershell
$SeedIdentity = "ltx-five-sec-candidates-v1"
$Opts = @{
    Image = $Image
    Prompt = $Prompt
    ExpectedPinnedImage = $PinnedImage
    DurationSeconds = 5
    SeedBase = 4242
    SeedIdentity = $SeedIdentity
    ProfileSelection = @("fast-vanilla", "fast-vanilla-triton", "fast-vanilla-none")
    RepeatCount = 2
    RunId = "fp8-fullres-$(Get-Date -Format yyyyMMddHHmmss)"
    AllowNoOffload = $true
    EnvFile = ".env"
    NonInteractive = $true
}
.\scripts\smoke\run_ltx25_i2v_fast_benchmark.ps1 @Opts
~~~

Place the potential-OOM FP8 none profile LAST, with explicit consent.
A failure stops capacity, preserves earlier clips/metrics and cannot
silently become a production configuration.

### Separate official native NVFP4 image

Dockerfile.nvfp4 uses the pinned upstream revision and PyTorch/CUDA
2.11/12.8 devel image to compile SM120 native kernels. It opts in
to the official distilled NVFP4 checkpoint at the exact pinned HF
revision; the normal worker Dockerfile and model assets are unchanged.
Expect a larger Docker pull and an extra model download.

Verify licensed access and the exact pinned checkpoint metadata BEFORE
building or allocating any GPU:

~~~powershell
uv run --locked --extra dev python scripts/smoke/check_ltx25_nvfp4_source.py --manifest docker/workers/ltx25/model-manifest.json --env-file .env
~~~

Build the experimental image with Dockerfile.nvfp4 (not the normal
Dockerfile) using your existing docker buildx build --push workflow
and obtain the immutable repository@sha256 digest. Prepare the STOPPED
I2V group with that new digest. Then execute the NVFP4-only batch:

~~~powershell
$Nvfp4Options = @{
    Image = $Image
    Prompt = $Prompt
    ExpectedPinnedImage = $Nvfp4PinnedImage
    DurationSeconds = 5
    SeedBase = 4242
    SeedIdentity = $SeedIdentity
    ProfileSelection = @("fast-vanilla-nvfp4")
    RepeatCount = 2
    RunId = "nvfp4-fullres-$(Get-Date -Format yyyyMMddHHmmss)"
    AllowNvfp4 = $true
    EnvFile = ".env"
    NonInteractive = $true
}
.\scripts\smoke\run_ltx25_i2v_fast_benchmark.ps1 @Nvfp4Options
~~~

Using the same SeedIdentity aligns the actual per-round seeds across
the two separate Docker-image runs. NVFP4 changes precision: do NOT
require identical MP4 hashes versus FP8; compare visual quality, motion,
camera behavior and prompt alignment in multiple representative scenes.

NVFP4 fails closed without its special image runtime marker,
Blackwell GPU capability, compiled native kernels and the precise
official checkpoint. NVFP4-prequant uses NONE offload because the
upstream CPU streamer cannot load its companion-key quantization.

Do not merge or choose a production profile before stable repeated
5s RTX 5090 inference and visual validation with several prompts.
