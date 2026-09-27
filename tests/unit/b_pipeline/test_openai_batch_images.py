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
        assert row["url"] in {"/v1/images/generations", "/v1/images/edits"}
        assert row["method"] == "POST"
        assert row["body"]["n"] == 1
        assert filename.endswith(".jsonl")
        beat = row["custom_id"].split("-")[1]
        file_id = f"file-{beat}"
        with self.lock:
            self.uploads.append((filename, row))
            self.by_file[file_id] = row
        return file_id

    def create_batch(
        self, file_id: str, *, beat_id: str, fingerprint: str,
        endpoint: str = "/v1/images/generations",
    ):
        with self.lock:
            self.created.append((file_id, beat_id, fingerprint))
        if self.barrier:
            self.barrier.wait(timeout=8)
        if self.failures.get(beat_id) == "unknown_create":
            raise URLError("batch creation POST response lost")
        batch = {
            "id": f"batch-{beat_id}",
            "input_file_id": file_id,
            "endpoint": endpoint,
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
                    "data": [{"b64_json": base64.b64encode(_png(
                        size=tuple(map(int, request["body"]["size"].split("x")))
                    )).decode()}],
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
        content = _png(size=tuple(map(int, kwargs["size"].split("x"))))
        target = kwargs["destination"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "width": tuple(map(int, kwargs["size"].split("x")))[0],
            "height": tuple(map(int, kwargs["size"].split("x")))[1],
            "size": kwargs["size"],
            "output_format": "png", "usage": {"input_tokens": 4}, "created": 8,
        }


def _opts(**overrides):
    return images.ImageBatchOptions(**overrides)



def _avatar_plan(tmp_path: Path, *beats: tuple[str, str]) -> tuple[dict, bytes]:
    avatar = _png(size=(3, 2))
    (tmp_path / "avatar.png").write_bytes(avatar)
    plan = {"avatar": {
        "file": "avatar.png", "sha256": hashlib.sha256(avatar).hexdigest(),
    }, "blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        *({"beat_id": beat, "visual_type": "media_image", "description": description}
          for beat, description in beats),
    ]}]}
    return plan, avatar


def test_only_avatar_descriptions_attach_selected_png_to_edit_batches(tmp_path: Path) -> None:
    plan, avatar = _avatar_plan(
        tmp_path, ("1B", "The AVATAR walking through the market"),
        ("1C", "An ancient city at dusk"), ("1D", "A portrait of avatar in the garden"),
    )
    original = json.loads(json.dumps(plan))
    batch = FakeBatch()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert [item["beat_id"] for item in result["items"]] == ["1B", "1C", "1D"]
    assert len(batch.created) == 3
    assert plan == original
    for _filename, row in batch.uploads:
        body = row["body"]
        beat = row["custom_id"].split("-")[1]
        if beat in {"1B", "1D"}:
            assert row["url"] == "/v1/images/edits"
            assert batch.by_batch["batch-" + beat]["endpoint"] == "/v1/images/edits"
            assert len(body["images"]) == 1
            url = body["images"][0]["image_url"]
            assert url.startswith("data:image/png;base64,")
            assert base64.b64decode(url.split(",", 1)[1]) == avatar
            assert "character reference" in body["prompt"]
            assert result["items"][[item["beat_id"] for item in result["items"]].index(beat)][
                "avatar_reference_sha256"
            ] == hashlib.sha256(avatar).hexdigest()
        else:
            assert row["url"] == "/v1/images/generations"
            assert "images" not in body
            assert body["prompt"] == images.effective_image_prompt("An ancient city at dusk")
    previous_calls = len(batch.created)
    again = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert again["items"] == result["items"]
    assert len(batch.created) == previous_calls


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("AVATAR at the harbor", True),
        ("near the avatar.", True),
        ("avatar-like silhouette", True),
        ("avatars in a crowd", False),
        ("avatarium", False),
        ("a temple at dawn", False),
    ],
)
def test_avatar_reference_requires_whole_word(description: str, expected: bool) -> None:
    assert images._avatar_requested(description) is expected


