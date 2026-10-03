# Professional post-B2 voice: V1 → V2 → V3 → Gemini 3.8

This is the **professional** audio path. Casero is unchanged and continues to offer
Fish Audio, MiniMax and ElevenLabs. Professional no longer starts Fish at B1.1:
it waits for a **validated complete B2** output so the director can interpret
the narration against both the source text and the planned image/motion.
Even B2 beats with `audio: false` receive narration (voice-over).

## Three stages and archived originals

The initial B8.1, B8.2, B8.3 documents supplied for this feature are preserved
under `docs/reference/professional-voice-original-b8-{1,2,3}.md`. The active
adaptations live under `src/ai_video_factory/bots/prompts/v{1,2,3}.md`.
B3 canonical scenes and B7.2 depictions do not exist in this repository's B2
contract; the active V2/V3 prompts instead operate on immutable B2 beat IDs.
They never invent scenes, pictures, timing or narration.

- **V1** reads the complete B2 film and assigns global curve and independent
  tempo and pressure baselines to each existing block.
- **V2** runs per block, distributes that block's argument and viewer
  trajectory across existing beats and modulates tempo/pressure locally.
  If the baseline genuinely fails to represent the beats, V2 reports it and
  V1 re-evaluates the whole film. A repeated unchanged baseline settles the
  axis.
- **V3** runs per block, turns those intentions into spoken fragments with
  sustained `style`, exact source `speak` substrings and optional point-in-time
  **human vocal events**. It does not change a character of B2 narration.

Every V stage stores its exact structured request and validated output under
`voice_direction/V1`, `voice_direction/V2` and `voice_direction/V3` for
recovery. A failed local response validation receives up to two explicitly
described correction calls through OpenAI Responses API. One block's V2/V3
validation failure never reruns successfully validated adjacent blocks within
that pass. Re-entry from V2's evidence may require reevaluating multiple
blocks because V1 governs the global curve.

## Gemini 3.8 style and vocal events

Google documents **sustained** delivery in `speech_metadata.style` and
point-in-time **angle-bracket** vocal tags inside the transcript. Accordingly,
V3 does not place whisper, pace, emotion, pitch or other acting instructions
in the transcript. It can add only these allowlisted events at exact,
validated character boundaries: `breath`, `exhales`, `sigh`, `chuckle`,
`gasp`, `short pause`, `long pause`. Zero events is normal; there is no
automatic breath quota. No filler words, ad-libbing, sound effects, arbitrary
tags or mid-word insertions. Each fragment's `style` is short and attached
to its own Gemini `speech_metadata`. The application inserts angle brackets
by code after validating exact source reconstruction.

Reference: https://ai.google.dev/gemini-api/docs/speech-generation

Gemini provider is `gemini-3.8-flash-tts` for professional runs.
One single-speaker GenerateContent request receives the styled parts for
one B2 block. The resulting WAV is retained per block and Whisper-1 supplies
word timestamps. The existing sample-accurate join and STT→B2 timeline
are reused; real LTX audio/video sync still follows the existing alignment
process, not guessed lengths from V3.

## First run: choose a voice

For professional avatars, the voice is stored separately from Casero under
`data/<avatar>/meta/gemini_voice.json` (or the corresponding selected
avatar library root). The first interactive run requests an optional Gemini
voice ID; Enter chooses the built-in default, Algenib. Non-interactive runs
choose Algenib unless `--voice-id` is explicitly supplied. Later runs reuse
the saved ID and reject silent changes. Use a supported Google voice ID
(e.g. `Algenib`, `Kore`, or a configured `voice_...` ID).

```powershell
.\run.ps1 ejemplo Jorge --style profesional --voice-id Algenib
.\run.ps1 ejemplo2 Jorge --style profesional
```

Requires `OPENAI_API_KEY` for planning/STT, `GEMINI_API_KEY` for Gemini
TTS, FFmpeg for joining, and whatever normal keys the selected image/video
stages need. `--no-audio` skips V planning and Gemini. A planning
`--stop B2` also skips V/audio. `--from B2` rebuilds the downstream
professional voice from saved B planning checkpoints.

## Recovery and paid POST safety

State for each paid Gemini block is written under
`audio/runs/gemini-<fingerprint>/blocks/block_<id>/task.json` before
submission. Completed WAVs and STT transcripts are hashed; the existing
`audio/latest.json` and block timeline contract are preserved.
`--resume-audio` reuses verified completed artifacts, or attempts only
unsubmitted/missing portions of one pending run. A `tts_submitting_unknown`
block is **never retried blindly**: reconcile the charge and saved result
manually. `--regenerate-audio` explicitly starts a distinct paid audio
generation using the saved professional B2 plan and direction, after
rejecting any unresolved prior paid attempts.

OpenAI STT reconciliation follows the existing two-retry policy. **Gemini
block TTS is not silently regenerated** after exhausting STT corrections;
otherwise a paid unknown-result POST could be duplicated. Resolve that
failure explicitly. No live paid Gemini API smoke is implied by offline
tests; verify the selected voice and tagged multilingual style with a
controlled real test before production deployment.
