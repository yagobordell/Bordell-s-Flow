# LTX-2.5 monk A2V: short prompt, mouth sync and static framing

Status: experimental investigation only. No paid generation or visual acceptance from this change.

## Exact next-test contract

- Positive prompt, byte for byte: `An elderly monk speaking calmly to the camera.`
- Use the existing `data/input/avatar/monje.png` and `monje.wav`, seed 4242, 1280x720, 24 fps.
- Preserve pinned audio-frozen A2V reference conditioning; never insert a duplicate last frame.
- `reference_stage_2_steps=2` is an **unvalidated speed experiment**, not proven quality-equivalent to 3.
- Accept only after manually reviewing original MP4 + original speech for phoneme timing, natural
  jaw and head motion, identity, full background and no visible global zoom in the full shot.
- A valid MP4, correct audio samples and a contact sheet are necessary diagnostics, **not** proof
  of phonetic sync or a static camera. No merge into main until visual acceptance and green CI.

## Source and code audit (2026-09-26)

1. [LTX-2.5 workflow compatibility](https://docs.ltx.io/open-source-model/reference/workflow-asset-compatibility)
   documents native two-stage A2V with input audio frozen and an optional first image.
   Our `DistilledReferenceA2VPipeline` preserves the encoded stereo audio latent in both stages.
2. [LTX two-stage and I2V guides](https://docs.ltx.io/open-source-model/usage-guides/two-stage-generation)
   describe the first-frame image as appearance/first-frame conditioning, **not** camera motion lock.
   At the exact upstream commit `a95ab856bf29407b6b066ede0abe1846050db56c`,
   `combined_image_conditionings` turns `frame_idx=0` into `VideoConditionByLatentIndex`.
3. Local `reference_recipe.py` has Stage-1 strength 0.7, Stage-2 strength 1.0.
   These values do not constrain the rest of the shot to identical camera geometry.
   The `1280x720` grid adapter pads to a fixed model grid, then center-crops a fixed rectangle.
   This code path does **not** implement a progressive zoom. The causal contribution of any
   source-image crop must be checked against actual output frames, not guessed.
4. [LTX camera-motion API changelog](https://docs.ltx.io/api-changelog/2026/8/19) adds
   `camera_motion` on the **hosted** audio-to-video API. The pinned local
   `A2VidPipelineTwoStage.__call__` has no `camera_motion` parameter; the API option cannot
   be passed to this open-source worker as though the interface were identical.
5. [LTX talking-avatar article](https://ltx.io/blog/how-to-build-talking-ai-avatars-from-audio)
   discusses `modality_scale=3` primarily for LTX-2.3 and a guided A2V denoiser.
   Our 2.5 `reference` path uses `SimpleDenoiser`, frozen audio and distilled sigmas:
   changing metadata to 3 does not implement guided cross-modal denoising.
   The much slower dev/guided comparison profile is separate and must not be silently swapped in.
6. [LTX-2.5 motion-track IC-LoRA guide](https://docs.ltx.io/open-source-model/feature-guides/structural-control/motion-control)
   provides real structural motion tracks and first-frame conditioning. Its published workflow
   does **not** establish compatibility with our pinned local two-stage audio-frozen A2V route.
   A compliant implementation would need explicit conditioning compatibility, checkpoint,
   quality tests, and a separate opt-in experiment. Do not improvise a stationary last frame.
7. Community evidence: [upstream static/zoom report](https://github.com/Lightricks/LTX-2/issues/117),
   [2026 Reddit report of static character plus zoom](https://www.reddit.com/r/StableDiffusion/comments/1qdpz4t/ltx2_characters_dont_talk_or_move_the_video_just/),
   and [2026 LTX-2.5 talking-head lip-sync report](https://www.reddit.com/r/StableDiffusion/comments/1vw17li/is_ltx_25_just_terrible_for_lipsynctalkingphoto/).
   These are anecdotes, often for other checkpoints or ComfyUI workflows, not verified fixes
   for our pinned model and original input.
8. [Lightricks LTX-2.5 discussion #54: unwanted zoom](https://huggingface.co/Lightricks/LTX-2.5/discussions/54)
   reports zoom despite explicit locked-tripod/static-camera prompts in ComfyUI and the
   desktop app (August 2026). This is direct user feedback on the 2.5 model, not a
   confirmed upstream defect or evidence that our padding alone causes the zoom.
   It rules out treating any camera wording as a reliable hard camera constraint.
9. [Official static-camera LoRA](https://huggingface.co/Lightricks/LTX-2-19b-LoRA-Camera-Control-Static)
   is explicitly trained for LTX-2 **19B** and documents the text/image two-stage
   pipeline, not our LTX-2.5 **22B** audio-to-video checkpoint. The
   [official LoRA guide](https://docs.ltx.io/open-source-model/usage-guides/lo-ra)
   says to verify base-model and workflow compatibility. Do NOT silently install
   this 19B LoRA on the 22B A2V pipeline or claim it ensures zero zoom.
10. [LTX-2.5 22B Cinemagraph LoRA](https://huggingface.co/Lightricks/LTX-2.5-22b-LoRA-Cinemagraph)
    matches the 2.5 base family but explicitly declares moving people and
    complex body motions **out of scope**; it is trained on locked shots with
    static humans and only isolated environmental/reflection movement.
    It is therefore unsuitable as an unverified lip-sync/head-motion fix,
    even though its model card contains locked-camera example prompts.
11. Video stabilization is not a model-side camera lock. Correcting a generated zoom
    can discard visual context, enlarge the output and obscure true face motion.
    Avoid auto-zoom stabilizers or crops that hide the failure. Compare full frames.

## Changes implemented now

- One exact short prompt in the protected monk benchmark wrapper; generic A2V defaults stay unchanged.
- Explicit `enhance_prompt=False` on the reference pipeline (no silent prompt expansion).
- The original audio-frozen schedule, image strengths, Qwen padding and Stage-2 experiment
  remain unchanged so prompt-related behavior can be compared to historical jobs.
- The retry wrapper saves `monje_framing_review.png` and its JSON report *after GPU cleanup*.
  Six full-frame samples and the same central ROI expose changes in face size and background
  framing. This is read-only and never alters the MP4. Its result explicitly requires manual review.

## Next diagnostic/experiment gate

First inspect the previous real MP4 if available, plus the new output and contact sheet, at
the same original scale. Compare the first, mid and last frames for background feature scale
and face size. Distinguish global background scale (camera zoom) from the monk leaning forward
(subject movement). Verify audible words against actual mouth and jaw motion and whether head
motion is natural across speech pauses. Video/audio technical checks alone cannot certify this.

If global zoom persists with the minimal prompt, do not merge the 2-step speed experiment.
Isolate ONE change per future paid trial. Candidate investigations are Stage 2 = 3 vs 2
(same image, audio, seed, prompt), bounded Stage-1 image-strength variation only after
checking whether stronger anchoring suppresses facial movement, or a separately validated
camera-motion structural-control route. No evidence yet that any one of these guarantees
pixel-perfect camera lock while preserving phonetic lips and free head motion.

Do not rebuild/publish or deploy a new image until the final head has green CI. Never merge
without user-reviewed visual evidence. Before a paid test, verify Salad is stopped, settled
and on the original immutable digest, and restore it on exit.


## 2026-09-27: real allocation gap and safe one-worker fallback

The latest paid run requested two replicas but the Salad API reported `instances=[]`
for roughly 1,900 seconds, then allocated exactly one running instance. That
worker passed its network preflight, downloaded the five pinned BF16 model assets
in bounded parallel mode, wrote the verified manifest and passed /ready twice.
The initial selector required a second ACTIVE instance for the deletion-cost
readback even when no second instance had been allocated. It therefore failed
before scale-in and did not submit a Postgres job. The wrapper restored the old
pinned image and stopped group with one configured replica.

Fix: a configured two-replica race can finish with exactly ONE verified ready
current-version instance if no other active instance exists: set its deletion
cost to 100000; read back exact instance ID, version and cost twice; check the
group still has the original version and configured replicas=2; and re-check
the exact current-version instance-ID set immediately before requesting
configured replicas=1. If a competitor appears while protecting the winner,
explicitly set its deletion cost to 0 and reconfirm the pair. If the topology
changes after readback or the winner does not survive scale-in, abort without
submitting a job and let the wrapper stop all compute. This is an adaptive
fallback for *unallocated* capacity, not proof of camera/lip-sync quality.

The latest uploaded worker CSV includes just one instance ID; absence of another
instance in this CSV alone cannot prove platform-wide nonallocation. The
full race-selector API trace corroborates there was only one active instance
at the relevant selection and cost-confirmation time.

Salad's [pricing documentation](https://salad.com/pricing/) states that
allocation and image download are not billed; running instances are billed
per second. Do not equate the >45-minute wall clock with two billed GPU-hours,
and use actual instance timelines/invoices rather than estimating cost from
configured replica count.

## 2026-09-27: repeated cost readback failure; single-GPU default

Real v33 test: one RTX 5090 was allocated and became `ready=True` twice at
~1,085 seconds from race start. PATCH acknowledged `deletion_cost=100000`,
but **both the exact-ID GET and list GET returned 0 on six consecutive
observations**. The selector correctly refused scale-in and did not submit any
Postgres job. The controlled wrapper stopped and restored the original image.
The container CSV verified that the one worker downloaded and verified all
five pinned model files and reached the canonical Postgres polling state.

This is now an observed Salad API readback problem on that instance, not a
hypothetical stale list. Do not keep spending on 2-GPU protected races to test
video quality while the platform does not confirm deletion cost. The guarded
monk benchmark now defaults to `StartupReplicas=1`, invoking the existing
one-replica `Start` + two matching current-version `/ready` checks, with a
3,600-second readiness budget after start. This avoids both 2→1 scale-in and
all deletion-cost assumptions. The earlier 2-GPU race remains explicitly
opt-in for a separate infrastructure experiment; do not treat it as needed
for lip-sync acceptance.

The pinned A2V recipe, one original first-frame image, audio-frozen speech,
short monk prompt, seed and 2-step Stage 2 stay unchanged. A new reviewed host
commit and Docker digest must pass all CI checks before another paid run. The
generated MP4 still requires human review for phoneme alignment, head motion,
identity and actual background-scale changes.

## 2026-09-27: single-group race, not independent groups

The isolated second-group attempt has been reverted. The protected monk benchmark requests two replicas in the original LTX Salad group and selects the first current-version worker verified /ready twice. Previous paid tests repeatedly saw exactly one ready active instance while the second capacity slot remained unallocated; the obsolete shared-group selector unnecessarily waited 90 seconds for deletion_cost=100000 readback on this sole active instance even though there was no allocated loser. The latest branch corrects this sole-active path without using a deletion-cost PATCH, and confirms that the exact same instance survives the 2-to-1 change before submitting a single Postgres job. When two active instances are present, the reviewed benchmark uses the winner's own local Salad IMDS (PUT followed by GET) as the source of protection, and the Postgres first-ready election lock prevents two workers from independently declaring themselves ready. The external deletion-cost PATCH is NOT used in this mode; older direct-selector comparisons still require actual individual GET confirmation. The selector verifies that a rival has never been seen ready and reports an unprotected/second-ready rival rather than risking the winner. The exact same selected worker must survive voluntary scale-in before the Postgres smoke is submitted. These cases are checked by offline tests; a real paid MP4 with good lip sync/no zoom remains unverified.

### Why readiness-scoped self-protection is needed

The official [Salad instance-deletion-cost guide](https://docs.salad.com/container-engine/explanation/infrastructure-platform/instance-deletion-cost) says voluntary scale-in selects the lower cost first, explains both the local IMDS and external API paths, and cautions against mixing writers. We previously saw external PATCH acknowledge 100000 while both exact GET and list GET returned 0; repeating the external write before every test did not make the worker survive reliably.

The experimental LTX runtime now opts into a dedicated first-ready Postgres advisory lock (distinct from the capacity-controller lock), then PUTs 100000 to its **own** IMDS deletion-cost endpoint and GETs it back before returning HTTP 200 on /ready. Other concurrent workers cannot acquire that lock and remain unready with default cost 0. The documented IMDS [GET](https://docs.salad.com/reference/imds/get-deletion-cost) and [PUT](https://docs.salad.com/reference/imds/replace-deletion-cost) require Metadata:true; the latter returns HTTP 204. This is isolated to LTX with explicit `LTX_RACE_PROTECT_READY_WITH_IMDS=true` in the experimental service manifest and a rebuilt image.

The original one-group/two-replica smoke passes `--ready-protected-by-imds` only when invoked with the protected benchmark flag and verified new image. The chooser first verifies the same worker's /ready twice, then confirms the other worker is unready with cost 0 (or is not allocated); never mixes local IMDS with external PATCH. After requesting configured replicas=1, it verifies the winning instance is the **only** current-version ready worker before sending a job. There is no guarantee against unplanned host failures or all possible concurrent Salad control-plane races; a failed winner check still prevents running on a different GPU.

Offline mocked tests cover the IMDS election, exact local PUT/GET, failed persistence, lost leadership, two-active-but-one-downloading, a rival with high cost and a second-ready rival. They cannot prove the Salad scheduler honors IMDS updates until a real paid execution verifies the surviving UUID. Do not merge before real MP4 lip/head/zoom acceptance.

## 2026-09-27: v37 real logs prove IMDS prevents /ready after successful bootstrap

The most recent user-run v37 race allocated **two** current-version RTX 5090 instances,
but both remained `ready=False`. The container logs (CSV export) show the first,
ID `63769dac-8626-474f-ae83-f4f90715ff94`, downloaded and verified all
five pinned model assets by 09:04:17 UTC and completed runtime preparation by
09:04:20 UTC. It acquired the first-ready Postgres election lock at 09:04:24.
The IMDS PUT immediately triggered the guard's "PUT was not confirmed (204)"
exception; later IMDS GET requests timed out and the cleanup PUT also failed.
Thus the guard blocked /ready despite fully loaded models. The other GPU,
`c23cf469-6735-42a7-bcd4-a6112965f100`, was still downloading in its latest
CSV rows. The user deliberately stopped the group around 1,134 seconds,
the selector reported no winner and the guarded cleanup restored the original
stopped image. No A2V job was submitted or video generated.

Official Salad IMDS reference documents PUT HTTP 204 and GET HTTP 200. The CSV
does NOT include the actual unexpected PUT HTTP status, so it is not evidence
of HTTP 200, a specific HTTP error, or a broken local route by itself.
The revised guard records non-204 2xx status and accepts its write ONLY when
the next GET returns the exact desired cost; HTTP 4xx/5xx and transport errors
produce short method/status/error-type diagnostics without private payloads.
The early IMDS preflight now runs inside `download_models.sh` BEFORE the ~70 GiB
bootstrap: GET initial 0, PUT temporary 1 and GET 1, then PUT 0 and GET 0.
It exercises actual writes rather than merely assuming an existing default
zero is evidence that PUT works. Transient GET network failures get bounded
retries only during this cheap preflight; /ready remains time-bounded. A failed
preflight exits before model download. Unit tests include a real local HTTP
server emulating the documented and unexpected 200 PUT contracts, and ensure
that a successful status without the exact readback does not pass.

**Operational gate:** no more paid 2-GPU runs with the old v37 image. The
new host/image commit must first pass final CI. If the next real preflight
cannot confirm local IMDS even for temporary cost 1, stop and investigate
Salad's per-container IMDS reachability and actual response before another
long model download. This fix is a fail-fast verified protocol/diagnostic
correction, NOT a claim that Salad's live IMDS is now operational, that
the winner's UUID has survived live 2→1, or that monk lip-sync/no zoom has
been visually accepted.
