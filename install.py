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
import ntpath
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None  # not Windows -- refresh_windows_path() is a no-op there

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
    # resolver instead, which handles this correctly; verified working end-to-end. `--force` on
    # the pipx step is a no-op on a fresh install and makes --update actually reinstall/refresh
    # aider-install (and, via it, aider itself) rather than pipx silently no-op'ing because it's
    # already present.
    "aider": [["pipx", "install", "--force", "aider-install"], ["aider-install"]],
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


def is_wsl(current: tuple[str, str] | None = None) -> bool:
    """True inside Windows Subsystem for Linux. platform.system() reports 'Linux' there like any
    other Linux box, but WSL has its own PATH/interop quirks (e.g. Windows binaries reachable on
    PATH, a separate Windows-side npm/Node install easy to confuse with the Linux-side one) worth
    flagging rather than silently treating identically to bare-metal Linux. Accepts the same
    `current` override as the rest of this module so a test simulating a non-Linux platform
    doesn't fall through to checking the real host.
    """
    if (current or current_platform())[0] != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="replace").lower()
    except OSError:
        return False


def is_musl_libc(current: tuple[str, str] | None = None) -> bool:
    """True on Linux systems using musl libc (e.g. Alpine) instead of glibc. Several of the npm
    packages this installer drives ship separate glibc/musl native binaries (see NPM_ARCH_SUPPORT)
    -- an (os, arch) pair "supported" there can still fail to actually run here if the vendor's
    package has no musl build. Same detection Antigravity's own official installer script uses.
    """
    if (current or current_platform())[0] != "linux":
        return False
    if any(Path("/lib").glob("libc.musl-*.so.1")):
        return True
    try:
        result = subprocess.run(["ldd", "/bin/ls"], capture_output=True, text=True, timeout=5)
        return "musl" in result.stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def refresh_windows_path(current: tuple[str, str] | None = None) -> None:
    """Re-read PATH from the registry and merge any new entries into this process's own
    os.environ before checking anything with shutil.which().

    An installer this script just ran (Node's MSI, Antigravity's setup step, etc.) updates PATH by
    writing the registry and broadcasting WM_SETTINGCHANGE -- but that broadcast only reaches
    already-running programs that listen for it (like Explorer), not an existing terminal's
    inherited environment block. A Command Prompt opened before those installs ran keeps reporting
    a just-installed CLI as "not found" even though it's genuinely on PATH for any new process.
    Re-reading the registry directly here means `roundtable --install` doesn't require the user to
    close and reopen their terminal just to see what it itself installed a moment ago.
    """
    if (current or current_platform())[0] != "windows" or winreg is None:
        return
    def read_reg_path(hive: int, subkey: str) -> str:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                # PATH is commonly stored as REG_EXPAND_SZ with literal tokens like
                # %LOCALAPPDATA% (e.g. Antigravity's own installer writes exactly that).
                # ntpath.expandvars specifically (not os.path.expandvars) -- Windows env-var
                # syntax is always %VAR%, regardless of what platform is actually running this
                # code; os.path.expandvars would follow the real host's rules instead (no-op for
                # %VAR% syntax on POSIX, where os.path is posixpath).
                return ntpath.expandvars(value)
        except OSError:
            return ""
    registry_dirs = (
        read_reg_path(winreg.HKEY_LOCAL_MACHINE,
                     r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
        + ";" + read_reg_path(winreg.HKEY_CURRENT_USER, "Environment")
    ).split(";")
    # Windows' PATH separator is always ';', regardless of what platform is actually running this
    # code (this function only does anything when `current` is windows -- but that can be a
    # simulated value in a test on a POSIX runner, where os.pathsep would wrongly be ':').
    current_dirs = os.environ.get("PATH", "").split(";")
    seen = {d.lower() for d in current_dirs if d}
    for d in registry_dirs:
        if d and d.lower() not in seen:
            current_dirs.append(d)
            seen.add(d.lower())
    os.environ["PATH"] = ";".join(d for d in current_dirs if d)


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


def ensure_windows_dependencies(dry_run: bool, current: tuple[str, str] | None = None) -> str | None:
    """Install the two packages Windows needs to run roundtable.py and its test suite at all --
    neither is an agent CLI, so they don't belong in CLI_INSTALLERS, but without them the program
    can't even start there. Returns a status message, or None on any other platform.

    - `windows-curses`: the standard library's `curses` module isn't built for Windows at all;
      importing roundtable.py fails immediately without this.
    - `tzdata`: Windows ships no IANA time zone database (unlike Linux/macOS, which usually have
      one on disk already), so `zoneinfo` can't resolve *any* named zone, not even "UTC", without
      it. roundtable.py's own reset-time parsing already handles a missing zone gracefully
      (falls back to safe polling rather than guessing), so this isn't required for correctness --
      it only lets the corresponding tests exercise that path instead of skipping it.
    """
    current = current or current_platform()
    if current[0] != "windows":
        return None
    packages = ["windows-curses", "tzdata"]
    if dry_run:
        return f"would run: {sys.executable} -m pip install {' '.join(packages)}"
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", *packages], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"windows-curses/tzdata install failed: {exc}"
    return "installed windows-curses, tzdata (needed to run roundtable.py and its tests here)"


def install_cli(name: str, executable: str, dry_run: bool, current: tuple[str, str] | None = None,
                musl: bool = False, update: bool = False) -> tuple[str, bool]:
    """Ensure one agent's CLI is installed, or (with update=True) re-run its install command even
    when already present, to pick up a newer version.

    Returns (status_message, ok). ok is False when an install/update was attempted and failed, or
    when the required package manager is missing so it cannot run at all. The informational "no
    automated installer" case (agy) is ok=True -- it is not a failure of this script.

    `musl` is a plain bool, not auto-detected here: detecting it shells out to `ldd` (see
    is_musl_libc), and this function is called once per agent, so the caller (main()) detects it
    once up front and passes the result through -- same reason `current` is threaded in rather
    than recomputed.
    """
    found = shutil.which(executable)
    if found and not update:
        return f"{name:<11} {executable:<7} already installed ({found})", True
    current = current or current_platform()
    command = CLI_INSTALLERS.get(executable)
    if command is None:
        caveat = NO_INSTALLER_ARCH_CAVEATS.get(executable)
        note = f" ({caveat})" if caveat and current[1] != _X64 else ""
        if found:
            return (f"{name:<11} {executable:<7} already installed ({found}) -- no automated "
                     f"update available; update it yourself per the vendor's own instructions"
                     f"{note}", True)
        return (f"{name:<11} {executable:<7} not found -- no automated installer here; "
                 f"install it yourself per the vendor's own instructions{note}", True)
    steps = command if isinstance(command[0], list) else [command]
    joined = " && ".join(" ".join(step) for step in steps)
    requirement = CLI_INSTALLER_REQUIRES.get(executable)
    if requirement and not shutil.which(requirement):
        if found:
            return (f"{name:<11} {executable:<7} already installed ({found}) -- needs "
                     f"`{requirement}` on PATH to auto-update; once available run: {joined}", True)
        return (f"{name:<11} {executable:<7} not found -- needs `{requirement}` on PATH to "
                 f"auto-install; once available run: {joined}", False)
    supported = NPM_ARCH_SUPPORT.get(executable)
    arch_note = ""
    if supported is not None and current not in supported:
        arch_note = f" [no verified prebuilt binary for {current[0]}-{current[1]}; may fail]"
    elif supported is not None and current[0] == "linux" and musl:
        arch_note = (" [running on musl libc (e.g. Alpine); this vendor's npm package may not "
                     "publish a musl build even though it covers this arch on glibc]")
    verb, verb_past = ("update", "updated") if found else ("install", "installed")
    if dry_run:
        return f"{name:<11} {executable:<7} would {verb}: {joined}{arch_note}", True
    try:
        for step in steps:
            # Resolve the launcher to its real path before handing it to subprocess. On Windows,
            # npm/pipx are .cmd/.exe wrappers that shutil.which() finds fine, but a bare "npm"
            # passed to subprocess.run(shell=False) fails with WinError 2 -- unlike a shell,
            # CreateProcess does not search PATHEXT for an unqualified name, only an exact path.
            resolved = shutil.which(step[0]) or step[0]
            subprocess.run([resolved, *step[1:]], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"{name:<11} {executable:<7} {verb} failed: {exc}{arch_note}", False
    found_after = shutil.which(executable)
    status = found_after if found_after else "not on PATH yet -- open a new shell"
    return f"{name:<11} {executable:<7} {verb_past} ({status}){arch_note}", True


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
    parser.add_argument("--update", action="store_true",
                        help="re-run each CLI's install command even if already present, to pick "
                             "up a newer version (also refreshes PATH from the registry on "
                             "Windows first, in case an earlier install just isn't visible yet)")
    return parser


def _confirm(prompt: str) -> bool:
    """Ask a yes/no question on stdin. Defaults to No whenever it can't meaningfully be answered
    (not a tty, EOF, Ctrl-C) -- an update prompt should never block a non-interactive run or
    silently assume "yes" just because input() came up empty.
    """
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{prompt} [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


def check_python_update() -> str:
    """Query python.org's official release index (its ftp directory listing, e.g. `3.13.15/`) for
    the latest patch release in the running major.minor series, and report if a newer one exists.
    Read-only network check; never installs anything -- see update_package_managers' docstring
    for why upgrading the interpreter itself is out of scope for this script. Best-effort: any
    network/parsing problem just says so rather than failing loudly.
    """
    major, minor, patch = platform.python_version_tuple()
    major_minor = f"{major}.{minor}"
    try:
        with urllib.request.urlopen("https://www.python.org/ftp/python/", timeout=5) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return f"Python {platform.python_version()} (couldn't reach python.org to check for a newer release)"
    patches = [int(m) for m in re.findall(rf'href="{re.escape(major_minor)}\.(\d+)/"', html)]
    if not patches:
        return f"Python {platform.python_version()} (couldn't find {major_minor}.x releases on python.org)"
    latest = max(patches)
    if latest > int(patch):
        return (f"Python {platform.python_version()} -- {major_minor}.{latest} is available "
                f"(python.org); update via your OS package manager, pyenv, or python.org's own "
                f"installer if you want it -- this script won't touch the interpreter itself.")
    return f"Python {platform.python_version()} is already the latest {major_minor}.x release."


def check_npm_update() -> str | None:
    """Query npm's own registry for the latest published npm version and compare to what's
    installed. Returns None if npm isn't on PATH at all (nothing to check). Read-only -- the
    actual upgrade attempt (and its own EBADENGINE handling) stays in update_package_managers.
    """
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        current = subprocess.run([npm, "--version"], capture_output=True, text=True,
                                 timeout=10, check=True).stdout.strip()
        latest = subprocess.run([npm, "view", "npm", "version"], capture_output=True, text=True,
                                timeout=10, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return "npm: couldn't check the registry for the latest version"
    if current == latest:
        return f"npm {current} is already the latest version"
    return f"npm {current} installed; {latest} is available"


def update_package_managers(dry_run: bool) -> tuple[list[str], bool]:
    """Upgrade pip, and npm/pipx if present. Returns (status_messages, any_failed).

    Deliberately does not touch the Python interpreter itself -- upgrading Python is a much
    bigger, install-method-specific operation (the Windows .msi, apt/dnf, pyenv, python.org's own
    installer all do this differently, and getting it wrong risks breaking every venv on the
    machine) that this installer has no business attempting. Just checks (and reports) whether a
    newer release exists, same as the npm version check below, so the user can decide.
    """
    messages = [check_python_update()]
    npm_check = check_npm_update()
    if npm_check:
        messages.append(npm_check)
    steps: list[tuple[str, list[str]]] = [
        ("pip", [sys.executable, "-m", "pip", "install", "--upgrade", "pip"]),
    ]
    if shutil.which("npm"):
        steps.append(("npm", [shutil.which("npm"), "install", "-g", "npm@latest"]))
    if shutil.which("pipx"):
        steps.append(("pipx", [sys.executable, "-m", "pip", "install", "--upgrade", "pipx"]))
    any_failed = False
    for label, command in steps:
        if dry_run:
            messages.append(f"would run: {' '.join(command)}")
            continue
        try:
            # Captured (unlike install_cli's live-streamed subprocess calls) specifically so a
            # PEP 668 "externally-managed-environment" refusal (common on Debian/Ubuntu -- pip
            # blocks *any* direct install/upgrade against the system Python, --user included) can
            # be told apart from a genuine failure and explained, instead of just "exit status 1".
            subprocess.run(command, check=True, capture_output=True, text=True)
            messages.append(f"{label} upgraded")
        except subprocess.CalledProcessError as exc:
            output = exc.stderr or exc.stdout or ""
            if "externally-managed-environment" in output:
                messages.append(
                    f"{label} upgrade skipped: this system's Python is externally managed (PEP "
                    f"668, e.g. Debian/Ubuntu) -- pip refuses to touch it directly, --user "
                    f"included. Use your OS package manager instead (e.g. `apt install "
                    f"--only-upgrade python3-pip`), or pass --break-system-packages yourself if "
                    f"you understand the risk; this script won't do that automatically.")
            elif "EBADENGINE" in output:
                # `npm install -g npm@latest` always targets the newest release, which can (and,
                # found live, does) require a newer Node than what's actually installed -- npm's
                # own engine check correctly refuses rather than installing something broken.
                messages.append(
                    f"{label} upgrade skipped: the latest npm needs a newer Node.js than is "
                    f"installed here (npm's own engine check refused, not a bug in this script) "
                    f"-- upgrade Node.js first if you want the newest npm; current npm is still "
                    f"perfectly usable otherwise.")
            else:
                tail = "\n".join(output.strip().splitlines()[-5:]) if output.strip() else None
                detail = f": {exc}" if tail is None else f":\n{tail}"
                messages.append(f"{label} upgrade failed{detail}")
                any_failed = True
        except OSError as exc:
            messages.append(f"{label} upgrade failed: {exc}")
            any_failed = True
    return messages, any_failed


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current = current_platform()
    refresh_windows_path(current)
    print(f"Detected platform: {current[0]}-{current[1]} (Python {platform.python_version()})")
    if is_wsl(current):
        print("note: running inside WSL -- PATH/binary interop with any Windows-side install of "
              "the same tools (npm, git, etc.) can be confusing; this installer only manages the "
              "Linux-side ones on this PATH.")
    musl = is_musl_libc(current)
    if musl:
        print("note: musl libc detected (e.g. Alpine) -- some agent CLIs' npm packages may only "
              "publish glibc-linked native binaries; see per-CLI notes below if one fails to run.")
    windows_deps_message = ensure_windows_dependencies(args.dry_run, current=current)
    windows_deps_failed = bool(windows_deps_message) and "failed" in windows_deps_message
    if windows_deps_message is not None:
        print(windows_deps_message)
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
        return 1 if windows_deps_failed else 0

    print()
    any_failed = windows_deps_failed
    for name in (args.only or AGENT_NAMES):
        message, ok = install_cli(
            name, AGENT_EXECUTABLES[name], args.dry_run, current=current, musl=musl,
            update=args.update)
        print(message)
        if not ok:
            any_failed = True

    if args.update:
        print()
        if args.dry_run:
            pkg_messages, pkg_failed = update_package_managers(dry_run=True)
        elif _confirm("Also update pip/npm/pipx?"):
            pkg_messages, pkg_failed = update_package_managers(dry_run=False)
        else:
            pkg_messages, pkg_failed = ["skipped pip/npm/pipx update"], False
        for message in pkg_messages:
            print(message)
        any_failed = any_failed or pkg_failed
    return 1 if any_failed else 0


def run(argv: list[str] | None = None) -> int:
    """Entry point wrapper: a bug anywhere in this installer should never dump a raw traceback on
    someone just trying to install roundtable -- always exit with a clean one-line message.
    """
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 -- intentionally broad, see docstring
        print(f"error: unexpected failure in installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
