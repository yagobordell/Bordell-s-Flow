from pathlib import Path

from ai_video_factory.workflows.production_runner import ProductionStage


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, stage: ProductionStage) -> None:
        self.calls.append(stage.name)
        for output in stage.outputs:
            if output.suffix:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(f"generated:{stage.name}\n", encoding="utf-8")
            else:
                output.mkdir(parents=True, exist_ok=True)
                (output / "artifact.txt").write_text(stage.name, encoding="utf-8")


def _script(tmp_path: Path, name: str = "stage.py") -> Path:
    path = tmp_path / name
    path.write_text("print('stage')\n", encoding="utf-8")
    return path
