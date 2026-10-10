from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from PIL import Image

from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.optium_image_upscaler import (
    CAPABILITY_CONTRACT,
    EPHEMERAL_ACCESS_CAPABILITY,
    RUNTIME_REQUEST_SCHEMA,
    ImageUpscalerError,
    OptiumLabsImageUpscaler,
    canonical_runtime_request_bytes,
    runtime_request_sha256,
)


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (20, 40, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> StoredObject:
        del content_type, metadata
        payload = source.read_bytes()
        self.objects[key] = payload
        return StoredObject(
            key=key,
            content_type="image/png",
            size_bytes=len(payload),
            etag=None,
            metadata={},
        )

    def download(self, key: str, destination: Path) -> StoredObject:
        payload = self.objects[key]
        destination.write_bytes(payload)
        return StoredObject(
            key=key,
            content_type="image/png",
            size_bytes=len(payload),
            etag=None,
            metadata={},
        )

    def presign_get(self, key: str, *, expires_seconds: int) -> str:
        return f"https://r2.test/get/{key}?expires={expires_seconds}"

    def presign_put(self, key: str, *, expires_seconds: int) -> str:
        return f"https://r2.test/put/{key}?expires={expires_seconds}"

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def stat(self, key: str) -> StoredObject | None:
        payload = self.objects.get(key)
        if payload is None:
            return None
        return StoredObject(
            key=key,
            content_type="image/png",
            size_bytes=len(payload),
            etag=None,
            metadata={},
        )


class FakeRuntimeClient:
    def __init__(self, storage: FakeStorage, *, wrong_dimensions: bool = False) -> None:
        self.storage = storage
        self.wrong_dimensions = wrong_dimensions
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        request = payload["request"]
        assert isinstance(request, Mapping)
        requirements = request["requirements"]
        output = request["output"]
        assert isinstance(requirements, Mapping)
        assert isinstance(output, Mapping)
        width = int(requirements["width"])
        height = int(requirements["height"])
        if self.wrong_dimensions:
            width += 1
        output_bytes = _png(width, height)
        output_key = _key_from_signed_url(str(output["put_url"]))
        self.storage.objects[output_key] = output_bytes
        self.calls.append(
            {
                "url": url,
                "payload": dict(payload),
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
            }
        )
        return {
            "schema_version": "runtime-http-response/v1",
            "execution_id": payload["execution_id"],
            "request_sha256": payload["request_sha256"],
            "capability_contract": CAPABILITY_CONTRACT,
            "result": {
                "schema_version": "optiumlabs-image-upscale-result/v1",
                "job_id": request["job_id"],
                "request_id": request["request_id"],
                "output": {
                    "artifact_id": output["artifact_id"],
                    "sha256": hashlib.sha256(output_bytes).hexdigest(),
                    "size_bytes": len(output_bytes),
                    "width": width,
                    "height": height,
                    "media_type": "image/png",
                    "container": "png",
                },
                "inputs": [],
                "runtime_identity": {"engine": "realesrgan-pytorch-cuda"},
            },
        }


def _key_from_signed_url(url: str) -> str:
    parsed = urlsplit(url)
    marker = "/put/"
    assert marker in parsed.path
    return unquote(parsed.path.split(marker, 1)[1])


def test_runtime_request_hash_redacts_signed_urls_and_uses_canonical_json() -> None:
    request = {
        "schema_version": CAPABILITY_CONTRACT,
        "job_id": "job",
        "request_id": "request",
        "inputs": [
            {
                "artifact_id": "input",
                "sha256": "a" * 64,
                "size_bytes": 123,
                "get_url": "https://r2.test/input?signature=secret-one",
            }
        ],
        "requirements": {
            "width": 1920,
            "height": 1080,
            "scale": None,
            "media_type": "image/png",
            "container": "png",
        },
        "output": {
            "artifact_id": "output",
            "get_url": "https://r2.test/output?signature=secret-two",
            "put_url": "https://r2.test/output?signature=secret-three",
        },
    }
    canonical = canonical_runtime_request_bytes(request)
    rendered = canonical.decode("utf-8")

    assert "secret-one" not in rendered
    assert "secret-two" not in rendered
    assert "secret-three" not in rendered
    assert rendered.count(EPHEMERAL_ACCESS_CAPABILITY) == 3
    assert '"capability":"image_upscale"' in rendered
    assert runtime_request_sha256(request) == hashlib.sha256(canonical).hexdigest()


def test_upscaler_uses_runtime_http_v1_salad_auth_and_cleans_r2(tmp_path: Path) -> None:
    storage = FakeStorage()
    runtime = FakeRuntimeClient(storage)
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud/",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        http_client=runtime,
    )

    result = provider.upscale(_png(64, 36), target_width=192, target_height=108)

    with Image.open(BytesIO(result)) as image:
        assert image.size == (192, 108)
        assert image.format == "PNG"
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call["url"] == "https://upscaler.salad.cloud/v1/execute"
    assert call["headers"] == {"Salad-Api-Key": "salad-secret"}
    payload = call["payload"]
    assert isinstance(payload, dict)
    assert payload["schema_version"] == RUNTIME_REQUEST_SCHEMA
    assert str(payload["execution_id"]).startswith("exe_")
    request = payload["request"]
    assert isinstance(request, dict)
    assert request["schema_version"] == CAPABILITY_CONTRACT
    assert request["capability"] == "image_upscale"
    assert request["requirements"] == {
        "width": 192,
        "height": 108,
        "scale": None,
        "media_type": "image/png",
        "container": "png",
    }
    assert payload["request_sha256"] == runtime_request_sha256(request)
    assert storage.objects == {}
    assert len(storage.deleted) == 2


