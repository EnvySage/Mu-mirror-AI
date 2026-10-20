"""Synchronize the explicitly supported AI runtime files into upload/.

The local config.yml is deliberately excluded: upload/config.yml may be a
deployment override, while config.example.yml contains only safe defaults.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

RUNTIME_ROOT_FILES = (
    "config.example.yml",
    "config.py",
    "errors.py",
    "generate_proto.py",
    "glossary_render.py",
    "llm_json.py",
    "prompts_loader.py",
    "recent_context_render.py",
    "requirements-minimal.txt",
    "requirements-minimal.lock.txt",
    "server.py",
    "time_substitution.py",
    "todo_render.py",
)

RUNTIME_GROUPS = {
    "agent_core": {".py"},
    "agent_data": {".py"},
    "embedding": {".py"},
    "generated": {".py"},
    "llm": {".py"},
    "prompts": {".txt"},
    "services": {".py"},
}


def runtime_files(repo_root: Path) -> list[Path]:
    """Return the deterministic, sorted runtime source file set."""
    root = Path(repo_root)
    files = {Path(name) for name in RUNTIME_ROOT_FILES}
    for directory, extensions in RUNTIME_GROUPS.items():
        group_root = root / directory
        if not group_root.is_dir():
            raise FileNotFoundError(f"Required runtime directory is missing: {directory}")
        files.update(
            path.relative_to(root)
            for path in group_root.rglob("*")
            if path.is_file() and path.suffix in extensions
        )

    missing = [rel for rel in files if not (root / rel).is_file()]
    if missing:
        names = ", ".join(rel.as_posix() for rel in sorted(missing))
        raise FileNotFoundError(f"Required runtime file(s) missing: {names}")
    return sorted(files, key=lambda path: path.as_posix())


def sync_upload(repo_root: Path, *, check: bool = False) -> list[Path]:
    """Copy runtime files, or return drift without changing files in check mode.

    Returned paths point to upload destinations and are sorted in runtime source
    order. No files are deleted; upload/config.yml and any non-runtime overrides
    are left untouched.
    """
    root = Path(repo_root)
    upload_root = root / "upload"
    changed: list[Path] = []

    for relative in runtime_files(root):
        source = root / relative
        destination = upload_root / relative
        same = destination.is_file() and source.read_bytes() == destination.read_bytes()
        if same:
            continue
        changed.append(destination)
        if not check:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report mirror drift without copying")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    changed = sync_upload(root, check=args.check)
    if args.check:
        for path in changed:
            print(f"DRIFT {path.relative_to(root).as_posix()}")
        if not changed:
            print("upload runtime files are in sync")
        return 1 if changed else 0

    for path in changed:
        print(f"SYNC {path.relative_to(root).as_posix()}")
    if not changed:
        print("upload runtime files are already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

\n