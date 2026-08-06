#!/usr/bin/env python3
"""Installer for roundtable and the six AI CLIs it drives.

Links `roundtable.py` onto PATH as `roundtable`, then installs whichever of the
AGENT_EXECUTABLES entries (see roundtable.py) it has a verified install command for. One of
the six -- Antigravity (`agy`) -- has no package-manager install command known to this script
(its official installer is a `curl | bash` / `irm | iex` script, not a registry package -- see
the note above CLI_INSTALLERS), so it only reports whether it is present; it never guesses a
curl/npm/pip command for it.

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

REPO_ROOT = Path(__file__).resolve().parent

# Keep this small manifest local: importing roundtable.py would import curses, which is absent from
# the Python standard-library build on Windows -- exactly where this installer must still be able to
# start and explain that limitation. test_install.py checks this stays in sync with roundtable.
AGENT_EXECUTABLES: dict[str, str] = {
    "Codex": "codex",
    "Claude": "claude",
    "Antigravity": "agy",
    "Aider": "aider",
    "Grok": "grok",
    "Qwen": "qwen",
}
AGENT_NAMES: tuple[str, ...] = tuple(AGENT_EXECUTABLES)

# Verified install commands, keyed by the executable name (AGENT_EXECUTABLES's values). Most are
# a single command (list[str]); a few need more than one step run in sequence (list[list[str]]).
# None means: no automated installer available -- report status only, never invent one. agy is
# None deliberately: its official installer (curl -fsSL https://antigravity.google/cli/install.sh
# | bash, or the PowerShell irm/iex equivalent on Windows) pulls and executes a remote script
# rather than installing from a package registry, which is a different trust/reversibility
# profile than every other command in this dict -- wiring that in as a silent default here is a
# call for whoever owns this script's security posture, not something to add unprompted.
CLI_INSTALLERS: dict[str, list[str] | list[list[str]] | None] = {
    "codex": ["npm", "install", "-g", "@openai/codex"],
    "claude": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
    "agy": None,
    # Not `pipx install aider-chat` directly: aider-chat hard-pins numpy==1.26.4, which has no
    # cp313 wheel on any platform (verified via PyPI's own JSON API, not just observed on one
    # machine) -- pip's naive resolver chokes on it. aider-install's own installer uses uv's
    # resolver instead, which handles this correctly; verified working end-to-end.
    "aider": [["pipx", "install", "aider-install"], ["aider-install"]],
    "grok": ["npm", "install", "-g", "@xai-official/grok"],
    "qwen": ["npm", "install", "-g", "@qwen-code/qwen-code"],
}

# The tool each install command above needs on PATH to run at all.
CLI_INSTALLER_REQUIRES: dict[str, str] = {
    "codex": "npm",
    "claude": "npm",
    "aider": "pipx",
    "grok": "npm",
    "qwen": "npm",
}

# (system, arch) pairs each npm package has a verified prebuilt binary for, per its own
# `optionalDependencies` on the npm registry (checked 2026-07-27, grok added 2026-08-06; re-verify
# with `npm view <package> optionalDependencies` if a vendor adds platform support later). Aider is
# pure Python and needs no entry here -- pip/pipx wheels are architecture-agnostic. Qwen's set is
# narrower than its own package's platforms because one of its native deps (`@lydell/node-pty`)
# publishes no linux-arm64 prebuild, so an aarch64 Linux install may fall back to compiling from
# source (needs a C toolchain) or fail outright. Grok has no such caveat -- its own os/cpu and
# optionalDependencies fields cover all six combinations below with dedicated native packages
# (verified working end-to-end on linux-arm64, not just declared).
_X64 = "x86_64"
_ARM64 = "arm64"
NPM_ARCH_SUPPORT: dict[str, set[tuple[str, str]]] = {
    "codex": {("linux", _X64), ("linux", _ARM64), ("darwin", _X64), ("darwin", _ARM64),
              ("windows", _X64), ("windows", _ARM64)},
    "claude": {("linux", _X64), ("linux", _ARM64), ("darwin", _X64), ("darwin", _ARM64),
               ("windows", _X64), ("windows", _ARM64)},
    "grok": {("linux", _X64), ("linux", _ARM64), ("darwin", _X64), ("darwin", _ARM64),
             ("windows", _X64), ("windows", _ARM64)},
    "qwen": {("linux", _X64), ("darwin", _X64), ("darwin", _ARM64),
             ("windows", _X64), ("windows", _ARM64)},
}

# Caveats about CLIs this script cannot auto-install (see CLI_INSTALLERS), surfaced only when the
# current machine isn't the common case (x86_64) they were observed on -- an inference from the
# actual binary shipped to one real machine, not a documented vendor guarantee.
NO_INSTALLER_ARCH_CAVEATS: dict[str, str] = {}


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


# Distinctive marker used to recognize a prior *copy* install of roundtable.py so a later
# re-run can refresh it without --force (the copy-fallback message invites exactly that).
_ROUNDTABLE_COPY_MARKER = (
    "Roundtable: a dependency-free terminal UI for collaborating coding agents."
)


def _windows_launcher(source: Path) -> str:
    """A cmd.exe launcher for an arbitrary Python/source path, including spaces and `%`."""
    python = str(Path(sys.executable).resolve()).replace("%", "%%")
    script = str(source).replace("%", "%%")
    return f'@"{python}" "{script}" %*\r\n'


def _is_our_windows_launcher(target: Path, launcher: str) -> bool:
    """True when target is this installer's .cmd shim (current or stale python/script path)."""
    if not target.is_file():
        return False
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if text == launcher:
        return True
    # Stale shim we wrote earlier: still points at some roundtable.py.
    stripped = text.lstrip("\ufeff").lstrip()
    return stripped.startswith("@") and "roundtable.py" in text


