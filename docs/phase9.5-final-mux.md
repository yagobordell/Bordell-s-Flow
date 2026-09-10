# Phase 9.5 — Final narration mux

Status: implemented, pending canonical local mux and audiovisual validation.

Phase 9.5 combines the closed Phase 9.4 motion render with the canonical Phase 5 narration WAV and
produces the first complete audiovisual artifact. It does not alter shot timing, caption timing,
visual frame count or narration playback speed.

## Inputs and outputs

```text
data/output/phase9/composition_plan.json
        |
data/output/phase9/visual_motion.mp4
        |
data/output/phase5/narration.json
        |
data/output/phase5/narration.wav
        v
Phase 9.5 validation + ffmpeg mux
        |
        +--> data/output/phase9/final_video.mp4
        |
        +--> data/output/phase9/final_video.json
```

`composition_plan.json` remains the canonical frame timeline. At the current profile it defines 1080
frames at 24 fps, therefore the authoritative final duration is exactly 45.000 seconds.

`NarrationAudio` remains the canonical narration metadata, while the physical WAV is the media asset
actually muxed. Both the measured WAV duration and its recorded metadata must remain within one video
frame of the composition duration, and they must remain within one video frame of each other. A
mismatch larger than that is rejected rather than corrected by stretching or retiming audio.

The tolerance is intentionally expressed in compositor time (one video frame, 1/24 s for the current
profile), not in one audio sample. The first canonical Phase 9.5 local attempt exposed that requiring
sample-exact equality between historical `narration.json` metadata and the WAV was unnecessarily
strict even though the compositor operates on a 24 fps timeline. The WAV itself still has to fit the
canonical timeline within one frame.

A second local attempt exposed an additional WAV-container detail from the Phase 5 speech response:
the streaming WAV can use `0xFFFFFFFF` as the `data` chunk size sentinel while the physical file is
much shorter. Python's `wave.getnframes()` then reports 2,147,483,647 frames for 16-bit mono PCM, which
at 24 kHz looks like 89,478.485 seconds. Phase 9.5 therefore follows the same rule already used by the
Phase 5 generator: it reads the PCM frames actually present in the file and derives duration from the
bytes read, never from the placeholder frame count in the streaming header.

## Mux policy

The Phase 9.4 H.264 video is already the accepted visual master. Phase 9.5 therefore stream-copies it
with FFmpeg instead of decoding and encoding it again:

```text
video: H.264 -> stream copy
```

The narration WAV cannot be stored as raw PCM in the intended delivery MP4 profile, so only audio is
encoded:

```text
audio: canonical WAV -> AAC @ 192 kbps
```

The canonical command shape is:

```text
ffmpeg
  -i visual_motion.mp4
  -i narration.wav
  -map 0:v:0
  -map 1:a:0
  -c:v copy
  -c:a aac
  -b:a 192k
  -t 45.000000
  -movflags +faststart
  -map_metadata -1
  final_video.mp4
```

`-t` is tied to `total_frames / fps`, not to an independently rounded audio duration. It protects the
closed 1080-frame visual timeline from accidental extension by AAC packet padding. The mux does not
use `-shortest`, because a slightly shorter narration track must not truncate the canonical visual
master.

`+faststart` moves the MP4 index to the beginning of the file for normal progressive playback.
Incidental input metadata is dropped with `-map_metadata -1`; canonical project metadata remains in
the repository artifacts rather than depending on container tags.

## FinalVideo contract

Phase 9.5 promotes the finished asset to the small provider-neutral domain contract:

```text
FinalVideo:
  uri
  duration_seconds
```

The default metadata artifact is:

```json
{
  "uri": "final_video.mp4",
  "duration_seconds": 45.0
}
```

The contract intentionally excludes FFmpeg flags, AAC bitrate, Remotion details and Phase 8/Salad
state. Those are implementation details rather than downstream domain identity.

## Validation

Before muxing, Python checks that:

1. `visual_motion.mp4` is H.264 at the plan dimensions and fps;
2. the visual contains exactly the canonical frame count and no audio stream;
3. `narration.wav` is a valid WAV with positive sample rate, channels and actual PCM frames;
4. WAV duration is measured from PCM frames actually read, not from a streaming size sentinel;
5. the measured WAV duration is within one video frame of `narration.json` metadata;
6. the measured WAV duration is within one video frame of the canonical composition duration;
7. `narration.json` names the selected WAV asset.

The runner prints the measured WAV duration and the metadata duration separately so any accepted
rounding difference remains visible during local validation.

After muxing, Python checks that:

1. the final video remains H.264 at the canonical dimensions and fps;
2. it still contains exactly 1080 frames;
3. it contains exactly one audio stream;
4. the audio codec is AAC;
5. the video duration remains within one frame of the canonical 45.000 seconds;
6. AAC stream duration remains within two video frames, allowing normal AAC packet rounding.

No automatic time-stretch, silence insertion, frame duplication or frame dropping is permitted.

## Command

From the repository root:

```powershell
python scripts/run_phase9_final.py
```

To validate all inputs and inspect the exact FFmpeg command without creating the final MP4:

```powershell
python scripts/run_phase9_final.py --prepare-only
```

The default artifacts are:

```text
data/output/phase9/final_video.mp4
data/output/phase9/final_video.json
```

The default AAC bitrate can be changed for an experiment with:

```text
--audio-bitrate-kbps
```

The 192 kbps value remains the canonical Phase 9.5 validation profile.

## Closure criteria

Phase 9.5 closes after the canonical local run confirms:

1. input validation accepts the closed 9.4 render and canonical Phase 5 narration;
2. FFmpeg stream-copies the H.264 video and encodes exactly one AAC narration stream;
3. `final_video.mp4` remains 768x1280, 24 fps and exactly 1080 frames;
4. final video duration remains 45.000 seconds within one-frame container tolerance;
5. `final_video.json` contains a valid `FinalVideo` contract pointing at the output MP4;
6. audiovisual inspection confirms narration starts at the expected timeline origin;
7. active-word highlighting tracks the audible narration consistently across the video;
8. the final word and final visual frame complete without an audible or visual truncation artifact;
9. Python Ruff, pytest and PowerShell syntax checks pass in CI.

Once these criteria pass, Phase 9 has a complete final audiovisual artifact. Further export presets,
platform-specific metadata or loudness mastering should be treated as later delivery concerns rather
than changes to the canonical compositor timeline.
