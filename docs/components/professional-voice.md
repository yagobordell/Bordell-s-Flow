# Professional post-B2 voice: V1 → V2 → V3 → Gemini 3.8

This is the **professional** audio path. Casero is unchanged and continues to offer
Fish Audio, MiniMax and ElevenLabs. Professional no longer starts Fish at B1.1:
it waits for a **validated complete B2** output so the director can interpret
the narration against both the source text and the planned image/motion.
Before V1, adjacent source blocks with fewer than five beats merge
progressively (one-beat candidates first, then two, three and four). A
candidate joins the shorter immediate neighbour, preferring the preceding
block on a tie. Sizes are recalculated after each merge; if the whole film
has fewer than five beats, one voice block remains. B2 and all its original
beat identities stay unchanged for visuals/video.
Even B2 beats with `audio: false` receive narration (voice-over).

## Three stages and archived originals

The initial B8.1, B8.2, B8.3 documents supplied for this feature are preserved
under `docs/reference/professional-voice-original-b8-{1,2,3}.md`. The active
adaptations live under `src/ai_video_factory/bots/prompts/v{1,2,3}.md`.
B3 canonical scenes and B7.2 depictions do not exist in this repository's B2
contract; the active V2/V3 prompts instead operate on immutable B2 beat IDs.
They never invent scenes, pictures, timing or narration.

- **V1** reads the complete B2 film and assigns global curve and independent
  tempo and pressure baselines to each resulting voice block.
- **V2** runs per grouped voice block, distributes that block's argument and viewer
  trajectory across existing beats and modulates tempo/pressure locally.
  If the baseline genuinely fails to represent the beats, V2 reports it and
  V1 re-evaluates the whole film. A repeated unchanged baseline settles the
  axis.
- **V3** runs per grouped voice block, turns those intentions into spoken fragments with
  sustained `style`, exact source `speak` substrings and optional point-in-time
  **human vocal events**. It does not change a character of B2 narration.

New runs store the full structured requests and validated outputs beside the B
stages: `V1/pass_<n>/`, `V2/pass_<n>/block_<id>/` and
`V3/block_<id>/`. Each stage also exports its final
`merged_output.json`. These directories, including corrective inputs,
rejected attempts and immutable per-API-call token/duration records, are
published as `<avatar>/<script>/outputs/V1`, `outputs/V2` and
`outputs/V3`, alongside B1.1/B1.2/B2. The final delivered publication
contains the same files. Existing unfinished runs without a grouping checkpoint retain their
original one-to-one B2/audio mapping for safe recovery. Group plans
are recomputed when a corrected B2 changes source beat counts. Existing
unfinished runs still resume their
`voice_direction/V*` checkpoints without an extra paid call; those files
are mapped into the same public `outputs/V*` layout. A failed local response validation receives up to two explicitly
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
For multiword Studio voices, Google's exported prebuilt-voice request may
return 24-kHz mono `audio/L16;rate=24000`; the adapter wraps that PCM16
into a canonical WAV and retains its sample-rate/channel checks. Existing
single-token voices retain their original `AUDIO_WAV` request path.
One single-speaker GenerateContent request receives the styled parts for
one resulting voice block. The resulting WAV is retained per voice block
and Whisper-1 supplies word timestamps. The V1/voice_block_plan.json
checkpoint records each voice block's ordered original B2 block IDs and
beat IDs. The audio artifact publishes the same mapping in block_groups,
allowing the sample-accurate join and STT→B2 timeline to map each word
back to the original visual beat. The regular 500-ms join pause applies only
between resulting voice blocks, not inside a newly merged one. Real LTX
audio/video sync still uses measured STT timing, not V3 guesses.

## First run: choose a voice

For professional avatars, the voice is stored separately from Casero under
`data/<avatar>/meta/gemini_voice.json` (or the corresponding selected
avatar library root). The first interactive run requests an optional Gemini voice name or ID;
Enter chooses the built-in default, Algenib. Non-interactive runs also use
Algenib unless `--voice-id` is explicitly supplied. Google AI Studio names
containing spaces (e.g. `Uncle Enzo 3`) must be quoted in PowerShell. When an
API key is available, the launcher looks up that exact display name through
`GET /v1beta/voices` and stores the returned `voice_...` ID. Legacy single-token
IDs (`Algenib`, `Kore`, `voice_...`) continue to work directly.
Google must recognize the chosen voice for the configured API account.

```powershell
# First run: choose the custom Studio voice by its exact visible name.
.\run.ps1 ejemplo Jorge --style profesional --voice-id "Uncle Enzo 3"

# Later videos of this avatar reuse the saved name automatically.
.\run.ps1 ejemplo2 Jorge

# If this avatar already has a different professional voice, explicitly
# change it on a NEW script (never on an old resumed run).
.\run.ps1 ejemplo3 Jorge --voice-id "Uncle Enzo 3" --replace-professional-voice
```

Without `--replace-professional-voice`, trying to change a saved voice
still fails. Replacement requires `--voice-id` during a new B1.1 planning
run. Existing saved run voice snapshots remain unchanged and each Gemini
audio cache fingerprint includes the voice, so blocks from different voices
cannot be accidentally reused. Casero uses separate voice settings.

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
`--resume-audio` reuses verified completed artifacts and retries incomplete
blocks of one pending run. A failed block is recorded as retryable and never
as an ambiguous permanent state. `--regenerate-audio` explicitly starts a
distinct paid audio generation using the saved professional B2 plan and
direction.

OpenAI STT reconciliation follows the existing two-retry policy. Gemini
block TTS retries incomplete blocks when `--resume-audio` is requested. No
live paid Gemini API smoke is implied by offline tests; verify the selected
voice and tagged multilingual style with a controlled real test before
production deployment.

## V1 / V2 / V3 metrics and cost accounting

`run_report.json` reports individual V API call durations and the actual
wall-clock total for each stage. V2/V3 block calls execute concurrently:
their stage wall time is measured once, **not** the sum of per-block durations.
If V2 evidence sends V1 through another iteration, both V1 and V2 elapsed
times accumulate for their respective stages. A resumed cached response
does not create another paid-call record.

Every V API return persists only its Responses API model, tier, token usage
and response ID in `api_call_<n>.json` next to that attempt. This lets the
billing ledger reconcile the same calls during full runs, audio-only
regeneration or recovery, without double-charging archived responses.
Unparsed responses and uncertain request outcomes are reported with unknown
cost instead of being counted as free. OpenAI V planning uses the verified
same Luna token price calculation as B1.1/B1.2/B2; **Gemini TTS and
OpenAI STT remain excluded** from that OpenAI planning estimate, as before.
