"""Human-facing launcher: loose TXT = pending, named folder = started.

Examples:
  data/Jorge.png + data/guion.txt -> data/Jorge/guion/guion.txt on start
  data/Jorge/otro.txt            -> data/Jorge/otro/otro.txt on start
  .\\run.ps1 guion Jorge --generate-video resumes an already started folder.
The audited B pipeline itself retains its legacy CLI and separate recovery files.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

from ai_video_factory.managed_layout import sync_managed_outputs
from ai_video_factory.providers.avatar_library import organize_avatar
from scripts.pipeline.run_b_pipeline import available_avatars, load_avatar_png

CORE = Path(__file__).with_name("run_b_pipeline.py")
WORK_ROOT = Path(".video_factory_work")
_HELP = """usage: run.ps1 [script] [avatar] [options]
Put a new avatar PNG and its first TXT directly in data/. After the first run,
place additional pending TXT files directly in data/<avatar>/.
One raw TXT = pending; a folder with that TXT = started or completed.

Options include --no-image, --no-video, --style, --from B2,
--images-only, --generate-video, --resume-audio, --regenerate-audio,
--publish-only, --list-avatars, --list-scripts, --data-root PATH.
Use --legacy-layout to call the previous B-pipeline interface unchanged.
"""


def _name(value: str, suffix: str) -> str:
    if (
        not value or "/" in value or chr(92) in value
        or value in {".", ".."} or Path(value).name != value
    ):
        raise SystemExit(f"Invalid input name: {value!r}")
    name = value[:-len(suffix)] if value.lower().endswith(suffix) else value
    if not name or name.startswith(".") or name in {".", ".."}:
        raise SystemExit(f"Invalid input name: {value!r}")
    return name


def _choose(candidates: list[str], kind: str) -> str:
    if not candidates:
        raise SystemExit(f"No available {kind} in data/")
    if len(candidates) == 1:
        return candidates[0]
    if not sys.stdin.isatty():
        raise SystemExit(f"Choose {kind} explicitly: {', '.join(candidates)}")
    print(f"Available {kind}:")
    for i, name in enumerate(candidates, 1):
        print(f"  {i}. {name}")
    selected = input(f"Select {kind} number: ").strip()
    if not selected.isdecimal() or not 1 <= int(selected) <= len(candidates):
        raise SystemExit(f"Invalid {kind} selection")
    return candidates[int(selected) - 1]


def _flags(argv: Sequence[str]) -> tuple[str | None, str | None, Path, list[str]]:
    args = list(argv)
    data = Path("data")
    if "--data-root" in args:
        index = args.index("--data-root")
        if index + 1 == len(args):
            raise SystemExit("--data-root requires a path")
        data = Path(args[index + 1])
        del args[index:index + 2]
    positional: list[str] = []
    while args and not args[0].startswith("-") and len(positional) < 2:
        positional.append(args.pop(0))
    return (
        positional[0] if positional else None,
        positional[1] if len(positional) == 2 else None,
        data,
        args,
    )


def prepare_managed_script(
    data: Path, avatar_name: str, script_name: str, *, resume: bool
) -> tuple[Path, Path]:
    """Only move ONE pending TXT; never alter other pending or completed scripts."""
    if data.is_symlink() or data.is_junction():
        raise SystemExit(f"Linked data root is not allowed: {data}")
    avatar_name = _name(avatar_name, ".png")
    script_name = _name(script_name, ".txt")
    avatar_dir = data / avatar_name
    if avatar_dir.is_symlink() or avatar_dir.is_junction():
        raise SystemExit(f"Linked avatar folder is not allowed: {avatar_dir}")
    raw_avatar = data / (avatar_name + ".png")
    organized = avatar_dir / (avatar_name + ".png")
    if raw_avatar.is_symlink() or organized.is_symlink():
        raise SystemExit("Linked PNG avatars are not allowed")
    if raw_avatar.is_file() and organized.is_file():
        raise SystemExit(f"Duplicate PNG avatar: {avatar_name}")
    avatar = raw_avatar if raw_avatar.is_file() else organized
    if not avatar.is_file():
        raise SystemExit(f"Avatar not found: {avatar_name}.png")
    destination = avatar_dir / script_name
    source = destination / (script_name + ".txt")
    if destination.is_symlink() or destination.is_junction() or source.is_symlink():
        raise SystemExit("Linked script run folder is not allowed")
    pending = [avatar_dir / (script_name + ".txt")]
    # Only a NEW, still-loose avatar may claim a loose TXT from data/.
    # After the avatar is organized, new scripts belong inside its folder.
    if raw_avatar.is_file():
        pending.append(data / (script_name + ".txt"))
    for item in pending:
        if item.is_symlink():
            raise SystemExit(f"Linked pending script: {item}")
    available = [item for item in pending if item.is_file()]
    if len(available) > 1:
        raise SystemExit(f"Ambiguous duplicate pending script: {script_name}")
    if resume:
        if available:
            raise SystemExit(
                "A loose TXT is still pending. Resume only an existing started script folder."
            )
        if not source.is_file():
            raise SystemExit(f"No started script to resume: {source}")
        return organized if organized.is_file() else avatar, source
    if destination.exists():
        raise SystemExit(
            f"The script already has a run folder: {destination}. "
            "Use the appropriate resume option instead of overwriting."
        )
    if not available:
        raise SystemExit(
            f"No pending .txt for {avatar_name}: place {script_name}.txt in {avatar_dir}."
        )
    # Validate inputs before moving any user file.
    load_avatar_png(avatar)
    try:
        raw = available[0].read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("The pending TXT must be valid UTF-8") from exc
    if not raw.strip():
        raise SystemExit("The pending TXT is empty")
    # Migrate the PNG into its canonical avatar folder. QR and voice then live beside it.
    if raw_avatar.is_file():
        avatar = organize_avatar(raw_avatar, data)
    destination.mkdir()
    try:
        available[0].rename(source)
    except Exception:
        destination.rmdir()
        raise
    return avatar, source


def _script_options(data: Path, avatar_name: str, resume: bool) -> list[str]:
    avatar_dir = data / avatar_name
    if avatar_dir.is_symlink():
        raise SystemExit("Linked avatar directory is not allowed")
    names: set[str] = set()
    if resume:
        if avatar_dir.is_dir():
            for folder in avatar_dir.iterdir():
                if (
                    folder.is_dir() and not folder.is_symlink()
                    and (folder / (folder.name + ".txt")).is_file()
                ):
                    names.add(folder.name)
    else:
        search_roots = [avatar_dir]
        if (data / (avatar_name + ".png")).is_file():
            search_roots.append(data)
        for folder in search_roots:
            if folder.is_dir():
                for item in folder.iterdir():
                    if item.is_symlink():
                        continue
                    if item.is_file() and item.suffix.lower() == ".txt":
                        names.add(item.stem)
    return sorted(names, key=str.casefold)


def _is_resume(flags: list[str]) -> bool:
    if any(flag in flags for flag in (
        "--images-only", "--generate-video", "--videos-only",
        "--regenerate-audio", "--resume-audio", "--publish-only",
    )):
        return True
    return "--from" in flags and (
        flags.index("--from") + 1 == len(flags)
        or flags[flags.index("--from") + 1] != "B1.1"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--legacy-layout" in args:
        args.remove("--legacy-layout")
        # Preserve the previous run.ps1 positional shortcut, not only the
        # Python runner's legacy flag interface.
        legacy: list[str] = ["--scripts-dir", "data/input/scripts"]
        while args and not args[0].startswith("-") and len(legacy) < 6:
            key = "--script" if "--script" not in legacy else "--avatar"
            legacy.extend([key, args.pop(0)])
        return subprocess.call([sys.executable, str(CORE), *legacy, *args])
    if "-h" in args or "--help" in args:
        print(_HELP)
        return 0
    script_arg, avatar_arg, data, flags = _flags(args)
    if any(flag in flags for flag in ("--output", "--avatars-dir", "--scripts-dir", "--script")):
        raise SystemExit("Managed layout controls paths. Use --data-root or --legacy-layout.")
    data.mkdir(parents=True, exist_ok=True)
    available_avatars_found = available_avatars(data)
    avatar_names = sorted({item.stem for item in available_avatars_found}, key=str.casefold)
    if "--list-avatars" in flags:
        print("\n".join(avatar_names) if avatar_names else "No PNG avatars found in data/")
        return 0
    avatar = _name(avatar_arg, ".png") if avatar_arg else _choose(avatar_names, "avatars")
    if avatar not in avatar_names:
        raise SystemExit(f"No PNG avatar found for {avatar}")
    resume = _is_resume(flags)
    if "--list-scripts" in flags:
        print("\n".join(_script_options(data, avatar, resume)) or "No pending .txt scripts")
        return 0
    script = _name(script_arg, ".txt") if script_arg else _choose(
        _script_options(data, avatar, resume), "scripts"
    )
    _, source = prepare_managed_script(data, avatar, script, resume=resume)
    destination = data / avatar / script
    work_root = WORK_ROOT / avatar
    command = [
        sys.executable, str(CORE), str(source),
        "--avatars-dir", str(data),
        "--output", str(work_root),
        "--managed-target", str(destination),
        *flags,
    ]
    if not any(flag in flags for flag in (
        "--images-only", "--generate-video", "--videos-only",
        "--regenerate-audio", "--resume-audio", "--publish-only",
    )):
        command.extend(["--avatar", avatar])
    print(f"  Avatar: {avatar} | Script: {source} | Work: {work_root}")
    try:
        return subprocess.call(command)
    finally:
        work = work_root / script
        # Show completed bot inputs/outputs even after a deliberately stopped or
        # interrupted run, while never modifying an already completed publication.
        sync_managed_outputs(work, destination)


if __name__ == "__main__":
    raise SystemExit(main())