def test_upscaler_rejects_runtime_output_with_wrong_dimensions(tmp_path: Path) -> None:
    storage = FakeStorage()
    runtime = FakeRuntimeClient(storage, wrong_dimensions=True)
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        http_client=runtime,
    )

    with pytest.raises(ImageUpscalerError, match="response dimensions"):
        provider.upscale(_png(64, 36), target_width=192, target_height=108)

    assert storage.objects == {}
    assert len(storage.deleted) == 2


def test_upscaler_rejects_invalid_input_before_remote_calls(tmp_path: Path) -> None:
    storage = FakeStorage()
    runtime = FakeRuntimeClient(storage)
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        http_client=runtime,
    )

    with pytest.raises(ValueError, match="valid image"):
        provider.upscale(b"not-an-image", target_width=192, target_height=108)

    assert runtime.calls == []
    assert storage.objects == {}



def test_upscaler_preserves_r2_on_ambiguous_post_failure(tmp_path: Path) -> None:
    class FailingRuntimeClient:
        def post_json(self, *args, **kwargs):
            raise ImageUpscalerError("transport timeout")

    storage = FakeStorage()
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        http_client=FailingRuntimeClient(),
    )

    with pytest.raises(ImageUpscalerError, match="transport timeout"):
        provider.upscale(_png(64, 36), target_width=192, target_height=108)

    assert storage.objects
    assert storage.deleted == []



def test_upscaler_recovers_524_after_output_appears_in_r2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    output_bytes = _png(192, 108)

    class RecoveringRuntimeClient:
        def __init__(self) -> None:
            self.calls: list[Mapping[str, object]] = []

        def post_json(
            self,
            url: str,
            payload: Mapping[str, object],
            *,
            headers: Mapping[str, str],
            timeout_seconds: float,
        ) -> Mapping[str, object]:
            del url, headers, timeout_seconds
            self.calls.append(payload)
            request = payload["request"]
            assert isinstance(request, Mapping)
            output = request["output"]
            assert isinstance(output, Mapping)
            output_key = _key_from_signed_url(str(output["put_url"]))
            if len(self.calls) == 1:
                storage.objects[output_key] = output_bytes
                raise ImageUpscalerError("HTTP 524", status_code=524)
            return {
                "schema_version": "runtime-http-response/v1",
                "execution_id": payload["execution_id"],
                "request_sha256": payload["request_sha256"],
                "capability_contract": CAPABILITY_CONTRACT,
                "result": {
                    "schema_version": "optiumlabs-image-upscale-result/v1",
                    "job_id": request["job_id"],
                    "request_id": request["request_id"],
                    "output": {
                        "artifact_id": output["artifact_id"],
                        "sha256": hashlib.sha256(output_bytes).hexdigest(),
                        "size_bytes": len(output_bytes),
                        "width": 192,
                        "height": 108,
                        "media_type": "image/png",
                        "container": "png",
                    },
                    "inputs": [],
                    "runtime_identity": {},
                },
            }

    runtime = RecoveringRuntimeClient()
    monkeypatch.setattr(
        "ai_video_factory.providers.optium_image_upscaler.time.sleep",
        lambda _: None,
    )
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        gateway_recovery_poll_seconds=0.01,
        http_client=runtime,
    )

    result = provider.upscale(_png(64, 36), target_width=192, target_height=108)

    assert result == output_bytes
    assert len(runtime.calls) == 2
    assert runtime.calls[0] == runtime.calls[1]
    assert storage.objects == {}


