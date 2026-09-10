# Phase 9.3 — Remotion visual renderer

Status: technically validated against the canonical local eight-shot composition; visual inspection
remains before formal closure.

Phase 9.3 is the first real visual render in Phase 9. It consumes the closed
`composition_plan.json` from Phase 9.2 and produces a silent H.264 MP4. It does not decide semantic
or audiovisual timing.

## Boundary

```text
composition_plan.json
        |
        v
Python renderer preparation
  - validate plan
  - stage shot MP4s
  - normalize display-only caption artifacts
        |
        v
remotion_props.json + remotion/public/media/*.mp4
        |
        v
Remotion Phase9Visual
  - hard-cut shot sequences
  - frame-exact captions
  - active-word highlight
        |
        v
data/output/phase9/visual.mp4
        |
        v
ffprobe validation
```

`ShotTiming` remains authoritative through the frame intervals already persisted in the composition
plan. Remotion never stretches a shot to the physical duration of its LTX source clip. Each source
starts at its local frame zero and is visible for exactly `duration_frames`; surplus LTX frames are
outside the corresponding `Sequence`.

## Renderer project

The renderer lives in the isolated `remotion/` Node project. Remotion packages are pinned to the same
exact version (`4.0.523`) because Remotion requires its package versions to stay aligned. The project
uses Node 20 or newer; CI validates it with Node 22.

Source video is rendered with `<Video>` from `@remotion/media`. Canonical shot and caption boundaries
are represented with Remotion `Sequence` components. Composition width, height, fps and duration are
returned from `calculateMetadata()` using the validated renderer props rather than duplicated as a
second production timing source.

The Phase 9.3 baseline intentionally uses hard cuts. Transitions remain a later compositor concern and
must not move canonical shot boundaries or change the total frame count.

## Media staging

The closed Phase 9.2 plan contains local machine-specific source paths. Before rendering, Python
stages the clips under:

```text
remotion/public/media/shot_001.mp4
...
remotion/public/media/shot_008.mp4
```

Staging prefers a filesystem hard link and falls back to `shutil.copy2()` when linking is unavailable.
An unchanged staged file is reused after size and SHA-256 comparison. Stale `shot_*.mp4` files are
removed. Generated staged media is ignored by Git.

Renderer props use only stable public URLs such as `/media/shot_001.mp4`; the absolute Windows source
paths never enter the TypeScript component tree.

## Caption presentation and dagger cleanup

Phase 9.2 deliberately preserved the canonical transcription text verbatim. The validated narration
contains two U+2020 dagger artifacts:

```text
word 41: †el
word 43: sirve†
```

Phase 9.3 removes U+2020 only in the presentation props:

```text
†el    -> el
sirve† -> sirve
```

The closed `composition_plan.json`, `NarrationWord[]`, timestamps and word IDs are not rewritten.
Every changed word is recorded in `presentation_normalizations[]` with source text, display text and
the explicit rule `remove_unicode_dagger_u2020`. This makes the cleanup deterministic and auditable
without pretending the renderer owns the canonical transcript.

The renderer rejects any remaining dagger in caption display text.

## Caption rendering

Each `CaptionCue` becomes a `Sequence` with its existing `start_frame` and `end_frame`. Individual
word frame intervals are retained. Inside the caption sequence, the active word is derived from the
sequence-local frame and highlighted visually; no timestamp reconstruction occurs in TypeScript.

The initial style is deliberately simple and readable for 9:16 output: centered lower-safe-area
caption card, high-contrast text and active-word emphasis. Styling can be refined after the first real
render without touching timing contracts.

`@remotion/media` expects video fitting to use the dedicated `objectFit` prop. The canonical component
therefore uses `objectFit="cover"` directly on `<Video>` rather than putting `objectFit` inside its
React `style`; this removes the warning observed on the first real local render.

## Output contract

`data/output/phase9/visual.mp4` must be:

```text
codec:        H.264
resolution:   768x1280
fps:          24
total frames: 1080
duration:     45.000 s
audio:        none
```

Python probes the rendered file and rejects codec, resolution, fps, frame-count or audio mismatches.
Narration muxing remains outside Phase 9.3.

## Canonical local validation

The first complete local Remotion render succeeded against the real eight-shot composition:

```text
shots=8
captions=29
words=105
presentation_normalizations=2
rendered frames=1080
duration=45.000s
fps=24
audio_streams=0
output size=39.9 MB
```

The two expected presentation fixes were applied to word IDs 41 and 43. Remotion downloaded and
cached Chrome Headless Shell on the first invocation, bundled the renderer, rendered all frames and
produced `data/output/phase9/visual.mp4`. The Python post-render probe accepted the result.

The first render also emitted a non-fatal `@remotion/media` warning asking for the dedicated
`objectFit` prop. The renderer was updated immediately afterward so subsequent renders use
`objectFit="cover"` directly and should not emit that warning.

## Commands

From the repository root after pulling the Phase 9.3 implementation, install the isolated Node project
from inside its directory. On Windows PowerShell, `npm.cmd` avoids environments where `npm.ps1` is
blocked by execution policy:

```powershell
Push-Location .\remotion
npm.cmd install
Pop-Location
python scripts/run_phase9_remotion.py
```

To validate staging and props without rendering Chromium frames:

```powershell
python scripts/run_phase9_remotion.py --prepare-only
```

The default outputs are:

```text
data/output/phase9/remotion_props.json
data/output/phase9/visual.mp4
```

The Remotion Studio can be opened separately with:

```powershell
Push-Location .\remotion
npm.cmd run studio
Pop-Location
```

## Closure criteria

Phase 9.3 closure status after the canonical local render:

1. eight staged source clips preserve the eight canonical shot intervals — confirmed;
2. all 29 caption cues and 105 words are present in renderer props — confirmed;
3. exactly the two known dagger artifacts are normalized for presentation — confirmed;
4. the canonical Phase 9.2 composition plan remains unchanged — confirmed by the read-only renderer
   preparation path;
5. Remotion produces one H.264 768x1280 video at 24 fps — confirmed;
6. the output contains exactly 1080 frames and no audio stream — confirmed;
7. visual inspection confirms hard cuts, readable captions and active-word timing — pending;
8. Python Ruff and pytest pass — confirmed in CI;
9. Remotion TypeScript typecheck passes in CI — confirmed.

Phase 9.3 is technically validated but remains open until the rendered MP4 is visually reviewed.
Transitions, motion graphics and final narration muxing remain outside Phase 9.3.
