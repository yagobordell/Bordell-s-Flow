from pathlib import Path

_OPERATIONAL_ROOTS = (
    Path("src"),
    Path("scripts"),
    Path("deploy"),
    Path("docker"),
)
_FORBIDDEN = (
    "flux_schnell",
    "FLUX.1-schnell",
    "ai-video-factory-flux-schnell-worker",
    "ai-video-factory-flux-schnell-jobs",
    "workers/flux-schnell",
    "workers\\flux-schnell",
)


def test_no_operational_flux1_schnell_references_remain() -> None:
    offenders: list[str] = []
    for root in _OPERATIONAL_ROOTS:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for needle in _FORBIDDEN:
                if needle in text:
                    offenders.append(f"{path}: {needle}")
    assert offenders == []