@pytest.mark.parametrize("failure", ["missing", "changed", "linked", "missing_metadata"])
def test_avatar_preflight_blocks_unverified_reference_before_any_paid_work(
    tmp_path: Path, failure: str,
) -> None:
    plan, _avatar = _avatar_plan(tmp_path, ("1B", "Avatar looking to camera"))
    target = tmp_path / "avatar.png"
    if failure == "missing":
        target.unlink()
    elif failure == "changed":
        target.write_bytes(_png(size=(4, 3)))
    elif failure == "linked":
        original = tmp_path / "source.png"
        target.replace(original)
        target.symlink_to(original)
    else:
        plan.pop("avatar")
    batch = FakeBatch()
    with pytest.raises(images.ImageBatchError, match="avatar|Avatar"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
        )
    assert batch.uploads == batch.created == []
    assert not (tmp_path / "images/manifest.json").exists()


def test_failed_edit_batch_keeps_avatar_reference_in_direct_fallback(tmp_path: Path) -> None:
    plan, avatar = _avatar_plan(tmp_path, ("1B", "The avatar behind a microphone"))
    batch = FakeBatch(failures={"1B": "failed"})
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(),
        batch_client=batch, direct_client=direct,
    )
    assert result["items"][0]["provider"] == "openai_direct_fallback"
    assert len(direct.calls) == 1
    url = direct.calls[0]["reference_image_url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == avatar
    assert "character reference" in direct.calls[0]["prompt"]


def test_existing_paid_text_batch_cannot_silently_switch_to_avatar_edit(
    tmp_path: Path,
) -> None:
    plan, _avatar = _avatar_plan(tmp_path, ("1B", "Avatar seated by the river"))
    job = images.described_beats(plan)[0]
    old_fingerprint = images._fingerprint(job, _opts())
    images._atomic_json(tmp_path / "images/block_1/1B.json", {
        **job, "schema_version": "openai-image-batch-v1",
        "fingerprint": old_fingerprint, "status": "batch_submitted",
    })
    batch = FakeBatch()
    with pytest.raises(images.ImageBatchError, match="differs"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
        )
    assert not batch.created



def test_images_only_uses_the_saved_run_avatar_for_referenced_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "input" / "roma.txt"
    script.parent.mkdir()
    script.write_bytes(b"Texto original.")
    output = tmp_path / "output" / "roma"
    (output / "B2").mkdir(parents=True)
    (output / "B2/merged_output.json").write_text("{}", encoding="utf-8")
    plan, avatar = _avatar_plan(output, ("1B", "Avatar studying the map"))
    (output / "visual_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (output / "run_report.json").write_text(json.dumps({
        "run": {
            "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "status": "failed",
        },
        "api_costs": {"run_status": "failed"},
        "timings": {"status": "failed"},
    }), encoding="utf-8")
    batch = FakeBatch()

    def fake_image_request(destination, saved_plan, *, api_key, options):
        assert destination == output
        assert api_key == "fake-openai-key"
        return images.generate_b2_images(
            destination, saved_plan, api_key=None, options=options,
            batch_client=batch,
        )

    monkeypatch.setattr(runner, "generate_b2_images", fake_image_request)
    monkeypatch.setattr(runner.settings, "openai_api_key", "fake-openai-key")
    asyncio.run(runner._run_images_only(script, tmp_path / "output"))
    assert len(batch.created) == 1
    row = batch.uploads[0][1]
    assert row["url"] == "/v1/images/edits"
    image_url = row["body"]["images"][0]["image_url"]
    assert base64.b64decode(image_url.split(",", 1)[1]) == avatar
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert report["run"]["image_generation"]["status"] == "completed"


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


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"])
@pytest.mark.parametrize("batch_failure", [None, "failed"])
def test_gpt_image_25_uses_same_model_for_individual_batch_and_direct_fallback(
    tmp_path: Path, model: str, batch_failure: str | None,
) -> None:
    opts = _opts(model_id=model)
    batch = FakeBatch(
        failures={"1B": batch_failure} if batch_failure is not None else {}
    )
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, _plan("1B"), api_key=None,
        options=opts, batch_client=batch, direct_client=direct,
    )
    assert result["status"] == "completed"
    assert len(batch.uploads) == len(batch.created) == 1
    assert batch.uploads[0][1]["body"] == opts.request("Original 1B  ")
    assert batch.uploads[0][1]["body"]["model"] == model
    assert batch.uploads[0][1]["body"]["n"] == 1
    if batch_failure is None:
        assert result["items"][0]["provider"] == "openai_batch"
        assert direct.calls == []
    else:
        assert result["items"][0]["provider"] == "openai_direct_fallback"
        assert result["items"][0]["fallback_reason"] == "batch_failed"
        assert len(direct.calls) == 1
        assert direct.calls[0]["model"] == model
        assert direct.calls[0]["prompt"] == images.effective_image_prompt(
            "Original 1B  "
        )
    assert result["items"][0]["model_id"] == model


