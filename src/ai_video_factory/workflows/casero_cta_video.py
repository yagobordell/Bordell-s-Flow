"""Submit cached per-avatar CTA A2V ahead of B2 and insert it per-script."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ai_video_factory.bots.contracts import B11Output
from ai_video_factory.bots.cta import CTAPlacement, validate_placement
from ai_video_factory.bots.workflow import materialize_blocks
from ai_video_factory.compositor.static_preview import (
    _align_stt_tokens,
    _tokens,
    _verified_asset,
)
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.gpu_failures import DEFAULT_GPU_MAX_ATTEMPTS
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.inference.worker_image_routes import route_for_image
from ai_video_factory.providers.inference_jobs import (
    InferenceJobExecutor,
    cached_inference_response,
)
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_RECOMMENDED_MAX_SECONDS,
    LTX_A2V_STATIC_SAFE_GENERATION_PROFILE,
    LTX_A2V_TASK,
)
from ai_video_factory.workers.ltx25.jobs import ltx_a2v_application_job_id

_FPS = 24
_PROMPT = "A Person speaking to the camera."


class CTACompositionError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CTACompositionError(f"CTA missing or linked file: {path}")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise CTACompositionError(f"Invalid CTA JSON: {path}")
    return result


def _atomic(path: Path, obj: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise CTACompositionError("Linked CTA artifact destination")
    temp = path.with_name(path.name + ".part")
    if temp.exists() or temp.is_symlink():
        raise CTACompositionError("Unreconciled temporary CTA artifact")
    try:
        temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise CTACompositionError("CTA FFmpeg failed: " + result.stderr[-1000:])


def cta_assets(directory: Path) -> dict[str, Any]:
    identity = _read(directory/"identity.json")
    tts = _read(directory/"tts_state.json")
    stt = _read(directory/"stt_state.json")
    image = _read(directory/"image_state.json")
    v2_motion = directory/"motion_v2_state.json"
    motion = _read(v2_motion if v2_motion.is_file() else directory/"motion_state.json")
    for state in (tts, stt, image, motion):
        if state.get("status") != "completed":
            raise CTACompositionError("Incomplete casero CTA media asset")
    files = {}
    for state, key in (
        (tts,"master"), (tts,"first_half"), (stt,"transcript"),
        (image,"image"), (motion,"motion"),
    ):
        meta = state[key]
        file = directory / meta["file"]
        if file.is_symlink() or not file.is_file() or sha256_file(file) != meta["sha256"]:
            raise CTACompositionError(f"CTA cache asset modified: {file}")
        files[key] = file
    if not (0 < tts["half_seconds"] <= LTX_A2V_RECOMMENDED_MAX_SECONDS):
        raise CTACompositionError("CTA first-half speech exceeds pinned LTX A2V duration")
    return {**identity, "files": files, "half_seconds": float(tts["half_seconds"]),
            "duration_seconds": float(tts["duration_seconds"])}


def _cta_request(
    directory: Path, assets: dict[str, Any], *, worker_image: str | None = None,
) -> InferenceJobRequest:
    image_sha = sha256_file(assets["files"]["image"])
    audio_sha = sha256_file(assets["files"]["first_half"])
    job_id = ltx_a2v_application_job_id(
        segment_id="casero-ebook-cta", prompt=_PROMPT,
        image_sha256=image_sha, audio_sha256=audio_sha, seed=4242,
        width=1280, height=720, fps=_FPS,
        generation_profile=LTX_A2V_STATIC_SAFE_GENERATION_PROFILE,
    )
    if worker_image:
        job_id += "-im" + worker_image.rsplit(":", 1)[-1][:16]
    return InferenceJobRequest(
        job_id=job_id,
        task=(
            route_for_image("ltx25", worker_image).task_name
            if worker_image else LTX_A2V_TASK
        ), max_attempts=DEFAULT_GPU_MAX_ATTEMPTS,
        inputs=[
            ObjectInput(name="image", key=f"cta/inputs/images/{image_sha}.png",
                        sha256=image_sha, content_type="image/png"),
            ObjectInput(name="audio", key=f"cta/inputs/audio/{audio_sha}.wav",
                        sha256=audio_sha, content_type="audio/wav"),
        ],
        output=ObjectOutput(key=f"jobs/{job_id}/cta_avatar.mp4",content_type="video/mp4"),
        sidecar_outputs={"metadata": ObjectOutput(
            key=f"jobs/{job_id}/metadata.json",content_type="application/json")},
        parameters={
            "generation_profile": LTX_A2V_STATIC_SAFE_GENERATION_PROFILE,
            "prompt": _PROMPT, "seed": 4242, "width": 1280, "height": 720,
            "fps": _FPS,
            **({"required_worker_image": worker_image} if worker_image else {}),
        },
    )


def submit_cta_first(
    directory: Path, executor: InferenceJobExecutor, queue: Any,
    *, worker_image: str | None = None,
) -> tuple[Any, Any] | None:
    """Submit before ALL normal B2 queue.submit calls, not merely wait first."""
    if (directory/"completed.json").is_file():
        ready = _read(directory/"completed.json")
        file = directory/ready.get("final", {}).get("file", "")
        if not file.is_file() or sha256_file(file) != ready["final"]["sha256"]:
            raise CTACompositionError("Stored reusable CTA is missing or modified")
        return None
    assets = cta_assets(directory)
    request = _cta_request(directory, assets, worker_image=worker_image)
    state_file = directory / (
        "a2v_state_" + worker_image.rsplit(":", 1)[-1][:16] + ".json"
        if worker_image else "a2v_state.json"
    )
    if state_file.is_file():
        state = _read(state_file)
        if state.get("request_sha256") != request.fingerprint():
            raise CTACompositionError("CTA A2V fingerprint changed; no paid resubmission")
    else:
        _atomic(state_file, {
            "status": "prepared", "job_id": request.job_id,
            "request_sha256": request.fingerprint(),
        })
    if cached_inference_response(executor.storage, request) is None:
        for item in request.inputs:
            source = assets["files"]["first_half" if item.name=="audio" else "image"]
            executor.ensure_input(source, key=item.key, sha256=item.sha256,
                                  content_type=item.content_type,
                                  metadata={"purpose":"casero-cta-a2v"})
        queue.submit(request, metadata={"capability":"casero-cta-a2v"})
        _atomic(state_file, {
            "status": "submitted", "job_id": request.job_id,
            "request_sha256": request.fingerprint(),
        })
    return request, assets


def finish_cta(directory: Path, submitted: tuple[Any, Any] | None,
               executor: InferenceJobExecutor) -> Path:
    if submitted is None:
        record = _read(directory/"completed.json")
        return directory/record["final"]["file"]
    request, assets = submitted
    video = directory/"avatar_a2v.mp4"
    worker_image = request.parameters.get("required_worker_image")
    a2v_state = directory / (
        "a2v_state_" + worker_image.rsplit(":", 1)[-1][:16] + ".json"
        if isinstance(worker_image, str) else "a2v_state.json"
    )
    if not video.is_file():
        response = executor.execute(request, metadata={"capability":"casero-cta-a2v"})
        temporary = directory/"avatar_a2v.part.mp4"
        if temporary.is_symlink():
            raise CTACompositionError("Unsafe CTA A2V temporary path")
        executor.download_output(response, temporary)
        from ai_video_factory.workflows.b_video import B2VideoJob, _probe_video
        expected = B2VideoJob(
            kind="a2v", beats=("CTA",), first_index=0, last_index=0,
            start_seconds=0.0, end_seconds=assets["half_seconds"],
            image=assets["files"]["image"], audio=assets["files"]["first_half"],
            request=request,
        )
        _probe_video(temporary, expected)
        # Same pinned static-camera proof required by ordinary A2V jobs.
        metadata = request.sidecar_outputs["metadata"]
        stat = executor.storage.stat(metadata.key)
        if stat is None:
            raise CTACompositionError("CTA A2V metadata sidecar is missing")
        sidecar = directory/"avatar_a2v.metadata.json"
        executor.storage.download(metadata.key, sidecar)
        if sha256_file(sidecar) != stat.metadata.get("artifact-sha256"):
            raise CTACompositionError("CTA A2V metadata sidecar differs")
        data = _read(sidecar)
        if (data.get("generation_profile") != LTX_A2V_STATIC_SAFE_GENERATION_PROFILE
            or data.get("negative_prompt_applied") is not True
            or data.get("positive_prompt_enhanced") is not False):
            raise CTACompositionError("CTA A2V did not use pinned static profile")
        temporary.replace(video)
        _atomic(a2v_state, {
            "status": "completed", "job_id": request.job_id,
            "request_sha256": request.fingerprint(), "file": video.name,
            "sha256": sha256_file(video),
        })
    else:
        saved = _read(a2v_state)
        if saved.get("status") != "completed" or saved.get("sha256") != sha256_file(video):
            raise CTACompositionError("Saved CTA A2V clip lacks a validated checkpoint")
    return compose_reusable_cta(directory, assets)


def compose_reusable_cta(directory: Path, assets: dict[str, Any] | None = None, *,
                         upgrade_legacy: bool = False) -> Path:
    previous: dict[str, Any] | None = None
    if (directory/"completed.json").is_file():
        previous = _read(directory/"completed.json")
        path = directory/previous["final"]["file"]
        if path.is_symlink() or not path.is_file() or (
            sha256_file(path) != previous["final"]["sha256"]
        ):
            raise CTACompositionError("Reusable CTA digest changed")
        if not upgrade_legacy:
            return path
        if previous.get("schema_version") != "casero-cta-final-v1":
            raise CTACompositionError("Only a legacy CTA can be locally upgraded")
    if assets is None:
        assets = cta_assets(directory)
    half = assets["half_seconds"]
    duration = assets["duration_seconds"] + 1.5
    first_frames = round(half*_FPS)
    total_frames = round(duration*_FPS)
    tail_frames = total_frames-first_frames
    if first_frames < 1 or tail_frames < 1:
        raise CTACompositionError("CTA media frame count invalid")
    destination = directory/("final_cta_v2.mp4" if upgrade_legacy else "final_cta.mp4")
    temp = directory/("final_cta_v2.part.mp4" if upgrade_legacy else "final_cta.part.mp4")
    if temp.exists() or temp.is_symlink() or destination.is_symlink():
        raise CTACompositionError("Unreconciled CTA final composition")
    # First 50% of speech uses A2V. The other 50% plus 1500ms uses QR motion.
    _ffmpeg([
        "-i", str(directory/"avatar_a2v.mp4"),
        "-i", str(assets["files"]["motion"]),
        "-i", str(assets["files"]["master"]),
        "-filter_complex",
        f"[0:v]fps={_FPS},scale=1280:720,tpad=stop_mode=clone:stop_duration=0.3,"
        f"trim=end_frame={first_frames},setpts=N/({_FPS}*TB)[a];"
        f"[1:v]fps={_FPS},scale=1280:720,tpad=stop_mode=clone:stop_duration=0.3,"
        f"trim=end_frame={tail_frames},setpts=N/({_FPS}*TB)[m];"
        "[a][m]concat=n=2:v=1:a=0,format=yuv420p[v];"
        "[2:a]apad=pad_dur=1.5[audio]",
        "-map","[v]","-map","[audio]", "-frames:v",str(total_frames),
        "-c:v","libx264","-preset","veryfast","-crf","19","-r",str(_FPS),
        "-pix_fmt","yuv420p","-c:a","aac","-b:a","192k",
        "-t",f"{duration:.9f}","-movflags","+faststart",str(temp),
    ])
    temp.replace(destination)
    v2_motion = (directory / "motion_v2_state.json").is_file()
    completion = {
        "schema_version": "casero-cta-final-v2" if v2_motion else "casero-cta-final-v1",
        "status": "completed",
        "duration_seconds": duration, "speech_seconds": assets["duration_seconds"],
        "first_half_seconds": half, "trailing_silence_seconds": 1.5,
        "final": {"file": destination.name, "sha256": sha256_file(destination)},
    }
    if v2_motion:
        completion["motion_copy_sha256"] = sha256_file(directory / "motion_copy.json")
    if upgrade_legacy:
        # Preserve the original finished video until a verified replacement exists.
        # Never remove old files, paid media or the old completion reference mid-render.
        saved = _read(directory / "completed.json")
        if saved != previous:
            raise CTACompositionError("CTA completion changed during local upgrade")
        temp_record = directory / "completed.json.v2.part"
        if temp_record.exists() or temp_record.is_symlink():
            raise CTACompositionError("Unreconciled CTA upgrade checkpoint")
        try:
            temp_record.write_text(
                json.dumps(completion, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp_record.replace(directory / "completed.json")
        finally:
            temp_record.unlink(missing_ok=True)
    else:
        _atomic(directory/"completed.json", completion)
    return destination


def upgrade_cached_cta(directory: Path) -> Path:
    """Upgrade a v1 CTA with local A2V+speech only, preserving paid artifacts."""
    legacy = _read(directory / "completed.json")
    if legacy.get("schema_version") != "casero-cta-final-v1":
        raise CTACompositionError("CTA upgrade requires legacy v1 completion")
    old_clip = directory / legacy["final"]["file"]
    if (old_clip.is_symlink() or not old_clip.is_file()
            or sha256_file(old_clip) != legacy["final"]["sha256"]):
        raise CTACompositionError("Legacy finished CTA changed or missing")
    a2v_state = _read(directory / "a2v_state.json")
    original_a2v = directory / "avatar_a2v.mp4"
    if (a2v_state.get("status") != "completed" or
            original_a2v.is_symlink() or not original_a2v.is_file() or
            sha256_file(original_a2v) != a2v_state.get("sha256")):
        raise CTACompositionError(
            "Legacy CTA A2V cannot be verified locally. Original CTA preserved; "
            "reconcile avatar_a2v.mp4 without resubmitting a paid job."
        )
    if not (directory / "motion_v2_state.json").is_file():
        raise CTACompositionError("Missing validated new CTA motion")
    return compose_reusable_cta(directory, cta_assets(directory), upgrade_legacy=True)


def insertion_second(
    output: Path, source_script: str, placement: CTAPlacement,
    plan: dict[str, Any], audio: dict[str, Any],
) -> float:
    """Map unique CTA text boundary to actual source STT without timing guesses."""
    cut = validate_placement(source_script, placement)
    b11 = B11Output.model_validate(_read(output/"B1.1"/"output.json"))
    blocks = materialize_blocks(source_script, b11.blocks)
    offset = 0
    for index, block in enumerate(blocks):
        end = offset+len(block.text)
        if cut <= end:
            local = cut-offset
            expected = _tokens(block.text)
            spoken_before = len(_tokens(block.text[:local]))
            if spoken_before == 0:
                return float(audio["block_start_seconds"][index])
            if spoken_before == len(expected):
                return float(audio["block_start_seconds"][index] +
                             audio["block_durations_seconds"][index] +
                             (0.5 if index<len(blocks)-1 else 0.0))
            record = audio["blocks"][index]
            transcript = _read(_verified_asset(output, record["stt"]["file"],
                                               record["stt"]["sha256"]))
            aligned = [
                {"text": token, "start": word["start"], "end": word["end"]}
                for word in transcript["words"] for token in _tokens(word["word"])
            ]
            mapping = _align_stt_tokens(
                expected,[word["text"] for word in aligned],block_id=block.block_id,
            )
            following = next(
                (mapping[n] for n in range(spoken_before,len(expected))
                 if mapping[n] is not None),None,
            )
            if following is None:
                raise CTACompositionError("CTA STT following-word boundary unavailable")
            return float(audio["block_start_seconds"][index]+aligned[following]["start"])
        offset = end
    raise CTACompositionError("CTA insertion point escapes source script")


def insert_cta(
    output: Path, source_script: str, plan: dict[str, Any], audio: dict[str, Any],
    main_video: Path, directory: Path, destination: Path,
) -> dict[str, Any]:
    placement_data = _read(output/"CTA"/"output.json")
    placement = CTAPlacement.model_validate({
        "previous_sentence": placement_data["previous_sentence"],
        "next_sentence": placement_data["next_sentence"],
    })
    cut = insertion_second(output, source_script, placement, plan, audio)
    main_duration = float(audio["duration_seconds"])
    # 24-fps cuts are a video-frame constraint; place the speech cut at the
    # closest frame, while retaining its precise measured STT boundary in metadata.
    frame = round(cut*_FPS)
    if frame <= 0 or frame >= round(main_duration*_FPS):
        raise CTACompositionError("CTA speech boundary lies outside main video")
    snapped = frame/_FPS
    clip = compose_reusable_cta(directory)
    cache = _read(directory/"completed.json")
    cta_duration = float(cache["duration_seconds"])
    temp = destination.with_suffix(".part.mp4")
    if temp.is_symlink() or temp.exists() or destination.is_symlink():
        raise CTACompositionError("Unsafe CTA insertion output")
    main_master = _verified_asset(output,audio["file"],audio["sha256"])
    _ffmpeg([
        "-i",str(main_video),"-i",str(clip),"-i",str(main_master),
        "-filter_complex",
        f"[0:v]trim=end_frame={frame},setpts=PTS-STARTPTS[bv];"
        "[1:v]setpts=PTS-STARTPTS[cv];"
        f"[0:v]trim=start_frame={frame},setpts=PTS-STARTPTS[av];"
        "[bv][cv][av]concat=n=3:v=1:a=0,format=yuv420p[v];"
        f"[2:a]atrim=end={snapped:.9f},asetpts=PTS-STARTPTS[ba];"
        "[1:a]asetpts=PTS-STARTPTS[ca];"
        f"[2:a]atrim=start={snapped:.9f},asetpts=PTS-STARTPTS[aa];"
        "[ba][ca][aa]concat=n=3:v=0:a=1[a]",
        "-map","[v]","-map","[a]","-c:v","libx264","-preset","veryfast",
        "-crf","19","-r",str(_FPS),"-pix_fmt","yuv420p",
        "-c:a","aac","-b:a","192k","-movflags","+faststart",str(temp),
    ])
    temp.replace(destination)
    return {
        "file":destination.relative_to(output).as_posix(),
        "sha256":sha256_file(destination),
        "cta_insertion":{
            "source_boundary_seconds":cut,
            "video_frame":frame,"video_cut_seconds":snapped,
            "cta_duration_seconds":cta_duration,
            "avatar_cache_file":str(clip),
            "avatar_cache_sha256":sha256_file(clip),
        },
    }
