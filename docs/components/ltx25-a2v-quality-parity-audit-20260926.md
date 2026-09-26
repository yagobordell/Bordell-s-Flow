# LTX-2.5 A2V: lip-sync parity audit and quality gate (2026-09-26)

## Status and non-negotiable gate

Investigation branch: investigate/ltx25-a2v-lipsync-parity-20260926.
Base: main at 5b583ca5902affbe0c59b0431140004f48f21d82.
The existing reference and guided profiles have NOT passed human visual lip-sync acceptance.
No change in this branch approves their visual quality or an inference speedup.
Do not merge, deploy, build/push a Salad image, start a paid GPU, or change production routing without explicit owner approval.
PR #230 is unrelated. PRs #231 and #232 were abandoned unmerged; no implementation was recovered from them.

The model source checked into the dedicated Dockerfile is Lightricks/LTX-2
a95ab856bf29407b6b066ede0abe1846050db56c. The model asset revision is
Lightricks/LTX-2.5 6c7e5e573ac1667efc83407806fe9b0b93730e60.
The ComfyUI comparison is the workflow as of commit
Lightricks/ComfyUI-LTXVideo 63a7f34e641203b63e08897868be10f9548ef0be
(the file was last changed 2026-09-17).

Sources:
- https://ltx.io/blog/how-to-build-talking-ai-avatars-from-audio
- https://docs.ltx.io/open-source-model/usage-guides/audio-to-video
- https://github.com/Lightricks/LTX-2/blob/a95ab856bf29407b6b066ede0abe1846050db56c/packages/ltx-pipelines/src/ltx_pipelines/a2vid_two_stage.py
- https://github.com/Lightricks/LTX-2/blob/a95ab856bf29407b6b066ede0abe1846050db56c/packages/ltx-pipelines/src/ltx_pipelines/utils/constants.py
- https://github.com/Lightricks/ComfyUI-LTXVideo/blob/63a7f34e641203b63e08897868be10f9548ef0be/example_workflows/2.5/LTX-2.5_A2V_Two_Stage_Distilled.json

The prior research attachment called Pasted markdown(7).md was not available to
this investigation as a readable file. Its hypotheses are not treated as verified.

## Verified implementation parity

The current Bordell reference profile uses the pinned distilled transformer,
the same 8-step and 3-step sigma arrays as pinned upstream, Stage 1 Euler
ancestral and Stage 2 Euler, image strengths 0.7 and 1.0, and encoded audio
frozen in both stages with zero audio noise. Stage 2 receives the ORIGINAL
encoded audio latent rather than generated Stage 1 audio. The output mux
preserves the decoded input voice instead of decoding audio from generated
latents. These properties are already under offline regression tests.

The published ComfyUI distilled A2V workflow loads BF16 weights and uses CFG 1
in both stages. Its example Stage 1 latent video size is 960x544 and Stage 2
is 1920x1088. Bordell must produce 1280x720 output and internally pads to
1280x768, so its Stage 1 is 640x384. A different example resolution does not
prove that resolution causes the observed mouth-motion failure.

Bordell uses FP8_CAST and CPU offload in reference and adapts the supplied
avatar to its public output geometry. The ComfyUI workflow also includes
image resizing/preprocessing before image conditioning. Compare these
transformations and the FACE SIZE inside the frame using real assets before
changing the model or image-strength parameters.

Bordell selects the first 8k+1 video grid that covers every decoded speech
sample and silence-pads the CONDITIONING waveform, not the original user
input. This prevents a trailing speech cut by design, but requires a matched
audio/latent timing inspection. Compare the decoded waveform, padded audio,
Audio VAE latent temporal length, and the final muxed audio.

The LTX blog describes LTX-2.3. Its suggested modality_scale=3 and audio
CFG=7 are NOT equivalent to the published LTX-2.5 distilled ComfyUI CFG=1
workflow. In pinned upstream A2VidPipelineTwoStage, the audio guider is built
internally from default parameters and the public call accepts only
video_guider_params. Bordell's reference pipeline instead uses SimpleDenoiser:
a change to unused guidance metadata would not alter its denoising. The guided
dev experiment used video multimodal guidance, took about 530 seconds of
inference on the recorded monk sample, and visibly deformed final frames.
It is not an approved reference.

## Cold-bootstrap validity risk in current main (code-level finding)

The current reference backend's _validate_runtime() calls shared file
validate(), which checks file presence and positive size. It requires the
atomic installed-model manifest only when LTX_INCLUDE_A2V_DEV_ASSETS is true.
The downloader writes the shared installed-model manifest after verification.
The existing controlled PowerShell smoke starts one Salad replica and then
submits the job; it does not itself prove the manifest finished before
Postgres submission. Thus a cold trial could overlap model verification with
initial lazy model preparation. This is a **risk established by code inspection**,
not evidence that partial model files caused any previous lip-sync regression.
A 'running' container or a valid MP4 is not proof of isolated cold timings.

For a paid cold baseline, first verify the exact immutable deployed image,
one-replica state, the completed installed-model receipt and stable readiness
BEFORE submission; save timestamped proof. If the currently deployed image
cannot provide that proof, treat cold inference timing as contaminated and
do not promote the experiment as reproducible. Any future readiness fix
must be designed and reviewed independently on this branch; do not
cherry-pick the abandoned PR #232. Changing worker code requires a
new image/digest and separately authorized Salad deployment.

