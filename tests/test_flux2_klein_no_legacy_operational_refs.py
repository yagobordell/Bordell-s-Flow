from pathlib import Path

ROOTS = (
    Path("deploy"),
    Path("docker/workers"),
    Path("src"),
    Path("scripts"),
)
FORBIDDEN = (
    "flux_schnell",
    "flux-schnell",
    "FLUX.1-schnell",
    "black-forest-labs/FLUX.1-schnell",
    "ai-video-factory-flux-schnell-worker",
    "ai-video-factory-flux-schnell-jobs",
    "FLUX_SCHNELL",
)


def test_no_operational_flux1_schnell_references_remain() -> None:
    hits: list[str] = []
    for root in ROOTS:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for token in FORBIDDEN:
                if token in text:
                    hits.append(f"{path}: {token}")
    assert hits == []