def test_upscaler_524_without_output_preserves_r2_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRuntimeClient:
        def post_json(self, *args, **kwargs):
            raise ImageUpscalerError("HTTP 524", status_code=524)

    storage = FakeStorage()
    clock = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(
        "ai_video_factory.providers.optium_image_upscaler.time.monotonic",
        lambda: next(clock),
    )
    monkeypatch.setattr(
        "ai_video_factory.providers.optium_image_upscaler.time.sleep",
        lambda _: None,
    )
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        gateway_recovery_timeout_seconds=0.5,
        gateway_recovery_poll_seconds=0.01,
        http_client=FailingRuntimeClient(),
    )

    with pytest.raises(ImageUpscalerError, match="HTTP 524"):
        provider.upscale(_png(64, 36), target_width=192, target_height=108)

    assert storage.objects
    assert storage.deleted == []



@pytest.mark.parametrize("gateway_status", [502, 503])
def test_upscaler_retries_transient_gateway_error_with_same_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_status: int,
) -> None:
    storage = FakeStorage()
    output_bytes = _png(192, 108)

    class RecoveringRuntimeClient:
        def __init__(self) -> None:
            self.calls: list[Mapping[str, object]] = []

        def post_json(
            self,
            url: str,
            payload: Mapping[str, object],
            *,
            headers: Mapping[str, str],
            timeout_seconds: float,
        ) -> Mapping[str, object]:
            del url, headers, timeout_seconds
            self.calls.append(payload)
            if len(self.calls) == 1:
                raise ImageUpscalerError(
                    f"HTTP {gateway_status}", status_code=gateway_status,
                )
            request = payload["request"]
            assert isinstance(request, Mapping)
            output = request["output"]
            requirements = request["requirements"]
            assert isinstance(output, Mapping)
            assert isinstance(requirements, Mapping)
            output_key = _key_from_signed_url(str(output["put_url"]))
            storage.objects[output_key] = output_bytes
            return {
                "schema_version": "runtime-http-response/v1",
                "execution_id": payload["execution_id"],
                "request_sha256": payload["request_sha256"],
                "capability_contract": CAPABILITY_CONTRACT,
                "result": {
                    "schema_version": "optiumlabs-image-upscale-result/v1",
                    "job_id": request["job_id"],
                    "request_id": request["request_id"],
                    "output": {
                        "artifact_id": output["artifact_id"],
                        "sha256": hashlib.sha256(output_bytes).hexdigest(),
                        "size_bytes": len(output_bytes),
                        "width": int(requirements["width"]),
                        "height": int(requirements["height"]),
                        "media_type": "image/png",
                        "container": "png",
                    },
                    "inputs": [],
                    "runtime_identity": {},
                },
            }

    runtime = RecoveringRuntimeClient()
    monkeypatch.setattr(
        "ai_video_factory.providers.optium_image_upscaler.time.sleep",
        lambda _: None,
    )
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        gateway_recovery_poll_seconds=0.01,
        http_client=runtime,
    )

    result = provider.upscale(_png(64, 36), target_width=192, target_height=108)

    assert result == output_bytes
    assert len(runtime.calls) == 2
    assert runtime.calls[0] == runtime.calls[1]
    assert storage.objects == {}

def test_upscaler_recovers_previous_gateway_result_from_r2_without_post(
    tmp_path: Path,
) -> None:
    storage = FakeStorage()
    class InitialFailure:
        def __init__(self) -> None:
            self.calls = 0

        def post_json(self, *_args, **_kwargs):
            self.calls += 1
            raise ImageUpscalerError("gateway transport lost")

    runtime = InitialFailure()
    provider = OptiumLabsImageUpscaler(
        base_url="https://upscaler.salad.cloud",
        salad_api_key="salad-secret",
        storage=storage,
        temp_dir=tmp_path,
        http_client=runtime,
    )
    source = _png(64, 36)
    with pytest.raises(ImageUpscalerError, match="transport lost"):
        provider.upscale(source, target_width=192, target_height=108)
    assert runtime.calls == 1

    output_key = next(key for key in storage.objects if key.endswith("/output.png") is False)
    output_key = output_key.rsplit("/", 1)[0] + "/output.png"
    expected = _png(192, 108)
    storage.objects[output_key] = expected
    assert provider.upscale(source, target_width=192, target_height=108) == expected
    assert runtime.calls == 1
    assert storage.objects == {}


