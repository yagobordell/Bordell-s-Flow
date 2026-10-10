# Professional editorial postproduction (E1 + E2)

This is a postproduction layer for **professional** B-pipeline runs. Casero output and existing LTX job identities remain unchanged.

## Operation

- E1 reads the authoritative whole-video B plan and completed STT beat timeline. E1 decides a sparse collection of supported editorial interventions. It does not inspect image files or alter any B output.
- E1 supports occasional **progressive editorial text**, **accumulating lists**, **number emphasis**, and **single text cards** in v1. There is **no subtitle track** and no generated spoken dialogue.
- The compiler resolves E1's exact spoken anchors against already-saved, SHA-verified STT word records, using the existing token alignment logic. No additional STT request is made.
- E2 receives only the relevant intervention and chronological JPG samples extracted from the **assembled pre-CTA LTX video**. It decides final normalized placement, font size, color, stroke and alignment, or suppresses duplicate/inappropriate text. E2 never changes E1's wording.
- A deterministic validation step checks geometry, text sizing and protected CTA regions. Unsupported or unsafe E2 placements are suppressed.
- Remotion produces a transparent 24-fps ProRes 4444 graphics layer. FFmpeg overlays this layer onto the untouched LTX video and preserves its original audio stream. The existing professional CTA compositor then draws the verified QR and the STT-triggered zellenne.com website **after** the new graphics.
- Source media, narration, visual_plan.json, LTX fingerprints and completed LTX job manifests remain untouched.

The editorial content is independently content-addressed. E1 and E2 cache keys include model, instructions and verified relevant inputs; their associated API usage is stored in `editorial/e1/` and `editorial/e2/`. A completed render gets its own manifest with a source-video SHA and narration SHA.

## Installation

The editor is opt-in. Install Node.js 20+ and Remotion dependencies once:

```powershell
cd remotion/editorial
npm install --no-audit --no-fund
cd ../..
```

Remotion packages are pinned to the same exact version. A host running video generation normally still needs FFmpeg and the existing Python environment.

## Commands

A new professional generation:

```powershell
.\run.ps1 video02 enzo --editorial
```

Resume an existing incomplete LTX video and apply E1/E2:

```powershell
.\run.ps1 video02 enzo --generate-video --editorial
```

Reedit an already completed, verified professional LTX video without B, TTS, STT, image or GPU inference:

```powershell
.\run.ps1 video02 enzo --edit-only
```

An edit-only result is written under `editorial/runs/<fingerprint>/` and its location is printed. It intentionally **does not overwrite a previously published immutable result**. A fresh opt-in full run publishes the edited MP4 as the final master, preserving the LTX manifest as a separately verified upstream artifact.

Use the normal run or `--no-editorial` for the legacy FFmpeg-only production.

## Failure and recovery

- If E1 produces an unresolved spoken phrase or an invalid beat ID, reject before E2 or Remotion.
- A missing base video or mismatched source hash is fatal for the editorial stage; no GPU regeneration occurs.
- Model responses are strict Pydantic Structured Outputs. Cache entries must validate before reuse.
- An unsafe E2 layout is suppressed rather than rendered over protected CTA elements.
- The final published MP4 is verified against an editorial manifest, source LTX SHA and narration SHA.
- Existing immutable publications cannot be silently changed. Use edit-only for a separate revision.

## Supported scope and limitations

The initial renderer supports typography-based editorial interventions, not all future motion graphics. Sounds, diagrams and customized object tracking are out of scope of this first implementation. E2 sees sampled frames rather than every frame and does not guarantee that moving subjects cannot cross the text later. Its decisions remain bounded by deterministic layout and CTA safety checks.

Remotion + FFmpeg introduce an additional decode/encode pass and should be benchmarked on real completed videos before becoming the professional default.

## Regression checks

```powershell
uv run --locked --extra dev ruff check .
uv run --locked --extra dev python -m pytest
cd remotion/editorial
npm run typecheck
npm test
```

GitHub Actions `Editorial E1 E2 CI` runs focused editorial tests and Remotion checks. A full rendered-video smoke on a real saved production run is required before enabling E1/E2 by default.
