"""Offline contracts for one official OpenAI Batch API job per B2 image."""

import asyncio
import base64
import hashlib
import io
import json
import threading
import time
from pathlib import Path
from urllib.error import URLError

import pytest
from PIL import Image

from ai_video_factory.providers import openai_batch_images as images
from scripts.pipeline import run_b_pipeline as runner


def _png(*, size=(1280, 720)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format="PNG")
    return output.getvalue()


def _plan(*ids: str) -> dict[str, object]:
    return {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        *({"beat_id": beat, "visual_type": "media_image",
           "description": f"Original {beat}  "} for beat in ids),
    ]}]}


class FakeBatch:
    def __init__(
        self, *, failures: dict[str, str] | None = None,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.failures = failures or {}
        self.barrier = barrier
        self.uploads = []
        self.created = []
        self.polled = []
        self.cancelled = []
        self.by_file = {}
        self.by_batch = {}
        self.lock = threading.Lock()

    def upload_jsonl(self, content: bytes, *, filename: str) -> str:
        rows = content.decode("utf-8").splitlines()
        assert len(rows) == 1
        row = json.loads(rows[0])
        assert row["url"] == "/v1/images/generations"
        assert row["method"] == "POST"
        assert row["body"]["n"] == 1
        assert filename.endswith(".jsonl")
        beat = row["custom_id"].split("-")[1]
        file_id = f"file-{beat}"
        with self.lock:
            self.uploads.append((filename, row))
            self.by_file[file_id] = row
        return file_id

    def create_batch(self, file_id: str, *, beat_id: str, fingerprint: str):
        with self.lock:
            self.created.append((file_id, beat_id, fingerprint))
        if self.barrier:
            self.barrier.wait(timeout=8)
        if self.failures.get(beat_id) == "unknown_create":
            raise URLError("batch creation POST response lost")
        batch = {
            "id": f"batch-{beat_id}",
            "input_file_id": file_id,
            "endpoint": "/v1/images/generations",
            "status": "validating",
        }
        with self.lock:
            self.by_batch[batch["id"]] = batch
        return batch

    def retrieve_batch(self, batch_id: str):
        with self.lock:
            self.polled.append(batch_id)
            batch = self.by_batch[batch_id]
        beat = batch_id.removeprefix("batch-")
        failure = self.failures.get(beat)
        status = "in_progress" if failure == "pending" else (
            "failed" if failure == "failed" else "completed"
        )
        return {
            **batch,
            "status": status,
            "output_file_id": f"output-{beat}" if status == "completed" else None,
        }

    def cancel_batch(self, batch_id: str):
        with self.lock:
            self.cancelled.append(batch_id)
        return {"id": batch_id, "status": "cancelling"}

    def fetch_file_content(self, file_id: str) -> bytes:
        beat = file_id.removeprefix("output-")
        request = self.by_file[f"file-{beat}"]
        if self.failures.get(beat) == "bad_result":
            return b'{"custom_id":"incorrect","response":null}\n'
        row = {
            "id": f"batch_req_{beat}",
            "custom_id": request["custom_id"],
            "response": {
                "status_code": 200,
                "request_id": f"request-{beat}",
                "body": {
                    "created": 5,
                    "data": [{"b64_json": base64.b64encode(_png()).decode()}],
                    "usage": {"input_tokens": 3},
                },
            },
            "error": None,
        }
        return (json.dumps(row) + "\n").encode()


class FakeDirect:
    def __init__(self, *, unknown: bool = False) -> None:
        self.calls = []
        self.unknown = unknown
        self.lock = threading.Lock()

    def generate_png(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
        if self.unknown:
            raise URLError("lost direct image POST")
        content = _png()
        target = kwargs["destination"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "width": 1280, "height": 720, "size": "1280x720",
            "output_format": "png", "usage": {"input_tokens": 4}, "created": 8,
        }


def _opts(**overrides):
    return images.ImageBatchOptions(**overrides)


def test_n_images_create_n_single_request_batches_in_parallel_and_preserve_b2_order(
    tmp_path: Path,
) -> None:
    client = FakeBatch(barrier=threading.Barrier(3))
    plan = _plan("1B", "1C", "1D")
    before = json.loads(json.dumps(plan))
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(max_parallel_images=3),
        batch_client=client,
    )
    assert result["status"] == "completed"
    assert result["total_described_beats"] == 3
    assert result["openai_batch_count"] == 3
    assert result["openai_direct_fallback_count"] == 0
    assert len(client.uploads) == len(client.created) == 3
    assert sorted(file_id for file_id, _, _ in client.created) == [
        "file-1B", "file-1C", "file-1D"
    ]
    assert [item["beat_id"] for item in result["items"]] == ["1B", "1C", "1D"]
    assert plan == before
    assert all(item["provider"] == "openai_batch" for item in result["items"])
    for beat in ("1B", "1C", "1D"):
        saved = (tmp_path / f"images/block_1/{beat}.batch.jsonl").read_text()
        rows = saved.splitlines()
        assert len(rows) == 1
        assert json.loads(rows[0])["body"]["prompt"] == (
            f"iphone 6 photo done by an elderly:  Original {beat}  "
        )
        state = json.loads((tmp_path / f"images/block_1/{beat}.json").read_text())
        assert state["batch_id"] == f"batch-{beat}"
        assert state["status"] == "completed"
    assert [row["beat_id"] for row in json.loads(
        (tmp_path / "images/manifest.json").read_text()
    )["items"]] == ["1B", "1C", "1D"]
    first_calls = len(client.created)
    cached = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(max_parallel_images=3),
        batch_client=client,
    )
    assert cached["items"] == result["items"]
    assert len(client.created) == first_calls


