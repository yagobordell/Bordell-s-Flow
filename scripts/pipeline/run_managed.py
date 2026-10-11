"""Human-facing launcher: loose TXT = pending, named folder = started.

Examples:
  data/Jorge.png + data/guion.txt -> data/Jorge/guion/guion.txt on start
  data/Jorge/otro.txt            -> data/Jorge/otro/otro.txt on start
  .\\run.ps1 guion Jorge --generate-video resumes an already started folder.
The B pipeline retains verified checkpoints and safe paid-request recovery.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ai_video_factory.managed_layout import sync_managed_outputs
from ai_video_factory.providers.avatar_library import ensure_avatar_meta, organize_avatar

try:
    # Imported as a package by tests.
    from scripts.pipeline.run_b_pipeline import available_avatars, load_avatar_png
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.pipeline"}:
        raise
    # Called directly by run.ps1: Python puts scripts/pipeline, not the
    # repository root, on sys.path[0].
    from run_b_pipeline import available_avatars, load_avatar_png

CORE = Path(__file__).with_name("run_b_pipeline.py")
WORK_ROOT = Path(".video_factory_work")
_HELP = """usage: run.ps1 [script] [avatar] [options]
Put a new avatar PNG and its first TXT directly in data/. After the first run,
place additional pending TXT files directly in data/<avatar>/.
One raw TXT = pending; a folder with that TXT = started or completed.
The persistent QR, voice, optional clone audio and one-time visual style
are saved in data/<avatar>/meta/. A conflicting --style is refused.

Options include --no-image, --no-video, --style, --from B2, --rerun,
--images-only, --generate-video, --resume-audio, --regenerate-audio,
--publish-only, --youtube-only, --sync-only, --stop openai-agent,
--ebook-cover NOMBRE, --feedback-gpt '@feedback.md', --feedback-chat,
--approve-gpt-feedback FILE,
--list-avatars, --list-scripts, --data-root PATH.
--sync-only exports existing verified media without API or GPU calls.
For opted-in YouTube publication set YOUTUBE_ENABLED=true and provide per-run youtube.json.
Use scripts/youtube/publish_run.py to retry distribution without regenerating.
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


def _managed_avatar_names(data: Path) -> list[str]:
    """Ignore registered ebook cover PNGs when presenting managed avatar choices."""
    covers: set[str] = set()
    for folder in data.iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
            continue
        record = folder / "meta" / "ebook_covers.json"
        if record.is_symlink() or not record.is_file():
            continue
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The cover reader will validate its persisted record when needed.
            continue
        if not isinstance(payload, dict):
            continue
        entries = payload.get("covers")
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    covers.add(item["name"].casefold())

    avatars = available_avatars(data)
    return sorted({
        avatar.stem for avatar in avatars
        if not (
            avatar.parent == data and avatar.stem.casefold() in covers
        )
    }, key=str.casefold)


def _script_avatar_candidates(
    data: Path, script: str, *, resume: bool, avatar_names: list[str],
) -> list[str]:
    """A script inside one avatar folder already declares its owner."""
    matches: list[str] = []
    for avatar in avatar_names:
        folder = data / avatar
        if folder.is_symlink() or folder.is_junction():
            continue
        source = (
            folder / script / (script + ".txt")
            if resume else folder / (script + ".txt")
        )
        if source.is_file() and not source.is_symlink():
            matches.append(avatar)
    return matches


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
    data: Path, avatar_name: str, script_name: str, *, resume: bool,
    reference_audio: str | None = None,
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
            "Use --rerun to start it again, or the appropriate resume option "
            "to continue it."
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
        avatar = organize_avatar(raw_avatar, data, reference_audio=reference_audio)
    ensure_avatar_meta(avatar, data, reference_audio=reference_audio)
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
        "--youtube-only", "--sync-only",
        "--feedback-gpt", "--fedback-gpt", "--feedback-chat",
        "--approve-gpt-feedback",
    )):
        return True
    return "--from" in flags and (
        flags.index("--from") + 1 == len(flags)
        or flags[flags.index("--from") + 1] != "B1.1"
    )


