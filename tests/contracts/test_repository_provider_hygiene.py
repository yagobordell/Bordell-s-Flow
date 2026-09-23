import subprocess
from pathlib import Path

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
RETIRED_MARKERS = (
    bytes((102, 108, 117, 120)),
    bytes((107, 108, 101, 105, 110)),
    bytes((98, 108, 97, 99, 107, 45, 102, 111, 114, 101, 115, 116)),
    bytes((98, 108, 97, 99, 107, 32, 102, 111, 114, 101, 115, 116)),
)


def _tracked_repository_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        ROOT / relative.decode("utf-8")
        for relative in completed.stdout.split(b"\0")
        if relative
    ]


def test_repository_has_no_retired_image_provider_references() -> None:
    offenders: list[str] = []

    for path in _tracked_repository_files():
        if not path.is_file():
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
        "Retired image-provider identifiers remain in tracked repository paths or contents: "
        + ", ".join(sorted(offenders))
    )
