from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from openai import OpenAI

from ai_video_factory.config import settings
from ai_video_factory.providers.r2 import create_r2_storage

REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "node", "npm")


class TransientSaladPreflightError(RuntimeError):
    """Salad control-plane/queue state could not be read after bounded transient retries."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-fast whole-video preflight. This script never allocates a GPU."
    )
    parser.add_argument("script_file", type=Path)
    parser.add_argument(
        "--services",
        type=Path,
        default=Path("deploy/salad/services.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/output/preflight_report.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir,
        help="Output root used to recognize legitimate Phase 8 resume transports.",
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="Run only local/config checks. Intended for CI and unit tests.",
    )
    return parser.parse_args()


def _required(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is missing. Add it to .env before any GPU allocation.")
    return value.strip()


def _load_services(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Salad service manifest not found: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != "2":
        raise RuntimeError("deploy/salad/services.json must use schema_version=2")
    required = {
        "whisper",
        "breeze_tts2",
        "fish_speech",
        "ideogram4",
        "qwen_image_21",
        "ltx25",
        "realesrgan",
    }
    services = document.get("services")
    if not isinstance(services, dict):
        raise RuntimeError("Salad manifest services must be an object")
    missing = sorted(required.difference(services))
    if missing:
        raise RuntimeError("Salad manifest is missing services: " + ", ".join(missing))
    for name, service in services.items():
        autoscaler = service.get("autoscaler", {})
        if autoscaler.get("min_replicas") != 0:
            raise RuntimeError(f"Salad service {name} must retain min_replicas=0")
    return document


def _check_local_tools() -> list[str]:
    found: list[str] = []
    for name in REQUIRED_TOOLS:
        executable = shutil.which(name)
        if executable is None:
            raise RuntimeError(f"Required local executable is unavailable on PATH: {name}")
        found.append(f"{name}={executable}")
    remotion_cli = Path("remotion/node_modules/.bin") / (
        "remotion.cmd" if __import__("os").name == "nt" else "remotion"
    )
    if remotion_cli.is_file():
        found.append(f"remotion={remotion_cli}")
    else:
        found.append("remotion=missing-local-cache; runner will execute npm ci after preflight")
    return found


def _check_r2_and_fish_reference() -> list[str]:
    storage = create_r2_storage(
        endpoint_url=_required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )
    storage.ping()
    key = _required(
        "FISH_SPEECH_REFERENCE_AUDIO_KEY",
        settings.fish_speech_reference_audio_key,
    )
    expected = _required(
        "FISH_SPEECH_REFERENCE_AUDIO_SHA256",
        settings.fish_speech_reference_audio_sha256,
    ).lower()
    _required(
        "FISH_SPEECH_REFERENCE_PROFILE",
        settings.fish_speech_reference_profile,
    )
    _required(
        "FISH_SPEECH_REFERENCE_TRANSCRIPT",
        settings.fish_speech_reference_transcript,
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "reference.wav"
        stored = storage.download(key, path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError(
            "Fish reference SHA-256 mismatch: "
            f"expected={expected} actual={digest}"
        )
    if stored.content_type not in {"audio/wav", "audio/x-wav"}:
        raise RuntimeError(
            f"Fish reference must be WAV; R2 content type is {stored.content_type!r}"
        )
    return [f"r2_bucket={storage.bucket}", f"fish_reference={key}"]


def _check_postgres() -> str:
    dsn = _required("POSTGRES_DSN", settings.postgres_dsn)
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for production preflight; install the project dev/gpu extras."
        ) from exc

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with psycopg.connect(dsn, connect_timeout=10) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    value = cursor.fetchone()
            if value != (1,):
                raise RuntimeError("Postgres preflight query returned an unexpected result")
            return "postgres=ok"
        except psycopg.OperationalError as exc:
            if attempt >= max_attempts:
                raise
            delay_seconds = min(10, 2 * attempt)
            print(
                "VIDEO_FACTORY_PREFLIGHT_RETRY "
                f"operation='Postgres preflight' attempt={attempt}/{max_attempts} "
                f"delay_seconds={delay_seconds} error={exc}"
            )
            time.sleep(delay_seconds)

    raise RuntimeError("Postgres preflight retry loop exhausted")


def _active_resume_transport_ids(
    output_dir: Path,
    *,
    manifest_name: str = "video_generation_manifest.json",
    label: str = "Phase 8",
) -> set[str]:
    manifest_path = output_dir / "phase8" / manifest_name
    if not manifest_path.is_file():
        return set()
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"{label} resume manifest is unreadable: {manifest_path}"
        ) from exc
    jobs = document.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError(f"{label} resume manifest has invalid jobs: {manifest_path}")

    allowed: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise RuntimeError(f"{label} resume manifest has invalid job entry: {manifest_path}")
        transport_id = job.get("transport_job_id")
        status = job.get("transport_status")
        if (
            isinstance(transport_id, str)
            and transport_id.strip()
            and status in {"pending", "running"}
        ):
            allowed.add(transport_id.strip())
    return allowed


def _is_transient_salad_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 429} or 500 <= exc.code < 600
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return True
        message = str(reason).lower()
    else:
        message = str(exc).lower()
    return any(
        token in message
        for token in (
            "timed out",
            "timeout",
            "upstream connect error",
            "disconnect/reset",
            "remote connection failure",
            "server unavailable",
            "gateway timeout",
        )
    )


def _salad_json_get_with_retry(
    request: urllib.request.Request,
    *,
    operation: str,
    timeout_seconds: float = 15.0,
    max_attempts: int = 6,
) -> dict[str, Any]:
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"{operation} returned a non-object JSON payload")
            return payload
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            transient = _is_transient_salad_error(exc)
            if not transient or attempt >= max_attempts:
                if isinstance(exc, urllib.error.HTTPError):
                    detail = f"HTTP {exc.code}"
                else:
                    detail = str(exc)
                error_type = TransientSaladPreflightError if transient else RuntimeError
                raise error_type(f"{operation}: {detail}") from exc
            delay_seconds = min(10, 2 * attempt)
            print(
                "VIDEO_FACTORY_PREFLIGHT_RETRY "
                f"operation={operation!r} attempt={attempt}/{max_attempts} "
                f"delay_seconds={delay_seconds} error={exc}"
            )
            time.sleep(delay_seconds)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{operation}: invalid JSON response") from exc

    raise RuntimeError(f"{operation}: retry loop exhausted")


def _queue_summary(
    *,
    base_url: str,
    queue_name: str,
    api_key: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}/queues/{queue_name}",
        headers={
            "Salad-Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "ai-video-factory-preflight/1.1",
        },
        method="GET",
    )
    payload = _salad_json_get_with_retry(
        request,
        operation=f"Salad queue summary preflight failed for {queue_name}",
        timeout_seconds=10.0,
        max_attempts=2,
    )
    queue_length = payload.get("current_queue_length")
    if isinstance(queue_length, bool) or not isinstance(queue_length, int):
        raise RuntimeError(f"Salad queue {queue_name} returned invalid current_queue_length")
    if queue_length < 0:
        raise RuntimeError(f"Salad queue {queue_name} returned negative current_queue_length")
    return payload


def _queue_jobs(
    *,
    base_url: str,
    queue_name: str,
    api_key: str,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for page in range(1, 101):
        query = urllib.parse.urlencode({"page": page, "page_size": 25})
        request = urllib.request.Request(
            f"{base_url}/queues/{queue_name}/jobs?{query}",
            headers={
                "Salad-Api-Key": api_key,
                "Accept": "application/json",
                "User-Agent": "ai-video-factory-preflight/1.0",
            },
            method="GET",
        )
        payload = _salad_json_get_with_retry(
            request,
            operation=f"Salad queue preflight failed for {queue_name}",
        )

        raw_items = payload.get("items", payload.get("jobs", []))
        if not isinstance(raw_items, list):
            raise RuntimeError(f"Salad queue {queue_name} returned an invalid jobs payload")
        page_items = [item for item in raw_items if isinstance(item, dict)]
        jobs.extend(page_items)
        if len(page_items) < 25:
            break
    return jobs


def _check_salad_queues(
    document: dict[str, Any],
    output_dir: Path,
    *,
    service_names: set[str] | None = None,
    allow_transient_defer: bool = True,
) -> dict[str, Any]:
    api_key = _required("SALAD_API_KEY", settings.salad_api_key)
    organization = str(document["stack"]["organization"])
    project = str(document["stack"]["project"])
    base_url = (
        "https://api.salad.com/api/public/organizations/"
        f"{organization}/projects/{project}"
    )
    allowed_resume = {
        "ltx25": _active_resume_transport_ids(output_dir),
        "realesrgan": _active_resume_transport_ids(
            output_dir,
            manifest_name="video_upscale_manifest.json",
            label="Phase 8 Real-ESRGAN",
        ),
    }
    result: dict[str, Any] = {}

    for service_name, service in document["services"].items():
        if service_names is not None and service_name not in service_names:
            continue
        queue_name = service.get("queue_name")
        if not isinstance(queue_name, str) or not queue_name.strip():
            raise RuntimeError(f"Salad service {service_name} is missing queue_name")
        jobs: list[dict[str, Any]] = []
        should_enumerate_jobs = False
        verification = "verified"
        try:
            summary = _queue_summary(
                base_url=base_url,
                queue_name=queue_name,
                api_key=api_key,
            )
            should_enumerate_jobs = int(summary["current_queue_length"]) > 0
        except TransientSaladPreflightError as summary_error:
            should_enumerate_jobs = True
            print(
                "VIDEO_FACTORY_PREFLIGHT_FALLBACK "
                f"queue={queue_name!r} strategy='enumerate-jobs' "
                f"reason={summary_error}"
            )

        if should_enumerate_jobs:
            try:
                jobs = _queue_jobs(
                    base_url=base_url,
                    queue_name=queue_name,
                    api_key=api_key,
                )
            except TransientSaladPreflightError as jobs_error:
                if not allow_transient_defer:
                    raise
                verification = "deferred_transient"
                print(
                    "VIDEO_FACTORY_PREFLIGHT_DEFERRED "
                    f"queue={queue_name!r} reason={jobs_error} "
                    "enforcement='stage-pre-gpu-guard'"
                )

        active = [
            job
            for job in jobs
            if str(job.get("status", "")).lower() in {"pending", "running"}
        ]
        active_ids: set[str] = {
            str(job.get("id", "")).strip()
            for job in active
            if str(job.get("id", "")).strip()
        }

        allowed = allowed_resume.get(service_name, set())
        unexpected = sorted(active_ids.difference(allowed))
        if unexpected:
            rendered = ", ".join(unexpected[:10])
            raise RuntimeError(
                f"Salad queue {queue_name} has active jobs not owned by the local "
                f"resume manifest: {rendered}"
            )
        if verification == "verified":
            result[service_name] = {
                "active_jobs": len(active_ids),
                "recognized_resume_jobs": len(active_ids.intersection(allowed)),
            }
        else:
            result[service_name] = {
                "verification": verification,
                "active_jobs": None,
                "recognized_resume_jobs": None,
            }
    return result


def _check_openai() -> str:
    api_key = _required("OPENAI_API_KEY", settings.openai_api_key)
    client = OpenAI(api_key=api_key, timeout=15.0, max_retries=1)
    client.models.retrieve(settings.openai_model)
    return f"openai_model={settings.openai_model}"


def _hugging_face_repositories(document: dict[str, Any]) -> list[str]:
    repositories: set[str] = set()
    for service in document["services"].values():
        environment = service.get("environment", {})
        for key, value in environment.items():
            if key.endswith("_MODEL_REPOSITORY") and isinstance(value, str) and value.strip():
                repositories.add(value.strip())
    return sorted(repositories)


def _check_hugging_face(document: dict[str, Any]) -> list[str]:
    token = _required("HF_TOKEN", settings.hf_token)
    checked: list[str] = []
    for repository in _hugging_face_repositories(document):
        encoded = urllib.parse.quote(repository, safe="/")
        request = urllib.request.Request(
            f"https://huggingface.co/api/models/{encoded}",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Hugging Face access check failed for {repository}: {response.status}"
                    )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Hugging Face access check failed for {repository}: HTTP {exc.code}"
            ) from exc
        checked.append(repository)
    return checked


def main() -> None:
    args = parse_args()
    if not args.script_file.is_file():
        raise SystemExit(f"Production source script not found: {args.script_file}")

    try:
        services = _load_services(args.services)
        checks: dict[str, Any] = {
            "source": str(args.script_file.resolve()),
            "local_tools": _check_local_tools(),
            "salad_manifest": "ok",
        }
        _required("SALAD_API_KEY", settings.salad_api_key)
        _required("SALAD_ORGANIZATION", settings.salad_organization)
        _required("SALAD_PROJECT", settings.salad_project)
        if not args.skip_network:
            checks["r2"] = _check_r2_and_fish_reference()
            checks["postgres"] = _check_postgres()
            checks["openai"] = _check_openai()
            checks["hugging_face"] = _check_hugging_face(services)
            checks["salad_queues"] = _check_salad_queues(services, args.output_dir)
        else:
            checks["network"] = "skipped"
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"VIDEO_FACTORY_PREFLIGHT_FAILED: {exc}") from exc

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"schema_version": "1", "checks": checks}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"VIDEO_FACTORY_PREFLIGHT_OK report={args.report.resolve()}")


if __name__ == "__main__":
    main()