@pytest.mark.parametrize("old_model", ["gpt-image-2", "gpt-image-2.5"])
def test_b2_image_batch_rejects_old_or_incomplete_model_id(
    old_model: str,
) -> None:
    with pytest.raises(ValueError, match="Unsupported official GPT Image model"):
        _opts(model_id=old_model)


def test_text_and_image_configuration_defaults_use_gpt6_and_image25() -> None:
    from ai_video_factory.config import Settings

    assert Settings.model_fields["openai_model"].default == "gpt-6-luna"
    assert Settings.model_fields["openai_reasoning_effort"].default == "medium"
    assert "openai_b_model" not in Settings.model_fields
    assert "openai_b_reasoning_effort" not in Settings.model_fields
    assert Settings.model_fields["openai_image_model"].default == "gpt-image-2.5-flare"


def _numbered_grid_plan(
    numbered: int, *, before: int = 0, after: int = 0,
    video_positions: tuple[int, ...] = (),
) -> dict:
    beats = [{"beat_id": "1A", "visual_type": "avatar", "description": None}]
    for index in range(before + numbered + after):
        if index < before:
            description = f"Previous standalone photo {index + 1}"
        elif index < before + numbered:
            description = f"{index - before + 1}. Continuous scene step {index - before + 1}"
        else:
            description = f"Following standalone photo {index - before - numbered + 1}"
        beats.append({
            "beat_id": f"1{chr(ord('B') + index)}",
            "visual_type": "media_video" if index in video_positions else "media_image",
            "description": description,
        })
    return {"blocks": [{"block_id": 1, "beats": beats}]}