def test_30_minute_deadline_is_per_image_and_only_overdue_beat_uses_direct(
    tmp_path: Path,
) -> None:
    opts = _opts()
    client = FakeBatch(failures={"1B": "pending"})
    direct = FakeDirect()
    job = images.described_beats(_plan("1B"))[0]
    fingerprint = images._fingerprint(job, opts)
    custom_id = f"b2-1B-{fingerprint[:12]}"
    client.by_file["file-1B"] = {
        "custom_id": custom_id, "body": opts.request(job["description"]),
    }
    client.by_batch["batch-1B"] = {
        "id": "batch-1B", "input_file_id": "file-1B",
        "endpoint": "/v1/images/generations",
    }
    state_path = tmp_path / "images/block_1/1B.json"
    images._atomic_json(state_path, {
        **job, "schema_version": "openai-image-batch-v1",
        "fingerprint": fingerprint, "custom_id": custom_id,
        "status": "batch_submitted", "input_file_id": "file-1B",
        "batch_id": "batch-1B", "submitted_at_unix": time.time() - 1801,
    })
    result = images.generate_b2_images(
        tmp_path, _plan("1B", "1C"), api_key=None,
        options=opts, batch_client=client, direct_client=direct,
    )
    assert result["openai_batch_count"] == 1
    assert result["openai_direct_fallback_count"] == 1
    assert result["items"][0]["batch_id"] == "batch-1B"
    assert result["items"][0]["fallback_reason"] == "batch_timeout"
    assert result["items"][0]["batch_may_complete_later"] is True
    assert result["items"][1]["provider"] == "openai_batch"
    assert client.cancelled == ["batch-1B"]
    assert len(direct.calls) == 1
    assert direct.calls[0]["model"] == opts.model_id
    assert direct.calls[0]["prompt"] == images.effective_image_prompt("Original 1B  ")
    assert client.created == [("file-1C", "1C", images._fingerprint(
        images.described_beats(_plan("1C"))[0], opts
    ))]
    again = images.generate_b2_images(
        tmp_path, _plan("1B", "1C"), api_key=None,
        options=opts, batch_client=client, direct_client=direct,
    )
    assert again["items"] == result["items"]
    assert len(direct.calls) == 1


def test_terminal_batch_failure_uses_direct_without_blocking_healthy_beat(
    tmp_path: Path,
) -> None:
    batch = FakeBatch(failures={"1B": "failed"})
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, _plan("1B", "1C"), api_key=None,
        options=_opts(), batch_client=batch, direct_client=direct,
    )
    assert result["status"] == "completed"
    assert result["items"][0]["fallback_reason"] == "batch_failed"
    assert result["items"][1]["provider"] == "openai_batch"
    assert len(batch.created) == 2
    assert len(direct.calls) == 1


def test_mismatched_single_batch_result_never_silently_accepts_wrong_beat(
    tmp_path: Path,
) -> None:
    batch = FakeBatch(failures={"1B": "bad_result"})
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, _plan("1B"), api_key=None, options=_opts(),
        batch_client=batch, direct_client=direct,
    )
    assert result["items"][0]["provider"] == "openai_direct_fallback"
    assert result["items"][0]["fallback_reason"] == "batch_result_unusable"
    assert len(direct.calls) == 1


