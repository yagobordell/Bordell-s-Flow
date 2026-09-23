import asyncio
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobResponse, OutputArtifact
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus
from ai_video_factory.providers.salad_whisper import (
    SaladWhisperTranscriptionProvider,
    build_whisper_job_request,
)
from ai_video_factory.workers.whisper import WHISPER_MODEL_ID, WhisperTranscript, WhisperWord


class CompletingQueue:
    def __init__(self, storage: LocalObjectStorage, tmp_path: Path) -> None:
        self.storage = storage
        self.tmp_path = tmp_path
        self.last_request = None
        self.last_metadata = None

    def submit(self, request, *, metadata):
        self.last_request = request
        self.last_metadata = metadata
        transcript = WhisperTranscript(
            model_id=WHISPER_MODEL_ID,
            words=[
                WhisperWord(text="Hello", start_seconds=0.1, end_seconds=0.5),
                WhisperWord(text="world", start_seconds=0.6, end_seconds=1.0),
            ],
        )
        source = self.tmp_path / "worker-output.json"
        source.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
        stored = self.storage.upload(
            source,
            request.output.key,
            content_type="application/json",
            metadata={"sha256": sha256_file(source)},
        )
        response = InferenceJobResponse(
            job_id=request.job_id,
            request_sha256=request.fingerprint(),
            output=OutputArtifact(
                key=stored.key,
                content_type=stored.content_type,
                size_bytes=stored.size_bytes,
                sha256=sha256_file(source),
                etag=stored.etag,
            ),
            attempt_count=1,
        )
        return QueueJobSnapshot(
            id="transport-1",
            status=QueueJobStatus.SUCCEEDED,
            output=response.model_dump(mode="json"),
        )

    def get(self, transport_job_id: str):  # pragma: no cover - immediate completion
        raise AssertionError(f"unexpected poll for {transport_job_id}")


def test_salad_whisper_provider_preserves_transcription_contract(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    queue = CompletingQueue(storage, tmp_path)
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=0.01,
        timeout_seconds=1,
    )
    provider = SaladWhisperTranscriptionProvider(
        executor=executor,
        temp_dir=tmp_path / "client",
    )

    words = asyncio.run(
        provider.transcribe_words(
            b"fake-wav",
            filename="narration.wav",
            model=WHISPER_MODEL_ID,
            prompt="Hello world",
            language="en",
        )
    )

    assert [(word.text, word.start_seconds, word.end_seconds) for word in words] == [
        ("Hello", 0.1, 0.5),
        ("world", 0.6, 1.0),
    ]
    assert queue.last_request.task == "audio.whisper.transcribe"
    assert queue.last_request.parameters["language"] == "en"
    assert "prompt" not in queue.last_request.parameters
    assert queue.last_request.inputs[0].key.startswith("phase5/whisper/inputs/")
    assert queue.last_metadata["stage"] == "phase5-alignment"


def test_salad_whisper_provider_rejects_wrong_model(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    queue = CompletingQueue(storage, tmp_path)
    provider = SaladWhisperTranscriptionProvider(
        executor=InferenceJobExecutor(
            queue=queue,
            storage=storage,
            poll_seconds=0.01,
            timeout_seconds=1,
        ),
        temp_dir=tmp_path / "client",
    )

    try:
        asyncio.run(
            provider.transcribe_words(
                b"fake-wav",
                filename="narration.wav",
                model="different-model",
                prompt="Hello world",
                language="en",
            )
        )
    except ValueError as exc:
        assert WHISPER_MODEL_ID in str(exc)
    else:  # pragma: no cover - explicit failure diagnostic
        raise AssertionError("expected wrong Whisper model to be rejected")



def test_salad_whisper_provider_replays_cache_before_queue_submission(
    tmp_path: Path,
) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    queue = CompletingQueue(storage, tmp_path)
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=0.01,
        timeout_seconds=1,
    )
    audio = b"cached-wav"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(audio)
    request = build_whisper_job_request(
        audio_sha256=sha256_file(audio_path),
        filename="narration.wav",
        model=WHISPER_MODEL_ID,
        language="en",
    )
    transcript = WhisperTranscript(
        model_id=WHISPER_MODEL_ID,
        words=[
            WhisperWord(text="Hello", start_seconds=0.1, end_seconds=0.5),
            WhisperWord(text="world", start_seconds=0.6, end_seconds=1.0),
        ],
    )
    source = tmp_path / "cached-words.json"
    source.write_text(transcript.model_dump_json(), encoding="utf-8")
    digest = sha256_file(source)
    storage.upload(
        source,
        request.output.key,
        content_type="application/json",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": digest,
        },
    )
    provider = SaladWhisperTranscriptionProvider(
        executor=executor,
        temp_dir=tmp_path / "client",
    )

    words = asyncio.run(
        provider.transcribe_words(
            audio,
            filename="narration.wav",
            model=WHISPER_MODEL_ID,
            prompt="Hello world",
            language="en",
        )
    )

    assert [word.text for word in words] == ["Hello", "world"]
    assert queue.last_request is None
    input_key = request.inputs[0].key
    assert storage.stat(input_key) is None


def test_whisper_request_identity_ignores_canonical_script_prompt() -> None:
    from ai_video_factory.workers.whisper import WHISPER_GENERATION_PROFILE

    first = build_whisper_job_request(
        audio_sha256="a" * 64,
        filename="narration.wav",
        model=WHISPER_MODEL_ID,
        language="es",
    )
    second = build_whisper_job_request(
        audio_sha256="a" * 64,
        filename="narration.wav",
        model=WHISPER_MODEL_ID,
        language="es",
    )

    assert first.job_id == second.job_id
    assert first.parameters == {
        "generation_profile": WHISPER_GENERATION_PROFILE,
        "model_id": WHISPER_MODEL_ID,
        "language": "es",
    }
    assert "prompt" not in first.parameters