def _archive_managed_run(
    data: Path,
    avatar: str,
    script: str,
    destination: Path,
    work: Path,
) -> Path:
    """Move a prior run aside before an explicit fresh rerun.

    The script is copied back as a pending input after the old visible run has
    been archived.  Both the human-facing output and the internal checkpoints
    are retained, so a rerun cannot silently destroy a paid run's audit trail.
    """
    if destination.is_symlink() or destination.is_junction():
        raise SystemExit(f"Refusing to archive linked run folder: {destination}")
    source = destination / f"{script}.txt"
    if source.is_symlink() or not source.is_file():
        raise SystemExit(
            f"Cannot rerun {script}: missing source script in {destination}"
        )
    avatar_dir = data / avatar
    history = avatar_dir / ".history"
    if history.is_symlink() or history.is_junction():
        raise SystemExit(f"Refusing to use linked history folder: {history}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = history / f"{script}-{stamp}"
    counter = 1
    while archive.exists():
        archive = history / f"{script}-{stamp}-{counter}"
        counter += 1
    history.mkdir(parents=True, exist_ok=True)

    work_archive: Path | None = None
    if work.exists():
        if work.is_symlink() or work.is_junction():
            raise SystemExit(f"Refusing to archive linked work folder: {work}")
        work_history = work.parent / ".history"
        if work_history.is_symlink() or work_history.is_junction():
            raise SystemExit(f"Refusing to use linked work history folder: {work_history}")
        work_history.mkdir(parents=True, exist_ok=True)
        work_archive = work_history / archive.name
        work_counter = 1
        while work_archive.exists():
            work_archive = work_history / f"{archive.name}-{work_counter}"
            work_counter += 1

    shutil.move(str(destination), str(archive))
    pending = avatar_dir / f"{script}.txt"
    try:
        if pending.exists() or pending.is_symlink():
            raise SystemExit(
                f"Cannot rerun {script}: pending duplicate already exists at {pending}"
            )
        shutil.copy2(archive / f"{script}.txt", pending)
        if work_archive is not None:
            shutil.move(str(work), str(work_archive))
    except Exception:
        if pending.is_file() and not pending.is_symlink():
            pending.unlink()
        if archive.is_dir() and not destination.exists():
            shutil.move(str(archive), str(destination))
        raise
    return archive


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--legacy-layout" in args:
        raise SystemExit(
            "--legacy-layout has been retired; use the managed data/<avatar>/ layout."
        )
    if "-h" in args or "--help" in args:
        print(_HELP)
        return 0
    script_arg, avatar_arg, data, flags = _flags(args)
    if any(flag in flags for flag in ("--output", "--avatars-dir", "--scripts-dir", "--script")):
        raise SystemExit("Managed layout controls paths. Use --data-root.")
    data.mkdir(parents=True, exist_ok=True)
    avatar_names = _managed_avatar_names(data)
    if "--list-avatars" in flags:
        print("\n".join(avatar_names) if avatar_names else "No PNG avatars found in data/")
        return 0
    if "--qr-only" in flags:
        flags.remove("--qr-only")
        # QR setup needs only the PNG; it must not consume or move a pending TXT.
        if "--avatar" in flags:
            index = flags.index("--avatar")
            if index + 1 == len(flags):
                raise SystemExit("--avatar requires a PNG name")
            qr_avatar = _name(flags[index + 1], ".png")
            del flags[index:index + 2]
        else:
            qr_avatar = (
                _name(avatar_arg or script_arg, ".png")
                if avatar_arg or script_arg else _choose(avatar_names, "avatars")
            )
        if qr_avatar not in avatar_names:
            raise SystemExit(f"No PNG avatar found for {qr_avatar}")
        loose = data / (qr_avatar + ".png")
        stored = (
            organize_avatar(loose, data)
            if loose.is_file() else data / qr_avatar / (qr_avatar + ".png")
        )
        ensure_avatar_meta(stored, data)
        return subprocess.call([
            sys.executable, str(CORE), "--qr-only",
            "--avatars-dir", str(data), "--avatar", qr_avatar, *flags,
        ])
    rerun = "--rerun" in flags
    if rerun:
        flags.remove("--rerun")
        if any(
            flag in flags
            for flag in (
                "--from", "--images-only", "--generate-video", "--videos-only",
                "--regenerate-audio", "--resume-audio", "--publish-only",
                "--youtube-only", "--sync-only",
                "--feedback-gpt", "--fedback-gpt", "--feedback-chat",
                "--approve-gpt-feedback",
            )
        ):
            raise SystemExit(
                "--rerun starts from B1.1 and cannot be combined with resume options"
            )
    resume = _is_resume(flags)
    discovery_resume = resume or rerun
    if avatar_arg:
        avatar = _name(avatar_arg, ".png")
    elif script_arg:
        # Resuming a managed script must not ask about unrelated data/*.png
        # (for example, a book cover used by its casero CTA).
        script_name = _name(script_arg, ".txt")
        matches = _script_avatar_candidates(
            data, script_name, resume=discovery_resume, avatar_names=avatar_names,
        )
        if discovery_resume and not matches:
            raise SystemExit(
                f"No started script folder for {script_name}. "
                "Check the script name or provide its avatar explicitly."
            )
        avatar = _choose(matches or avatar_names, "avatars")
    else:
        avatar = _choose(avatar_names, "avatars")
    if avatar not in avatar_names:
        raise SystemExit(f"No PNG avatar found for {avatar}")
    if "--list-scripts" in flags:
        print(
            "\n".join(_script_options(data, avatar, discovery_resume))
            or "No pending .txt scripts"
        )
        return 0
    script = _name(script_arg, ".txt") if script_arg else _choose(
        _script_options(data, avatar, discovery_resume), "scripts"
    )
    destination = data / avatar / script
    work_root = WORK_ROOT / avatar
    work = work_root / script
    if rerun:
        if not destination.is_dir():
            raise SystemExit(f"Cannot rerun missing run folder: {destination}")
        archived = _archive_managed_run(data, avatar, script, destination, work)
        print(f"  Previous run archived at: {archived}")
        resume = False
    reference_audio = None
    if "--voice-audio" in flags:
        index = flags.index("--voice-audio")
        if index + 1 == len(flags):
            raise SystemExit("--voice-audio requires a file name")
        reference_audio = flags[index + 1]
    _, source = prepare_managed_script(
        data, avatar, script, resume=resume, reference_audio=reference_audio
    )
    if "--sync-only" in flags:
        if flags != ["--sync-only"]:
            raise SystemExit("--sync-only cannot be combined with generation options")
        work = work_root / script
        if (
            not work.is_dir() or work.is_symlink()
            or not (work / "run_report.json").is_file()
        ):
            raise SystemExit(
                f"No internal saved run to synchronize: {work}. "
                "Missing files cannot be recovered from a report alone."
            )
        sync_managed_outputs(work, destination)
        media = destination / "media"
        if not media.is_dir() or not any(media.iterdir()):
            raise SystemExit(f"No completed or partial verified media found in {work}")
        print(f"  Verified available media: {media}")
        print(f"  Human-facing report: {destination / 'outputs' / 'run_report.json'}")
        return 0
    command = [
        sys.executable, str(CORE), str(source),
        "--avatars-dir", str(data),
        "--output", str(work_root),
        "--managed-target", str(destination),
        *flags,
    ]
    if not any(flag in flags for flag in (
        "--images-only", "--generate-video", "--videos-only",
        "--regenerate-audio", "--resume-audio", "--publish-only", "--youtube-only",
        "--feedback-gpt", "--fedback-gpt", "--feedback-chat",
        "--approve-gpt-feedback",
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