def test_unknown_batch_creation_blocks_second_paid_batch_and_direct_generation(
    tmp_path: Path,
) -> None:
    batch = FakeBatch(failures={"1B": "unknown_create"})
    direct = FakeDirect()
    kwargs = {
        "api_key": None, "options": _opts(),
        "batch_client": batch, "direct_client": direct,
    }
    with pytest.raises(images.ImageBatchError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B", "1C"), **kwargs)
    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    manifest = json.loads((tmp_path / "images/manifest.json").read_text())
    assert state["status"] == "batch_creating_unknown"
    assert manifest["status"] == "incomplete"
    assert [item["beat_id"] for item in manifest["items"]] == ["1C"]
    with pytest.raises(images.ImageBatchError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B", "1C"), **kwargs)
    assert len(batch.created) == 2
    assert direct.calls == []


def test_unknown_direct_post_is_not_repeated(tmp_path: Path) -> None:
    batch = FakeBatch(failures={"1B": "failed"})
    direct = FakeDirect(unknown=True)
    kwargs = {
        "api_key": None, "options": _opts(),
        "batch_client": batch, "direct_client": direct,
    }
    with pytest.raises(images.ImageBatchError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B"), **kwargs)
    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    assert state["status"] == "direct_submitting_unknown"
    with pytest.raises(images.ImageBatchError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B"), **kwargs)
    assert len(direct.calls) == len(batch.created) == 1


def test_legacy_paid_image_state_blocks_new_official_batch_before_any_post(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "images/block_1/1B.json"
    images._atomic_json(state_path, {
        "status": "submitted", "task_id": "existing-ai33-paid-task",
    })
    batch = FakeBatch()
    with pytest.raises(images.ImageBatchError, match="legacy"):
        images.generate_b2_images(
            tmp_path, _plan("1B", "1C"), api_key=None,
            options=_opts(), batch_client=batch,
        )
    assert batch.created == [] and batch.uploads == []
    assert images.has_pending_image_tasks(tmp_path)


def test_changed_prompt_rejected_before_any_new_paid_batch(tmp_path: Path) -> None:
    batch = FakeBatch()
    options = _opts()
    images.generate_b2_images(
        tmp_path, _plan("1B"), api_key=None,
        options=options, batch_client=batch,
    )
    other = _plan("1B")
    other["blocks"][0]["beats"][1]["description"] = "changed"
    with pytest.raises(images.ImageBatchError, match="differs"):
        images.generate_b2_images(
            tmp_path, other, api_key=None, options=options,
            batch_client=batch,
        )
    assert len(batch.created) == 1


def test_empty_plan_does_not_need_an_api_key(tmp_path: Path) -> None:
    result = images.generate_b2_images(
        tmp_path, _plan(), api_key=None, options=_opts()
    )
    assert result["items"] == []
    assert result["status"] == "completed"


def test_images_only_uses_saved_b2_and_official_openai_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "input" / "roma.txt"
    script.parent.mkdir()
    script.write_bytes(b"Texto.")
    output = tmp_path / "output" / "roma"
    (output / "B2").mkdir(parents=True)
    (output / "B2/merged_output.json").write_text("{}", encoding="utf-8")
    (output / "visual_plan.json").write_text(
        json.dumps(_plan("1B")), encoding="utf-8"
    )
    (output / "run_report.json").write_text(
        json.dumps({
            "run": {
                "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "status": "failed",
            },
            "api_costs": {"run_status": "failed"},
            "timings": {"status": "failed"},
        }), encoding="utf-8",
    )
    observed = []

    def fake_stage(destination, plan, *, api_key, options):
        observed.append((destination, plan, api_key, options))
        return {
            "model_id": options.model_id,
            "items": [{
                "block_id": 1, "beat_id": "1B",
                "file": "images/block_1/1B.png",
                "sha256": "fake-sha",
                "provider": "openai_batch",
            }],
        }

    monkeypatch.setattr(runner, "generate_b2_images", fake_stage)
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai-key")
    asyncio.run(runner._run_images_only(script, tmp_path / "output"))
    assert len(observed) == 1
    assert observed[0][2] == "test-openai-key"
    assert observed[0][3].model_id == "gpt-image-2.5-flare"
    report = json.loads((output / "run_report.json").read_text())
    assert report["run"]["image_generation"] == {
        "status": "completed",
        "count": 1,
        "model_id": "gpt-image-2.5-flare",
        "openai_batch_count": 1,
        "openai_direct_fallback_count": 0,
        "batch_may_complete_later": False,
    }
    plan = json.loads((output / "visual_plan.json").read_text())
    assert plan["image_assets"] == [{
        "block_id": 1, "beat_id": "1B", "file": "images/block_1/1B.png",
        "sha256": "fake-sha", "provider": "openai_batch",
    }]