## Input and prior outcome record

Use the owner's source assets, never synthesized stand-ins:
image=data/input/avatar/monje.png,
SHA-256=aaaefa0acd0dbf25b7526ccb349fb6ad4fd7a8511b9eadec66107244bd7bbe64;
audio=data/input/avatar/monje.wav,
SHA-256=18d070d56289a7abb83abe1394bcbcc29d7cab09cf464220c38ac7297bc94553.
Prompt: An elderly monk speaking calmly to the camera.
Seed 4242; output 1280x720, 24 fps, 121 frames.
Do not accept silently different hashes, model revision, frame rate, prompt,
conditioning, or effective audio duration as the same experiment.

Previous reference: 157.36 s inference / 175.88 s worker total; better mouth
movement, still unacceptable. Reference compiled: 403.73 s / 425.53 s,
severe mouth-motion regression. Final reference eager: 153.97 s / 172.46 s;
quality remains insufficient. The earlier better-looking WhatsApp export has
no retained original generation metadata. Search saved Postgres/R2/sidecar
records by original job ID when access is available; do not invent its recipe.

The latest known eager job was
ltx-a2v-monje-verified-eager-20260926-153042-132d94b1e0a5. Its 21-minute
end-to-end time includes Salad capacity and worker bootstrap and is NOT
comparable to the 172.46-second worker-only metric.

## Experimental order

1. Recover available job requests, metadata sidecars, full videos, Docker
   immutable digests, Git commits and evidence from the older better-looking
   reference. Note unknown fields explicitly.
2. Offline audit first: check exact input hashes and decoded sample counts,
   time alignment of speech to frozen audio latents, image preprocessing,
   ComfyUI-to-Bordell conditioning semantics, face/mouth pixel size, stage
   shape and crop, random-noise seed behavior, and pinned checkpoint metadata.
3. Run ONE owner-authorized paid reference-quality experiment at a time on a
   single RTX 5090 with a new SegmentId and exact image digest. Capture the
   same full artifacts, and stop Salad safely even if a job fails.
4. Human review must check bilabials (/p/, /b/, /m/), vowel openings, jaw
   movement, phoneme timing, pauses, sentence boundaries, facial identity,
   temporal stability and final frames. Technical MP4/audio checks alone
   never approve visual quality.
5. Isolate each hypothesis in an opt-in profile if required. Change only one
   generation variable per controlled experiment and preserve reference/guided
   unchanged. Do not copy LTX-2.3 blog settings into LTX-2.5 without an API
   and checkpoint compatibility check.
6. Only AFTER visual approval, freeze the entire generation recipe and
   optimize operations outside the frame computation. Measure Salad capacity,
   image pull, model download and SHA verification, bootstrap/readiness,
   first inference, pipeline-reused inference, video decoding, encoding/mux
   and artifact transfer independently. Treat quantization, compile,
   sampler, steps, guidance, tiling and offload as isolated visual experiments.

## Offline comparison tool

scripts/diagnostics/compare_ltx25_a2v_runs.py reads two saved job request
JSON files and their metadata sidecars. It checks that image/audio SHA-256
hashes, request parameters and generation-defining metadata match, that job
IDs differ to prevent replay, and that cold jobs are compared with cold
jobs (or reused-pipeline jobs with reused-pipeline jobs). It requires each
job's full Git commit SHA and immutable Docker digest as explicit
provenance.

It does NOT run GPU inference or access Salad, Postgres or R2. It does NOT
assess face identity or lip-sync: two explicit human visual-approval flags
are required to enable a numeric inference-time comparison. These flags
are declarations by the operator; the tool does not infer or verify them
from MP4 metadata. A different image digest or Git commit is recorded,
not silently treated as the same build. The tool reports worker-time
metrics only and clearly excludes external bootstrap/transport time.

Typical command, AFTER both clips have been visually approved, with real
full commit SHAs and sha256 Docker digests supplied by the operator:

    python scripts/diagnostics/compare_ltx25_a2v_runs.py --baseline-request PATH_TO_BASELINE_REQUEST --baseline-metadata PATH_TO_BASELINE_METADATA --candidate-request PATH_TO_CANDIDATE_REQUEST --candidate-metadata PATH_TO_CANDIDATE_METADATA --baseline-commit FULL_BASE_COMMIT --candidate-commit FULL_CANDIDATE_COMMIT --baseline-docker-digest sha256:BASE_DIGEST --candidate-docker-digest sha256:CANDIDATE_DIGEST --baseline-visual-approved --candidate-visual-approved --expected-image-sha256 aaaefa0acd0dbf25b7526ccb349fb6ad4fd7a8511b9eadec66107244bd7bbe64 --expected-audio-sha256 18d070d56289a7abb83abe1394bcbcc29d7cab09cf464220c38ac7297bc94553 --report NEW_REPORT_PATH.json

The report path is create-only, preserving existing local evidence. Without
human sign-off or exact provenance, the tool fails the comparison gate and
still prints its reasons. This tool cannot establish an end-to-end speedup:
use separate, verified Salad and transfer timestamps for that conclusion.
