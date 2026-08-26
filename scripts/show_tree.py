from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


for path in sorted(ROOT.rglob("*")):
    if ".git" in path.parts:
        continue
    indent = "  " * (len(path.relative_to(ROOT).parts) - 1)
    suffix = "/" if path.is_dir() else ""
    print(f"{indent}{path.name}{suffix}")
