from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from openai import OpenAI
from r2_client import create_r2_storage

from ai_video_factory.config import settings

REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "node", "npm")


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
        "flux2_klein",
        "ltx25",
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
    if not remotion_cli.is_file():
        raise RuntimeError(
            "Remotion CLI is not installed. Run npm ci in remotion/ before production."
        )
    found.append(f"remotion={remotion_cli}")
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
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            value = cursor.fetchone()
    if value != (1,):
        raise RuntimeError("Postgres preflight query returned an unexpected result")
    return "postgres=ok"


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
