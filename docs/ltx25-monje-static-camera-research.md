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