@pytest.mark.parametrize(
    ("numbered", "columns", "rows", "size"),
    [
        (2, 2, 2, "2560x1440"),
        (3, 2, 2, "2560x1440"),
        (4, 2, 2, "2560x1440"),
        (5, 3, 2, "3840x1440"),
        (6, 3, 2, "3840x1440"),
        (7, 3, 3, "3840x2160"),
        (8, 3, 3, "3840x2160"),
        (9, 3, 3, "3840x2160"),
    ],
)
def test_numbered_beats_make_one_full_resolution_grid_per_shape(
    tmp_path: Path, numbered: int, columns: int, rows: int, size: str,
) -> None:
    padding = columns * rows - numbered
    plan = _numbered_grid_plan(numbered, after=padding)
    unchanged = json.loads(json.dumps(plan))
    batch = FakeBatch()
    manifest = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert manifest["status"] == "completed"
    assert manifest["grid_count"] == 1
    assert manifest["total_described_beats"] == numbered + padding
    assert manifest["total_generation_jobs"] == 1
    assert len(batch.uploads) == len(batch.created) == len(manifest["items"]) == 1
    artifact = manifest["items"][0]
    assert artifact["size"] == size
    assert artifact["grid"]["columns"] == columns
    assert artifact["grid"]["rows"] == rows
    assert artifact["grid"]["panel_width"] == 1280
    assert artifact["grid"]["panel_height"] == 720
    assert len(artifact["panels"]) == columns * rows
    assert len(artifact["grid"]["numbered_beat_ids"]) == numbered
    assert [panel["source"] for panel in artifact["panels"]] == (
        ["numbered"] * numbered + ["filler"] * padding
    )
    assert all(
        (panel["row"], panel["column"]) == (index // columns, index % columns)
        for index, panel in enumerate(artifact["panels"])
    )
    request = batch.uploads[0][1]
    assert request["url"] == "/v1/images/generations"
    assert request["body"]["size"] == size
    assert request["body"]["n"] == 1
    assert "Panel 1" in request["body"]["prompt"]
    assert f"Panel {columns * rows}" in request["body"]["prompt"]
    assert plan == unchanged
    with Image.open(tmp_path / artifact["file"]) as grid:
        assert grid.size == tuple(map(int, size.split("x")))
    assert not any((tmp_path / "images").glob("block_*/*.png"))
    resumed = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert resumed["items"] == manifest["items"]
    assert len(batch.created) == 1


def test_video_start_images_and_following_images_share_one_grid(tmp_path: Path) -> None:
    plan = _numbered_grid_plan(2, after=2, video_positions=(1, 3))
    batch = FakeBatch()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert len(batch.created) == 1
    assert [panel["visual_type"] for panel in result["items"][0]["panels"]] == [
        "media_image", "media_video", "media_image", "media_video",
    ]
    assert [panel["beat_id"] for panel in result["items"][0]["panels"]] == [
        "1B", "1C", "1D", "1E",
    ]


def test_filler_prefers_following_unassigned_beats_over_preceding(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, before=2, after=2)
    batch = FakeBatch()
    manifest = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert manifest["total_generation_jobs"] == 3
    assert manifest["grid_count"] == 1
    grid = next(item for item in manifest["items"] if item.get("grid"))
    assert [p["beat_id"] for p in grid["panels"]] == ["1D", "1E", "1F", "1G"]
    assert {item["beat_id"] for item in manifest["items"] if not item.get("grid")} == {
        "1B", "1C",
    }


def test_grid_uses_nearest_free_previous_images_when_no_following(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, before=2)
    batch = FakeBatch()
    manifest = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert manifest["total_generation_jobs"] == 1
    assert [panel["beat_id"] for panel in manifest["items"][0]["panels"]] == [
        "1B", "1C", "1D", "1E",
    ]
    assert [panel["source"] for panel in manifest["items"][0]["panels"]] == [
        "filler", "filler", "numbered", "numbered",
    ]


def test_grid_fails_before_paid_requests_when_no_distinct_fillers(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, after=1)
    batch = FakeBatch()
    with pytest.raises(images.ImageBatchError, match="insufficient free images"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
        )
    assert batch.created == batch.uploads == []
    assert not (tmp_path / "images/manifest.json").exists()


@pytest.mark.parametrize(
    "descriptions",
    [
        ("1. First beat", "3. Missing beat 2"),
        ("1. First beat", "An unrelated photo"),
    ],
)
def test_invalid_continuity_numbering_fails_before_any_paid_request(
    tmp_path: Path, descriptions: tuple[str, str],
) -> None:
    plan = _numbered_grid_plan(2, after=2)
    for beat, description in zip(plan["blocks"][0]["beats"][1:], descriptions):
        beat["description"] = description
    batch = FakeBatch()
    with pytest.raises(images.ImageBatchError, match="number"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
        )
    assert not batch.created


def test_avatar_in_video_grid_uses_selected_run_reference_and_direct_fallback(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, after=2, video_positions=(1,))
    plan["blocks"][0]["beats"][2]["description"] = (
        "2. avatar gently turns toward the window"
    )
    avatar = _png(size=(3, 2))
    (tmp_path / "avatar.png").write_bytes(avatar)
    plan["avatar"] = {
        "file": "avatar.png", "sha256": hashlib.sha256(avatar).hexdigest(),
    }
    batch = FakeBatch(failures={"1B": "failed"})
    direct = FakeDirect()
    manifest = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(),
        batch_client=batch, direct_client=direct,
    )
    assert len(batch.uploads) == len(batch.created) == len(direct.calls) == 1
    row = batch.uploads[0][1]
    assert row["url"] == "/v1/images/edits"
    assert row["body"]["size"] == "2560x1440"
    assert base64.b64decode(
        row["body"]["images"][0]["image_url"].split(",", 1)[1]
    ) == avatar
    assert direct.calls[0]["size"] == "2560x1440"
    assert direct.calls[0]["reference_image_url"] == (
        row["body"]["images"][0]["image_url"]
    )
    assert manifest["items"][0]["provider"] == "openai_direct_fallback"


def test_grid_rejects_existing_individual_paid_state_before_all_uploads(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, after=2)
    images._atomic_json(tmp_path / "images/block_1/1C.json", {
        "schema_version": "openai-image-batch-v1",
        "status": "batch_submitted", "fingerprint": "old",
    })
    batch = FakeBatch()
    with pytest.raises(images.ImageBatchError, match="individual paid image state"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
        )
    assert not batch.uploads


def test_grid_pending_state_prevents_repeated_paid_submission(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, after=2)
    batch = FakeBatch(failures={"1B": "unknown_create"})
    with pytest.raises(images.ImageBatchError, match="unknown"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
        )
    state = tmp_path / "images/grids/block_1_1B.json"
    assert state.is_file()
    assert images.has_pending_image_tasks(tmp_path)
    with pytest.raises(images.ImageBatchError, match="unknown"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
        )
    assert len(batch.created) == 1


