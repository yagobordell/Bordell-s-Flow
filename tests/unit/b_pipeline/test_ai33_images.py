"""Offline contracts for B2 descriptions -> resumable AI33 PNG artifacts."""

import asyncio
import hashlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from PIL import Image

from ai_video_factory.providers import ai33_images as ai33
from scripts.pipeline import run_b_pipeline as runner


def _plan() -> dict[str, object]:
    return {
        "schema_version": "b-pipeline-v1",
        "blocks": [
            {
                "block_id": 1,
                "beats": [
                    {"beat_id": "1A", "visual_type": "avatar", "description": None},
                    {
                        "beat_id": "1B",
                        "visual_type": "media_image",
                        "description": "  Producto en una mesa  ",
                    },
                    {
                        "beat_id": "1C",
                        "visual_type": "avatar_media",
                        "description": "Mapa del Mediterráneo",
                    },
                ],
            },
        ],
    }


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.created = 0
        self.polled: dict[str, int] = {}
        self.busy_once = False

    def request_json(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        self.calls.append((method, path, payload))
        if path == "/v1i/task/price":
            return {"success": True, "credits": 123}
        if path == "/v1i/task/generate-image":
            self.created += 1
            return {"success": True, "task_id": f"task-{self.created}"}
        task_id = path.rsplit("/", 1)[-1]
        polls = self.polled.get(task_id, 0)
        self.polled[task_id] = polls + 1
        if self.busy_once and polls == 0:
            raise HTTPError(path, 503, "server_busy", None, io.BytesIO(b""))
        if polls == 0:
            return {"id": task_id, "status": "doing", "progress": 20}
        return {
            "id": task_id,
            "status": "done",
            "progress": 100,
            "credit_cost": 882,
            "metadata": {
                "providerCreditCost": 191,
                "result_images": [
                    {
                        "imageUrl": f"https://media.example.test/{task_id}.png",
                        "mimeType": "image/png",
                        "width": 1280,
                        "height": 720,
                    }
                ],
            },
        }

    def download_png(self, url: str, destination: Path) -> tuple[str, int, int]:
        assert url.startswith("https://media.example.test/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        Image.new("RGB", (1280, 720)).save(buffer, format="PNG")
        data = buffer.getvalue()
        destination.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), 1280, 720


def test_b2_uses_exact_description_and_skips_null_avatar() -> None:
    plan = _plan()
    jobs = ai33.described_beats(plan)
    assert [(job["beat_id"], job["description"]) for job in jobs] == [
        ("1B", "  Producto en una mesa  "),
        ("1C", "Mapa del Mediterráneo"),
    ]
    assert plan["blocks"][0]["beats"][0]["description"] is None
    assert ai33.AI33ImageOptions().request(jobs[0]["description"])["prompt"] == (
        "iphone 6 photo done by an elderly:    Producto en una mesa  "
    )


def test_duplicate_or_unsafe_beat_id_rejected_before_api_calls() -> None:
    plan = _plan()
    plan["blocks"][0]["beats"][1]["beat_id"] = "../../unsafe"
    with pytest.raises(ai33.AI33ImageError, match="Invalid described"):
        ai33.described_beats(plan)
    plan = _plan()
    plan["blocks"][0]["beats"].append(dict(plan["blocks"][0]["beats"][1]))
    with pytest.raises(ai33.AI33ImageError, match="Duplicate"):
        ai33.described_beats(plan)


def test_flare_generation_persists_both_images_and_reuses_completed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClient()
    fake.busy_once = True
    monkeypatch.setattr(ai33.time, "sleep", lambda _seconds: None)
    options = ai33.AI33ImageOptions(poll_interval_seconds=0.01)
    plan = _plan()
    first = ai33.generate_b2_images(
        tmp_path, plan, api_key=None, options=options, client=fake
    )
    assert first["status"] == "completed"
    assert first["total_described_beats"] == 2
    assert len(first["items"]) == 2
    assert first["model_id"] == "gpt-image-2.5-flare"
    assert [item["file"] for item in first["items"]] == [
        "images/block_1/1B.png",
        "images/block_1/1C.png",
    ]
    assert [item["description"] for item in first["items"]] == [
        "  Producto en una mesa  ",
        "Mapa del Mediterráneo",
    ]
    assert all(item["credit_cost"] == 882 for item in first["items"])
    assert all(item["provider_credit_cost"] == 191 for item in first["items"])
    for item in first["items"]:
        assert (tmp_path / item["file"]).is_file()
        state = json.loads(
            (tmp_path / "images" / "block_1" / f"{item['beat_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        assert state["task_id"] == item["task_id"]
        assert state["status"] == "completed"
    counts = len(fake.calls)
    repeated = ai33.generate_b2_images(
        tmp_path, plan, api_key=None, options=options, client=fake
    )
    assert repeated["items"] == first["items"]
    assert all(
        call[2]["prompt"].startswith(ai33.IMAGE_PROMPT_PREFIX)
        for call in fake.calls if call[1] in {"/v1i/task/price", "/v1i/task/generate-image"}
    )
    assert len(fake.calls) == counts
    assert fake.created == 2


def test_submission_network_failure_never_reposts_a_paid_task(tmp_path: Path) -> None:
    class LostSubmission(FakeClient):
        def request_json(self, method, path, payload=None):
            if path == "/v1i/task/generate-image":
                self.created += 1
                raise URLError("lost response after submit")
            return super().request_json(method, path, payload)

    fake = LostSubmission()
    options = ai33.AI33ImageOptions()
    plan = _plan()
    plan["blocks"][0]["beats"] = [plan["blocks"][0]["beats"][1]]
    with pytest.raises(ai33.AI33ImageError, match="outcome unknown"):
        ai33.generate_b2_images(
            tmp_path, plan, api_key=None, options=options, client=fake
        )
    state = json.loads(
        (tmp_path / "images/block_1/1B.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "submitting_unknown"
    with pytest.raises(ai33.AI33ImageError, match="cannot be retried safely"):
        ai33.generate_b2_images(
            tmp_path, plan, api_key=None, options=options, client=fake
        )
    assert fake.created == 1


def test_timeout_keeps_task_id_and_images_stage_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClient()
    plan = _plan()
    plan["blocks"][0]["beats"] = [plan["blocks"][0]["beats"][1]]
    options = ai33.AI33ImageOptions(
        poll_timeout_seconds=1, poll_interval_seconds=0.01
    )
    original_request = fake.request_json
    busy = [True]

    def request_until_resume(method, path, payload=None):
        if busy[0] and method == "GET":
            return {"id": "task-1", "status": "doing", "progress": 10}
        return original_request(method, path, payload)

    fake.request_json = request_until_resume
    ticks = iter([0.0, 0.0, 2.0])
    with monkeypatch.context() as patch:
        patch.setattr(ai33.time, "monotonic", lambda: next(ticks))
        patch.setattr(ai33.time, "sleep", lambda _seconds: None)
        with pytest.raises(ai33.AI33ImageError, match="still pending"):
            ai33.generate_b2_images(
                tmp_path, plan, api_key=None, options=options, client=fake
            )
    state = json.loads(
        (tmp_path / "images/block_1/1B.json").read_text(encoding="utf-8")
    )
    assert state["task_id"] == "task-1"
    assert state["status"] == "submitted"
    assert ai33.has_pending_image_tasks(tmp_path)
    busy[0] = False
    with monkeypatch.context() as patch:
        patch.setattr(ai33.time, "sleep", lambda _seconds: None)
        result = ai33.generate_b2_images(
            tmp_path, plan, api_key=None, options=options, client=fake
        )
    assert result["status"] == "completed"
    assert fake.created == 1
    assert not ai33.has_pending_image_tasks(tmp_path)


def test_modified_b2_prompt_cannot_reuse_stale_image_or_submit_new_task(
    tmp_path: Path
) -> None:
    fake = FakeClient()
    plan = _plan()
    options = ai33.AI33ImageOptions()
    # Simulate an earlier pending request for the first described beat.
    job = ai33.described_beats(plan)[0]
    state_path = tmp_path / "images/block_1/1B.json"
    ai33._atomic_json(
        state_path,
        {
            **job,
            "fingerprint": ai33._fingerprint(job, options),
            "status": "submitted",
            "task_id": "old-task",
        },
    )
    changed = _plan()
    changed["blocks"][0]["beats"][1]["description"] = "Un prompt distinto"
    with pytest.raises(ai33.AI33ImageError, match="differs"):
        ai33.generate_b2_images(
            tmp_path, changed, api_key=None, options=options, client=fake
        )
    assert fake.calls == []


def test_zero_descriptions_needs_no_api_key(tmp_path: Path) -> None:
    plan = {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None}
    ]}]}
    result = ai33.generate_b2_images(
        tmp_path, plan, api_key=None, options=ai33.AI33ImageOptions()
    )
    assert result["items"] == []
    assert result["status"] == "completed"


def test_download_rejects_non_png_and_http_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ai33.AI33ImageClient("test-key")
    output = tmp_path / "image.png"
    with pytest.raises(ai33.AI33ImageError, match="unsafe"):
        client.download_png("http://media.example.test/image.png", output)

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="JPEG")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return buffer.getvalue()

    monkeypatch.setattr(ai33, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ai33.AI33ImageError, match="did not return a PNG"):
        client.download_png("https://media.example.test/image.png", output)
    assert not output.exists()


