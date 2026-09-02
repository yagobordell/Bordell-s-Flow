# Canonical Phase 7 keyframe

`shot_001.png` is intentionally not duplicated in Git. The exact keyframe is embedded in the
published recovery image. Do not overwrite this tag:

```text
docker.io/yagobordell/ai-video-factory-benchmark:phase7-ltx25-torch211-cu128-natten0216-v8-salad
```

On a new Windows machine, recover it after cloning the repository:

```powershell
.\scripts\resume_phase7_benchmark.ps1 -Action RecoverKeyframe
```

The script pulls the immutable recovery checkpoint, copies the file from
`/opt/factory/docker/phase7-benchmark/assets/shot_001.png`, removes only its temporary container and
verifies that the recovered file is not empty. Run this action before rebuilding a later image.
