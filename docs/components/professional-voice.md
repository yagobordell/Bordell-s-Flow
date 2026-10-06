# Professional post-B2 voice: V1 → V2 → V3 → V4 → Gemini 3.8

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

## Four active voice stages

The only prompts for **new** professional voice runs are
`src/ai_video_factory/bots/prompts/v{1,2,3,4}.md`. The original
B8 reference documents live in Git history, not in the current tree.
The obsolete legacy V3 code and its competing tag compiler have been removed. Old
legacy-V3 checkpoints are preserved but explicitly rejected when resuming;
start a new modern V3→V4 voice run instead. Existing completed paid audio
files are never automatically regenerated.
The current B3 is a visual camera-continuity stage, not the historical B8
"canonical scene" concept. V2/V3 voice planning still operates on immutable B2
beat IDs and narration; B3 camera cuts do not rewrite the speech text or vocal
ownership and therefore do not become voice-direction input.

- **V1** reads the complete B2 film and assigns global curve and independent
  tempo and pressure baselines to each resulting voice block.
- **V2** runs per grouped voice block, distributes that block's argument and viewer
  trajectory across existing beats and modulates tempo/pressure locally.
  If the baseline genuinely fails to represent the beats, V2 reports it and
  V1 re-evaluates the whole film. A repeated unchanged baseline settles the
  axis.
- **V3** is solely the artistic director: block-wide vocal register, pace,
  intensity movement, verbatim spoken substrings, endings, stress, functional
  pauses and rare **semantic** human intentions. V3 must never emit an inline
  tag or technical code, even in its delivery, register or pause prose.
- **V4** is the only technical compiler and the only owner of all inline-tag
  syntax, selection and insertion. It translates validated V3 choices into one
  Gemini style per full group plus exactly sourced punctual tags; it must not
  independently change the artistic interpretation. The V3 validator now
  rejects leaked markup in every prose field, not just altered transcript.
  A contaminated modern V3 checkpoint is preserved for diagnosis and only
  its affected block is recompiled. Legacy V3 checkpoints are no longer
  executable and fail closed, preserving previously purchased audio files.

New runs store the full structured requests and validated outputs beside the B
stages: `V1/pass_<n>/`, `V2/pass_<n>/block_<id>/` and
`V3/block_<id>/` and `V4/block_<id>/`. Each stage also exports its final
`merged_output.json`. These directories, including corrective inputs,
rejected attempts and immutable per-API-call token/duration records, are
published as `<avatar>/<script>/outputs/V1`, `outputs/V2` and
`outputs/V3` and `outputs/V4`, alongside B1.1/B1.2/B2. The final delivered publication
contains the same files. Existing unfinished runs without a grouping checkpoint retain their
original one-to-one B2/audio mapping for safe recovery. Group plans
are recomputed when a corrected B2 changes source beat counts. Existing
unfinished runs still resume their
`voice_direction/V*` checkpoints without an extra paid call; those files
are mapped into the same public `outputs/V*` layout. A failed local response validation receives up to two explicitly
described correction calls through OpenAI Responses API. One block's V2/V3/V4
validation failure never reruns successfully validated adjacent blocks within
that pass. Re-entry from V2's evidence may require reevaluating multiple
blocks because V1 governs the global curve.

## V4 Gemini compilation: final authority

V3's output is artistic context only. V4 alone authors the final narrator
profile, whole-group directors_notes, optional local spoken_directions, and
all inline technical tags. It may ignore, modify, or contradict every V3
instruction, including stress, endings, delivery character, human behavior
and planned silence. No validator compares V4's output against V3, and no
code adds any fallback V3 pause or direction to V4's final style.

V4 emits exactly ONE style and ONE full original B2 transcript per voice
group (possibly containing several beats). Each beat has text="", with
optional inserts containing only a supported inline tag and the immediately
neighboring original words. The runtime validates each selected tag's exact
B2 character offset, supported token and minimal source-word excerpt,
then reconstructs the original spoken text exclusively from B2. This
technical validation does not evaluate V3's artistic preferences.

Older tag-generating V3 checkpoints are preserved but may not resume
through the active pipeline. Start a fresh modern V3→V4 run; completed
paid audio files are not deleted or automatically replayed. A distinct
compiler version prevents recycling paid audio created under the obsolete
V4 authority contract.

Gemini remains `gemini-3.8-flash-tts`; voice selection, original B2 beat
identities, STT and join behavior are unaffected. Every professional Gemini
style sent to TTS is prefixed with
`Respect the next style indications while keeping a normal voice speed, not paused, its for a youtube video:`
before the V4-authored style.

Reference: https://ai.google.dev/gemini-api/docs/speech-generation

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

## V1 / V2 / V3 / V4 metrics and cost accounting

`run_report.json` reports individual V API call durations and the actual
wall-clock total for each stage. V2/V3/V4 block calls execute concurrently:
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


## B8-to-V audit: scope and silence fidelity

B8.1 originally sees the whole film; V1 still sees the whole B2 film, but assigns baselines to the resulting voice groups. B8.2 originally sees B3 canonical scene scopes and B7.2 finished depictions, neither of which exists in this B2 pipeline. V2 deliberately uses immutable B2 beats as local intent units and one overall landing per resulting voice group, preserving the B8.2 separation between cognitive and attitudinal change and relative tempo/pressure modulation. V3 receives each full voice group and treats B2's imagery strictly as planned depiction, not observed completed footage. These are transparent adaptations, not a literal scene-level port of the original B8 documents.

V4 retains B8.4's group-wide style principle while taking final authority
over technical tags and performance wording. V3 supplies context only.
B2 remains the nonnegotiable source of narration words and beat order.
No V3-derived condition is a reason to reject, rewrite, or retry V4.

## V4 is final authority (sovereign contract v4)

V3 is advisory artistic context only, not a set of requirements. V4 has final authority to include, ignore or contradict every V3 direction, including stresses, endings, silent pauses, human vocalizations and their positions. The compiler NEVER compares a V4 decision to V3, NEVER retries because V4 diverged from V3, and NEVER injects fallback pauses or style prose from V3. V4 outputs one final style per entire B2 voice group plus optional local notes and independently selected inline tags. Its sparse per-beat text fields always remain empty, with only optional short, B2-offset-anchored snippets for selected tags. Validations are exclusively technical: match B2 beats/words, supported Gemini tags, exact boundary and minimal excerpt, no duplicate identical tag at a boundary, and provider size limits. Compiler fingerprint v4 prevents recycling audio built under the superseded contract.
