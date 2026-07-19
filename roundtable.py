#!/usr/bin/env python3
"""Roundtable: a dependency-free terminal UI for collaborating coding agents."""

from __future__ import annotations

import argparse
import curses
import concurrent.futures
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, TextIO


SYSTEM_BRIEF = """You are one member of a multi-AI roundtable. Work toward the user's objective.
Be concrete, inspect the workspace when useful, and openly identify uncertainty. Read the other
member's latest contribution, keep what is correct, challenge weak assumptions, and improve the
solution. Do not merely agree. Your response becomes part of a shared transcript, so make it
self-contained and concise. Do not address the user until asked for the final answer."""

# Prefixed onto the objective for --self runs, so it's part of every prompt for the whole session
# (including follow-ups) without threading a new parameter through prompt_for/final_prompt/conduct.
SELF_EDIT_NOTE = (
    "This objective is about roundtable's own source (roundtable.py, test_roundtable.py, README.md) "
    "in the current workspace. Read the existing code and tests before changing anything, and match "
    "its existing style and conventions. Keep the project dependency-free — standard library only, "
    "no pip installs or new third-party imports. Add or update tests for any behavior change, and "
    "run `python3 -m unittest test_roundtable` yourself before finishing — report the result."
)

AGENT_NAMES: tuple[str, ...] = ("Codex", "Claude", "Antigravity")

# Nudges agents toward complementary parts of the work instead of three redundant attempts at the
# same whole task. Which agent gets which nudge rotates by objective (see role_hints_for) rather
# than being fixed to one identity, so no agent is permanently typecast into the same lane.
ROLE_HINTS_BY_SLOT: tuple[str, ...] = (
    "Your edge here is direct, sandboxed execution in this workspace: lean on actually running and "
    "testing the solution rather than only describing it.",
    "Your edge here is structured reasoning and clear writing: focus on the architecture, tradeoffs, "
    "and edge cases, and make sure the approach is well-explained and sound.",
    "Your edge here is breadth: look for angles, edge cases, or alternate approaches the others "
    "might miss, and verify or stress-test what's being proposed.",
)


def role_hints_for(objective: str) -> dict[str, str]:
    """Assign the three role hints to the three agents, rotated by objective.

    Stable across follow-ups in the same session (same objective), but varies session to session so
    each agent leads execution, reasoning, and breadth roughly equally over time instead of always
    the same one.
    """
    offset = int(hashlib.sha256(("roles:" + objective).encode()).hexdigest(), 16) % len(AGENT_NAMES)
    rotated = AGENT_NAMES[offset:] + AGENT_NAMES[:offset]
    return dict(zip(rotated, ROLE_HINTS_BY_SLOT))


@dataclass
class Turn:
    speaker: str
    phase: str
    content: str


@dataclass
class Session:
    objective: str
    workspace: str
    rounds: int
    started_at: str
    turns: list[Turn]
    final: str = ""


@dataclass
class LineEditor:
    """Small curses-independent model for an editable multiline text box."""

    buffer: list[str]
    cursor: int

    def __init__(self, text: str = ""):
        self.buffer = list(text)
        self.cursor = len(self.buffer)

    @property
    def text(self) -> str:
        return "".join(self.buffer)

    def handle_key(self, key: object) -> str | None:
        if key in ("\n", "\r"):
            return "submit" if self.text.strip() else None
        if key == "\x1b":
            return "cancel"
        if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            if self.cursor:
                self.buffer.pop(self.cursor - 1)
                self.cursor -= 1
        elif key == curses.KEY_DC:
            if self.cursor < len(self.buffer):
                self.buffer.pop(self.cursor)
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.buffer), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.buffer)
        elif key == "\x0e":  # Ctrl-N: newline without submitting.
            self.buffer.insert(self.cursor, "\n")
            self.cursor += 1
        elif isinstance(key, str) and key.isprintable():
            self.buffer.insert(self.cursor, key)
            self.cursor += 1
        return None


def editor_layout(editor: LineEditor, width: int, height: int) -> tuple[list[str], int, int]:
    """Return visible wrapped lines and the cursor location within them."""
    width, height = max(1, width), max(1, height)
    lines = [""]
    cursor_line = cursor_col = 0
    for index, char in enumerate(editor.text):
        if index == editor.cursor:
            cursor_line, cursor_col = len(lines) - 1, len(lines[-1])
        if char == "\n":
            lines.append("")
        else:
            if len(lines[-1]) >= width:
                lines.append("")
            lines[-1] += char
    if editor.cursor == len(editor.text):
        if len(lines[-1]) >= width:
            lines.append("")
        cursor_line, cursor_col = len(lines) - 1, len(lines[-1])
    start = max(0, min(cursor_line, len(lines) - height))
    visible = lines[start:start + height]
    return visible, cursor_line - start, cursor_col


def balanced_columns(total_width: int, count: int, gap: int = 2,
                     margin: int = 1) -> list[tuple[int, int]]:
    """Return evenly spaced (x, width) columns, distributing spare cells."""
    usable = total_width - (2 * margin) - (gap * (count - 1))
    if count < 1 or usable < count:
        return []
    base, remainder = divmod(usable, count)
    columns: list[tuple[int, int]] = []
    x = margin
    for index in range(count):
        width = base + (1 if index < remainder else 0)
        columns.append((x, width))
        x += width + gap
    return columns


@dataclass(frozen=True)
class CodeChange:
    kind: str
    path: str


class WorkspaceMonitor:
    """Tracks file changes made during the roundtable without requiring Git."""

    SKIP_DIRS = {".git", ".roundtable", "node_modules", "__pycache__", ".venv", "venv"}

    def __init__(self, root: Path, max_files: int = 10_000):
        self.root = root
        self.max_files = max_files
        self.baseline = self._snapshot()
        self.changes: list[CodeChange] = []
        self.last_scan = 0.0
        self.truncated = len(self.baseline) >= self.max_files

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        files: dict[str, tuple[int, int]] = {}
        for base, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for name in names:
                path = Path(base) / name
                try:
                    stat = path.stat()
                    relative = str(path.relative_to(self.root))
                    files[relative] = (stat.st_mtime_ns, stat.st_size)
                except (OSError, ValueError):
                    continue
                if len(files) >= self.max_files:
                    return files
        return files

    def refresh(self, force: bool = False) -> list[CodeChange]:
        now = time.monotonic()
        if not force and now - self.last_scan < 0.75:
            return self.changes
        self.last_scan = now
        current = self._snapshot()
        changes = [CodeChange("added", path) for path in current.keys() - self.baseline.keys()]
        changes += [CodeChange("deleted", path) for path in self.baseline.keys() - current.keys()]
        changes += [CodeChange("modified", path) for path in current.keys() & self.baseline.keys()
                    if current[path] != self.baseline[path]]
        order = {"modified": 0, "added": 1, "deleted": 2}
        self.changes = sorted(changes, key=lambda item: (order[item.kind], item.path))
        return self.changes


def has_touchscreen(device_data: str | None = None) -> bool:
    """Detect touch digitizers, including the Atmel maXTouch used by Lenovo Yoga devices."""
    if device_data is None:
        try:
            device_data = Path("/proc/bus/input/devices").read_text(errors="replace")
        except OSError:
            return False
    names = [line.lower() for line in device_data.splitlines() if line.startswith("N: Name=")]
    markers = ("touchscreen", "touch screen", "digitizer", "maxtouch")
    return any(any(marker in name for marker in markers) for name in names)


def battery_summary() -> str:
    supplies = Path("/sys/class/power_supply")
    try:
        batteries = sorted(supplies.glob("BAT*"))
    except OSError:
        return ""
    if not batteries:
        return ""
    try:
        capacity = int((batteries[0] / "capacity").read_text().strip())
        status = (batteries[0] / "status").read_text().strip().lower()
    except (OSError, ValueError):
        return ""
    icon = "⚡" if status == "charging" else "▱"
    return f"{icon} {capacity}%"


def suppress_focus_reporting() -> None:
    """Turn off the terminal's focus-in/out reporting (DECSET mode 1004) for this session.

    If it's on, the terminal sends `ESC [ I` / `ESC [ O` on every focus change (e.g. alt-tab away
    and back). Curses has no special handling for that sequence, so it's read as a bare Escape
    keypress followed by stray characters — and every text box here treats Escape as cancel, which
    silently ends the whole session. curses.wrapper's endwin() can also reassert a terminal's default
    modes between screens, so this needs to run at the start of every fresh curses session, not just
    once for the whole process.
    """
    try:
        sys.stdout.write("\x1b[?1004l")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int) -> str:
    """Render recent numeric history as a compact block-character trend line."""
    if width <= 0:
        return ""
    if not values:
        return "·" * width
    recent = values[-width:]
    lo, hi = min(recent), max(recent)
    span = hi - lo
    if span <= 0:
        bars = (_SPARK_BLOCKS[-1] if hi > 0 else _SPARK_BLOCKS[0]) * len(recent)
    else:
        bars = "".join(
            _SPARK_BLOCKS[min(len(_SPARK_BLOCKS) - 1, int((v - lo) / span * (len(_SPARK_BLOCKS) - 1)))]
            for v in recent
        )
    return bars.rjust(width, "·")


def activity_sparkline(pulses: Iterable[float], width: int, window: float = 12.0,
                       now: float | None = None) -> str:
    """Render a live pulse of recent activity ticks bucketed across a trailing window."""
    if width <= 0:
        return ""
    now = time.monotonic() if now is None else now
    bucket = window / width
    counts = [0.0] * width
    for pulse in pulses:
        age = now - pulse
        if 0 <= age <= window:
            index = width - 1 - min(width - 1, int(age / bucket))
            counts[index] += 1
    return sparkline(counts, width)


DEFAULT_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Distinct working-indicator animations per agent, so a glance at the panel identifies
# who is active without reading the header. 'speed' slows the frame advance for a calmer pulse.
AGENT_SPINNERS: dict[str, tuple[tuple[str, ...], int]] = {
    "Claude": (("·", "✢", "✳", "∗", "✻", "✽", "✻", "∗", "✳", "✢"), 3),
    "Codex": (("◜", "◠", "◝", "◞", "◡", "◟"), 1),
    "Antigravity": (("⠁", "⠂", "⠄", "⡀", "⢀", "⠠", "⠐", "⠈"), 1),
}


