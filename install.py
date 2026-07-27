#!/usr/bin/env python3
"""Installer for roundtable and the six AI CLIs it drives.

Links `roundtable.py` onto PATH as `roundtable`, then installs whichever of the
AGENT_EXECUTABLES entries (see roundtable.py) it has a verified install command for. Two of
the six -- Antigravity (`agy`) and Grok (`grok`) -- have no publicly documented package-manager
install command known to this script, so it only reports whether they are present; it never
guesses a curl/npm/pip command for them.

Usage:
    python3 install.py                 # link roundtable + install all installable CLIs
    python3 install.py --dry-run       # show what would happen, change nothing
    python3 install.py --skip-clis     # only link the roundtable command
    python3 install.py --only Codex Aider
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtable import AGENT_EXECUTABLES, AGENT_NAMES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent

# Verified install commands, keyed by the executable name (AGENT_EXECUTABLES's values).
# None means: no automated installer available -- report status only, never invent one.
CLI_INSTALLERS: dict[str, list[str] | None] = {
    "codex": ["npm", "install", "-g", "@openai/codex"],
    "claude": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
    "agy": None,
    "aider": ["pipx", "install", "aider-chat"],
    "grok": None,
    "qwen": ["npm", "install", "-g", "@qwen-code/qwen-code"],
}

# The tool each install command above needs on PATH to run at all.
CLI_INSTALLER_REQUIRES: dict[str, str] = {
    "codex": "npm",
    "claude": "npm",
    "aider": "pipx",
    "qwen": "npm",
}

# (system, arch) pairs each npm package has a verified prebuilt binary for, per its own
# `optionalDependencies` on the npm registry (checked 2026-07-27; re-verify with
# `npm view <package> optionalDependencies` if a vendor adds platform support later). Aider is
# pure Python and needs no entry here -- pip/pipx wheels are architecture-agnostic. Qwen's set is
# narrower than its own package's platforms because one of its native deps (`@lydell/node-pty`)
# publishes no linux-arm64 prebuild, so an aarch64 Linux install may fall back to compiling from
# source (needs a C toolchain) or fail outright.
_X64 = "x86_64"
_ARM64 = "arm64"
NPM_ARCH_SUPPORT: dict[str, set[tuple[str, str]]] = {
    "codex": {("linux", _X64), ("linux", _ARM64), ("darwin", _X64), ("darwin", _ARM64),
              ("windows", _X64), ("windows", _ARM64)},
    "claude": {("linux", _X64), ("linux", _ARM64), ("darwin", _X64), ("darwin", _ARM64),
               ("windows", _X64), ("windows", _ARM64)},
    "qwen": {("linux", _X64), ("darwin", _X64), ("darwin", _ARM64),
             ("windows", _X64), ("windows", _ARM64)},
}

# Caveats about CLIs this script cannot auto-install (see CLI_INSTALLERS), surfaced only when the
# current machine isn't the common case (x86_64) they were observed on -- an inference from the
# actual binary shipped to one real machine, not a documented vendor guarantee.
NO_INSTALLER_ARCH_CAVEATS: dict[str, str] = {
    "grok": "this vendor's Linux build has been observed shipping as an x86_64-only binary; "
            "unconfirmed for other architectures",
}


def current_platform() -> tuple[str, str]:
    """(os family, cpu arch) normalized to the vocabulary vendors publish prebuilt binaries under."""
    system = platform.system().lower()  # 'linux', 'darwin', 'windows'
    arch_aliases = {
        "x86_64": _X64, "amd64": _X64,
        "aarch64": _ARM64, "arm64": _ARM64,
        "armv7l": "arm32", "armv6l": "arm32", "armhf": "arm32",
    }
    arch = arch_aliases.get(platform.machine().lower(), platform.machine().lower())
    return system, arch


def default_bin_dir() -> Path:
    """~/.local/bin if it's on PATH (the conventional place for user-installed CLIs); otherwise
    the first other writable, PATH-listed directory under the user's home; otherwise
    ~/.local/bin anyway (created even though it wasn't on PATH, with a warning from the caller).
    """
    home = Path.home()
    preferred = home / ".local" / "bin"
    path_dirs = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if preferred in path_dirs:
        return preferred
    for candidate in path_dirs:
        if (candidate == home or home in candidate.parents) and candidate.is_dir() \
                and os.access(candidate, os.W_OK):
            return candidate
    return preferred


def install_roundtable_symlink(bin_dir: Path, dry_run: bool) -> str:
    """Link roundtable.py onto bin_dir as `roundtable`, executable.

    Symlinks where the platform allows it (every POSIX system); falls back to a plain copy where
    it doesn't (e.g. Windows without developer mode / admin rights), since a copy still works, it
    just needs re-running after a future `git pull` to pick up source changes.
    """
    source = REPO_ROOT / "roundtable.py"
    target = bin_dir / "roundtable"
    if dry_run:
        return f"would link {target} -> {source}"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == source.resolve():
        return f"already linked: {target} -> {source}"
    if target.is_symlink() or target.exists():
        target.unlink()
    try:
        target.symlink_to(source)
        source.chmod(source.stat().st_mode | 0o111)
        return f"linked {target} -> {source}"
    except OSError:
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o111)
        return f"copied {source} -> {target} (symlinks unavailable on this platform; re-run this installer after updating roundtable.py)"


def install_cli(name: str, executable: str, dry_run: bool,
                current: tuple[str, str] | None = None) -> str:
    """Ensure one agent's CLI is installed; return a one-line status message."""
    found = shutil.which(executable)
    if found:
        return f"{name:<11} {executable:<7} already installed ({found})"
    current = current or current_platform()
    command = CLI_INSTALLERS.get(executable)
    if command is None:
        caveat = NO_INSTALLER_ARCH_CAVEATS.get(executable)
        note = f" ({caveat})" if caveat and current[1] != _X64 else ""
        return (f"{name:<11} {executable:<7} not found -- no automated installer here; "
                 f"install it yourself per the vendor's own instructions{note}")
    requirement = CLI_INSTALLER_REQUIRES.get(executable)
    if requirement and not shutil.which(requirement):
        return (f"{name:<11} {executable:<7} not found -- needs `{requirement}` on PATH to "
                 f"auto-install; once available run: {' '.join(command)}")
    supported = NPM_ARCH_SUPPORT.get(executable)
    arch_note = ""
    if supported is not None and current not in supported:
        arch_note = f" [no verified prebuilt binary for {current[0]}-{current[1]}; may fail]"
    if dry_run:
        return f"{name:<11} {executable:<7} would run: {' '.join(command)}{arch_note}"
    try:
        subprocess.run(command, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"{name:<11} {executable:<7} install failed: {exc}{arch_note}"
    found_after = shutil.which(executable)
    status = found_after if found_after else "not on PATH yet -- open a new shell"
    return f"{name:<11} {executable:<7} installed ({status}){arch_note}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bin-dir", type=Path, default=None,
                        help="where to link the `roundtable` command (default: first writable "
                             "user directory on PATH, else ~/.local/bin)")
    parser.add_argument("--skip-clis", action="store_true",
                        help="only link the roundtable command; skip installing agent CLIs")
    parser.add_argument("--only", nargs="+", choices=AGENT_NAMES, default=None, metavar="AGENT",
                        help="install only these agents' CLIs, by roundtable agent name "
                             f"(choices: {', '.join(AGENT_NAMES)})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen without changing anything")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current = current_platform()
    print(f"Detected platform: {current[0]}-{current[1]}")
    if current[0] == "windows":
        print("warning: roundtable's GUI needs the `curses` module, which Windows does not ship "
              "in its standard library -- you'll need the third-party `windows-curses` package "
              "to actually run it; this installer does not manage that dependency.")
    if current[1] == "arm32":
        print("warning: Codex and Claude Code publish no 32-bit ARM build; those two will not "
              "be installable here even though the commands below will be attempted.")

    bin_dir = args.bin_dir or default_bin_dir()
    print(install_roundtable_symlink(bin_dir, args.dry_run))
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(bin_dir) not in path_dirs:
        print(f"warning: {bin_dir} is not on PATH -- add it to your shell profile")

    if args.skip_clis:
        return 0

    print()
    for name in (args.only or AGENT_NAMES):
        print(install_cli(name, AGENT_EXECUTABLES[name], args.dry_run, current=current))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
