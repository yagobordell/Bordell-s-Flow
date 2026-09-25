from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import InferenceJobRequest
from .errors import OutputConflictError
from .ports import ObjectStorage, StoredObject
from .storage import sha256_file

_BUNDLE_PREFIX = "__ai_video_factory/bundles"
_BUNDLE_MANIFEST_CONTENT_TYPE = "application/json"


class StagedBundleObject(BaseModel):
    """Content-addressed object retained until final publication is complete."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1)
    final_key: str = Field(min_length=1)
    staging_key: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)


class BundlePublicationManifest(BaseModel):
    """Durable commit record for one complete inference artifact bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    job_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    primary: StagedBundleObject
    sidecars: dict[str, StagedBundleObject] = Field(default_factory=dict)


def bundle_manifest_key(request: InferenceJobRequest, request_sha256: str) -> str:
    return f"{_BUNDLE_PREFIX}/{request.job_id}/{request_sha256}/ready.json"


def load_committed_bundle(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    request_sha256: str,
    work_dir: Path,
) -> BundlePublicationManifest | None:
    """Load and validate a previously committed staging manifest."""

    key = bundle_manifest_key(request, request_sha256)
    stored = storage.stat(key)
    if stored is None:
        return None
    if (
        stored.content_type != _BUNDLE_MANIFEST_CONTENT_TYPE
        or stored.size_bytes < 1
        or stored.metadata.get("job-id") != request.job_id
        or stored.metadata.get("request-sha256") != request_sha256
        or stored.metadata.get("bundle-state") != "ready"
        or not stored.metadata.get("artifact-sha256")
    ):
        raise OutputConflictError(f"bundle publication manifest is invalid: {key}")

    destination = work_dir / "bundle-publication-manifest.json"
    downloaded = storage.download(key, destination)
    if downloaded.metadata != stored.metadata or downloaded.size_bytes != stored.size_bytes:
        raise OutputConflictError(f"bundle publication manifest changed while reading: {key}")
    digest = sha256_file(destination)
    if digest != stored.metadata["artifact-sha256"]:
        raise OutputConflictError(f"bundle publication manifest digest is invalid: {key}")

    try:
        manifest = BundlePublicationManifest.model_validate_json(
            destination.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise OutputConflictError(f"bundle publication manifest cannot be parsed: {key}") from error
    _validate_manifest_contract(manifest, request, request_sha256)
    return manifest


def stage_bundle(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    request_sha256: str,
    *,
    primary_path: Path,
    primary_content_type: str,
    sidecars: Mapping[str, tuple[Path, str]],
    work_dir: Path,
) -> BundlePublicationManifest:
    """Stage a complete bundle, then atomically commit its deterministic ready marker."""

    primary = _stage_object(
        storage,
        request,
        request_sha256,
        role="primary",
        final_key=request.output.key,
        path=primary_path,
        content_type=primary_content_type,
        primary_sha256=sha256_file(primary_path),
    )
    staged_sidecars: dict[str, StagedBundleObject] = {}
    for name, contract in (request.sidecar_outputs or {}).items():
        try:
            path, content_type = sidecars[name]
        except KeyError as error:
            raise ValueError(f"missing local sidecar artifact: {name}") from error
        if content_type != contract.content_type:
            raise ValueError(
                f"sidecar {name!r} content type does not match the request contract"
            )
        staged_sidecars[name] = _stage_object(
            storage,
            request,
            request_sha256,
            role=f"sidecar:{name}",
            final_key=contract.key,
            path=path,
            content_type=content_type,
            primary_sha256=primary.sha256,
        )

    if set(staged_sidecars) != set(request.sidecar_outputs or {}):
        raise ValueError("task sidecar artifacts do not match the request contract")

    manifest = BundlePublicationManifest(
        job_id=request.job_id,
        request_sha256=request_sha256,
        primary=primary,
        sidecars=staged_sidecars,
    )
    manifest_path = work_dir / "bundle-publication-manifest-ready.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    manifest_digest = sha256_file(manifest_path)
    manifest_key = bundle_manifest_key(request, request_sha256)
    publication = storage.create_if_absent(
        manifest_path,
        manifest_key,
        content_type=_BUNDLE_MANIFEST_CONTENT_TYPE,
        metadata={
            "job-id": request.job_id,
            "request-sha256": request_sha256,
            "artifact-sha256": manifest_digest,
            "bundle-state": "ready",
        },
    )
    if publication.stored.size_bytes < 1:
        raise OutputConflictError(f"bundle publication manifest is empty: {manifest_key}")

    committed = load_committed_bundle(storage, request, request_sha256, work_dir)
    if committed is None:  # pragma: no cover - defensive storage invariant
        raise RuntimeError(f"bundle publication manifest disappeared: {manifest_key}")
    return committed


def publish_committed_bundle(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    request_sha256: str,
    manifest: BundlePublicationManifest,
    work_dir: Path,
    *,
    local_sources: Mapping[str, Path] | None = None,
) -> tuple[StoredObject, bool]:
    """Publish sidecars first and primary last from one committed staging manifest."""

    _validate_manifest_contract(manifest, request, request_sha256)
    sources = dict(local_sources or {})
    manifest_key = bundle_manifest_key(request, request_sha256)

    for name, staged in manifest.sidecars.items():
        _ensure_final_object(
            storage,
            request,
            request_sha256,
            staged,
            work_dir,
            source=sources.get(staged.role),
            sidecar_name=name,
            primary_sha256=manifest.primary.sha256,
            manifest_key=manifest_key,
        )

    primary, created = _ensure_final_object(
        storage,
        request,
        request_sha256,
        manifest.primary,
        work_dir,
        source=sources.get("primary"),
        sidecar_name=None,
        primary_sha256=manifest.primary.sha256,
        manifest_key=manifest_key,
    )
    return primary, created


def recover_committed_bundle(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    work_dir: Path,
) -> tuple[StoredObject, bool] | None:
    """Finish an interrupted final publication without rerunning inference."""

    request_sha256 = request.fingerprint()
    manifest = load_committed_bundle(storage, request, request_sha256, work_dir)
    if manifest is None:
        return None
    return publish_committed_bundle(
        storage,
        request,
        request_sha256,
        manifest,
        work_dir,
    )


def _stage_object(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    request_sha256: str,
    *,
    role: str,
    final_key: str,
    path: Path,
    content_type: str,
    primary_sha256: str,
) -> StagedBundleObject:
    digest = sha256_file(path)
    size_bytes = path.stat().st_size
    if size_bytes < 1:
        raise ValueError(f"cannot stage empty bundle object: {final_key}")
    role_path = role.replace(":", "/")
    staging_key = (
        f"{_BUNDLE_PREFIX}/{request.job_id}/{request_sha256}/objects/"
        f"{role_path}/{digest}"
    )
    metadata = {
        "job-id": request.job_id,
        "request-sha256": request_sha256,
        "artifact-sha256": digest,
        "bundle-role": role,
        "final-key": final_key,
        "primary-artifact-sha256": primary_sha256,
    }
    publication = storage.create_if_absent(
        path,
        staging_key,
        content_type=content_type,
        metadata=metadata,
    )
    _validate_staged_object(
        publication.stored,
        expected_key=staging_key,
        expected_content_type=content_type,
        expected_size=size_bytes,
        expected_metadata=metadata,
    )
    return StagedBundleObject(
        role=role,
        final_key=final_key,
        staging_key=staging_key,
        content_type=content_type,
        sha256=digest,
        size_bytes=size_bytes,
    )


def _ensure_final_object(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    request_sha256: str,
    staged: StagedBundleObject,
    work_dir: Path,
    *,
    source: Path | None,
    sidecar_name: str | None,
    primary_sha256: str,
    manifest_key: str,
) -> tuple[StoredObject, bool]:
    metadata = {
        "job-id": request.job_id,
        "request-sha256": request_sha256,
        "artifact-sha256": staged.sha256,
        "bundle-manifest-key": manifest_key,
        "bundle-schema-version": "1",
    }
    if sidecar_name is not None:
        metadata.update(
            {
                "sidecar-name": sidecar_name,
                "primary-artifact-sha256": primary_sha256,
            }
        )

    existing = storage.stat(staged.final_key)
    if existing is not None:
        _validate_final_object(
            existing,
            staged=staged,
            expected_metadata=metadata,
        )
        return existing, False

    resolved_source = source
    if resolved_source is not None:
        if (
            not resolved_source.is_file()
            or resolved_source.stat().st_size != staged.size_bytes
            or sha256_file(resolved_source) != staged.sha256
        ):
            raise OutputConflictError(
                f"local bundle source does not match committed manifest: {staged.final_key}"
            )
    else:
        role_file = staged.role.replace(":", "-")
        resolved_source = work_dir / f"recover-{role_file}"
        downloaded = storage.download(staged.staging_key, resolved_source)
        _validate_staged_object(
            downloaded,
            expected_key=staged.staging_key,
            expected_content_type=staged.content_type,
            expected_size=staged.size_bytes,
            expected_metadata={
                "job-id": request.job_id,
                "request-sha256": request_sha256,
                "artifact-sha256": staged.sha256,
                "bundle-role": staged.role,
                "final-key": staged.final_key,
                "primary-artifact-sha256": primary_sha256,
            },
        )
        if sha256_file(resolved_source) != staged.sha256:
            raise OutputConflictError(
                f"staged bundle bytes do not match committed manifest: {staged.staging_key}"
            )

    publication = storage.create_if_absent(
        resolved_source,
        staged.final_key,
        content_type=staged.content_type,
        metadata=metadata,
    )
    _validate_final_object(
        publication.stored,
        staged=staged,
        expected_metadata=metadata,
    )
    return publication.stored, publication.created


def _validate_manifest_contract(
    manifest: BundlePublicationManifest,
    request: InferenceJobRequest,
    request_sha256: str,
) -> None:
    if manifest.job_id != request.job_id or manifest.request_sha256 != request_sha256:
        raise OutputConflictError("bundle publication manifest belongs to a different request")
    if (
        manifest.primary.role != "primary"
        or manifest.primary.final_key != request.output.key
        or manifest.primary.content_type != request.output.content_type
    ):
        raise OutputConflictError("bundle publication manifest primary contract is invalid")

    declared = request.sidecar_outputs or {}
    if set(manifest.sidecars) != set(declared):
        raise OutputConflictError("bundle publication manifest sidecars do not match request")
    for name, contract in declared.items():
        staged = manifest.sidecars[name]
        if (
            staged.role != f"sidecar:{name}"
            or staged.final_key != contract.key
            or staged.content_type != contract.content_type
        ):
            raise OutputConflictError(
                f"bundle publication manifest sidecar contract is invalid: {name}"
            )


def _validate_staged_object(
    stored: StoredObject,
    *,
    expected_key: str,
    expected_content_type: str,
    expected_size: int,
    expected_metadata: Mapping[str, str],
) -> None:
    if (
        stored.key != expected_key
        or stored.content_type != expected_content_type
        or stored.size_bytes != expected_size
        or any(stored.metadata.get(key) != value for key, value in expected_metadata.items())
    ):
        raise OutputConflictError(
            f"staged bundle object conflicts with committed data: {expected_key}"
        )


def _validate_final_object(
    stored: StoredObject,
    *,
    staged: StagedBundleObject,
    expected_metadata: Mapping[str, str],
) -> None:
    if (
        stored.key != staged.final_key
        or stored.content_type != staged.content_type
        or stored.size_bytes != staged.size_bytes
        or any(stored.metadata.get(key) != value for key, value in expected_metadata.items())
    ):
        raise OutputConflictError(
            f"final bundle object conflicts with committed publication: {staged.final_key}"
        )
