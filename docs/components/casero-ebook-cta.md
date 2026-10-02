# Casero-only reusable ebook CTA

## First avatar run

The existing avatar style remains immutable. When the selected style is `casero`,
the first planning run additionally requests one or two ebook cover PNG stems.
Put these files **directly in the active `data/` root** and enter only the
base name (for example `portada`, **not** `portada.png`).
The first is always the primary. For non-interactive calls supply:

```powershell
.\run.ps1 primer_guion Jorge --style casero --qr-url https://example.com/ebook --ebook-cover portada
.\run.ps1 primer_guion Jorge --style casero --qr-url https://example.com/ebook --ebook-cover principal --ebook-cover segundo
```

Only use ONE of these examples for first-time onboarding. Both selected PNGs
are snapshotted in `data/<avatar>/meta/ebook/` with SHA-256 metadata, so new
scripts do not depend on keeping the original uploads in the data root.
A changed original cover, QR, avatar image or voice is never silently applied
to the paid CTA cache.

## Independent detector, reusable first-script creative

Each new *casero* B1.1 run launches `CTA.md` concurrently with B1.1.
The output `CTA/output.json` stores two exact adjacent, unique sentences
and the character insertion index. This is per-script, **not** cached by avatar.
Professional mode does not run either CTA bot.

On the first full casero generation only, `CTA_EBOOK.md` receives the original
script and **primary cover only**. Its script is persisted in the avatar cache;
later scripts reuse that first script exactly and regenerate only the per-video
insertion position. The main narration script and B1.1/B1.2/B2 contracts are
not rewritten with sales text.

For the one-time media, the selected Fish/AI33 avatar voice generates CTA
speech. OpenAI Whisper word timestamps are saved separately. A GPT Image 2.5
edit (quality **medium**) attaches the primary cover, optional second cover,
then the avatar image **last**, with the user's exact one-/two-cover promotional
image prompts. The attached v4 QR-top motion design is parameterized for the
primary cover, actual QR and verified QR hostname.

The two attached example motion scripts are retained under
`scripts/cta/reference/`. The production adaptation lives in
`src/ai_video_factory/compositor/cta_motion.py`. Example-specific claims
(e.g. "NEW RELEASE" and hard-coded benefits) are intentionally excluded:
the first-run inputs do not establish such claims for another ebook.

## LTX and output

The CTA's first-half PCM audio (exactly half the sample count, using one
silence sample to make odd-length recordings even) conditions a dedicated
LTX A2V generation. On the shared Postgres queue, this request is published
**before all uncached normal B2 requests**. A2V frames occupy the first half
of CTA speech. The motion graphic occupies the second half plus exactly
**1500 ms** at the end. The resulting full CTA (speech plus 1.5s silence)
is retained under `data/<avatar>/meta/cta/final_cta.mp4` and reused.

For each casero video, the independently detected text boundary maps to the
saved narration's real word-level STT. A frame-aligned cut inserts the cached
CTA between the introduction and development, then resumes the original audio.
The original B2 output remains in the work folder for recovery; the published
final points to the version with the inserted CTA.

The durable request states prohibit automatically repeating TTS, image or
STT requests after uncertain billed outcomes. If a first run is interrupted,
inspect `meta/cta/*_state.json` before recovery. The avatar CTA may need its
original first script again only when the paid text request never completed.
No automated API calls are made when a fully verified cached CTA exists.

CLI `--no-audio` and stage `--stop` preserve their suppression of media
requests; they still allow one-time cover onboarding and per-script placement.
`--generate-video` and `--videos-only` can use a completed prepared cache
without repeating its text, TTS or image calls.

OpenAI planning and CTA image usage is included in the API ledger when first
generated. As before, AI33 TTS and OpenAI STT provider charges are distinct
from the project's reported planning/image total.

**Release verification:** these changes require unit tests plus a real
single-book and two-book smoke with configured OpenAI, AI33 and Salad before
merging. A source-level test cannot certify generated voice, book appearance
or paid GPU timing.
