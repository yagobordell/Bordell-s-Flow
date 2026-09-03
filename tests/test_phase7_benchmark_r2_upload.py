from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "upload_phase7_benchmark_results.py"
    spec = importlib.util.spec_from_file_location("upload_phase7_benchmark_results", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


uploader = _load_script()


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}

    def upload_file(
        self,
        source: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, object],
    ) -> None:
        path = Path(source)
        self.objects[(bucket, key)] = {
            "body": path.read_bytes(),
            "content_type": ExtraArgs["ContentType"],
            "metadata": ExtraArgs["Metadata"],
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        stored = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(stored["body"]),
            "Metadata": stored["metadata"],
            "ETag": '"fake-etag"',
        }


def test_upload_results_persists_artifacts_and_manifest(tmp_path: Path) -> None:
    for relative_path, _ in uploader.ARTIFACTS:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"content:{relative_path}".encode())

    client = FakeS3Client()
    manifest = uploader.upload_results(
        client,
        results_root=tmp_path,
        bucket="benchmark-bucket",
        prefix="phase7/test/",
    )

    assert len(manifest["objects"]) == len(uploader.ARTIFACTS)
    assert manifest["manifest"]["key"] == "phase7/test/r2-upload-manifest.json"
    assert ("benchmark-bucket", "phase7/test/rtx5090/matrix.json") in client.objects
    assert ("benchmark-bucket", "phase7/test/r2-upload-manifest.json") in client.objects