def test_images_only_uses_existing_b2_without_openai_or_avatar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "input" / "roma.txt"
    script.parent.mkdir()
    script.write_bytes(b"Texto.")
    output = tmp_path / "output" / "roma"
    (output / "B2").mkdir(parents=True)
    (output / "B2" / "merged_output.json").write_text("{}", encoding="utf-8")
    (output / "visual_plan.json").write_text(
        json.dumps(_plan(), ensure_ascii=False), encoding="utf-8"
    )
    (output / "run_report.json").write_text(
        json.dumps({
            "run": {
                "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "status": "failed",
            },
            "api_costs": {"run_status": "failed"},
            "timings": {"status": "failed"},
        }),
        encoding="utf-8",
    )
    observed = []

    def fake_stage(
        destination, plan, *, api_key, options, fallback_enabled, openai_api_key
    ):
        observed.append(
            (destination, plan, api_key, options, fallback_enabled, openai_api_key)
        )
        return {
            **options.public(),
            "items": [{
                "block_id": 1,
                "beat_id": "1B",
                "file": "images/block_1/1B.png",
                "sha256": "test-sha",
                "credit_cost": 882,
            }],
        }

    monkeypatch.setattr(runner, "generate_b2_images", fake_stage)
    monkeypatch.setattr(runner.settings, "ai33_api_key", "test-key")
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(runner.settings, "openai_image_fallback_enabled", True)
    asyncio.run(runner._run_images_only(script, tmp_path / "output"))
    assert len(observed) == 1
    assert observed[0][2] == "test-key"
    assert observed[0][3].model_id == "gpt-image-2.5-flare"
    assert observed[0][4] is True
    assert observed[0][5] == "test-openai-key"
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert report["run"]["status"] == "completed"
    assert report["run"]["image_generation"] == {
        "status": "completed",
        "count": 1,
        "model_id": "gpt-image-2.5-flare",
        "credits": 882,
        "ai33_count": 1,
        "openai_fallback_count": 0,
        "ai33_timeout_charge_may_be_pending": False,
    }
    plan = json.loads((output / "visual_plan.json").read_text(encoding="utf-8"))
    assert plan["image_assets"] == [
        {
            "block_id": 1, "beat_id": "1B",
            "file": "images/block_1/1B.png", "sha256": "test-sha",
            "provider": "ai33",
        }
    ]