def _is_our_posix_install(target: Path, source: Path) -> bool:
    """True when target is our symlink or a prior copy of roundtable.py."""
    if target.is_symlink():
        try:
            return target.resolve() == source.resolve()
        except OSError:
            return False
    if not target.is_file():
        return False
    try:
        data = target.read_bytes()
    except OSError:
        return False
    try:
        if data == source.read_bytes():
            return True
    except OSError:
        return False
    # Stale copy from an older checkout: still our file if it carries the module docstring.
    try:
        head = data[:800].decode("utf-8", errors="replace")
    except Exception:
        return False
    return head.startswith("#!/usr/bin/env python3") and _ROUNDTABLE_COPY_MARKER in head


def install_roundtable_symlink(bin_dir: Path, dry_run: bool, force: bool = False,
                               current: tuple[str, str] | None = None) -> str:
    """Install roundtable.py onto PATH as `roundtable` (or `roundtable.cmd` on Windows).

    POSIX uses a symlink and falls back to a copy if symlinks are unavailable. Windows gets a
    `.cmd` shim because cmd.exe does not execute extensionless shebang scripts from PATH. Existing
    unrelated commands are preserved unless --force was explicitly supplied. A prior install that
    this script itself created (symlink, matching .cmd launcher, or copy of roundtable.py) is
    refreshed without --force so `python3 install.py` stays idempotent across updates.
    """
    source = REPO_ROOT / "roundtable.py"
    is_windows = (current or current_platform())[0] == "windows"
    target = bin_dir / ("roundtable.cmd" if is_windows else "roundtable")
    launcher = _windows_launcher(source) if is_windows else None
    if dry_run:
        action = "write launcher" if is_windows else "link"
        return f"would {action} {target} -> {source}"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if is_windows and launcher is not None and _is_our_windows_launcher(target, launcher):
        if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == launcher:
            return f"already installed: {target} -> {source}"
        target.write_text(launcher, encoding="utf-8", newline="")
        return f"updated launcher {target} -> {source}"
    if not is_windows and target.is_symlink() and _is_our_posix_install(target, source):
        return f"already linked: {target} -> {source}"
    if not is_windows and not target.is_symlink() and _is_our_posix_install(target, source):
        try:
            if target.read_bytes() == source.read_bytes():
                return f"already installed: {target} (copy of {source})"
        except OSError:
            pass
        # Stale copy -- refresh in place (still our install; no --force required).
        source.chmod(source.stat().st_mode | 0o111)
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o111)
        return f"updated copy {source} -> {target}"
    if target.exists() or target.is_symlink():
        if target.is_dir():
            raise IsADirectoryError(f"refusing to replace directory: {target}")
        if not force:
            raise FileExistsError(
                f"refusing to replace existing command: {target} (pass --force to replace it)")
        target.unlink()
    if is_windows:
        target.write_text(launcher, encoding="utf-8", newline="")
        return f"installed launcher {target} -> {source}"
    source.chmod(source.stat().st_mode | 0o111)
    try:
        target.symlink_to(source)
        return f"linked {target} -> {source}"
    except OSError:
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o111)
        return f"copied {source} -> {target} (symlinks unavailable on this platform; re-run this installer after updating roundtable.py)"


