from pathlib import Path

script = r'''#!/usr/bin/env python3
"""
Application View prototype for Linux.

Creates ~/Program Files/<Application>/ as a human-friendly view of installed
desktop applications. It does not move or modify the real application files.
Instead, it creates symlinks to the desktop entry, executable, icon, and (on
RPM-based systems with rpm installed) files belonging to the RPM that owns the
executable.

Usage:
    python3 appview.py
    python3 appview.py --root "$HOME/Program Files"
    python3 appview.py --dry-run

This is a prototype: package ownership is heuristic, and shared RPM packages
may contain files used by more than one application.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def run(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            args, check=True, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def desktop_files() -> list[Path]:
    dirs = [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path.home() / ".local/share/applications",
    ]
    found: dict[str, Path] = {}
    # User-local entries take precedence over system entries with the same name.
    for directory in dirs:
        if directory.is_dir():
            for path in directory.rglob("*.desktop"):
                found[path.name] = path
    return list(found.values())


def parse_desktop(path: Path) -> dict[str, str] | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
        section = parser["Desktop Entry"]
    except (OSError, KeyError, configparser.Error):
        return None

    if section.get("Type", "Application") != "Application":
        return None
    if section.get("NoDisplay", "false").lower() == "true":
        return None
    if section.get("Hidden", "false").lower() == "true":
        return None

    name = section.get("Name", path.stem).strip()
    exec_line = section.get("Exec", "").strip()
    icon = section.get("Icon", "").strip()
    if not exec_line:
        return None
    return {"name": name, "exec": exec_line, "icon": icon}


def executable_from_exec(exec_line: str) -> Path | None:
    # Desktop Entry Exec fields can contain quoted arguments and field codes
    # such as %U, %F, %c, and %k. Remove field codes before tokenizing.
    cleaned = re.sub(r"%[fFuUdDnNickvm]", "", exec_line)
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        return None
    if not tokens:
        return None

    command = tokens[0]
    if command in {"env", "sh", "bash", "flatpak", "gtk-launch"} and len(tokens) > 1:
        # Keep the wrapper as the actual launch command when it is meaningful;
        # package-file discovery is best-effort and may not find an RPM owner.
        if command == "env":
            for token in tokens[1:]:
                if "=" in token and not token.startswith("/"):
                    continue
                command = token
                break
        elif command == "flatpak":
            return None
        else:
            command = tokens[1]

    candidate = Path(command)
    if candidate.is_absolute() and candidate.exists():
        return candidate.resolve()
    located = shutil.which(command)
    return Path(located).resolve() if located else None


def rpm_owner(executable: Path) -> str | None:
    owner = run(["rpm", "-qf", str(executable)])
    if not owner or owner.endswith(" is not owned by any package"):
        return None
    if owner.startswith("file "):
        return None
    return owner


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]', "_", name).strip(" .")
    return (name or "Unnamed Application")[:100]


def link_to(target: Path, link: Path, dry_run: bool) -> bool:
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return False

    if link.is_symlink():
        try:
            if link.resolve() == target:
                return True
        except OSError:
            pass
        if not dry_run:
            link.unlink()
    elif link.exists():
        # Never overwrite a real file or directory.
        return False

    if dry_run:
        print(f"  LINK {link} -> {target}")
        return True
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return True
    except OSError as exc:
        print(f"  warning: couldn't link {link}: {exc}", file=sys.stderr)
        return False


def add_package_file_links(executable: Path, app_dir: Path, dry_run: bool) -> str | None:
    owner = rpm_owner(executable)
    if not owner:
        return None
    listing = run(["rpm", "-ql", owner])
    if not listing:
        return owner

    files_root = app_dir / "Package Files"
    count = 0
    for raw in listing.splitlines():
        source = Path(raw)
        # This view is for files, not directory entries; ignore missing paths.
        if not source.is_absolute() or not source.exists() or source.is_dir():
            continue
        # Mirror the absolute path below "Package Files", e.g.
        # /usr/share/foo -> "Package Files/usr/share/foo".
        relative = Path(str(source).lstrip("/"))
        destination = files_root / relative
        if link_to(source, destination, dry_run):
            count += 1
    print(f"  RPM package: {owner} ({count} file links)")
    return owner


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a symlink-based application view.")
    parser.add_argument(
        "--root", type=Path,
        default=Path.home() / "Program Files",
        help='directory to generate (default: ~/Program Files)'
    )
    parser.add_argument("--dry-run", action="store_true", help="show planned links without writing")
    args = parser.parse_args()
    root = args.root.expanduser()

    if not args.dry_run:
        root.mkdir(parents=True, exist_ok=True)

    entries = desktop_files()
    created = 0
    print(f"Application View root: {root}")
    print(f"Found {len(entries)} desktop-entry files.")

    for desktop in entries:
        info = parse_desktop(desktop)
        if not info:
            continue

        name = safe_name(info["name"])
        app_dir = root / name
        executable = executable_from_exec(info["exec"])

        print(f"\n{name}")
        if not args.dry_run:
            app_dir.mkdir(parents=True, exist_ok=True)

        linked_any = link_to(desktop, app_dir / "Desktop Entry.desktop", args.dry_run)
        if executable:
            linked_any |= link_to(executable, app_dir / "Program", args.dry_run)
            add_package_file_links(executable, app_dir, args.dry_run)
        else:
            print("  executable not resolved (possibly Flatpak, wrapper, or custom launcher)")

        icon = info["icon"]
        if icon.startswith("/") and Path(icon).exists():
            linked_any |= link_to(Path(icon), app_dir / "Icon", args.dry_run)
        elif icon:
            # Named icons are normally resolved through the icon theme. Record
            # the name for inspection rather than guessing a path.
            icon_note = app_dir / "Icon Name.txt"
            if args.dry_run:
                print(f"  NOTE {icon_note} = {icon}")
            else:
                icon_note.write_text(icon + "\n", encoding="utf-8")
            linked_any = True

        if linked_any:
            created += 1

    print(f"\nDone. Processed {created} application views.")
    if args.dry_run:
        print("Dry run only: no files were written.")
    else:
        print("Original application files were not moved or modified.")
        print("To remove this view, delete the generated root directory.")
        print("Note: this prototype does not automatically remove stale entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

path = Path("/mnt/data/appview.py")
path.write_text(script, encoding="utf-8")
print(f"Created prototype: {path}")
