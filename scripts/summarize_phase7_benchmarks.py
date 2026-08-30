from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load_matrix(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or document.get("schema_version") != "1":
        raise ValueError(f"Unsupported benchmark matrix: {path}")
    if not isinstance(document.get("hardware_label"), str):
        raise ValueError(f"Matrix has no hardware_label: {path}")
    if not isinstance(document.get("workload"), dict):
        raise ValueError(f"Matrix has no workload: {path}")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"Matrix has no cases: {path}")
    return document


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _comparison_document(matrices: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    if not matrices:
        raise ValueError("At least one benchmark matrix is required")

    workload = matrices[0][1]["workload"]
    hardware_labels: set[str] = set()
    sources: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for path, matrix in matrices:
        hardware_label = matrix["hardware_label"]
        if hardware_label in hardware_labels:
            raise ValueError(f"Duplicate hardware_label: {hardware_label}")
        hardware_labels.add(hardware_label)
        if matrix["workload"] != workload:
            raise ValueError(f"Workload mismatch in {path}")
        sources.append(
            {
                "hardware_label": hardware_label,
                "path": path.as_posix(),
                "sha256": _sha256(path),
            }
        )
        for case in matrix["cases"]:
            if not isinstance(case, dict) or not isinstance(
                case.get("mean_duration_seconds"),
                int | float,
            ):
                raise ValueError(f"Invalid case in {path}")
            rows.append({"hardware_label": hardware_label, **case})

    fastest = min(rows, key=lambda row: row["mean_duration_seconds"])
    return {
        "schema_version": "1",
        "created_at": datetime.now(UTC).isoformat(),
        "workload": workload,
        "source_matrices": sources,
        "cases": rows,
        "fastest_case_by_mean_duration": {
            "hardware_label": fastest["hardware_label"],
            "label": fastest["label"],
        },
        "selection_note": (
            "Speed is not the final selection: review output quality, availability, and cost."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine per-GPU Phase 7 matrices into one auditable comparison JSON."
    )
    parser.add_argument("matrices", nargs="+", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/output/phase7/benchmarks/comparison.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    matrices = [(path, _load_matrix(path)) for path in args.matrices]
    comparison = _comparison_document(matrices)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Phase 7 benchmark comparison: {args.output.resolve()}")


if __name__ == "__main__":
    main()
