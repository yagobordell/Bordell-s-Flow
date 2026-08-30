from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "run_phase7_benchmark_matrix.py"
    spec = importlib.util.spec_from_file_location("run_phase7_benchmark_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = _load_script()


def test_parse_case() -> None:
    case = matrix._parse_case("distilled-fp8-cpu:fp8-cast:cpu")

    assert case.label == "distilled-fp8-cpu"
    assert case.quantization == "fp8-cast"
    assert case.offload == "cpu"


@pytest.mark.parametrize(
    "value",
    [
        "missing-fields",
        "Upper:bf16:none",
        "case:int8:none",
        "case:bf16:network",
    ],
)
def test_parse_case_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        matrix._parse_case(value)


def test_render_case_command_substitutes_matrix_values() -> None:
    case = matrix.BenchmarkCase("distilled-fp8-cpu", "fp8-cast", "cpu")

    command = matrix._render_case_command(
        [
            "python",
            "run.py",
            "{prompt}",
            "{conditioning_image}",
            "{seed}",
            "{quantization_args}",
            "{offload_args}",
            "--output={output}",
        ],
        case,
        prompt="A prompt",
        conditioning_image=Path("frame.png"),
        seed=42,
    )

    assert command == [
        "python",
        "run.py",
        "A prompt",
        str(Path("frame.png").resolve()),
        "42",
        "--quantization",
        "fp8-cast",
        "--offload",
        "cpu",
        "--output={output}",
    ]


def test_validate_command_requires_all_placeholders() -> None:
    with pytest.raises(ValueError, match="quantization"):
        matrix._validate_command_template(
            [
                "python",
                "run.py",
                "{prompt}",
                "{conditioning_image}",
                "{seed}",
                "{offload_args}",
                "{output}",
            ]
        )


def test_render_case_command_omits_default_bf16_and_no_offload_flags() -> None:
    case = matrix.BenchmarkCase("distilled-bf16-none", "bf16", "none")

    command = matrix._render_case_command(
        [
            "python",
            "run.py",
            "{prompt}",
            "{conditioning_image}",
            "{seed}",
            "{quantization_args}",
            "{offload_args}",
            "{output}",
        ],
        case,
        prompt="A prompt",
        conditioning_image=Path("frame.png"),
        seed=42,
    )

    assert command == [
        "python",
        "run.py",
        "A prompt",
        str(Path("frame.png").resolve()),
        "42",
        "{output}",
    ]