def install_cli(name: str, executable: str, dry_run: bool,
                current: tuple[str, str] | None = None) -> tuple[str, bool]:
    """Ensure one agent's CLI is installed.

    Returns (status_message, ok). ok is False when an install was attempted and failed, or when
    the required package manager is missing so a needed auto-install cannot run. The informational
    "no automated installer" case (agy) is ok=True -- it is not a failure of this script.
    """
    found = shutil.which(executable)
    if found:
        return f"{name:<11} {executable:<7} already installed ({found})", True
    current = current or current_platform()
    command = CLI_INSTALLERS.get(executable)
    if command is None:
        caveat = NO_INSTALLER_ARCH_CAVEATS.get(executable)
        note = f" ({caveat})" if caveat and current[1] != _X64 else ""
        return (f"{name:<11} {executable:<7} not found -- no automated installer here; "
                 f"install it yourself per the vendor's own instructions{note}", True)
    steps = command if isinstance(command[0], list) else [command]
    joined = " && ".join(" ".join(step) for step in steps)
    requirement = CLI_INSTALLER_REQUIRES.get(executable)
    if requirement and not shutil.which(requirement):
        return (f"{name:<11} {executable:<7} not found -- needs `{requirement}` on PATH to "
                 f"auto-install; once available run: {joined}", False)
    supported = NPM_ARCH_SUPPORT.get(executable)
    arch_note = ""
    if supported is not None and current not in supported:
        arch_note = f" [no verified prebuilt binary for {current[0]}-{current[1]}; may fail]"
    if dry_run:
        return f"{name:<11} {executable:<7} would run: {joined}{arch_note}", True
    try:
        for step in steps:
            # Resolve the launcher to its real path before handing it to subprocess. On Windows,
            # npm/pipx are .cmd/.exe wrappers that shutil.which() finds fine, but a bare "npm"
            # passed to subprocess.run(shell=False) fails with WinError 2 -- unlike a shell,
            # CreateProcess does not search PATHEXT for an unqualified name, only an exact path.
            resolved = shutil.which(step[0]) or step[0]
            subprocess.run([resolved, *step[1:]], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"{name:<11} {executable:<7} install failed: {exc}{arch_note}", False
    found_after = shutil.which(executable)
    status = found_after if found_after else "not on PATH yet -- open a new shell"
    return f"{name:<11} {executable:<7} installed ({status}){arch_note}", True


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
    parser.add_argument("--force", action="store_true",
                        help="replace an existing roundtable command in --bin-dir")
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
    try:
        print(install_roundtable_symlink(
            bin_dir, args.dry_run, force=args.force, current=current))
    except (FileExistsError, IsADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(bin_dir) not in path_dirs:
        print(f"warning: {bin_dir} is not on PATH -- add it to your shell profile")

    if args.skip_clis:
        return 0

    print()
    any_failed = False
    for name in (args.only or AGENT_NAMES):
        message, ok = install_cli(
            name, AGENT_EXECUTABLES[name], args.dry_run, current=current)
        print(message)
        if not ok:
            any_failed = True
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
