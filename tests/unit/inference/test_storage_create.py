from pathlib import Path

from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file


def test_local_create_is_first_writer_wins(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    first = tmp_path / "first.txt"
    stale = tmp_path / "stale.txt"
    first.write_text("winner\n", encoding="utf-8")
    stale.write_text("stale\n", encoding="utf-8")

    created = storage.create(
        first,
        "jobs/job/output.txt",
        content_type="text/plain",
        metadata={
            "job-id": "job",
            "request-sha256": "a" * 64,
            "artifact-sha256": sha256_file(first),
        },
    )
    rejected = storage.create(
        stale,
        "jobs/job/output.txt",
        content_type="text/plain",
        metadata={
            "job-id": "job",
            "request-sha256": "a" * 64,
            "artifact-sha256": sha256_file(stale),
        },
    )

    assert created.created is True
    assert rejected.created is False
    assert rejected.stored.metadata["artifact-sha256"] == sha256_file(first)

    downloaded = tmp_path / "downloaded.txt"
    storage.download("jobs/job/output.txt", downloaded)
    assert downloaded.read_text(encoding="utf-8") == "winner\n"
