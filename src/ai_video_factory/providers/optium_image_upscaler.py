from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from PIL import Image

from ai_video_factory.image_upscaling import ImageUpscaler
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.inference.storage import R2ObjectStorage

logger = logging.getLogger(__name__)

RUNTIME_REQUEST_SCHEMA = "runtime-http/v1"
RUNTIME_RESPONSE_SCHEMA = "runtime-http-response/v1"
CAPABILITY_CONTRACT = "optiumlabs-image-upscale/v1"
RESULT_SCHEMA = "optiumlabs-image-upscale-result/v1"
CAPABILITY = "image_upscale"
EPHEMERAL_ACCESS_CAPABILITY = "<ephemeral-access-capability>"
_OUTPUT_MEDIA_TYPE = "image/png"
_OUTPUT_CONTAINER = "png"
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 3600.0
_DEFAULT_SIGNED_URL_EXPIRY_SECONDS = 7200
_DEFAULT_GATEWAY_RECOVERY_TIMEOUT_SECONDS = 900.0
_DEFAULT_GATEWAY_RECOVERY_POLL_SECONDS = 10.0
_MAX_TLS_RECORD_RETRIES = 2


class ImageUpscalerError(RuntimeError):
    """Raised when the external image-upscaler request cannot be completed safely."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _UpscalerObjectStorage(Protocol):
    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> StoredObject: ...

    def download(self, key: str, destination: Path) -> StoredObject: ...

    def presign_get(self, key: str, *, expires_seconds: int) -> str: ...

    def presign_put(self, key: str, *, expires_seconds: int) -> str: ...

    def delete(self, key: str) -> None: ...

    def stat(self, key: str) -> StoredObject | None: ...


class _RuntimeHttpClient(Protocol):
    def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...


class _UrllibRuntimeHttpClient:
    def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                response_body = response.read()
        except HTTPError as exc:
            safe_detail = _safe_http_error_detail(exc)
            raise ImageUpscalerError(
                f"Image upscaler returned HTTP {exc.code}: {safe_detail}",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise ImageUpscalerError(f"Image upscaler request failed: {exc.reason}") from exc

        try:
            decoded = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageUpscalerError("Image upscaler returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ImageUpscalerError("Image upscaler response must be a JSON object")
        return cast(Mapping[str, object], decoded)


def _is_tls_record_error(error: ImageUpscalerError) -> bool:
    """Only retry a broken TLS record; unrelated HTTP 500s are not transient."""
    if error.status_code != 500:
        return False
    detail = str(error).casefold()
    return (
        "sslv3_alert_bad_record_mac" in detail
        or "bad record mac" in detail
        or "bad_record_mac" in detail
    )


def _safe_http_error_detail(error: HTTPError) -> str:
    try:
        body = error.read()
        decoded = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return error.reason if isinstance(error.reason, str) else "request rejected"
    if isinstance(decoded, dict) and isinstance(decoded.get("error"), str):
        return decoded["error"]
    return "request rejected"


def _normalize_ephemeral_access(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                EPHEMERAL_ACCESS_CAPABILITY
                if str(key) in {"get_url", "put_url"}
                else _normalize_ephemeral_access(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_ephemeral_access(item) for item in value]
    return value


def canonical_runtime_request_bytes(request_payload: Mapping[str, object]) -> bytes:
    """Serialize the capability request using OptiumLabs Runtime HTTP v1 hash rules."""

    semantic = dict(request_payload)
    semantic.setdefault("capability", CAPABILITY)
    return json.dumps(
        _normalize_ephemeral_access(semantic),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def runtime_request_sha256(request_payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_runtime_request_bytes(request_payload)).hexdigest()


def _positive_dimension(value: int, field: str) -> int:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _inspect_input_image(image_bytes: bytes) -> tuple[str, str]:
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            image_format = (image.format or "").upper()
    except (OSError, SyntaxError) as exc:
        raise ValueError("image_bytes must contain a valid image") from exc
    media_type = Image.MIME.get(image_format)
    if not image_format or not media_type:
        raise ValueError("input image format is not recognized")
    extension = {"JPEG": "jpg", "TIFF": "tiff"}.get(image_format, image_format.lower())
    return media_type, extension


def _semantic_identity(
    *,
    input_sha256: str,
    input_size_bytes: int,
    target_width: int,
    target_height: int,
) -> str:
    payload = {
        "input_sha256": input_sha256,
        "input_size_bytes": input_size_bytes,
        "target_width": target_width,
        "target_height": target_height,
        "output_media_type": _OUTPUT_MEDIA_TYPE,
        "output_container": _OUTPUT_CONTAINER,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _execution_id(request_hash: str) -> str:
    entropy = f"{request_hash}:{uuid.uuid4().hex}".encode()
    return f"exe_{hashlib.sha256(entropy).hexdigest()}"


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ImageUpscalerError(f"Image upscaler response field {field} must be an object")
    return cast(Mapping[str, object], value)


def _require_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ImageUpscalerError(f"Image upscaler response field {field} is invalid")
    return value


def _require_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImageUpscalerError(f"Image upscaler response field {field} is invalid")
    return value


def _validate_output_bytes(
    image_bytes: bytes,
    *,
    target_width: int,
    target_height: int,
) -> tuple[str, int]:
    if not image_bytes:
        raise ImageUpscalerError("Image upscaler produced an empty output")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            image_format = (image.format or "").upper()
            dimensions = image.size
    except (OSError, SyntaxError) as exc:
        raise ImageUpscalerError("Image upscaler output is not a valid image") from exc
    if dimensions != (target_width, target_height):
        raise ImageUpscalerError(
            "Image upscaler output dimensions do not match the requested target: "
            f"expected {target_width}x{target_height}, got {dimensions[0]}x{dimensions[1]}"
        )
    if image_format != "PNG":
        raise ImageUpscalerError(
            f"Image upscaler output must be PNG, got {image_format or 'unknown'}"
        )
    return hashlib.sha256(image_bytes).hexdigest(), len(image_bytes)


class OptiumLabsImageUpscaler(ImageUpscaler):
    """HTTP client for the OptiumLabs image-upscaler deployed behind Salad Gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        salad_api_key: str,
        storage: _UpscalerObjectStorage,
        temp_dir: Path,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        signed_url_expiry_seconds: int = _DEFAULT_SIGNED_URL_EXPIRY_SECONDS,
        gateway_recovery_timeout_seconds: float = _DEFAULT_GATEWAY_RECOVERY_TIMEOUT_SECONDS,
        gateway_recovery_poll_seconds: float = _DEFAULT_GATEWAY_RECOVERY_POLL_SECONDS,
        http_client: _RuntimeHttpClient | None = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be a valid HTTP(S) URL")
        if not salad_api_key.strip():
            raise ValueError("salad_api_key must not be empty")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if signed_url_expiry_seconds <= 0:
            raise ValueError("signed_url_expiry_seconds must be positive")
        if gateway_recovery_timeout_seconds <= 0:
            raise ValueError("gateway_recovery_timeout_seconds must be positive")
        if gateway_recovery_poll_seconds <= 0:
            raise ValueError("gateway_recovery_poll_seconds must be positive")

        self._base_url = normalized_url
        self._salad_api_key = salad_api_key.strip()
        self._storage = storage
        self._temp_dir = temp_dir
        self._request_timeout_seconds = request_timeout_seconds
        self._signed_url_expiry_seconds = signed_url_expiry_seconds
        self._gateway_recovery_timeout_seconds = gateway_recovery_timeout_seconds
        self._gateway_recovery_poll_seconds = gateway_recovery_poll_seconds
        self._http_client = http_client or _UrllibRuntimeHttpClient()

    def check_storage_ready(self) -> None:
        """Verify the same R2 HEAD path used for result recovery before Salad starts.

        Use an intentionally absent object key. Both a successful HEAD and
        an object-not-found response establish connectivity; only an actual
        transport/authentication problem prevents paid GPU startup.
        """
        try:
            self._storage.stat("image-upscaler/runtime/connectivity-preflight")
        except Exception as exc:
            raise ImageUpscalerError(
                "Cloudflare R2 is unavailable for image finishing. "
                "Salad was not started; saved GPT images and finishing "
                "checkpoints are unchanged. Retry --images-only after R2 "
                "connectivity recovers."
            ) from exc

    def upscale(
        self,
        image_bytes: bytes,
        *,
        target_width: int,
        target_height: int,
    ) -> bytes:
        target_width = _positive_dimension(target_width, "target_width")
        target_height = _positive_dimension(target_height, "target_height")
        input_media_type, extension = _inspect_input_image(image_bytes)
        input_sha = hashlib.sha256(image_bytes).hexdigest()
        semantic_id = _semantic_identity(
            input_sha256=input_sha,
            input_size_bytes=len(image_bytes),
            target_width=target_width,
            target_height=target_height,
        )
        job_id = f"image-upscale-{semantic_id}"
        request_id = f"req_{semantic_id}"
        input_artifact_id = f"input-sha256-{input_sha}"
        output_artifact_id = f"image-upscale-{semantic_id}-png"

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="optium-image-upscale-", dir=self._temp_dir) as raw:
            work_dir = Path(raw)
            input_path = work_dir / f"input.{extension}"
            output_path = work_dir / "output.png"
            input_path.write_bytes(image_bytes)

            object_prefix = f"image-upscaler/runtime/{semantic_id}"
            input_key = f"{object_prefix}/input.{extension}"
            output_key = f"{object_prefix}/output.png"
            # An earlier gateway failure may have hidden a successful GPU
            # inference. Reconcile the exact content-addressed R2 result
            # before submitting anything to Salad again.
            if self._storage.stat(output_key) is not None:
                self._storage.download(output_key, output_path)
                recovered = output_path.read_bytes()
                _validate_output_bytes(
                    recovered,
                    target_width=target_width,
                    target_height=target_height,
                )
                self._best_effort_delete(input_key)
                self._best_effort_delete(output_key)
                logger.info("Recovered completed image upscale from its R2 output")
                return recovered

            uploaded = False
            request_sent = False
            request_completed = False
            succeeded = False
            try:
                self._storage.upload(
                    input_path,
                    input_key,
                    content_type=input_media_type,
                    metadata={
                        "purpose": "image-upscaler-input",
                        "input-sha256": input_sha,
                    },
                )
                uploaded = True
                request_payload = self._build_capability_request(
                    job_id=job_id,
                    request_id=request_id,
                    input_artifact_id=input_artifact_id,
                    input_sha=input_sha,
                    input_size_bytes=len(image_bytes),
                    input_get_url=self._storage.presign_get(
                        input_key,
                        expires_seconds=self._signed_url_expiry_seconds,
                    ),
                    output_artifact_id=output_artifact_id,
                    output_get_url=self._storage.presign_get(
                        output_key,
                        expires_seconds=self._signed_url_expiry_seconds,
                    ),
                    output_put_url=self._storage.presign_put(
                        output_key,
                        expires_seconds=self._signed_url_expiry_seconds,
                    ),
                    target_width=target_width,
                    target_height=target_height,
                )
                request_hash = runtime_request_sha256(request_payload)
                execution_id = _execution_id(request_hash)
                envelope: dict[str, object] = {
                    "schema_version": RUNTIME_REQUEST_SCHEMA,
                    "execution_id": execution_id,
                    "request_sha256": request_hash,
                    "capability_contract": CAPABILITY_CONTRACT,
                    "request": request_payload,
                }
                request_sent = True
                response = self._post_runtime_with_gateway_recovery(
                    envelope=envelope,
                    output_key=output_key,
                )
                request_completed = True
                expected_output = self._validate_response(
                    response,
                    execution_id=execution_id,
                    request_hash=request_hash,
                    job_id=job_id,
                    request_id=request_id,
                    output_artifact_id=output_artifact_id,
                    target_width=target_width,
                    target_height=target_height,
                )
                self._storage.download(output_key, output_path)
                output_bytes = output_path.read_bytes()
                actual_sha, actual_size = _validate_output_bytes(
                    output_bytes,
                    target_width=target_width,
                    target_height=target_height,
                )
                if actual_sha != expected_output["sha256"]:
                    raise ImageUpscalerError(
                        "Downloaded output SHA-256 does not match runtime evidence"
                    )
                if actual_size != expected_output["size_bytes"]:
                    raise ImageUpscalerError(
                        "Downloaded output size does not match runtime evidence"
                    )
                succeeded = True
                return output_bytes
            finally:
                # A transport failure after POST is ambiguous: Salad may still be writing
                # through the signed URLs. Preserve those objects so a retry with the same
                # semantic request can reuse/reconcile the request-scoped output safely.
                if succeeded or request_completed or not request_sent:
                    if uploaded:
                        self._best_effort_delete(input_key)
                    self._best_effort_delete(output_key)

    def _post_runtime_with_gateway_recovery(
        self,
        *,
        envelope: Mapping[str, object],
        output_key: str,
    ) -> Mapping[str, object]:
        url = f"{self._base_url}/v1/execute"
        headers = {"Salad-Api-Key": self._salad_api_key}
        deadline = time.monotonic() + self._gateway_recovery_timeout_seconds
        last_error: ImageUpscalerError | None = None
        wait_for_output = False
        tls_record_retries = 0

        while True:
            if last_error is not None:
                if time.monotonic() >= deadline:
                    raise last_error
                time.sleep(self._gateway_recovery_poll_seconds)
                if wait_for_output and self._storage.stat(output_key) is None:
                    continue

            try:
                return self._http_client.post_json(
                    url,
                    envelope,
                    headers=headers,
                    timeout_seconds=self._request_timeout_seconds,
                )
            except ImageUpscalerError as exc:
                if exc.status_code == 524:
                    if last_error is None or not wait_for_output:
                        logger.warning(
                            "Image upscaler gateway returned HTTP 524; waiting for "
                            "request-scoped output in R2 before retrying the same request."
                        )
                    wait_for_output = True
                    last_error = exc
                    continue
                if exc.status_code in {502, 503}:
                    if last_error is None:
                        logger.warning(
                            "Image upscaler gateway returned HTTP %s; the container may "
                            "still be warming up or serving an earlier inference. "
                            "Retrying the same deterministic request until it recovers.",
                            exc.status_code,
                        )
                    last_error = exc
                    continue
                if _is_tls_record_error(exc):
                    # The worker can fail an R2 GET/PUT on a corrupted TLS
                    # record after accepting our POST. Keep the identical
                    # execution envelope, signed URLs and R2 output key.
                    # Cap retries to avoid repeatedly charging GPU time if
                    # the remote TLS connection stays broken.
                    if tls_record_retries >= _MAX_TLS_RECORD_RETRIES:
                        raise
                    tls_record_retries += 1
                    logger.warning(
                        "Image upscaler encountered remote TLS record corruption; "
                        "retrying the same request (%s/%s) without clearing R2.",
                        tls_record_retries, _MAX_TLS_RECORD_RETRIES,
                    )
                    last_error = exc
                    continue
                raise

    def _build_capability_request(
        self,
        *,
        job_id: str,
        request_id: str,
        input_artifact_id: str,
        input_sha: str,
        input_size_bytes: int,
        input_get_url: str,
        output_artifact_id: str,
        output_get_url: str,
        output_put_url: str,
        target_width: int,
        target_height: int,
    ) -> dict[str, object]:
        return {
            "schema_version": CAPABILITY_CONTRACT,
            "capability": CAPABILITY,
            "job_id": job_id,
            "request_id": request_id,
            "inputs": [
                {
                    "artifact_id": input_artifact_id,
                    "sha256": input_sha,
                    "size_bytes": input_size_bytes,
                    "get_url": input_get_url,
                }
            ],
            "requirements": {
                "width": target_width,
                "height": target_height,
                "scale": None,
                "media_type": _OUTPUT_MEDIA_TYPE,
                "container": _OUTPUT_CONTAINER,
            },
            "output": {
                "artifact_id": output_artifact_id,
                "get_url": output_get_url,
                "put_url": output_put_url,
            },
        }

    def _validate_response(
        self,
        response: Mapping[str, object],
        *,
        execution_id: str,
        request_hash: str,
        job_id: str,
        request_id: str,
        output_artifact_id: str,
        target_width: int,
        target_height: int,
    ) -> dict[str, object]:
        if _require_text(response, "schema_version") != RUNTIME_RESPONSE_SCHEMA:
            raise ImageUpscalerError(
                "Image upscaler returned an unsupported runtime response schema"
            )
        if _require_text(response, "execution_id") != execution_id:
            raise ImageUpscalerError("Image upscaler response execution_id does not match request")
        if _require_text(response, "request_sha256") != request_hash:
            raise ImageUpscalerError(
                "Image upscaler response request_sha256 does not match request"
            )
        if _require_text(response, "capability_contract") != CAPABILITY_CONTRACT:
            raise ImageUpscalerError(
                "Image upscaler response capability_contract does not match request"
            )

        result = _require_mapping(response.get("result"), "result")
        if _require_text(result, "schema_version") != RESULT_SCHEMA:
            raise ImageUpscalerError(
                "Image upscaler returned an unsupported capability result schema"
            )
        if _require_text(result, "job_id") != job_id:
            raise ImageUpscalerError("Image upscaler response job_id does not match request")
        if _require_text(result, "request_id") != request_id:
            raise ImageUpscalerError("Image upscaler response request_id does not match request")

        output = _require_mapping(result.get("output"), "result.output")
        if _require_text(output, "artifact_id") != output_artifact_id:
            raise ImageUpscalerError("Image upscaler output artifact_id does not match request")
        if _require_int(output, "width") != target_width or _require_int(
            output, "height"
        ) != target_height:
            raise ImageUpscalerError(
                "Image upscaler response dimensions do not match requested target"
            )
        if _require_text(output, "media_type").casefold() != _OUTPUT_MEDIA_TYPE:
            raise ImageUpscalerError("Image upscaler response media_type is not image/png")
        if _require_text(output, "container").casefold().lstrip(".") != _OUTPUT_CONTAINER:
            raise ImageUpscalerError("Image upscaler response container is not png")
        output_sha = _require_text(output, "sha256")
        if len(output_sha) != 64 or any(char not in "0123456789abcdef" for char in output_sha):
            raise ImageUpscalerError("Image upscaler response output SHA-256 is invalid")
        output_size = _require_int(output, "size_bytes")
        if output_size <= 0:
            raise ImageUpscalerError("Image upscaler response output size is invalid")
        return {"sha256": output_sha, "size_bytes": output_size}

    def _best_effort_delete(self, key: str) -> None:
        try:
            self._storage.delete(key)
        except Exception as exc:  # pragma: no cover - cleanup must not mask primary result/error
            logger.warning("Could not delete temporary image-upscaler R2 object %s: %s", key, exc)


def create_optium_image_upscaler(
    *,
    base_url: str,
    salad_api_key: str,
    r2_endpoint_url: str,
    r2_bucket: str,
    r2_access_key_id: str,
    r2_secret_access_key: str,
    temp_dir: Path,
) -> OptiumLabsImageUpscaler:
    from ai_video_factory.providers.r2 import create_r2_storage

    storage: R2ObjectStorage = create_r2_storage(
        endpoint_url=r2_endpoint_url,
        bucket=r2_bucket,
        access_key_id=r2_access_key_id,
        secret_access_key=r2_secret_access_key,
    )
    return OptiumLabsImageUpscaler(
        base_url=base_url,
        salad_api_key=salad_api_key,
        storage=storage,
        temp_dir=temp_dir,
    )
