# Phase 10 — Verification agents plan

Status: **PLANNED / NOT IMPLEMENTED**

Phase 10 is the next unfinished roadmap stage after the formal closure of Phase 9.

Its purpose is to turn the finished `FinalVideo` into a verified production artifact and, when a
problem is found, request the smallest safe regeneration scope instead of rerunning the whole
pipeline.

## Input boundary

Phase 10 consumes the provider-neutral Phase 9 handoff:

```text
FinalVideo = {
  uri,
  duration_seconds
}
```

Verification may also read earlier canonical artifacts as evidence:

```text
SourceScript
Beat[]
Scene[]
Shot[]
ContinuityEntity[]
ReferenceAsset[]
NarrationWord[]
ShotTiming[]
StoryboardKeyframe[]
VideoClip[]
FinalVideo
```

It must not depend on Salad transport IDs, Remotion internals, LTX checkpoint paths or provider raw
responses to decide whether the final audiovisual result is acceptable.

Operational metadata may be attached as evidence, but it is not the verification domain contract.

## Design principles

1. **Deterministic checks run before model-based checks.** Codec, duration, frame count, stream layout,
   hashes and timeline invariants should never require an LLM.
2. **Findings are structured and auditable.** Every failure records a stable code, severity, evidence,
   affected scope and recommended regeneration boundary.
3. **Verification does not mutate canonical artifacts.** It emits a report and explicit regeneration
   requests.
4. **Regenerate the smallest useful stage.** A caption/mux issue should not regenerate LTX video; a
   motion defect should not rerun narrative planning.
5. **Expensive verification is resumable.** Evidence extraction and model judgments should be cached
   by deterministic input hashes.
6. **Loops are bounded.** Automatic regeneration must have explicit maximum attempts and must stop on
   unchanged repeated failure.
7. **Human review remains possible.** The verification report should be useful even when no automatic
   remediation is allowed.

## Proposed contracts

Keep the contracts small and provider-neutral.

```text
VerificationFinding = {
  code,
  category,
  severity,
  message,
  target_type,
  target_id,
  evidence_refs,
  recommended_stage
}

VerificationReport = {
  final_video_uri,
  passed,
  findings
}

RegenerationRequest = {
  stage,
  target_type,
  target_ids,
  reason_codes
}
```

Suggested categories:

```text
technical
narrative
visual_identity
visual_continuity
motion
caption
sync
composition
```

Suggested severities:

```text
info
warning
error
blocking
```

`recommended_stage` should identify the earliest stage that can fix the problem without invalidating
unrelated accepted work.

## Phase 10.1 — Deterministic technical verification

Implement a local verifier using FFmpeg/ffprobe and existing canonical metadata.

Minimum checks:

- final file exists and is readable;
- expected video/audio stream count;
- H.264 video codec;
- expected resolution and pixel orientation;
- expected frame rate;
- duration and frame count agree with canonical Phase 9 timeline;
- audio duration covers the canonical narration interval;
- no unexpected second audio/video streams;
- output size is nonzero and SHA-256 is recorded;
- `FinalVideo.duration_seconds` agrees with probed media;
- shot boundaries reconstructed from `ShotTiming[]` remain within the final frame range;
- caption timing evidence remains inside the final timeline.

Optional deterministic media diagnostics should be added only when they are stable enough to be
automation gates. Examples include prolonged black frames, fully silent audio windows or obviously
truncated media. Thresholds must be explicit and tested; they must not silently become subjective
quality scores.

Expected output:

```text
data/output/phase10/technical_report.json
```

## Phase 10.2 — Narrative and semantic verification

Verify that the final audiovisual result still represents the intended source script and planned
shots.

The verifier should compare bounded evidence, not free-form global memory:

```text
SourceScript + Shot[] + sampled final-video evidence
```

Potential checks:

- each planned shot action is represented in the corresponding final interval;
- no major narrative beat is omitted or contradicted;
- visible text or imagery does not introduce an obvious incompatible claim;
- the final sequence preserves the intended shot order.

Model output must be Structured Output mapped into `VerificationFinding[]`. The model does not assign
canonical IDs and does not directly trigger regeneration.

