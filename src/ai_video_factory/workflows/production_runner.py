from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

StageStatus = Literal["current", "adoptable", "pending", "stale", "blocked"]
RecordOrigin = Literal["executed", "adopted"]
StageResource = Literal["cpu", "gpu"]

PRODUCTION_STAGE_NAMES = (
    "phase2-narrative",
    "phase3-continuity",
    "phase3-shots",
    "phase4-reference-prompts",
    "phase4-reference-assets",
    "phase5-narration",
    "phase5-alignment",
    "phase5-beat-timing",
    "phase5-shot-timing",
    "phase6-storyboard",
    "phase8-video-prompts",
    "phase6-keyframes",
    "phase8-videos",
    "phase8-upscale",
)


@dataclass(frozen=True, slots=True)
class ProductionStage:
    """One resumable production stage backed by an existing validated phase script."""

    name: str
    description: str
    inputs: tuple[Path, ...]
    outputs: tuple[Path, ...]
    script: Path
    arguments: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    resource: StageResource = "cpu"
    resource_key: str | None = None

    def command(self, python_executable: str = sys.executable) -> tuple[str, ...]:
        return (python_executable, self.script.as_posix(), *self.arguments)


class ProductionStageRecord(BaseModel):
    """Fingerprints proving that a persisted stage still matches its current inputs."""

    model_config = ConfigDict(extra="forbid")

    stage_name: str
    spec_sha256: str
    input_sha256: str
    output_sha256: str
    origin: RecordOrigin
    command: list[str] = Field(default_factory=list)


