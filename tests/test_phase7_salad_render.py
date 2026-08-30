from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "render_phase7_salad.py"
    spec = importlib.util.spec_from_file_location("render_phase7_salad", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load_script()


def test_immutable_image_reference_accepts_digest() -> None:
    image = "docker.io/example/worker@sha256:" + "a" * 64

    assert render._is_immutable_image_reference(image)


def test_immutable_image_reference_rejects_tag() -> None:
    assert not render._is_immutable_image_reference("docker.io/example/worker:phase7")


def test_immutable_image_reference_accepts_repository_with_letter_s() -> None:
    image = "docker.io/example/service@sha256:" + "b" * 64

    assert render._is_immutable_image_reference(image)
