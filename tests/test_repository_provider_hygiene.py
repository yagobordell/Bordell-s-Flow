from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
RETIRED_MARKERS = (
    bytes((102, 108, 117, 120)),
    bytes((107, 108, 101, 105, 110)),
    bytes((98, 108, 97, 99, 107, 45, 102, 111, 114, 101, 115, 116)),
    bytes((98, 108, 97, 99, 107, 32, 102, 111, 114, 101, 115, 116)),
)


def test_repository_has_no_retired_image_provider_references() -> None:
    offenders: list[str] = []

    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue

        relative = path.relative_to(ROOT).as_posix()
        relative_bytes = relative.lower().encode("utf-8")
        if any(marker in relative_bytes for marker in RETIRED_MARKERS):
            offenders.append(relative)
            continue

        try:
            payload = path.read_bytes().lower()
        except OSError:
            continue

        if any(marker in payload for marker in RETIRED_MARKERS):
            offenders.append(relative)

    assert not offenders, (
        "Retired image-provider identifiers remain in repository paths or contents: "
        + ", ".join(sorted(offenders))
    )