## Phase 10.3 — Visual identity and continuity verification

Compare final-video evidence against canonical visual identity artifacts:

```text
ReferenceAsset[]
StoryboardKeyframe[]
VideoClip[] / sampled FinalVideo frames
```

Checks should be scoped by entity and shot:

- referenced objects/locations retain recognizable identity;
- adjacent shots do not introduce unexplained identity changes;
- generated motion does not destroy the key visual constraints selected by Phase 6;
- final composition does not crop away a required subject for most of a shot.

Evidence extraction should be deterministic: fixed frame positions derived from each canonical shot
interval, with hashes persisted so the same final video reuses the same evidence.

## Phase 10.4 — Sync, captions and composition verification

This stage checks the final presentation against Phase 5/9 evidence:

- narration begins and ends within expected bounds;
- active-word/caption timing remains aligned with `NarrationWord[]`;
- captions remain inside safe areas and are not clipped;
- scene/shot boundaries do not contain black gaps or accidental overlaps;
- final mux did not introduce measurable A/V offset beyond an explicit tolerance.

Deterministic timing checks should run locally. Visual readability checks may use a model only after
the deterministic evidence is prepared.

## Phase 10.5 — Selective regeneration planner

A planner converts blocking/error findings into bounded `RegenerationRequest[]`.

Initial mapping should be explicit rather than model-invented:

| Finding type | Default regeneration boundary |
| --- | --- |
| broken/missing reference identity | Phase 4 reference asset, then dependent Phase 6/8/9 artifacts |
| bad storyboard composition | Phase 6 target shot, then Phase 8/9 dependents |
| bad generated motion/video artifact | Phase 8 target shot, then Phase 9 |
| caption layout/highlight problem | Phase 9 only |
| final mux/audio container problem | Phase 9.5 only |
| narrative planning contradiction | stop for human review before broad Phase 2/3 regeneration |

The dependency expansion from one target to downstream artifacts must be deterministic Python logic.
A model may classify a finding, but it must not silently choose an unbounded rerun scope.

## Phase 10.6 — Bounded verification/regeneration loop

After selective regeneration:

1. rebuild only invalidated downstream artifacts;
2. rerun deterministic verification;
3. rerun only model verifiers whose evidence hashes changed;
4. compare new findings with the previous report;
5. stop when all blocking findings are cleared or the attempt budget is exhausted.

The first implementation should use a conservative maximum of one automatic regeneration attempt per
finding/target. Repeated unchanged failure should stop for human review rather than loop on paid GPU
inference.

## Persisted artifacts

Proposed Phase 10 output layout:

```text
data/output/phase10/
├── technical_report.json
├── evidence_manifest.json
├── verification_report.json
├── regeneration_requests.json
└── history/
    └── attempt_<n>.json
```

The report should contain hashes for `FinalVideo` and all extracted evidence required to reproduce a
judgment.

## Implementation order

Recommended sequence:

```text
10.1 deterministic technical verifier
  -> 10.2 evidence extraction + narrative verifier
  -> 10.3 visual identity/continuity verifier
  -> 10.4 sync/caption/composition verifier
  -> 10.5 selective regeneration planner
  -> 10.6 bounded end-to-end verification loop
```

Do not start with automatic regeneration. First establish a trustworthy report format and deterministic
technical gate.

## Definition of done

Phase 10 can be considered closed when a real canonical end-to-end run demonstrates:

- deterministic technical verification of the Phase 9 master;
- structured semantic/visual findings with evidence references;
- clean run with zero blocking findings, or a deliberately injected defect that is detected;
- at least one selective-regeneration test proving unrelated accepted artifacts are not regenerated;
- bounded retry behavior;
- replay of an unchanged verified video without new model/GPU work;
- green repository CI;
- a formal Phase 10 closure document with the real validation evidence.

## Immediate next task

Implement **Phase 10.1 — deterministic technical verification** first.

It has no GPU dependency, creates the stable verification contracts needed by later agents and can be
validated immediately against the already accepted Phase 9 `final_video.mp4`.