def test_grid_png_with_standard_dimensions_is_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(images.ImageBatchError, match="2560x1440"):
        images._save_batch_png(
            {
                "data": [{"b64_json": base64.b64encode(_png()).decode()}],
            },
            tmp_path / "invalid-grid.png",
            options=_opts(),
            size="2560x1440",
        )
    assert not (tmp_path / "invalid-grid.png").exists()


def test_images_only_saves_grid_map_and_defers_preview_without_cropping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "input" / "roma.txt"
    script.parent.mkdir()
    script.write_bytes(b"Un guion.")
    output = tmp_path / "output" / "roma"
    (output / "B2").mkdir(parents=True)
    (output / "B2/merged_output.json").write_text("{}", encoding="utf-8")
    plan = _numbered_grid_plan(2, after=2)
    (output / "visual_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (output / "run_report.json").write_text(json.dumps({
        "run": {
            "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "status": "failed",
        },
        "api_costs": {"run_status": "failed"},
        "timings": {"status": "failed"},
    }), encoding="utf-8")
    batch = FakeBatch()

    def fake_generate(destination, saved_plan, *, api_key, options):
        assert api_key == "fake-openai-key"
        return images.generate_b2_images(
            destination, saved_plan, api_key=None, options=options,
            batch_client=batch,
        )

    monkeypatch.setattr(runner, "generate_b2_images", fake_generate)
    monkeypatch.setattr(runner.settings, "openai_api_key", "fake-openai-key")
    asyncio.run(runner._run_images_only(script, tmp_path / "output"))
    assert len(batch.created) == 1
    assert not (output / "preview").exists()
    saved = json.loads((output / "visual_plan.json").read_text(encoding="utf-8"))
    assert saved["image_assets"] == []
    assert len(saved["image_grids"]) == 1
    assert len(saved["image_grids"][0]["panels"]) == 4
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert report["run"]["image_generation"]["grid_count"] == 1
    assert report["run"]["video_preview"] == {
        "status": "deferred", "reason": "grid_panels_not_cropped", "grid_count": 1,
    }


def test_separate_numbered_runs_make_two_grids_without_duplicate_fillers(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, after=2)
    plan["blocks"][0]["beats"].extend([
        {"beat_id": "1F", "visual_type": "media_video",
         "description": "1. A continuous second scene begins"},
        {"beat_id": "1G", "visual_type": "media_image",
         "description": "2. The second scene continues"},
        {"beat_id": "1H", "visual_type": "media_image",
         "description": "Second scene following photo one"},
        {"beat_id": "1I", "visual_type": "media_video",
         "description": "Second scene following photo two"},
    ])
    batch = FakeBatch()
    manifest = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert manifest["grid_count"] == manifest["total_generation_jobs"] == 2
    assert len(batch.created) == 2
    assert [item["grid"]["numbered_beat_ids"] for item in manifest["items"]] == [
        ["1B", "1C"], ["1F", "1G"],
    ]
    mapped = [panel["beat_id"] for item in manifest["items"] for panel in item["panels"]]
    assert mapped == ["1B", "1C", "1D", "1E", "1F", "1G", "1H", "1I"]


def test_final_grid_can_take_following_free_images_from_next_block(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2)
    plan["blocks"].append({"block_id": 2, "beats": [
        {"beat_id": "2A", "visual_type": "media_video",
         "description": "The following scene image"},
        {"beat_id": "2B", "visual_type": "media_image",
         "description": "An additional still"},
    ]})
    batch = FakeBatch()
    manifest = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    assert manifest["grid_count"] == 1
    assert [p["beat_id"] for p in manifest["items"][0]["panels"]] == [
        "1B", "1C", "2A", "2B",
    ]
    assert len(batch.created) == 1


def test_changed_grid_panel_rejects_resume_without_second_paid_post(
    tmp_path: Path,
) -> None:
    plan = _numbered_grid_plan(2, after=2)
    batch = FakeBatch()
    images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), batch_client=batch,
    )
    changed = json.loads(json.dumps(plan))
    changed["blocks"][0]["beats"][-1]["description"] = "Different last panel"
    with pytest.raises(images.ImageBatchError, match="differs"):
        images.generate_b2_images(
            tmp_path, changed, api_key=None, options=_opts(), batch_client=batch,
        )
    assert len(batch.created) == 1
