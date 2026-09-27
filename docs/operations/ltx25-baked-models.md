# LTX-2.5 baked models: candidate build and validation

This change moves the pinned **shared** LTX-2.5 model assets from Salad's paid
`running` bootstrap to the Docker **build**. Salad downloads the larger OCI image
before startup. This does not preload CUDA/VRAM or guarantee faster end-to-end time.
No A2V recipe, prompts, sampler, precision or audio parameters change.

## Scope and safety

- The exact shared file list and pinned repository/revision are defined by
  `docker/workers/ltx25/model-manifest.json`; optional dev assets are excluded by
  default. Runtime `LTX_REQUIRE_BAKED_MODELS=true` rejects missing/corrupt assets
  before any Hugging Face or Salad network preflight. This is intentional: a partial
  image must not silently spend paid GPU time downloading.
- BuildKit injects the Hugging Face token as a **secret mount**, not a build argument.
  Do not put tokens in command lines, Dockerfiles, build logs or image metadata.
  The ordinary Salad manager loads `HF_TOKEN` from `.env` before a new LTX build.
- Docker build bypasses Salad-only bandwidth thresholds and IMDS reallocation. The
  runtime fallback exists solely for explicitly opted-in recovery
  (`LTX_REQUIRE_BAKED_MODELS=false`) and is **not** the production profile.
- The candidate uses a distinct `v11-baked` tag. Do not overwrite the validated
  `v10` image, run `Prepare` against the production group or merge this PR until
  the following gates pass.

## Offline gates

1. CI: Ruff, Python/Postgres tests, Bash and PowerShell syntax, and Remotion.
2. Build a candidate image on a sufficiently provisioned builder, using
   `docker buildx build --platform linux/amd64 --file docker/workers/ltx25/Dockerfile
   --secret id=hf_token,env=HF_TOKEN --tag <candidate-tag> --load .`
   (or `--push` to an **isolated** candidate registry/tag). Capture exit code,
   image digest and compressed/uncompressed image sizes; do not assume that the
   160 GiB Salad storage allocation is sufficient without measuring.
3. Verify in that image, **without HF_TOKEN/network**, that `download-models`
   exits successfully and emits `MODEL_MANIFEST_VALID` and `MODEL_READY` for every
   pinned shared file. A corrupted or missing checkpoint must fail closed with
   `BAKED_MODEL_MANIFEST_INVALID`; an unauthorized change of revision must fail.
   Ensure the token is absent from `docker history` and image configuration.
4. Keep optional dev assets on a separate build: pass
   `--build-arg LTX_BAKE_A2V_DEV_ASSETS=true` and set
   `LTX_INCLUDE_A2V_DEV_ASSETS=true` only in that dedicated runtime. Do not enable
   dev assets on a shared-only image.

## Paid validation gate (requires explicit operator authorization)

Use an isolated Salad group or approved safe rollout, pinned to the candidate's
immutable image digest. Do not mutate a production group that is currently serving
jobs or shares a live capacity controller. Record allocation and image-pull time,
`running` to `/ready`, image size, worker model loading, inference, encode,
MP4/R2 retrieval and stable stop. Compare against the validated `v10` baseline
using the **same** monje PNG/WAV hashes, seed, prompt and A2V reference profile.
Use a fresh SegmentId to avoid R2 cache replay. Confirm that no model download
occurs after `running` and inspect lips/jaw motion and identity manually; technical
MP4/audio checks alone are insufficient. Stop capacity in a `finally`-equivalent
cleanup, confirm no pending change and zero effective running replicas.

Only after the full image build, container check, paid cold-start comparison,
human-reviewed output and green CI should this PR be considered for merge.