def spinner_frame(name: str, frame: int) -> str:
    """Pick the animation frame for one agent's working indicator."""
    frames, speed = AGENT_SPINNERS.get(name, (DEFAULT_SPINNER_FRAMES, 1))
    return frames[(frame // speed) % len(frames)]


def clip(text: str, limit: int = 16_000) -> str:
    if len(text) <= limit:
        return text
    return "[Earlier content clipped]\n" + text[-limit:]


def transcript(turns: list[Turn]) -> str:
    return clip("\n\n".join(f"## {t.speaker} — {t.phase}\n{t.content}" for t in turns))


class Agent:
    def __init__(self, name: str, workspace: Path, model: str | None = None,
                elevated: bool = False):
        self.name = name
        self.workspace = workspace
        self.model = model
        self.elevated = elevated
        self.cancel_event: threading.Event | None = None

    def command(self, prompt: str, output_file: Path | None = None) -> list[str]:
        if self.name == "Codex":
            cmd = ["codex", "exec", "--skip-git-repo-check", "--color", "never", "--ephemeral",
                   "-C", str(self.workspace)]
            cmd += (["--dangerously-bypass-approvals-and-sandbox"] if self.elevated
                    else ["--sandbox", "workspace-write"])
            if self.model:
                cmd += ["--model", self.model]
            if output_file:
                cmd += ["--output-last-message", str(output_file)]
            return cmd + [prompt]
        if self.name == "Claude":
            cmd = ["claude", "--print", "--no-session-persistence", "--output-format", "text"]
            cmd += (["--dangerously-skip-permissions"] if self.elevated
                    else ["--permission-mode", "acceptEdits"])
            if self.model:
                cmd += ["--model", self.model]
            return cmd + [prompt]
        if self.name == "Antigravity":
            cmd = ["agy", "--print", prompt, "--mode", "accept-edits"]
            cmd += ["--dangerously-skip-permissions"] if self.elevated else ["--sandbox"]
            if self.model:
                cmd += ["--model", self.model]
            return cmd
        raise ValueError(f"Unsupported agent: {self.name}")

    def run(self, prompt: str, on_tick: Callable[[str], None],
            cancel_event: threading.Event | None = None) -> str:
        cancel_event = cancel_event or self.cancel_event
        with tempfile.TemporaryDirectory(prefix="roundtable-") as td:
            output_file = Path(td) / "last.txt" if self.name == "Codex" else None
            cmd = self.command(prompt, output_file)
            proc = subprocess.Popen(
                cmd, cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
            captured: list[str] = []
            events: queue.SimpleQueue[str] = queue.SimpleQueue()

            def read_output() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    captured.append(line)
                    events.put(line.rstrip())

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            def stop_process() -> None:
                if proc.poll() is not None:
                    return
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()

            try:
                while proc.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        stop_process()
                        raise RuntimeError(f"{self.name} cancelled")
                    latest = ""
                    while True:
                        try:
                            latest = events.get_nowait()
                        except queue.Empty:
                            break
                    on_tick(latest)
                    time.sleep(0.1)
            except KeyboardInterrupt:
                stop_process()
                raise
            code = proc.returncode
            reader.join(timeout=2)
            while True:
                try:
                    on_tick(events.get_nowait())
                except queue.Empty:
                    break
            raw = "".join(captured).strip()
            if output_file and output_file.exists():
                answer = output_file.read_text(encoding="utf-8", errors="replace").strip()
            else:
                answer = raw
            if code != 0:
                raise RuntimeError(f"{self.name} exited with status {code}\n{raw[-2000:]}")
            if not answer:
                raise RuntimeError(f"{self.name} returned an empty response")
            return answer


class MockAgent(Agent):
    def run(self, prompt: str, on_tick: Callable[[str], None],
            cancel_event: threading.Event | None = None) -> str:
        cancel_event = cancel_event or self.cancel_event
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(f"{self.name} cancelled")
        time.sleep(0.15)
        on_tick("thinking…")
        return (f"{self.name} contribution: I evaluated the objective, incorporated the other "
                "agent's useful points, and proposed a concrete next step with explicit tradeoffs.")


TASK_STATUS_COMPLETE = re.compile(r"task status:[ \t]*complete", re.IGNORECASE)
TASK_STATUS_HINT = (
    "\n\nEnd your response with a final line reading exactly `TASK STATUS: complete` if the "
    "objective is now fully done and verified (files written, checked, working), or `TASK STATUS: "
    "in-progress` otherwise. If you mark it complete, the other agents still active this phase will "
    "stop their own attempt and review/refine your work instead of redoing it from scratch."
)


def signals_task_complete(text: str) -> bool:
    """Whether an agent's response ends with a TASK STATUS: complete marker."""
    lines = text.rstrip().splitlines()
    return bool(lines and TASK_STATUS_COMPLETE.fullmatch(lines[-1].strip()))


DIBS_PATTERN = re.compile(r"^\s*dibs:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
DIBS_HINT = (
    "\n\nStart your response with a line reading `DIBS: <short claim>` naming the specific part of "
    "the objective you're taking ownership of this round (e.g. `DIBS: the retry/backoff logic`), "
    "then give your actual contribution below it. This lets the other agents pick something else "
    "next round instead of redoing the same ground."
)


def extract_dibs(turns: list[Turn]) -> dict[str, str]:
    """Each named agent's most recent DIBS claim, later turns overriding earlier ones from the same
    agent — this is what each agent currently owns, not a full claim history."""
    claims: dict[str, str] = {}
    for turn in turns:
        if turn.speaker not in AGENT_NAMES:
            continue
        match = DIBS_PATTERN.search(turn.content)
        if match:
            claims[turn.speaker] = match.group(1).strip()
    return claims


def prompt_for(objective: str, turns: list[Turn], phase: str, speaker: str,
              sequential: bool = False, scope: str = "", task_status_check: bool = False) -> str:
    history = transcript(turns) or "(No contributions yet.)"
    collab_note = (
        "You are working in a live sequential relay: the shared transcript above already includes "
        "this round's most recent contribution from another agent, if any. Build directly on it, "
        "correct it where it is wrong, and avoid restating it.\n" if sequential else
        "You are working independently and in parallel with the other agents this round; you will not "
        "see their output until the round is complete.\n"
    )
    if phase == "proposal":
        task = (collab_note + "Take ownership of a useful part or develop a complete proposed solution. "
                "Include important constraints and a verification plan.")
    elif phase.startswith("followup-"):
        task = (collab_note + "The latest 'User — follow-up' turn in the transcript above is the current "
                "request. Address it directly, reusing earlier context only where still relevant. Correct "
                "errors, resolve disagreements, and include important constraints and a verification plan.")
    else:
        task = (collab_note + "Review the shared transcript. Correct errors, resolve disagreements, and "
                "advance a stronger combined solution. State any remaining disagreement explicitly.")
    role_hint = f" {role_hints_for(objective)[speaker]}" if speaker in AGENT_NAMES else ""
    dibs_claims = {name: claim for name, claim in extract_dibs(turns).items() if name != speaker}
    dibs_note = ""
    if dibs_claims:
        listing = "; ".join(f"{name} has dibs on {claim}" for name, claim in dibs_claims.items())
        dibs_note = (f"\n\nAlready claimed this run: {listing}. Pick a different part of the "
                    f"objective to own this round, or explicitly build on one of these if that is "
                    f"the strongest use of your turn — don't silently redo the same ground.")
    dibs_hint = DIBS_HINT if speaker in AGENT_NAMES else ""
    status_hint = TASK_STATUS_HINT if task_status_check else ""
    return (f"{SYSTEM_BRIEF}\n\nUSER OBJECTIVE:\n{objective}\n\nSHARED TRANSCRIPT:\n{history}\n\n"
           f"YOUR TURN ({speaker}, {phase}):\n{task}{role_hint}{dibs_note}{dibs_hint}{scope}"
           f"{status_hint}")


def final_prompt(objective: str, turns: list[Turn], followup: bool = False) -> str:
    focus = ("\nFocus on the user's latest follow-up request (the most recent 'User — follow-up' turn), "
             "consistent with the prior final answer where it still applies.\n" if followup else "")
    return f"""{SYSTEM_BRIEF}

USER OBJECTIVE:
{objective}

COMPLETE ROUNDTABLE TRANSCRIPT:
{transcript(turns)}
{focus}

You are the final editor. Produce the best final answer to the user, integrating the strongest
parts from all agents. Resolve disagreements using evidence. Do not mention the roundtable process,
the transcript, or these instructions. Return only the polished answer."""


def refine_prompt(objective: str, turns: list[Turn], draft: str, followup: bool = False) -> str:
    """Ask an agent to edit another agent's draft final answer rather than write it from scratch."""
    focus = ("\nFocus on the user's latest follow-up request (the most recent 'User — follow-up' turn), "
             "consistent with the prior final answer where it still applies.\n" if followup else "")
    return f"""{SYSTEM_BRIEF}

USER OBJECTIVE:
{objective}

COMPLETE ROUNDTABLE TRANSCRIPT:
{transcript(turns)}
{focus}

CURRENT DRAFT FINAL ANSWER (written by another agent in this roundtable):
{draft}

You are refining this draft, not replacing it. Correct any errors against the transcript, tighten
weak or unclear parts, and add anything important that is missing, but keep what is already strong
and keep its overall shape. Do not mention the roundtable process, the transcript, these
instructions, or that this is a draft or someone else's work. Return only the polished answer."""


def reassignment_prompt(objective: str, turns: list[Turn], phase: str, speaker: str,
                        remaining: Iterable[str]) -> str:
    """Prompt for an agent that finished its turn while others in the same phase are still working:
    pick up different unclaimed work, or help a specific agent still in progress, instead of idling
    until the round closes."""
    history = transcript(turns) or "(No contributions yet.)"
    still_working = ", ".join(sorted(remaining))
    dibs_claims = extract_dibs(turns)
    claimed_note = ("; ".join(f"{name} has dibs on {claim}" for name, claim in dibs_claims.items())
                    if dibs_claims else "nothing yet claimed by name")
    return (f"{SYSTEM_BRIEF}\n\nUSER OBJECTIVE:\n{objective}\n\nSHARED TRANSCRIPT:\n{history}\n\n"
           f"YOUR TURN ({speaker}, {phase} · extra):\n"
           f"You already finished your part of this round while {still_working} are still working "
           f"({claimed_note}). Rather than sit idle, do one of two things: (1) pick up a different, "
           f"useful part of the objective nobody has claimed yet — open with a DIBS: line as usual — "
           f"or (2) prepare something that will genuinely help {still_working}: a check, a test, "
           f"missing context, or an alternative worth them considering. Say up front which you're "
           f"doing and, for (2), who it's for. Choose whichever is more valuable; be concrete either "
           f"way. If there is truly nothing useful left to add, say so briefly instead of padding.")


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file atomically without risking the previous saved version."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _session_stamp(session: Session) -> str:
    """Stable filename stamp for a session, shared by its transcript and log file."""
    try:
        return datetime.fromisoformat(session.started_at).strftime("%Y%m%d-%H%M%S-%f")
    except ValueError:
        digest = hashlib.sha256(
            f"{session.started_at}\0{session.workspace}\0{session.objective}".encode()
        ).hexdigest()[:12]
        return f"legacy-{digest}"


def log_path_for(session: Session, output_dir: Path) -> Path:
    """Path to this session's activity log — pairs with its .json/.md transcript."""
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"roundtable-{_session_stamp(session)}.log"


def save_session(session: Session, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _session_stamp(session)
    json_path = output_dir / f"roundtable-{stamp}.json"
    md_path = output_dir / f"roundtable-{stamp}.md"
    data = asdict(session)
    atomic_write_text(json_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    body = [f"# Roundtable\n\n**Objective:** {session.objective}\n"]
    body += [f"## {t.speaker} — {t.phase}\n\n{t.content}\n" for t in session.turns]
    body.append(f"## Final answer\n\n{session.final}\n")
    atomic_write_text(md_path, "\n".join(body))
    return json_path, md_path


def load_session(path: Path) -> Session:
    """Load a saved JSON session, rejecting malformed or incomplete transcripts."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read session {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("session JSON must contain an object")
    required = {"objective": str, "workspace": str, "rounds": int,
                "started_at": str, "turns": list}
    for field, expected in required.items():
        if field not in data or not isinstance(data[field], expected):
            raise ValueError(f"session field {field!r} must be {expected.__name__}")
    if not 0 <= data["rounds"] <= 5:
        raise ValueError("session rounds must be between 0 and 5")
    turns: list[Turn] = []
    for index, item in enumerate(data["turns"]):
        if not isinstance(item, dict) or any(
                not isinstance(item.get(field), str)
                for field in ("speaker", "phase", "content")):
            raise ValueError(f"session turn {index} is malformed")
        turns.append(Turn(item["speaker"], item["phase"], item["content"]))
    final = data.get("final", "")
    if not isinstance(final, str):
        raise ValueError("session field 'final' must be str")
    return Session(data["objective"], data["workspace"], data["rounds"],
                   data["started_at"], turns, final)


class RunLog:
    """Append-only per-run log of phase transitions, prompts sent, output ticks, and errors.

    The in-memory console panel keeps only a truncated, capped history for display; this file
    keeps everything — including full prompt text — so a live run can be tailed, or a completed
    one inspected, for detail --mock never produces (auth failures, bad exit codes, empty
    responses, timeouts).
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.started = time.monotonic()
        self._handle: TextIO | None = path.open("a", encoding="utf-8") if path else None
        if self._handle is not None:
            path.chmod(0o600)
            self._handle.write(f"\n# Roundtable run started {datetime.now(timezone.utc).isoformat()}\n")
            self._handle.flush()

    def write(self, kind: str, text: str) -> None:
        if self._handle is None:
            return
        elapsed = time.monotonic() - self.started
        for line in text.splitlines() or [""]:
            self._handle.write(f"+{elapsed:8.1f}s  {kind.upper():7s} {line}\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


# (label, kinds-to-show — None means everything). Cycled with the 'c' key; "key events" is the
# default so the console opens signal-dense (phase changes, completed turns, errors) rather than a
# firehose of raw per-line ticks, with the option to drill into everything or one category on demand.
CONSOLE_FILTERS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("key events", ("phase", "turn", "error")),
    ("all activity", None),
    ("prompts", ("prompt",)),
    ("errors only", ("error",)),
)
CONSOLE_KIND_GLYPH: dict[str, str] = {
    "phase": "▶", "turn": "✓", "tick": "·", "prompt": "➤", "error": "✗", "info": "·",
}


class Display:
    def __init__(self, stdscr: curses.window, session: Session, touch_mode: bool = False,
                run_log: RunLog | None = None):
        self.s = stdscr
        self.session = session
        self.run_log = run_log or RunLog(None)
        self.status = "Ready"
        self.activity: dict[str, str] = {}
        self.active: set[str] = set()
        self.phase_completed: set[str] = set()
        self.busy = False
        self.error = ""
        self.frame = 0
        self.monitor = WorkspaceMonitor(Path(session.workspace))
        self.started = time.monotonic()
        self.touch_mode = touch_mode
        self.hitboxes: dict[str, tuple[int, int, int, int]] = {}
        self.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0, "Console": 0}
        self.expanded: str | None = None
        self.console_filter = 0
        self.usage_names = ("Codex", "Claude", "Antigravity")
        self.turn_times: dict[str, list[float]] = {name: [] for name in self.usage_names}
        self.turn_outputs: dict[str, list[int]] = {name: [] for name in self.usage_names}
        self.activity_pulses: dict[str, deque[float]] = {
            name: deque(maxlen=200) for name in self.usage_names}
        self.turn_start: dict[str, float] = {}
        for turn in session.turns:
            if turn.speaker in self.turn_outputs:
                self.turn_outputs[turn.speaker].append(len(turn.content))
        self._known_turn_count = len(session.turns)
        self.console: deque[tuple[str, str]] = deque(maxlen=300)
        self.log(f"Objective: {session.objective}", kind="phase")
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.s.nodelay(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_MAGENTA, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_YELLOW, -1)
            curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)

    def log(self, text: str, kind: str = "info", file_text: str | None = None) -> None:
        elapsed = time.monotonic() - self.started
        self.console.append((kind, f"+{elapsed:6.1f}s  {text}"))
        self.run_log.write(kind, file_text if file_text is not None else text)

    def log_prompt(self, name: str, prompt: str) -> None:
        """Record a prompt sent to an agent: a short summary on-screen, the full text to disk."""
        summary = textwrap.shorten(prompt.replace("\n", " ").strip(), width=100, placeholder="…")
        self.log(f"[{name}] prompt sent ({len(prompt)} chars): {summary}", kind="prompt",
                file_text=f"[{name}] PROMPT:\n{prompt}")

    def update_status(self, active: Iterable[str], message: str) -> None:
        next_active = set(active)
        for name in next_active - self.active:
            self.turn_start[name] = time.monotonic()
        if message != self.status:
            self.log(message, kind="phase")
        if next_active < self.active:
            self.phase_completed.update(self.active - next_active)
        else:
            self.phase_completed = set()
            self.activity = {}
        self.active, self.status = next_active, message
        for name in self.active:
            self.activity.setdefault(name, "")

    def tick(self, name: str, line: str = "") -> None:
        self.frame += 1
        if line:
            self.activity[name] = line[-180:]
            if name:
                self.log(f"[{name}] {line}", kind="tick")
        if name in self.activity_pulses:
            self.activity_pulses[name].append(time.monotonic())
        if len(self.session.turns) > self._known_turn_count:
            for turn in self.session.turns[self._known_turn_count:]:
                start = self.turn_start.pop(turn.speaker, None)
                duration = None
                if turn.speaker in self.turn_outputs:
                    self.turn_outputs[turn.speaker].append(len(turn.content))
                    if start is not None:
                        duration = time.monotonic() - start
                        self.turn_times[turn.speaker].append(duration)
                detail = f"{duration:.1f}s · " if duration is not None else ""
                self.log(f"{turn.speaker} · {turn.phase} · {detail}{len(turn.content)} chars", kind="turn")
            self._known_turn_count = len(self.session.turns)
        self.monitor.refresh()
        self.draw()
        self.poll_input()

    @staticmethod
    def _inside(box: tuple[int, int, int, int], y: int, x: int) -> bool:
        top, left, bottom, right = box
        return top <= y <= bottom and left <= x <= right

    EXPAND_KEYS = {ord("1"): "Codex", ord("2"): "Claude", ord("3"): "Antigravity",
                  ord("f"): "Final", ord("F"): "Final", ord("0"): "Console"}
    COLLAPSE_KEYS = (27, ord("q"), ord("Q"))  # Esc, q
    CONSOLE_FILTER_KEYS = (ord("c"), ord("C"))
    PANEL_NAMES = ("Codex", "Claude", "Antigravity", "Final", "Console")
    AGENTS = (
        ("Codex", "◇", "OpenAI coding agent", 1),
        ("Claude", "✳", "Anthropic coding agent", 5),
        ("Antigravity", "△", "Google coding agent", 2),
    )

    def toggle_expanded(self, name: str) -> None:
        """Show one panel full-size for its complete content, or collapse back to the dashboard."""
        self.expanded = None if self.expanded == name else name
        self.draw()

    def cycle_console_filter(self) -> None:
        """Switch the console between key-events / all-activity / prompts-only / errors-only."""
        self.console_filter = (getattr(self, "console_filter", 0) + 1) % len(CONSOLE_FILTERS)
        self.draw()

    def _filtered_console(self) -> tuple[str, list[tuple[str, str]]]:
        label, kinds = CONSOLE_FILTERS[getattr(self, "console_filter", 0) % len(CONSOLE_FILTERS)]
        entries = list(self.console) if kinds is None else [e for e in self.console if e[0] in kinds]
        return label, entries

    @staticmethod
    def _kind_attr(kind: str) -> int:
        return {
            "phase": curses.color_pair(1) | curses.A_BOLD,
            "turn": curses.color_pair(3),
            "tick": curses.A_DIM,
            "error": curses.color_pair(4) | curses.A_BOLD,
            "prompt": curses.color_pair(5),
        }.get(kind, curses.A_DIM)

    def poll_input(self) -> None:
        """Handle touch, keyboard cancellation, and panel-expand shortcuts while agents work."""
        while True:
            key = self.s.getch()
            if key == -1:
                return
            if key == 3:
                raise KeyboardInterrupt
            if key in self.EXPAND_KEYS:
                self.toggle_expanded(self.EXPAND_KEYS[key])
                continue
            if key in self.COLLAPSE_KEYS and self.expanded:
                self.expanded = None
                self.draw()
                continue
            if key in self.CONSOLE_FILTER_KEYS:
                self.cycle_console_filter()
                continue
            if key != curses.KEY_MOUSE:
                continue
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                continue
            action = self.handle_mouse(x, y, state)
            if action == "stop":
                raise KeyboardInterrupt

    def handle_mouse(self, x: int, y: int, state: int) -> str | None:
        """Translate terminal mouse events, including touchscreen taps and swipes."""
        # BUTTON1_PRESSED is deliberately excluded: with mouse-position reporting on (needed for
        # swipe/scroll), most terminals send a press event AND a separate release event for one
        # physical click. Treating both as a "tap" fired every toggle twice — expand then instantly
        # collapse again — which looked like clicking only worked while held down.
        tapped = state & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)
        if tapped and "stop" in self.hitboxes and self._inside(self.hitboxes["stop"], y, x):
            return "stop"
        if tapped:
            for action in ("send", "newline", "finish", "clear"):
                box = self.hitboxes.get(action)
                if box and self._inside(box, y, x):
                    return action
            for name in self.PANEL_NAMES:
                box = self.hitboxes.get(name.lower())
                if box and self._inside(box, y, x):
                    self.toggle_expanded(name)
                    return None
        direction = 0
        if state & curses.BUTTON4_PRESSED:
            direction = 3
        elif hasattr(curses, "BUTTON5_PRESSED") and state & curses.BUTTON5_PRESSED:
            direction = -3
        if direction:
            for name in self.PANEL_NAMES:
                box = self.hitboxes.get(name.lower())
                if box and self._inside(box, y, x):
                    self.scroll[name] = max(0, self.scroll.get(name, 0) + direction)
                    self.draw()
                    break
        return None

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.s.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.s.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    def _box(self, y: int, x: int, height: int, width: int, color: int = 0) -> None:
        if height < 2 or width < 4:
            return
        self._put(y, x, "╭" + "─" * (width - 2) + "╮", color)
        for row in range(y + 1, y + height - 1):
            self._put(row, x, "│", color)
            self._put(row, x + width - 1, "│", color)
        self._put(y + height - 1, x, "╰" + "─" * (width - 2) + "╯", color)

    @staticmethod
    def _wrapped(content: str, width: int) -> list[str]:
        lines: list[str] = []
        for paragraph in content.splitlines() or [""]:
            lines.extend(textwrap.wrap(paragraph, width=max(1, width),
                                       replace_whitespace=False) or [""])
        return lines

    def _agent_panel(self, y: int, x: int, height: int, width: int, name: str,
                     icon: str, subtitle: str, color: int) -> None:
        self._box(y, x, height, width, color)
        is_active = name in self.active and self.busy
        has_responded = name in self.phase_completed or any(
            t.speaker == name for t in self.session.turns)
        state = "● working" if is_active else ("✓ responded" if has_responded else "○ waiting")
        state_attr = color | (curses.A_BOLD if is_active else curses.A_DIM)
        self._put(y, x + 2, f" {icon}  {name.upper()} ", color | curses.A_BOLD)
        state_x = max(x + 2, x + width - len(state) - 2)
        subtitle_width = max(0, state_x - (x + 2) - 1)
        self._put(y + 1, x + 2, subtitle[:subtitle_width], curses.A_DIM)
        self._put(y + 1, state_x, state, state_attr)
        self._put(y + 2, x + 1, "─" * (width - 2), curses.A_DIM)

        content_top = y + 3
        available = max(1, height - 5)
        usage_width = width - 4
        if name in self.usage_names and usage_width >= 24 and height >= 9:
            spark_width = max(4, min(6, (usage_width - 10) // 3))
            time_spark = sparkline(self.turn_times[name], spark_width)
            output_spark = sparkline([float(v) for v in self.turn_outputs[name]], spark_width)
            pulse_spark = activity_sparkline(self.activity_pulses[name], spark_width)
            usage_line = f"⏱{time_spark} ✎{output_spark} ⚡{pulse_spark}"
            self._put(y + 3, x + 2, usage_line[:usage_width], color | curses.A_DIM)
            content_top = y + 4
            available = max(1, height - 6)

        items = [t for t in self.session.turns if t.speaker == name]
        content = items[-1].content if items else (
            "Waiting for the shared task…" if not is_active else "Starting a new session…")
        lines = self._wrapped(content, width - 4)
        offset = min(self.scroll[name], max(0, len(lines) - available))
        end = len(lines) - offset if offset else len(lines)
        lines = lines[max(0, end - available):end]
        for row, line in enumerate(lines):
            self._put(content_top + row, x + 2, line)
        if is_active:
            spinner = spinner_frame(name, self.frame)
            elapsed = time.monotonic() - self.turn_start.get(name, time.monotonic())
            activity = textwrap.shorten(self.activity.get(name) or "Thinking",
                                        width=max(5, width - 16),
                                        placeholder="…")
            self._put(y + height - 2, x + 2, f"{spinner} {elapsed:4.1f}s  {activity}",
                     color | curses.A_DIM)
        elif offset:
            self._put(y + height - 2, x + 2, f"↑ {offset} lines from latest", color | curses.A_DIM)
        self.hitboxes[name.lower()] = (y, x, y + height - 1, x + width - 1)

    def draw(self, reserved_bottom: int = 0) -> None:
        """Draw the dashboard above an optional reserved area at the bottom."""
        self.s.erase()
        self.hitboxes = {}
        h, w = self.s.getmaxyx()
        content_height = max(0, h - reserved_bottom)
        if content_height < 20 or w < 72:
            self._put(0, 0, " Roundtable ".ljust(max(1, w - 1)), curses.A_REVERSE | curses.A_BOLD)
            self._put(2, 2, "Terminal too small", curses.A_BOLD)
            required_height = 20 + reserved_bottom
            self._put(4, 2, f"Resize to at least 72 × {required_height}. Ctrl-C cancels.")
            self.s.refresh()
            return

        # Product-style header, close to the restrained chrome of coding CLIs.
        self._put(0, 0, " " * (w - 1), curses.A_REVERSE)
        self._put(0, 2, "◈  ROUNDTABLE", curses.A_REVERSE | curses.A_BOLD)
        phase = f"{len([t for t in self.session.turns if t.speaker != 'Final'])} turns"
        hardware = "  ".join(item for item in
                             (("☝ touch" if self.touch_mode else ""), battery_summary()) if item)
        right = hardware or phase
        self._put(0, w - len(right) - 3, right, curses.A_REVERSE | curses.A_DIM)
        if self.busy and self.touch_mode:
            label = "  ■ STOP  "
            stop_x = max(20, w - len(right) - len(label) - 6)
            self._put(0, stop_x, label, curses.color_pair(4) | curses.A_REVERSE | curses.A_BOLD)
            self.hitboxes["stop"] = (0, stop_x, 0, stop_x + len(label) - 1)

        self._put(2, 2, "›", curses.color_pair(3) | curses.A_BOLD)
        obj = textwrap.shorten(self.session.objective, width=max(20, w - 8), placeholder="…")
        self._put(2, 4, obj, curses.A_BOLD)
        self._put(3, 4, f"{self.status}  ·  {self.session.workspace}", curses.A_DIM)

        if self.expanded:
            self._draw_expanded(content_height, w)
            self.s.refresh()
            return

        top = 5
        consensus_height = max(6, min(14, content_height // 3))
        agent_height = content_height - top - consensus_height - 4
        gap = 2
        agents = [(name, icon, subtitle, curses.color_pair(color_num))
                 for name, icon, subtitle, color_num in self.AGENTS]
        columns = balanced_columns(w, len(agents), gap=gap)
        for (x, panel_w), (name, icon, subtitle, color) in zip(columns, agents):
            self._agent_panel(top, x, agent_height, panel_w,
                              name, icon, subtitle, color)

        cy = top + agent_height + 1
        monitor_width = max(25, min(38, w // 3))
        answer_width = w - monitor_width - 4
        self._box(cy, 1, consensus_height, answer_width, curses.color_pair(3))
        self._put(cy, 3, " ◆  SHARED ANSWER ", curses.color_pair(3) | curses.A_BOLD)
        final_text = self.session.final or "The combined answer will appear here after all agents review the task."
        all_final_lines = self._wrapped(final_text, answer_width - 4)
        available_final = consensus_height - 3
        final_offset = min(self.scroll["Final"], max(0, len(all_final_lines) - available_final))
        final_end = len(all_final_lines) - final_offset if final_offset else len(all_final_lines)
        final_lines = all_final_lines[max(0, final_end - available_final):final_end]
        for row, line in enumerate(final_lines):
            self._put(cy + 2 + row, 4, line)
        self.hitboxes["final"] = (cy, 1, cy + consensus_height - 1, answer_width)

        mx = answer_width + 2
        monitor_height = consensus_height
        console_height = 0
        show_console = consensus_height >= 10
        if show_console:
            console_height = max(4, consensus_height // 2)
            monitor_height = consensus_height - console_height - 1
            if monitor_height < 4:
                show_console, monitor_height, console_height = False, consensus_height, 0

        self._box(cy, mx, monitor_height, monitor_width, curses.color_pair(2))
        changes = self.monitor.changes
        self._put(cy, mx + 2, f" ⌁  CODE MONITOR · {len(changes)} ",
                  curses.color_pair(2) | curses.A_BOLD)
        if changes:
            icons = {"added": "+", "modified": "~", "deleted": "−"}
            colors = {"added": curses.color_pair(3), "modified": curses.color_pair(5),
                      "deleted": curses.color_pair(4)}
            visible = changes[-max(1, monitor_height - 3):]
            for row, change in enumerate(visible):
                label = textwrap.shorten(change.path, width=max(5, monitor_width - 7), placeholder="…")
                self._put(cy + 2 + row, mx + 3, f"{icons[change.kind]} {label}", colors[change.kind])
        else:
            self._put(cy + 2, mx + 3, "No file changes yet", curses.A_DIM)
            if self.monitor.truncated:
                self._put(cy + 3, mx + 3, "Large workspace · partial scan", curses.A_DIM)

        if show_console:
            console_y = cy + monitor_height + 1
            self._box(console_y, mx, console_height, monitor_width, curses.color_pair(1))
            label, filtered = self._filtered_console()
            self._put(console_y, mx + 2, f" »  CONSOLE · {label} ", curses.color_pair(1) | curses.A_BOLD)
            counts = Counter(kind for kind, _ in self.console)
            summary = " ".join(f"{counts[k]}{CONSOLE_KIND_GLYPH[k]}" for k in
                               ("error", "phase", "turn", "prompt", "tick") if counts.get(k))
            if summary:
                self._put(console_y + 1, mx + 3, summary[:max(0, monitor_width - 5)], curses.A_DIM)
            available_console = max(1, console_height - 3)
            entries = filtered[-available_console:]
            for row, (kind, text) in enumerate(entries):
                glyph = CONSOLE_KIND_GLYPH.get(kind, "·")
                shown = textwrap.shorten(f"{glyph} {text}", width=max(5, monitor_width - 5),
                                         placeholder="…")
                self._put(console_y + 2 + row, mx + 3, shown, self._kind_attr(kind))
            self.hitboxes["console"] = (console_y, mx, console_y + console_height - 1,
                                        mx + monitor_width - 1)

        controls = ("tap STOP to cancel   ·   tap a panel to expand" if self.touch_mode else
                    "ctrl+c cancel   ·   1/2/3/f/0 expand   ·   c cycles console filter   ·   "
                    "click a panel to expand")
        footer = self.error or f"{controls}   ·   transcript saved automatically"
        attr = curses.color_pair(4) if self.error and curses.has_colors() else curses.A_DIM
        self._put(content_height - 1, 2,
                  textwrap.shorten(footer, width=max(10, w - 5), placeholder="…"), attr)
        self.s.refresh()

    def _draw_expanded(self, content_height: int, w: int) -> None:
        """Show one panel full-size with its complete content, in place of the normal dashboard."""
        top = 5
        height = content_height - top - 2
        by_name = {name: (icon, curses.color_pair(color_num), subtitle)
                  for name, icon, subtitle, color_num in self.AGENTS}
        if self.expanded in by_name:
            icon, color, subtitle = by_name[self.expanded]
            self._agent_panel(top, 1, height, w - 2, self.expanded, icon, subtitle, color)
        elif self.expanded == "Console":
            color = curses.color_pair(1)
            self._box(top, 1, height, w - 2, color)
            label, filtered = self._filtered_console()
            self._put(top, 3, f" »  CONSOLE (expanded) · {label} ", color | curses.A_BOLD)
            available = max(1, height - 3)
            offset = min(self.scroll.get("Console", 0), max(0, len(filtered) - available))
            end = len(filtered) - offset if offset else len(filtered)
            for row, (kind, text) in enumerate(filtered[max(0, end - available):end]):
                glyph = CONSOLE_KIND_GLYPH.get(kind, "·")
                line = textwrap.shorten(f"{glyph} {text}", width=w - 6, placeholder="…")
                self._put(top + 2 + row, 3, line, self._kind_attr(kind))
            self.hitboxes["console"] = (top, 1, top + height - 1, w - 2)
        else:  # "Final"
            color = curses.color_pair(3)
            self._box(top, 1, height, w - 2, color)
            self._put(top, 3, " ◆  SHARED ANSWER (expanded) ", color | curses.A_BOLD)
            content = self.session.final or "The combined answer will appear here after all agents review the task."
            lines = self._wrapped(content, w - 6)
            available = max(1, height - 3)
            offset = min(self.scroll["Final"], max(0, len(lines) - available))
            end = len(lines) - offset if offset else len(lines)
            for row, line in enumerate(lines[max(0, end - available):end]):
                self._put(top + 2 + row, 3, line)
            self.hitboxes["final"] = (top, 1, top + height - 1, w - 2)
        hint = ("tap panel to collapse" if self.touch_mode else
               "same key or Esc/q collapses   ·   1/2/3/f/0 switch panels   ·   c cycles filter   ·   "
               "wheel/click scrolls")
        self._put(top + height, 2, textwrap.shorten(hint, width=max(10, w - 5), placeholder="…"),
                 curses.A_DIM)

    def draw_followup(self, editor: LineEditor) -> None:
        """Draw a multiline editor while retaining the completed answer above."""
        h, w = self.s.getmaxyx()
        if h < 12 or w < 40:
            self.draw()
            return
        touch_mode = getattr(self, "touch_mode", False)
        height = 7 if touch_mode else 6
        reserved_bottom = height + 1
        self.draw(reserved_bottom=reserved_bottom)
        y = h - reserved_bottom
        self._box(y, 1, height, w - 3, curses.color_pair(1))
        self._put(y, 3, " ›  ASK A FOLLOW-UP ", curses.color_pair(1) | curses.A_BOLD)
        text_height = height - (3 if touch_mode else 2)
        lines, cursor_y, cursor_x = editor_layout(editor, w - 9, text_height)
        for row, line in enumerate(lines):
            self._put(y + 1 + row, 4, line)
        if not editor.text:
            self._put(y + 1, 4, "Add context, request changes, or ask the agents to continue…",
                      curses.A_DIM)
        if touch_mode:
            buttons = [("send", "  SEND  ", curses.color_pair(3) | curses.A_BOLD),
                       ("newline", "  NEW LINE  ", curses.color_pair(1)),
                       ("clear", "  CLEAR  ", curses.A_DIM),
                       ("finish", "  FINISH  ", curses.color_pair(5))]
            bx = 4
            for action, label, attr in buttons:
                self._put(y + height - 2, bx, label, attr | curses.A_REVERSE)
                self.hitboxes[action] = (y + height - 2, bx, y + height - 2, bx + len(label) - 1)
                bx += len(label) + 2
            self._put(h - 1, 2, "Tap a button · swipe agent panels to scroll", curses.A_DIM)
        else:
            self._put(h - 1, 2,
                      "Enter send  ·  Ctrl+N new line  ·  Esc finish  ·  Ctrl+C cancel",
                      curses.A_DIM)
        try:
            self.s.move(y + 1 + cursor_y, min(w - 5, 4 + cursor_x))
        except curses.error:
            pass
        self.s.refresh()


OPTION_TOGGLES: tuple[tuple[str, str], ...] = (
    ("elevated", "Elevated permissions — all agents bypass sandboxing (dangerous)"),
    ("balance_load", "Balance load — scope down an agent running much slower than the others"),
    ("task_status_check", "Task status check — stop redundant work once one agent finishes"),
    ("self", "Self-edit — point the workspace at roundtable's own source to improve itself"),
    ("skip_preflight", "Skip preflight — bypass the preliminary system check (saves time)"),
    ("reassign_idle", "Reassign idle — a finished agent picks up other work instead of waiting"),
)


def apply_option_key(values: dict[str, bool], key: object) -> tuple[dict[str, bool], bool]:
    """Apply one keypress from the options screen. Returns (possibly updated values, done)."""
    if key in ("\n", "\r", "\x1b", "q", "Q") or key in (curses.KEY_ENTER, 10, 13, 27):
        return values, True
    if isinstance(key, str) and key.isdigit():
        index = int(key) - 1
        if 0 <= index < len(OPTION_TOGGLES):
            name = OPTION_TOGGLES[index][0]
            values = {**values, name: not values[name]}
    return values, False


def read_options_ui(stdscr: curses.window, defaults: dict[str, bool]) -> dict[str, bool]:
    """A quick numbered-toggle screen for opt-in flags, shown right after startup so they can be
    switched on or off without remembering CLI flag names. CLI flags still set the starting values
    shown here, and Enter/Esc/q all just continue with whatever is currently checked."""
    suppress_focus_reporting()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    values = dict(defaults)
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, " ◈  ROUNDTABLE — OPTIONS ".ljust(max(1, w - 1)), max(1, w - 1),
                      curses.A_REVERSE | curses.A_BOLD)
        if h >= 5 + len(OPTION_TOGGLES) and w >= 40:
            stdscr.addstr(2, 3, "Press a number to toggle, Enter to continue:", curses.A_BOLD)
            for i, (name, label) in enumerate(OPTION_TOGGLES):
                mark = "x" if values[name] else " "
                stdscr.addnstr(4 + i, 3, f"[{mark}] {i + 1}  {label}", w - 6)
            stdscr.addnstr(5 + len(OPTION_TOGGLES), 3, "Enter / Esc / q continue", w - 6,
                          curses.A_DIM)
        else:
            stdscr.addnstr(2, 1, "Resize terminal to at least 40 columns", max(1, w - 2))
        stdscr.refresh()
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        values, done = apply_option_key(values, key)
        if done:
            return values


def read_objective_ui(stdscr: curses.window, workspace: Path, touch_mode: bool = False) -> str:
    """Collect the task inside the same visual language as the main application."""
    suppress_focus_reporting()
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(5, curses.COLOR_YELLOW, -1)
    if touch_mode:
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except curses.error:
            pass
    editor = LineEditor()
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, " ◈  ROUNDTABLE ".ljust(max(1, w - 1)), max(1, w - 1),
                      curses.A_REVERSE | curses.A_BOLD)
        buttons: dict[str, tuple[int, int, int, int]] = {}
        if h >= (16 if touch_mode else 14) and w >= 50:
            stdscr.addstr(2, 3, "What should the agents solve together?", curses.A_BOLD)
            box_y, box_height, box_width = 4, 7, w - 6
            stdscr.addstr(box_y, 3, "╭" + "─" * (box_width - 2) + "╮", curses.color_pair(1))
            for row in range(box_y + 1, box_y + box_height - 1):
                stdscr.addstr(row, 3, "│", curses.color_pair(1))
                stdscr.addstr(row, w - 4, "│", curses.color_pair(1))
            stdscr.addstr(box_y + box_height - 1, 3,
                          "╰" + "─" * (box_width - 2) + "╯", curses.color_pair(1))
            lines, cursor_y, cursor_x = editor_layout(editor, w - 12, box_height - 2)
            for row, line in enumerate(lines):
                stdscr.addnstr(box_y + 1 + row, 6, line, w - 12)
            if not editor.text:
                stdscr.addnstr(box_y + 1, 6, "Describe a bug, feature, or question…", w - 12,
                              curses.A_DIM)
            if touch_mode:
                bx = 3
                for action, label, attr in [
                    ("start", "  START  ", curses.color_pair(3) | curses.A_BOLD),
                    ("newline", "  NEW LINE  ", curses.color_pair(1)),
                    ("clear", "  CLEAR  ", curses.A_DIM),
                    ("exit", "  EXIT  ", curses.color_pair(5)),
                ]:
                    stdscr.addnstr(12, bx, label, len(label), attr | curses.A_REVERSE)
                    buttons[action] = (12, bx, 12, bx + len(label) - 1)
                    bx += len(label) + 2
                stdscr.addnstr(14, 3, f"☝ Touch mode · {workspace}", w - 6, curses.A_DIM)
            else:
                stdscr.addnstr(12, 3,
                              f"↳ {workspace}  ·  Enter start  ·  Ctrl+N new line  ·  Esc exit",
                              w - 6, curses.A_DIM)
            stdscr.move(box_y + 1 + cursor_y, min(w - 6, 6 + cursor_x))
        else:
            stdscr.addnstr(2, 1, "Resize terminal to at least 50 × 14", max(1, w - 2))
        stdscr.refresh()
        key = stdscr.get_wch()
        if key == curses.KEY_MOUSE and touch_mode:
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                continue
            # See Display.handle_mouse: excluding BUTTON1_PRESSED avoids double-firing a tap once
            # for the press and again for the release of the same physical touch/click.
            tapped = state & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)
            action = next((name for name, box in buttons.items()
                           if tapped and Display._inside(box, y, x)), None)
            if action == "start" and editor.text.strip():
                return editor.text.strip()
            if action == "newline":
                editor.handle_key("\x0e")
            elif action == "clear":
                editor = LineEditor()
            elif action == "exit":
                return ""
            continue
        action = editor.handle_key(key)
        if action == "submit":
            return editor.text.strip()
        if action == "cancel":
            return ""


def read_followup_ui(stdscr: curses.window, ui: Display) -> str:
    editor = LineEditor()
    stdscr.nodelay(False)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    while True:
        ui.draw_followup(editor)
        key = stdscr.get_wch()
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                continue
            action = ui.handle_mouse(x, y, state)
            if action == "send" and editor.text.strip():
                return editor.text.strip()
            if action == "newline":
                editor.handle_key("\x0e")
            elif action == "clear":
                editor = LineEditor()
            elif action == "finish":
                return ""
            continue
        action = editor.handle_key(key)
        if action == "submit":
            return editor.text.strip()
        if action == "cancel":
            return ""


SCOPE_HINT_THRESHOLD = 1.5  # an agent must be at least this many times slower than the fastest to get scoped down


def scope_hint(name: str, agent_speed: dict[str, list[float]]) -> str:
    """Suggest a narrower scope for an agent that has been notably slower than the others so far.

    A parallel phase only finishes once its slowest agent does, so giving that agent a smaller,
    more tightly scoped ask (instead of the same full-size task everyone else gets) shortens the
    round without dropping any agent from it. Returns "" when there isn't enough data yet or the
    agent isn't meaningfully behind.
    """
    if not agent_speed or any(not durations for durations in agent_speed.values()):
        return ""
    averages = {n: sum(d) / len(d) for n, d in agent_speed.items()}
    fastest = min(averages.values())
    if fastest <= 0 or name not in averages or averages[name] < fastest * SCOPE_HINT_THRESHOLD:
        return ""
    return (
        "\n\nYou have been slower than the other agents so far this run. Keep this contribution "
        "tightly scoped to one clear, well-defined improvement rather than a full rewrite or a "
        "broad survey, so you can keep pace with the round."
    )


def _run_parallel_phase(session: Session, agents: list[tuple[str, Agent]], phase: str,
                        tick: Callable[[str, str], None],
                        status: Callable[[Iterable[str], str], None], message: str,
                        log_prompt: Callable[[str, str], None] = lambda *_: None,
                        agent_speed: dict[str, list[float]] | None = None,
                        task_status_check: bool = False, reassign_idle: bool = False) -> None:
    """Run one collaboration phase concurrently and record results deterministically.

    When agent_speed is provided, an agent running notably slower than the others (based on
    durations observed in earlier phases this run) gets a narrower-scoped prompt, and this
    phase's own durations are appended back into it for the next phase to use.

    When task_status_check is set, agents are asked to mark their turn TASK STATUS: complete once
    the objective is fully done. The first agent to do so stops the other still-running agents in
    this phase rather than letting them duplicate finished work — they get a turn again in the next
    phase (typically a review round) to check and refine it instead.

    When reassign_idle is set, an agent that finishes its turn while others are still working (and
    nobody has declared the whole task complete) gets one extra prompt asking it to pick up different
    unclaimed work or help a still-running agent, instead of sitting idle for the rest of the round.
    At most one extra attempt per agent per phase; it's cancelled if it's still going once the round
    would otherwise be over, so it can't drag a phase out past its slowest primary agent.
    """
    names = [name for name, _ in agents]
    by_name = dict(agents)
    status(names, message)
    prompts = {
        name: prompt_for(session.objective, session.turns, phase, name, sequential=False,
                         scope=scope_hint(name, agent_speed) if agent_speed is not None else "",
                         task_status_check=task_status_check)
        for name, _ in agents
    }
    for name in names:
        log_prompt(name, prompts[name])
    events: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
    cancel_event = threading.Event()
    for _, agent in agents:
        agent.cancel_event = cancel_event
    phase_start = time.monotonic()
    completed_by: str | None = None
    skipped: set[str] = set()
    bonus_futures: dict[str, concurrent.futures.Future] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents),
                                               thread_name_prefix="roundtable") as pool:
        futures = {
            name: pool.submit(agent.run, prompts[name],
                              lambda line, speaker=name: events.put((speaker, line)))
            for name, agent in agents
        }
        pending = set(names)
        try:
            while not all(future.done() for future in futures.values()):
                try:
                    speaker, line = events.get(timeout=0.1)
                    tick(speaker, line)
                except queue.Empty:
                    tick("", "")
                remaining = {name for name in names if not futures[name].done()}
                if remaining != pending:
                    for name in pending - remaining:
                        elapsed = time.monotonic() - phase_start
                        if name in skipped:
                            tick(name, f"stopped early ({elapsed:.1f}s) — {completed_by} already "
                                        f"completed the task; will review it next phase instead")
                            continue
                        if agent_speed is not None:
                            agent_speed.setdefault(name, []).append(elapsed)
                        tick(name, f"finished this phase ({elapsed:.1f}s) — waiting on "
                                    f"{', '.join(sorted(remaining)) or 'nothing else'}")
                        declared_complete = False
                        if task_status_check and completed_by is None and remaining:
                            try:
                                declared_complete = signals_task_complete(futures[name].result())
                            except Exception:
                                declared_complete = False
                            if declared_complete:
                                completed_by, skipped = name, set(remaining)
                                cancel_event.set()
                                tick(name, f"marked the task complete — skipping "
                                            f"{', '.join(sorted(skipped))} this phase")
                        if (reassign_idle and not declared_complete and completed_by is None
                                and remaining and name not in bonus_futures):
                            bonus_prompt = reassignment_prompt(session.objective, session.turns,
                                                               phase, name, remaining)
                            log_prompt(name, bonus_prompt)
                            tick(name, f"picking up extra work while "
                                        f"{', '.join(sorted(remaining))} finish")
                            bonus_futures[name] = pool.submit(
                                by_name[name].run, bonus_prompt,
                                lambda line, speaker=name: events.put((speaker, line)))
                    pending = remaining
                    status([name for name in names if name in pending], message)
        except BaseException:
            cancel_event.set()
            for future in futures.values():
                future.cancel()
            raise
        if pending:
            for name in pending:
                elapsed = time.monotonic() - phase_start
                if name in skipped:
                    tick(name, f"stopped early ({elapsed:.1f}s) — {completed_by} already completed "
                                f"the task; will review it next phase instead")
                    continue
                if agent_speed is not None:
                    agent_speed.setdefault(name, []).append(elapsed)
                tick(name, f"finished this phase ({elapsed:.1f}s)")
            status([], message)
        while True:
            try:
                tick(*events.get_nowait())
            except queue.Empty:
                break
        bonus_results: dict[str, str] = {}
        if bonus_futures:
            # The round is otherwise over: let any bonus attempt still running wind down rather than
            # dragging the phase out, then take whichever ones made it in time. Resolved here, while
            # the pool is still open, so this stays bounded instead of blocking on shutdown below.
            cancel_event.set()
            for name, future in bonus_futures.items():
                try:
                    bonus_results[name] = future.result(timeout=4.0)
                except Exception:
                    continue
        results = {name: futures[name].result() for name in names if name not in skipped}
    # A task_status_check completion sets cancel_event on every agent (line ~1404 above) so the
    # other agents in THIS phase stop — but that Event object stays attached to the Agent instances
    # after this function returns. Left alone, it silently poisons the next call that runs any of
    # these agents without passing its own cancel_event (e.g. synthesize()), which would see an
    # already-cancelled event and abort instantly. Clear it here so only an active phase can cancel.
    for _, agent in agents:
        agent.cancel_event = None
    session.turns.extend(Turn(name, phase, results[name]) for name in names if name in results)
    for name, content in bonus_results.items():
        tick(name, f"extra contribution ({len(content)} chars)")
        session.turns.append(Turn(name, f"{phase} · extra", content))
    for name in names:
        tick(name, "")


def _run_sequential_phase(session: Session, agents: list[tuple[str, Agent]], phase: str,
                          tick: Callable[[str, str], None],
                          status: Callable[[Iterable[str], str], None], message: str,
                          log_prompt: Callable[[str, str], None] = lambda *_: None) -> None:
    """Run one collaboration phase as a live relay: each agent reads and builds on the one before it.

    Unlike the parallel phase, each agent's prompt is built right before it runs, after the previous
    agent's turn has already been appended to the transcript — so it sees fresh, same-round context
    rather than only what existed before the phase started.
    """
    for name, agent in agents:
        status([name], message)
        prompt = prompt_for(session.objective, session.turns, phase, name, sequential=True)
        log_prompt(name, prompt)
        content = agent.run(prompt, lambda line, speaker=name: tick(speaker, line))
        session.turns.append(Turn(name, phase, content))
        tick(name, "")
    status([], message)


def _phase_runner(collab: str, round_no: int) -> Callable[..., None]:
    """Pick a phase strategy: 'mixed' alternates relay and independent rounds."""
    if collab == "sequential":
        return _run_sequential_phase
    if collab == "mixed":
        return _run_sequential_phase if round_no % 2 == 1 else _run_parallel_phase
    return _run_parallel_phase


def pick_synthesizer(choice: str, session: Session, codex: Agent, claude: Agent,
                     antigravity: Agent) -> tuple[str, Agent]:
    """Choose who writes the final answer.

    'rotate' spreads the role across agents by objective instead of always favoring one model,
    so the same session stays consistent across follow-ups while different sessions vary.
    """
    options = [("Codex", codex), ("Claude", claude), ("Antigravity", antigravity)]
    by_name = {"codex": 0, "claude": 1, "antigravity": 2}
    if choice in by_name:
        return options[by_name[choice]]
    index = int(hashlib.sha256(session.objective.encode()).hexdigest(), 16) % len(options)
    return options[index]


def synthesis_order(choice: str, session: Session, codex: Agent, claude: Agent,
                    antigravity: Agent) -> list[tuple[str, Agent]]:
    """Full relay order for the final answer: who drafts it, then who refines it, in turn.

    Reuses pick_synthesizer for the drafting agent (so --synthesizer keeps its meaning), then
    rotates the remaining two by a second objective-derived hash, so the refining order also varies
    across sessions instead of always following the same Codex/Claude/Antigravity order.
    """
    options = [("Codex", codex), ("Claude", claude), ("Antigravity", antigravity)]
    first_name, first_agent = pick_synthesizer(choice, session, codex, claude, antigravity)
    rest = [pair for pair in options if pair[0] != first_name]
    index = int(hashlib.sha256((session.objective + first_name).encode()).hexdigest(), 16) % len(rest)
    rest = rest[index:] + rest[:index]
    return [(first_name, first_agent)] + rest


def synthesize(session: Session, order: list[tuple[str, Agent]],
               tick: Callable[[str, str], None], status: Callable[[Iterable[str], str], None],
               log_prompt: Callable[[str, str], None] = lambda *_: None,
               followup: bool = False) -> str:
    """Produce the final answer as a relay: one agent drafts it, the other two refine it in turn,
    so the result is a merge shaped by all three rather than the output of a single agent."""
    draft = ""
    for index, (name, agent) in enumerate(order):
        verb = "drafting" if index == 0 else "refining"
        status([name], f"{name} is {verb} the final answer")
        prompt = (final_prompt(session.objective, session.turns, followup) if index == 0 else
                 refine_prompt(session.objective, session.turns, draft, followup))
        log_prompt(name, prompt)
        # Pass a fresh Event explicitly rather than relying on agent.cancel_event: a prior phase's
        # task_status_check cancellation could otherwise leave that attribute already set, which
        # would abort this agent's turn before it starts.
        draft = agent.run(prompt, lambda line, speaker=name: tick(speaker, line), threading.Event())
    status([], "Final answer complete")
    return draft


PREFLIGHT_PROMPT = ("This is a startup connectivity check, not the real task. Reply with exactly "
                    "the single word OK and do not read, write, or execute anything.")


def preflight_check(name: str, agent: Agent, tick: Callable[[str, str], None],
                    cancel_event: threading.Event, timeout: float) -> tuple[bool, str]:
    """Confirm one agent's CLI is authenticated and responsive within a bounded timeout."""
    agent.cancel_event = cancel_event
    timer = threading.Timer(timeout, cancel_event.set)
    timer.daemon = True
    timer.start()
    try:
        agent.run(PREFLIGHT_PROMPT, lambda line: tick(name, line))
        return True, "ready"
    except Exception as exc:
        if cancel_event.is_set():
            return False, f"timed out after {timeout:.0f}s"
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else "no response"
        return False, message
    finally:
        timer.cancel()


def run_preflight(agents: list[tuple[str, Agent]], tick: Callable[[str, str], None],
                  status: Callable[[Iterable[str], str], None], timeout: float = 25.0) -> None:
    """Check every agent CLI is reachable before committing to the real task.

    Without this, a hung or unauthenticated CLI leaves every panel stuck on
    'waiting for task' with no explanation. This fails fast with a clear reason instead.
    """
    names = [name for name, _ in agents]
    status(names, "Running a preliminary system check")
    cancel_events = {name: threading.Event() for name in names}
    events: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents),
                                               thread_name_prefix="preflight") as pool:
        futures = {
            name: pool.submit(preflight_check, name, agent,
                              lambda speaker, line: events.put((speaker, line)),
                              cancel_events[name], timeout)
            for name, agent in agents
        }
        pending = set(names)
        try:
            while not all(future.done() for future in futures.values()):
                try:
                    speaker, line = events.get(timeout=0.1)
                    tick(speaker, line)
                except queue.Empty:
                    tick("", "")
                remaining = {name for name in names if not futures[name].done()}
                if remaining != pending:
                    pending = remaining
                    status([name for name in names if name in pending],
                          "Running a preliminary system check")
        except BaseException:
            for cancel_event in cancel_events.values():
                cancel_event.set()
            for future in futures.values():
                future.cancel()
            raise
        results = {name: futures[name].result() for name in names}
    status([], "System check complete")
    for name, (ok, detail) in results.items():
        tick(name, "check passed" if ok else f"check failed: {detail}")
    failed = [f"{name} ({detail})" for name, (ok, detail) in results.items() if not ok]
    if failed:
        raise RuntimeError("Preliminary system check failed — " + "; ".join(failed))


def _run_phase(runner: Callable[..., None], session: Session, agents: list[tuple[str, Agent]],
              phase: str, tick: Callable[[str, str], None],
              status: Callable[[Iterable[str], str], None], message: str,
              log_prompt: Callable[[str, str], None],
              agent_speed: dict[str, list[float]] | None,
              task_status_check: bool = False, reassign_idle: bool = False) -> None:
    """Dispatch to a phase runner, passing agent_speed/task_status_check/reassign_idle only to the
    parallel runner that uses them."""
    if runner is _run_parallel_phase:
        runner(session, agents, phase, tick, status, message, log_prompt, agent_speed,
              task_status_check, reassign_idle)
    else:
        runner(session, agents, phase, tick, status, message, log_prompt)


def conduct(session: Session, codex: Agent, claude: Agent, antigravity: Agent,
            tick: Callable[[str, str], None],
            status: Callable[[Iterable[str], str], None],
            followup: bool = False, collab: str = "parallel", synthesizer: str = "rotate",
            log_prompt: Callable[[str, str], None] = lambda *_: None,
            balance_load: bool = False, task_status_check: bool = False,
            reassign_idle: bool = False) -> None:
    agents = [("Codex", codex), ("Claude", claude), ("Antigravity", antigravity)]
    agent_speed: dict[str, list[float]] | None = {} if balance_load else None
    phase = "followup-proposal" if followup else "proposal"
    proposal_runner = _run_sequential_phase if collab == "sequential" else _run_parallel_phase
    style = "in sequence" if proposal_runner is _run_sequential_phase else "in parallel"
    message = (f"Agents are addressing the follow-up {style}" if followup else
               f"Agents are developing solutions {style}")
    _run_phase(proposal_runner, session, agents, phase, tick, status, message, log_prompt,
              agent_speed, task_status_check, reassign_idle)
    for round_no in range(1, session.rounds + 1):
        phase = f"followup-review {round_no}" if followup else f"review {round_no}"
        runner = _phase_runner(collab, round_no)
        round_style = "in sequence" if runner is _run_sequential_phase else "in parallel"
        _run_phase(
            runner, session, agents, phase, tick, status,
            f"Agents are reviewing {round_style} · round {round_no}/{session.rounds}",
            log_prompt, agent_speed, task_status_check, reassign_idle,
        )
    order = synthesis_order(synthesizer, session, codex, claude, antigravity)
    session.final = synthesize(session, order, tick, status, log_prompt, followup)
    session.turns.append(Turn("Final", "consensus", session.final))


def run_tui(stdscr: curses.window, args: argparse.Namespace, session: Session,
            codex: Agent, claude: Agent, antigravity: Agent,
            resumed: bool = False) -> int:
    suppress_focus_reporting()
    run_log = RunLog(log_path_for(session, Path(args.output_dir)))
    ui = Display(stdscr, session, args.touch_mode, run_log)
    def status(active: Iterable[str], message: str) -> None:
        ui.update_status(active, message)
        ui.draw()
    try:
        ui.busy = True
        stdscr.nodelay(True)
        if not args.skip_preflight:
            run_preflight([("Codex", codex), ("Claude", claude), ("Antigravity", antigravity)],
                          ui.tick, status, timeout=args.preflight_timeout)
        ui.busy = False
        ui.status = "Ready"
        followup = resumed
        if resumed and not session.turns[-1:]:
            raise ValueError("a resumed session has no transcript")
        if resumed and session.turns[-1].speaker != "User":
            request = read_followup_ui(stdscr, ui)
            if not request:
                return 0
            session.turns.append(Turn("User", "follow-up", request))
        while True:
            ui.busy = True
            stdscr.nodelay(True)
            conduct(session, codex, claude, antigravity, ui.tick, status, followup,
                   collab=args.collab, synthesizer=args.synthesizer, log_prompt=ui.log_prompt,
                   balance_load=args.balance_load, task_status_check=args.task_status_check,
                   reassign_idle=args.reassign_idle)
            ui.busy = False
            paths = save_session(session, Path(args.output_dir))
            ui.status = "Complete"
            ui.activity = {}
            request = read_followup_ui(stdscr, ui)
            if not request:
                break
            session.turns.append(Turn("User", "follow-up", request))
            ui.status = "Continuing"
            followup = True
        return 0
    except KeyboardInterrupt:
        ui.busy = False
        ui.status, ui.activity = "Cancelled", {}
        ui.log("Cancelled by user", kind="error")
        if session.turns:
            save_session(session, Path(args.output_dir))
        ui.draw()
        time.sleep(0.35)
        return 130
    except Exception as exc:
        ui.busy = False
        ui.status = "Could not complete the roundtable"
        ui.error = textwrap.shorten(str(exc).replace("\n", " · "), width=240, placeholder="…")
        ui.log(f"ERROR: {exc}", kind="error")
        if session.turns:
            try:
                save_session(session, Path(args.output_dir))
            except OSError:
                pass
        ui.draw()
        stdscr.nodelay(False)
        stdscr.getch()
        return 1
    finally:
        run_log.close()


def verify_clis(mock: bool) -> None:
    if mock:
        return
    missing = [name for name in ("codex", "claude", "agy") if not shutil.which(name)]
    if missing:
        raise SystemExit(f"Missing required CLI(s): {', '.join(missing)}")


def positive_finite_float(value: str) -> float:
    """Argparse type for positive, finite timeout values."""
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roundtable",
                                     description="A shared terminal roundtable for Codex, Claude, and Antigravity")
    parser.add_argument("objective", nargs="?", help="the problem the agents should solve")
    parser.add_argument("-r", "--rounds", type=int, choices=range(0, 6), metavar="0-5")
    parser.add_argument("-C", "--workspace", help="shared working directory")
    parser.add_argument("--self", action="store_true",
                        help="point the workspace at roundtable's own source so the agents can "
                             "improve roundtable itself (overridden by -C); adds a standing note to "
                             "read the existing code/tests, stay dependency-free, and run the test "
                             "suite before finishing")
    parser.add_argument("--codex-model")
    parser.add_argument("--claude-model")
    parser.add_argument("--antigravity-model")
    parser.add_argument("--collab", choices=["parallel", "sequential", "mixed"], default="parallel",
                        help="how agents coordinate: independent parallel turns (default), a "
                             "strict Codex-Claude-Antigravity relay, or a mix that alternates "
                             "relay and parallel review rounds")
    parser.add_argument("--synthesizer", choices=["codex", "claude", "antigravity", "rotate"],
                        default="rotate",
                        help="who drafts the final answer first, before the other two refine it in "
                             "turn (default: rotate by objective so no one model always drafts)")
    parser.add_argument("--balance-load", action="store_true",
                        help="in parallel phases, give an agent running notably slower than the "
                             "others a narrower-scoped prompt instead of the same full task, so "
                             "one slow CLI blocks the round less")
    parser.add_argument("--task-status-check", action="store_true",
                        help="in parallel phases, ask each agent to mark its turn TASK STATUS: "
                             "complete once the objective is fully done; the first agent to do so "
                             "stops the others from redoing the same finished work, leaving them "
                             "to review and refine it in the next phase instead")
    parser.add_argument("--reassign-idle", action="store_true",
                        help="in parallel phases, an agent that finishes while others are still "
                             "working gets one extra prompt to pick up different unclaimed work or "
                             "help a still-running agent, instead of sitting idle for the round")
    parser.add_argument("--elevated", choices=["codex", "claude", "antigravity", "all"],
                        action="append", default=[], metavar="AGENT",
                        help="run the named agent (repeatable, or 'all') with its CLI's own "
                             "permission-bypass flag instead of the sandboxed default, so tool "
                             "calls it would otherwise need to prompt for (e.g. running a shell "
                             "command) are auto-approved instead of silently soft-denied. "
                             "DANGEROUS: that agent can then run arbitrary commands in your "
                             "workspace unsandboxed and unconfirmed — only use this for a workspace "
                             "you trust the agents in.")
    parser.add_argument("--output-dir", help="transcript directory")
    parser.add_argument("--resume", type=Path, metavar="SESSION.json",
                        help="resume a saved JSON session; objective becomes a follow-up")
    parser.add_argument("--plain", action="store_true", help="disable fullscreen UI")
    parser.add_argument("--touch", action=argparse.BooleanOptionalAction, default=None,
                        help="enable or disable touchscreen controls (auto-detected by default)")
    parser.add_argument("--preflight-timeout", type=positive_finite_float, default=25.0,
                        help="timeout in seconds for each agent's preflight connectivity check "
                             "(default: 25.0)")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="skip the preliminary system check entirely")
    parser.add_argument("--mock", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.touch_mode = has_touchscreen() if args.touch is None else args.touch
    if not args.plain and sys.stdin.isatty() and sys.stdout.isatty():
        started_elevated = bool(args.elevated)
        toggled = curses.wrapper(read_options_ui, {
            "elevated": started_elevated,
            "balance_load": args.balance_load,
            "task_status_check": args.task_status_check,
            "self": args.self,
            "skip_preflight": args.skip_preflight,
            "reassign_idle": args.reassign_idle,
        })
        if toggled["elevated"] != started_elevated:
            # Only overwrite a specific --elevated CODEX/CLAUDE/... choice if the toggle actually
            # changed; otherwise leave whatever CLI flags already set untouched.
            args.elevated = ["all"] if toggled["elevated"] else []
        args.balance_load = toggled["balance_load"]
        args.task_status_check = toggled["task_status_check"]
        args.self = toggled["self"]
        args.skip_preflight = toggled["skip_preflight"]
        args.reassign_idle = toggled["reassign_idle"]
    verify_clis(args.mock)
    self_dir = Path(__file__).resolve().parent
    resumed = args.resume is not None
    if resumed:
        resume_path = args.resume.expanduser().resolve()
        try:
            session = load_session(resume_path)
        except ValueError as exc:
            parser.error(str(exc))
        workspace = Path(args.workspace or (self_dir if args.self else session.workspace)
                         ).expanduser().resolve()
        session.workspace = str(workspace)
        if args.rounds is not None:
            session.rounds = args.rounds
        args.output_dir = args.output_dir or str(resume_path.parent)
    else:
        workspace = Path(args.workspace or (self_dir if args.self else os.getcwd())
                         ).expanduser().resolve()
        args.output_dir = args.output_dir or ".roundtable"
    if not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    request = args.objective
    if not request and not (resumed and sys.stdin.isatty()):
        if not sys.stdin.isatty():
            request = sys.stdin.read().strip()
        else:
            request = curses.wrapper(read_objective_ui, workspace, args.touch_mode)
    if resumed:
        if request:
            session.turns.append(Turn("User", "follow-up", request))
        elif not sys.stdin.isatty():
            parser.error("a follow-up is required when resuming in non-interactive mode")
        elif args.plain:
            parser.error("--resume with --plain requires follow-up text as an argument or piped stdin")
    else:
        if not request:
            parser.error("an objective is required")
        if args.self:
            # Suffixed, not prefixed: the header shows a truncated objective, and the real request
            # should survive that truncation rather than the boilerplate note leading it.
            request = f"{request}\n\n{SELF_EDIT_NOTE}"
        session = Session(request, str(workspace), args.rounds if args.rounds is not None else 1,
                          datetime.now(timezone.utc).isoformat(), [])
    cls = MockAgent if args.mock else Agent
    elevated_all = "all" in args.elevated
    elevated = {
        "codex": elevated_all or "codex" in args.elevated,
        "claude": elevated_all or "claude" in args.elevated,
        "antigravity": elevated_all or "antigravity" in args.elevated,
    }
    codex = cls("Codex", workspace, args.codex_model, elevated=elevated["codex"])
    claude = cls("Claude", workspace, args.claude_model, elevated=elevated["claude"])
    antigravity = cls("Antigravity", workspace, args.antigravity_model,
                      elevated=elevated["antigravity"])

    if args.plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
        run_log = RunLog(log_path_for(session, Path(args.output_dir)))
        def tick(name: str, line: str) -> None:
            if line:
                run_log.write("tick", f"[{name}] {line}")
                if args.plain:
                    print(f"  [{name}] {line}", flush=True)
        def status(names: Iterable[str], message: str) -> None:
            label = ", ".join(names) or "complete"
            run_log.write("phase", f"[{label}] {message}")
            print(f"[{label}] {message}", flush=True)
        def log_prompt(name: str, prompt: str) -> None:
            run_log.write("prompt", f"[{name}] PROMPT:\n{prompt}")
        try:
            if not args.skip_preflight:
                run_preflight([("Codex", codex), ("Claude", claude), ("Antigravity", antigravity)],
                              tick, status, timeout=args.preflight_timeout)
            conduct(session, codex, claude, antigravity, tick, status, followup=resumed,
                   collab=args.collab, synthesizer=args.synthesizer, log_prompt=log_prompt,
                   balance_load=args.balance_load, task_status_check=args.task_status_check,
                   reassign_idle=args.reassign_idle)
        except KeyboardInterrupt:
            run_log.write("error", "Cancelled by user")
            if session.turns:
                save_session(session, Path(args.output_dir))
            print("\nCancelled.", file=sys.stderr)
            return 130
        except Exception as exc:
            run_log.write("error", str(exc))
            if session.turns:
                try:
                    save_session(session, Path(args.output_dir))
                except OSError:
                    pass
            print(f"\nRoundtable could not complete: {exc}", file=sys.stderr)
            return 1
        finally:
            run_log.close()
        _, md_path = save_session(session, Path(args.output_dir))
        print(f"\n{session.final}\n\nTranscript: {md_path}")
    else:
        return curses.wrapper(run_tui, args, session, codex, claude, antigravity, resumed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
