# Casero: Fish Audio, MiniMax or ElevenLabs voice sources

On the **first** use of a new casero avatar, choose its voice library
**before** supplying a Voice ID:

```text
Selecciona el proveedor de voz para Casero:
  1. Fish Audio
  2. MiniMax
  3. ElevenLabs
Selecciona 1, 2 o 3:
```

After choosing Fish, existing options are unchanged: enter an OpenSpeaker
Fish Voice ID, choose a consented reference recording for Fish cloning, or
choose the saved default. For MiniMax and ElevenLabs, enter an OpenSpeaker
Voice ID from the corresponding voice library. Their voices do **not** use
the Fish director and do not permit the Fish-specific cloning workflow.

For a non-interactive runner use `--tts-provider` with `--voice-id`:

```powershell
# First run, new avatar (illustrative IDs, replace with actual OpenSpeaker IDs)
.\run.ps1 --style casero --tts-provider minimax --voice-id YOUR_MINIMAX_ID ...
.\run.ps1 --style casero --tts-provider elevenlabs --voice-id YOUR_ELEVENLABS_ID ...
.\run.ps1 --style casero --tts-provider fishaudio --voice-id YOUR_FISH_ID ...
```

Existing avatars without `provider` in `meta/voice.json` are treated as
Fish Audio, never silently migrated to another provider. The voice source,
ID and mode are frozen in the per-avatar record and run voice snapshot.
Switching providers on an existing avatar is rejected; it requires explicit
reconfiguration/migration instead of quietly invalidating paid CTA assets.

The same OpenSpeaker `/v3/text-to-speech`, task polling and download client
handles all three voice libraries. Fish runs its per-block expressive director;
the other two pass the **original block text unchanged** to TTS, with no
Fish-specific tags and no Fish director API charge. Existing STT, block
joining, video timing and audio-only regeneration are reused.

The first-script ebook CTA is synthesized with the *same frozen voice library
and ID* as the main narration; the CTA cache also checks both fields so an
asset generated for another provider cannot be reused.

`--tts-provider` is only accepted with `--style casero`. There is no
interactive provider menu for Profesional, which keeps the existing Fish
Audio process. Saved older runs default their missing provider to Fish.

Provider labels describe the OpenSpeaker voice-source library. The common
TTS bridge does not guarantee it uses the original ElevenLabs or MiniMax
upstream speech model. No live account-specific voice list is inferred from
an ID alone; confirm IDs in the OpenSpeaker account's selected library.