class ProductionRunManifest(BaseModel):
    """Operational resume state; domain artifacts remain the source of production content."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = "2"
    stages: dict[str, ProductionStageRecord] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StageInspection:
    stage: ProductionStage
    status: StageStatus
    reason: str
    spec_sha256: str | None = None
    input_sha256: str | None = None
    output_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ProductionStageMetric:
    stage_name: str
    outcome: Literal["executed", "adopted", "skipped"]
    resource: StageResource
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class ProductionRunSummary:
    executed: tuple[str, ...]
    adopted: tuple[str, ...]
    skipped: tuple[str, ...]
    metrics: tuple[ProductionStageMetric, ...] = ()
    total_elapsed_seconds: float = 0.0


class StageExecutor(Protocol):
    def __call__(self, stage: ProductionStage) -> None: ...


class ProductionStageBlocked(RuntimeError):
    def __init__(self, stage: ProductionStage, missing: tuple[Path, ...]) -> None:
        rendered = ", ".join(path.as_posix() for path in missing)
        super().__init__(f"Stage {stage.name} is blocked by missing inputs: {rendered}")
        self.stage = stage
        self.missing = missing


class SubprocessStageExecutor:
    """Execute stage subprocesses and make active process trees cancellable."""

    def __init__(self, *, repo_root: Path, python_executable: str = sys.executable) -> None:
        self._repo_root = repo_root
        self._python_executable = python_executable
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._process_lock = threading.Lock()

    def __call__(self, stage: ProductionStage) -> None:
        self.run_command(stage.command(self._python_executable))

    def run_command(self, command: tuple[str, ...] | list[str]) -> None:
        kwargs: dict[str, object] = {
            "cwd": self._repo_root,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        process = subprocess.Popen(command, **kwargs)
        with self._process_lock:
            self._processes.add(process)
        try:
            returncode = process.wait()
            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, command)
        finally:
            with self._process_lock:
                self._processes.discard(process)

    def cancel_running(self) -> None:
        """Terminate every active stage process tree so DAG failures are fail-fast."""

        with self._process_lock:
            processes = tuple(self._processes)

        for process in processes:
            if process.poll() is not None:
                continue
            if os.name == "nt":
                subprocess.run(
                    [
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    continue

        for process in processes:
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class ProductionRunner:
    """Inspect persisted artifacts, adopt valid historical runs, and execute stale stages."""

    def __init__(
        self,
        stages: list[ProductionStage],
        *,
        manifest_path: Path,
        repo_root: Path = Path("."),
        executor: StageExecutor | None = None,
        max_workers: int = 1,
        max_gpu_stages: int = 1,
    ) -> None:
        if not stages:
            raise ValueError("Production runner requires at least one stage")
        if max_workers < 1:
            raise ValueError("Production runner max_workers must be at least 1")
        if max_gpu_stages < 1:
            raise ValueError("Production runner max_gpu_stages must be at least 1")
        names = [stage.name for stage in stages]
        if len(names) != len(set(names)):
            raise ValueError("Production stage names must be unique")
        known_names = set(names)
        for stage in stages:
            unknown = set(stage.dependencies).difference(known_names)
            if unknown:
                rendered = ", ".join(sorted(unknown))
                raise ValueError(f"Stage {stage.name} has unknown dependencies: {rendered}")
            if stage.name in stage.dependencies:
                raise ValueError(f"Stage {stage.name} cannot depend on itself")
            if stage.resource_key is not None and not stage.resource_key.strip():
                raise ValueError(f"Stage {stage.name} has an empty resource_key")

        self._stages = stages
        self._manifest_path = manifest_path
        self._repo_root = repo_root
        self._executor = executor or SubprocessStageExecutor(repo_root=repo_root)
        self._max_workers = max_workers
        self._max_gpu_stages = max_gpu_stages

    @property
    def stages(self) -> tuple[ProductionStage, ...]:
        return tuple(self._stages)

    def plan(self, *, through: str | None = None) -> list[StageInspection]:
        manifest = self._load_manifest()
        return [self._inspect(stage, manifest) for stage in self._selected_stages(through)]

    def run(
        self,
        *,
        through: str | None = None,
        force_stages: set[str] | None = None,
    ) -> ProductionRunSummary:
        """Run the selected production DAG with bounded, dependency-aware concurrency."""

        started_at = time.monotonic()
        force = force_stages or set()
        unknown_force = force.difference(stage.name for stage in self._stages)
        if unknown_force:
            rendered = ", ".join(sorted(unknown_force))
            raise ValueError(f"Unknown forced production stages: {rendered}")

        selected = self._selected_stages(through)
        selected_names = {stage.name for stage in selected}
        for stage in selected:
            outside = set(stage.dependencies).difference(selected_names)
            if outside:
                rendered = ", ".join(sorted(outside))
                raise ValueError(
                    f"Selected run excludes dependencies required by {stage.name}: {rendered}"
                )

        manifest = self._load_manifest()
        executed: list[str] = []
        adopted: list[str] = []
        skipped: list[str] = []
        metrics: list[ProductionStageMetric] = []
        completed: set[str] = set()
        pending = {stage.name: stage for stage in selected}
        running: dict[Future[None], tuple[ProductionStage, float]] = {}
        running_resource_keys: set[str] = set()
        running_gpu_stages = 0

        pool = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="production-stage",
        )
        try:
            while pending or running:
                made_progress = False

                for stage in selected:
                    if stage.name not in pending:
                        continue
                    if not set(stage.dependencies).issubset(completed):
                        continue
                    if len(running) >= self._max_workers:
                        break
                    if stage.resource == "gpu" and running_gpu_stages >= self._max_gpu_stages:
                        continue
                    if (
                        stage.resource_key is not None
                        and stage.resource_key in running_resource_keys
                    ):
                        continue

                    inspection = self._inspect(stage, manifest)
                    if inspection.status == "blocked":
                        missing = tuple(path for path in stage.inputs if not path.exists())
                        raise ProductionStageBlocked(stage, missing)

                    if stage.name not in force and inspection.status == "current":
                        print(f"SKIP  {stage.name}: {inspection.reason}")
                        skipped.append(stage.name)
                        metrics.append(
                            ProductionStageMetric(
                                stage_name=stage.name,
                                outcome="skipped",
                                resource=stage.resource,
                                elapsed_seconds=0.0,
                            )
                        )
                        completed.add(stage.name)
                        pending.pop(stage.name)
                        made_progress = True
                        continue

                    if stage.name not in force and inspection.status == "adoptable":
                        self._record_stage(stage, manifest, origin="adopted")
                        self._write_manifest(manifest)
                        print(
                            "ADOPT "
                            f"{stage.name}: existing artifacts recorded in production manifest"
                        )
                        adopted.append(stage.name)
                        metrics.append(
                            ProductionStageMetric(
                                stage_name=stage.name,
                                outcome="adopted",
                                resource=stage.resource,
                                elapsed_seconds=0.0,
                            )
                        )
                        completed.add(stage.name)
                        pending.pop(stage.name)
                        made_progress = True
                        continue

                    print(f"RUN   {stage.name}: {stage.description}")
                    stage_started = time.monotonic()
                    future = pool.submit(self._executor, stage)
                    running[future] = (stage, stage_started)
                    pending.pop(stage.name)
                    if stage.resource == "gpu":
                        running_gpu_stages += 1
                    if stage.resource_key is not None:
                        running_resource_keys.add(stage.resource_key)
                    made_progress = True

                if not running:
                    if pending and not made_progress:
                        unresolved = ", ".join(pending)
                        raise RuntimeError(
                            "Production DAG cannot make progress; unresolved stages: "
                            f"{unresolved}"
                        )
                    continue

                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    stage, stage_started = running.pop(future)
                    if stage.resource == "gpu":
                        running_gpu_stages -= 1
                    if stage.resource_key is not None:
                        running_resource_keys.discard(stage.resource_key)

                    future.result()

                    missing_outputs = tuple(
                        path for path in stage.outputs if not path.exists()
                    )
                    if missing_outputs:
                        rendered = ", ".join(path.as_posix() for path in missing_outputs)
                        raise RuntimeError(
                            f"Stage {stage.name} completed without required outputs: {rendered}"
                        )

                    elapsed = time.monotonic() - stage_started
                    self._record_stage(stage, manifest, origin="executed")
                    self._write_manifest(manifest)
                    executed.append(stage.name)
                    metrics.append(
                        ProductionStageMetric(
                            stage_name=stage.name,
                            outcome="executed",
                            resource=stage.resource,
                            elapsed_seconds=elapsed,
                        )
                    )
                    completed.add(stage.name)
                    print(f"DONE  {stage.name}: {elapsed:.1f} s")
        except BaseException:
            for outstanding in running:
                outstanding.cancel()
            cancel_running = getattr(self._executor, "cancel_running", None)
            if callable(cancel_running):
                cancel_running()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)

        return ProductionRunSummary(
            executed=tuple(executed),
            adopted=tuple(adopted),
            skipped=tuple(skipped),
            metrics=tuple(metrics),
            total_elapsed_seconds=time.monotonic() - started_at,
        )

    def _selected_stages(self, through: str | None) -> list[ProductionStage]:
        if through is None:
            return list(self._stages)
        for index, stage in enumerate(self._stages):
            if stage.name == through:
                return self._stages[: index + 1]
        raise ValueError(f"Unknown production stage: {through}")

    def _inspect(
        self,
        stage: ProductionStage,
        manifest: ProductionRunManifest,
    ) -> StageInspection:
        missing_inputs = tuple(path for path in stage.inputs if not path.exists())
        if missing_inputs:
            rendered = ", ".join(path.as_posix() for path in missing_inputs)
            return StageInspection(
                stage=stage,
                status="blocked",
                reason=f"missing inputs: {rendered}",
            )

        spec_sha256 = self._stage_spec_sha256(stage)
        input_sha256 = _digest_paths(stage.inputs)
        existing_outputs = tuple(path.exists() for path in stage.outputs)
        outputs_complete = all(existing_outputs)
        outputs_partial = any(existing_outputs) and not outputs_complete
        record = manifest.stages.get(stage.name)

        if outputs_complete:
            output_sha256 = _digest_paths(stage.outputs)
            if record is None:
                return StageInspection(
                    stage=stage,
                    status="adoptable",
                    reason="all required outputs already exist",
                    spec_sha256=spec_sha256,
                    input_sha256=input_sha256,
                    output_sha256=output_sha256,
                )
            if (
                record.spec_sha256 == spec_sha256
                and record.input_sha256 == input_sha256
                and record.output_sha256 == output_sha256
            ):
                return StageInspection(
                    stage=stage,
                    status="current",
                    reason="manifest fingerprints match inputs and outputs",
                    spec_sha256=spec_sha256,
                    input_sha256=input_sha256,
                    output_sha256=output_sha256,
                )
            return StageInspection(
                stage=stage,
                status="stale",
                reason="persisted outputs no longer match the recorded stage fingerprints",
                spec_sha256=spec_sha256,
                input_sha256=input_sha256,
                output_sha256=output_sha256,
            )

        if outputs_partial or record is not None:
            return StageInspection(
                stage=stage,
                status="stale",
                reason="stage outputs are incomplete or missing from a recorded run",
                spec_sha256=spec_sha256,
                input_sha256=input_sha256,
            )
        return StageInspection(
            stage=stage,
            status="pending",
            reason="required outputs are not present",
            spec_sha256=spec_sha256,
            input_sha256=input_sha256,
        )

    def _record_stage(
        self,
        stage: ProductionStage,
        manifest: ProductionRunManifest,
        *,
        origin: RecordOrigin,
    ) -> None:
        missing_inputs = tuple(path for path in stage.inputs if not path.exists())
        missing_outputs = tuple(path for path in stage.outputs if not path.exists())
        if missing_inputs or missing_outputs:
            raise RuntimeError(f"Cannot record incomplete production stage {stage.name}")

        manifest.stages[stage.name] = ProductionStageRecord(
            stage_name=stage.name,
            spec_sha256=self._stage_spec_sha256(stage),
            input_sha256=_digest_paths(stage.inputs),
            output_sha256=_digest_paths(stage.outputs),
            origin=origin,
            command=list(stage.command()),
        )

    def _stage_spec_sha256(self, stage: ProductionStage) -> str:
        script_path = stage.script
        if not script_path.is_absolute():
            script_path = self._repo_root / script_path
        if not script_path.is_file():
            raise FileNotFoundError(f"Production stage script not found: {script_path}")

        payload = {
            "name": stage.name,
            "description": stage.description,
            "script": stage.script.as_posix(),
            "script_sha256": _sha256_file(script_path),
            "arguments": list(stage.arguments),
            "inputs": [path.as_posix() for path in stage.inputs],
            "outputs": [path.as_posix() for path in stage.outputs],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _load_manifest(self) -> ProductionRunManifest:
        if not self._manifest_path.is_file():
            return ProductionRunManifest()

        payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") == "1":
            payload = _upgrade_v1_manifest(payload)
        return ProductionRunManifest.model_validate(payload)

    def _write_manifest(self, manifest: ProductionRunManifest) -> None:
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._manifest_path.with_name(
            f".{self._manifest_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._manifest_path)


def build_production_stages(*, script_file: Path, output_dir: Path) -> list[ProductionStage]:
    """Describe the production DAG while preserving validated phase scripts as executors."""

    phase2 = output_dir / "phase2"
    phase3 = output_dir / "phase3"
    phase4 = output_dir / "phase4"
    phase5 = output_dir / "phase5"
    phase6 = output_dir / "phase6"
    phase8 = output_dir / "phase8"

    source_script = phase2 / "source_script.json"
    narrative_blocks = phase2 / "narrative_blocks.json"
    beats = phase2 / "beats.json"
    scenes = phase2 / "scenes.json"
    entities = phase3 / "entities.json"
    continuity = phase3 / "block_continuity.json"
    shots = phase3 / "shots.json"
    visual_references = phase4 / "visual_references.json"
    reference_assets = phase4 / "reference_assets.json"
    reference_assets_dir = phase4 / "reference_assets"
    narration = phase5 / "narration.json"
    narration_audio = phase5 / "narration.wav"
    narration_words = phase5 / "narration_words.json"
    beat_timings = phase5 / "beat_timings.json"
    shot_timings = phase5 / "shot_timings.json"
    storyboard_frames = phase6 / "storyboard_frames.json"
    storyboard_keyframes = phase6 / "storyboard_keyframes.json"
    storyboard_keyframes_dir = phase6 / "storyboard_keyframes"
    video_prompts = phase8 / "video_prompts.json"
    video_clips = phase8 / "video_clips.json"
    video_clips_dir = phase8 / "video_clips"
    upscaled_clips = phase8 / "upscaled_clips.json"
    upscaled_clips_dir = phase8 / "upscaled_clips"

    return [
        ProductionStage(
            name="phase2-narrative",
            description="plan narrative blocks, beats, and scenes",
            script=Path("scripts/run_phase2.py"),
            arguments=(str(script_file), "--output", str(phase2)),
            inputs=(script_file,),
            outputs=(source_script, narrative_blocks, beats, scenes),
        ),
        ProductionStage(
            name="phase3-continuity",
            description="build the stateful continuity registry",
            dependencies=("phase2-narrative",),
            script=Path("scripts/run_phase3.py"),
            arguments=(str(narrative_blocks), "--output", str(phase3)),
            inputs=(narrative_blocks,),
            outputs=(entities, continuity),
        ),
        ProductionStage(
            name="phase3-shots",
            description="plan ordered shots from scenes and continuity",
            dependencies=("phase3-continuity",),
            script=Path("scripts/run_phase3_shots.py"),
            arguments=(
                "--beats", str(beats),
                "--scenes", str(scenes),
                "--entities", str(entities),
                "--continuity", str(continuity),
                "--output", str(shots),
            ),
            inputs=(beats, scenes, entities, continuity),
            outputs=(shots,),
        ),
        ProductionStage(
            name="phase4-reference-prompts",
            description="build canonical visual reference prompts",
            dependencies=("phase3-continuity",),
            script=Path("scripts/run_phase4.py"),
            arguments=(
                str(entities),
                "--blocks", str(narrative_blocks),
                "--continuity", str(continuity),
                "--aspect-ratio", "16:9",
                "--output", str(visual_references),
            ),
            inputs=(entities, narrative_blocks, continuity),
            outputs=(visual_references,),
        ),
        ProductionStage(
            name="phase4-reference-assets",
            description="generate reference images through the Ideogram 4 Salad worker",
            dependencies=("phase4-reference-prompts",),
            resource="gpu",
            resource_key="ideogram4",
            script=Path("scripts/run_phase4_assets.py"),
            arguments=(
                str(visual_references),
                "--size", "1536x864",
                "--output-dir", str(reference_assets_dir),
                "--metadata", str(reference_assets),
            ),
            inputs=(visual_references,),
            outputs=(reference_assets, reference_assets_dir),
        ),
        ProductionStage(
            name="phase5-narration",
            description="generate canonical narration through the Breeze TTS 2 Salad worker",
            dependencies=("phase2-narrative",),
            resource="gpu",
            resource_key="speech",
            script=Path("scripts/run_phase5_audio.py"),
            arguments=(
                str(source_script),
                "--output-dir", str(phase5),
                "--metadata", str(narration),
            ),
            inputs=(source_script,),
            outputs=(narration, narration_audio),
        ),
        ProductionStage(
            name="phase5-alignment",
            description="align narration words through the Whisper Salad worker",
            dependencies=("phase5-narration",),
            resource="gpu",
            resource_key="whisper",
            script=Path("scripts/run_phase5_alignment.py"),
            arguments=(
                "--source", str(source_script),
                "--narration", str(narration),
                "--audio", str(narration_audio),
                "--output", str(narration_words),
            ),
            inputs=(source_script, narration, narration_audio),
            outputs=(narration_words,),
        ),
        ProductionStage(
            name="phase5-beat-timing",
            description="map beats to aligned narration words",
            dependencies=("phase5-alignment",),
            script=Path("scripts/run_phase5_beat_timing.py"),
            arguments=(
                "--source", str(source_script),
                "--beats", str(beats),
                "--narration", str(narration),
                "--words", str(narration_words),
                "--output", str(beat_timings),
            ),
            inputs=(source_script, beats, narration, narration_words),
            outputs=(beat_timings,),
        ),
        ProductionStage(
            name="phase5-shot-timing",
            description="derive deterministic shot timing",
            dependencies=("phase3-shots", "phase5-beat-timing"),
            script=Path("scripts/run_phase5_shot_timing.py"),
            arguments=(
                "--shots", str(shots),
                "--beat-timings", str(beat_timings),
                "--output", str(shot_timings),
            ),
            inputs=(shots, beat_timings),
            outputs=(shot_timings,),
        ),
        ProductionStage(
            name="phase6-storyboard",
            description="build provider-neutral storyboard frame prompts",
            dependencies=(
                "phase3-shots",
                "phase5-shot-timing",
                "phase4-reference-prompts",
            ),
            script=Path("scripts/run_phase6_storyboard.py"),
            arguments=(
                "--shots", str(shots),
                "--timings", str(shot_timings),
                "--references", str(visual_references),
                "--aspect-ratio", "16:9",
                "--output", str(storyboard_frames),
            ),
            inputs=(shots, shot_timings, visual_references),
            outputs=(storyboard_frames,),
        ),
        ProductionStage(
            name="phase8-video-prompts",
            description="prepare motion prompts from the storyboard plan",
            dependencies=("phase6-storyboard",),
            script=Path("scripts/run_phase8_video_prompts.py"),
            arguments=(
                "--shots", str(shots),
                "--timings", str(shot_timings),
                "--storyboard-frames", str(storyboard_frames),
                "--aspect-ratio", "16:9",
                "--output", str(video_prompts),
            ),
            inputs=(shots, shot_timings, storyboard_frames),
            outputs=(video_prompts,),
        ),
        ProductionStage(
            name="phase6-keyframes",
            description="generate storyboard keyframes through Ideogram 4 Quality",
            dependencies=("phase6-storyboard",),
            resource="gpu",
            resource_key="ideogram4",
            script=Path("scripts/run_phase6_keyframes.py"),
            arguments=(
                "--frames", str(storyboard_frames),
                "--shots", str(shots),
                "--size", "1536x864",
                "--quality", "high",
                "--output-dir", str(storyboard_keyframes_dir),
                "--output", str(storyboard_keyframes),
            ),
            inputs=(storyboard_frames, shots),
            outputs=(storyboard_keyframes, storyboard_keyframes_dir),
        ),
        ProductionStage(
            name="phase8-videos",
            description="fan out, resume, verify, and download LTX 2.5 720p video clips",
            dependencies=(
                "phase8-video-prompts",
                "phase6-keyframes",
                "phase5-shot-timing",
            ),
            resource="gpu",
            resource_key="ltx25",
            script=Path("scripts/run_phase8_videos.py"),
            arguments=(
                "--keyframes", str(storyboard_keyframes),
                "--prompts", str(video_prompts),
                "--timings", str(shot_timings),
                "--width", "1280",
                "--height", "720",
                "--fps", "24",
                "--output-dir", str(phase8),
            ),
            inputs=(storyboard_keyframes, storyboard_keyframes_dir, video_prompts, shot_timings),
            outputs=(video_clips, video_clips_dir),
        ),
        ProductionStage(
            name="phase8-upscale",
            description="upscale LTX clips 2x with self-hosted Real-ESRGAN",
            dependencies=("phase8-videos",),
            resource="gpu",
            resource_key="realesrgan",
            script=Path("scripts/run_phase8_upscale.py"),
            arguments=(
                "--clips", str(video_clips),
                "--output-dir", str(phase8),
            ),
            inputs=(video_clips, video_clips_dir),
            outputs=(upscaled_clips, upscaled_clips_dir),
        ),
    ]


def _upgrade_v1_manifest(payload: dict[str, object]) -> dict[str, object]:
    stages = payload.get("stages")
    upgraded_stages: dict[str, object] = {}
    if isinstance(stages, dict):
        for name, raw_record in stages.items():
            if not isinstance(raw_record, dict):
                continue
            if raw_record.get("kind") == "manual_gate":
                continue
            record = dict(raw_record)
            record.pop("kind", None)
            upgraded_stages[str(name)] = record
    return {"schema_version": "2", "stages": upgraded_stages}


def _digest_paths(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"Cannot fingerprint missing path: {path}")
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(b"file\0")
            digest.update(_sha256_file(path).encode("ascii"))
        elif path.is_dir():
            digest.update(b"dir\0")
            children = sorted(child for child in path.rglob("*") if child.is_file())
            for child in children:
                digest.update(child.relative_to(path).as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(_sha256_file(child).encode("ascii"))
                digest.update(b"\0")
        else:
            raise ValueError(f"Unsupported production artifact path: {path}")
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
