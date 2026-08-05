import contextlib
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import roundtable


class FailingAgent(roundtable.Agent):
    def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
        raise RuntimeError("boom: simulated failure")


class RestartVotingAgent(roundtable.Agent):
    """Casts a fixed RESTART: now/later vote only when its prompt actually asks for one, and
    records every prompt it receives so a test can inspect which phases got the vote hint."""

    def __init__(self, name, workspace, vote):
        super().__init__(name, workspace)
        self.vote = vote
        self.received_prompts = []

    def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
        self.received_prompts.append(prompt)
        content = f"{self.name} did some work."
        if "RESTART:" in prompt:
            content += f"\n\nRESTART: {self.vote} — because reasons"
        return content


class FakeScreen:
    """Minimal curses-window stand-in that records what was drawn as a text grid."""

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.grid = [[" "] * w for _ in range(h)]

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.grid = [[" "] * self.w for _ in range(self.h)]

    def addnstr(self, y, x, text, n, attr=0):
        for i, ch in enumerate(text[:n]):
            if 0 <= y < self.h and 0 <= x + i < self.w:
                self.grid[y][x + i] = ch

    def addstr(self, y, x, text, attr=0):
        self.addnstr(y, x, text, len(text), attr)

    def refresh(self):
        pass

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.grid)

    def nodelay(self, flag):
        pass

    def resize(self, h, w):
        self.h, self.w = h, w
        self.grid = [[" "] * w for _ in range(h)]


def make_test_display(h=48, w=160, turns=None):
    """A Display wired up enough to call draw()/handle_mouse()/poll_input(), bypassing __init__."""
    display = roundtable.Display.__new__(roundtable.Display)
    display.s = FakeScreen(h, w)
    display.session = roundtable.Session("Goal", "/tmp", 0, "now", turns or [])
    display.status = "Ready"
    display.activity = {}
    display.active = set()
    display.phase_completed = set()
    display.phase_failed = set()
    display.busy = False
    display.error = ""
    display.frame = 0
    display.monitor = mock.Mock(changes=[], truncated=False)
    display.started = time.monotonic()
    display.touch_mode = False
    display.hitboxes = {}
    display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Aider": 0, "Grok": 0, "Qwen": 0,
                      "Final": 0, "Console": 0, "Code": 0}
    display.usage_names = ("Codex", "Claude", "Antigravity", "Aider", "Grok", "Qwen")
    display.turn_times = {name: [] for name in display.usage_names}
    display.turn_outputs = {name: [] for name in display.usage_names}
    display.activity_pulses = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
    display.work_activity = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
    display.work_reads = {name: 0 for name in display.usage_names}
    display.work_execs = {name: 0 for name in display.usage_names}
    display.work_writes = {name: 0 for name in display.usage_names}
    display.usage_percent = {}
    display.retry_state = {}
    display.turn_start = {}
    display._known_turn_count = len(display.session.turns)
    display.console = roundtable.deque(maxlen=300)
    display.run_log = roundtable.RunLog(None)
    display.expanded = None
    display.focused_panel = None
    display.show_help = False
    display.console_filter = 0
    return display


class RoundtableTests(unittest.TestCase):
    def setUp(self):
        # Real runs stagger agent spawns (see AGENT_SPAWN_STAGGER_SECONDS) to avoid a startup
        # resource-contention spike when launching several CLI subprocesses at once. Tests want the
        # old instant-concurrent behavior instead -- both so short mock-agent sleeps still overlap
        # the way concurrency assertions expect, and so the suite doesn't accumulate real delay
        # across dozens of parallel-phase/preflight tests. The staggering mechanism itself still
        # gets exercised directly, with an explicit override, in its own dedicated test.
        patcher = mock.patch.object(roundtable, "AGENT_SPAWN_STAGGER_SECONDS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        # --self verification caches suite results by source fingerprint; isolate each test so a
        # prior case's PASS/FAIL cannot leak into a later assertion about subprocess calls.
        roundtable.clear_self_verification_cache()

    def test_work_event_strips_terminal_codes_and_labels_common_operations(self):
        self.assertEqual(roundtable.work_event("\x1b[32mReading app.py\x1b[0m"),
                         "⌕ Reading app.py")
        self.assertEqual(roundtable.work_event("executing python3 -m unittest"),
                         "▶ executing python3 -m unittest")
        self.assertEqual(roundtable.work_event("Applying patch"), "✎ Applying patch")
        # Tool-ish names get the right glyph even without a prose verb phrase.
        self.assertEqual(roundtable.work_event("view_file('x.py')"), "⌕ view_file('x.py')")
        self.assertEqual(roundtable.work_event("run_command: pytest"), "▶ run_command: pytest")
        self.assertEqual(roundtable.work_event("wrote file: README.md"),
                         "✎ wrote file: README.md")

    def test_work_event_ignores_prose_false_positives(self):
        """Bare English words must not paint the live work feed as tool activity.

        An earlier broad glyph matcher tagged 'open source', 'list of findings', and
        '… is running' as read/exec lines; keep ordinary narration as the neutral · glyph.
        """
        for line in (
            "I will check the review and do the run next",
            "planning to implement the change and apply the patch",
            "The latest contest is running",
            "list of findings",
            "open source library",
            "looking at how to perform the call",
        ):
            self.assertTrue(
                roundtable.work_event(line).startswith("· "),
                msg=f"expected neutral glyph for {line!r}, got {roundtable.work_event(line)!r}",
            )

    def test_active_agent_boxes_show_their_own_work_feeds(self):
        display = make_test_display(w=240)
        display.busy = True
        display.active = {"Codex", "Claude"}
        display.work_activity["Codex"].extend([
            "⌕ Reading roundtable.py", "▶ python3 -m unittest test_roundtable"])
        display.work_activity["Claude"].append("✎ Editing README.md")
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
        rendered = display.s.text()
        self.assertEqual(rendered.count("LIVE WORK"), 2)
        self.assertIn("Reading roundtable.py", rendered)
        self.assertIn("python3 -m unittest", rendered)
        self.assertIn("Editing README.md", rendered)

    def test_tick_keeps_work_history_separate_and_deduplicated(self):
        display = make_test_display()
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None
        display.tick("Codex", "Reading one.py")
        display.tick("Codex", "Reading one.py")
        display.tick("Claude", "Executing tests")
        self.assertEqual(list(display.work_activity["Codex"]), ["⌕ Reading one.py"])
        self.assertEqual(list(display.work_activity["Claude"]), ["▶ Executing tests"])

    def test_tick_logs_turn_duration_and_generation_rate(self):
        display = make_test_display()
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None
        display.turn_start["Codex"] = roundtable.time.monotonic() - 2.0
        display.session.turns.append(roundtable.Turn("Codex", "discussion", "Sample response content text."))
        display.tick("Codex", "done")
        last_log = display.console[-1]
        self.assertEqual(last_log[0], "turn")
        self.assertIn("Codex · discussion · 2.0s · ", last_log[1])
        self.assertIn("c/s)", last_log[1])

    def test_agent_panel_shows_active_elapsed_duration(self):
        display = make_test_display(w=120)
        display.busy = True
        display.active = {"Codex"}
        with mock.patch.object(roundtable.time, "monotonic", return_value=100.0):
            display.turn_start["Codex"] = 94.5
            with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
                 mock.patch.object(roundtable.curses, "has_colors", return_value=False):
                display.draw()
        rendered = display.s.text()
        self.assertIn("● working (5.5s)", rendered)

    def test_line_editor(self):
        editor = roundtable.LineEditor()
        self.assertIsNone(editor.handle_key("é"))
        self.assertEqual(editor.text, "é")
        editor.handle_key("\x7f")
        self.assertEqual(editor.text, "")
        self.assertIsNone(editor.handle_key("\n"))
        for char in "request":
            editor.handle_key(char)
        self.assertEqual(editor.handle_key("\n"), "submit")
        self.assertEqual(roundtable.LineEditor().handle_key("\x1b"), "cancel")

    def test_line_editor_readline_shortcuts_and_multiline_navigation(self):
        editor = roundtable.LineEditor("hello world")
        # Ctrl-A (home)
        editor.handle_key("\x01")
        self.assertEqual(editor.cursor, 0)
        # Ctrl-E (end)
        editor.handle_key("\x05")
        self.assertEqual(editor.cursor, 11)
        # Ctrl-W (delete word backward)
        editor.handle_key("\x17")
        self.assertEqual(editor.text, "hello ")
        # Ctrl-U (clear line before cursor)
        editor.handle_key("\x15")
        self.assertEqual(editor.text, "")
        # Tab (insert 2 spaces)
        editor.handle_key("\t")
        self.assertEqual(editor.text, "  ")
        # Multiline string paste
        editor.handle_key("first line\nsecond line")
        self.assertEqual(editor.text, "  first line\nsecond line")
        # Ctrl-K (clear text after cursor)
        editor.handle_key("\x01")  # go home
        editor.handle_key(roundtable.curses.KEY_RIGHT)  # cursor at index 1
        editor.handle_key("\x0b")  # clear after
        self.assertEqual(editor.text, " ")
        # Up and Down navigation
        m_editor = roundtable.LineEditor("line 1\nline 2")
        m_editor.handle_key(roundtable.curses.KEY_UP)
        self.assertEqual(m_editor.cursor, 6)  # at end of line 1
        m_editor.handle_key(roundtable.curses.KEY_DOWN)
        self.assertEqual(m_editor.cursor, 13)  # at end of line 2

    def test_workspace_monitor_tracks_file_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            existing = root / "app.py"
            existing.write_text("one")
            monitor = roundtable.WorkspaceMonitor(root)
            existing.write_text("changed and longer")
            (root / "new.py").write_text("new")
            changes = {(item.kind, item.path) for item in monitor.refresh(force=True)}
            self.assertIn(("modified", "app.py"), changes)
            self.assertIn(("added", "new.py"), changes)
            existing.unlink()
            changes = {(item.kind, item.path) for item in monitor.refresh(force=True)}
            self.assertIn(("deleted", "app.py"), changes)

    def test_workspace_monitor_skips_ignored_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text("ref: refs/heads/main")
            pycache_dir = root / "__pycache__"
            pycache_dir.mkdir()
            (pycache_dir / "module.pyc").write_text("binary")
            valid_file = root / "src" / "main.py"
            valid_file.parent.mkdir(parents=True)
            valid_file.write_text("print('hello')")

            monitor = roundtable.WorkspaceMonitor(root)
            snapshot = monitor.baseline
            self.assertIn(str(Path("src") / "main.py"), snapshot)
            self.assertNotIn(str(Path(".git") / "HEAD"), snapshot)
            self.assertNotIn(str(Path("__pycache__") / "module.pyc"), snapshot)

    def test_workspace_monitor_large_file_handling(self):
        """Test that the monitor handles large files correctly without performance issues."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            monitor = roundtable.WorkspaceMonitor(root, max_files=5)  # Small limit for testing

            # Create more files than the limit
            for i in range(10):
                (root / f"file_{i}.txt").write_text(f"Content {i}")

            # The monitor should handle this gracefully
            changes = monitor.refresh(force=True)
            # Should be truncated due to file limit
            self.assertTrue(monitor.truncated)

    def test_touchscreen_detection_matches_yoga_digitizer(self):
        devices = 'N: Name="Atmel Atmel maXTouch Digitizer"\nH: Handlers=mouse1 event5\n'
        self.assertTrue(roundtable.has_touchscreen(devices))
        self.assertFalse(roundtable.has_touchscreen('N: Name="Synaptics TouchPad"\n'))

    def test_click_on_agent_panel_toggles_expanded_and_click_again_collapses(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.hitboxes = {"codex": (2, 2, 8, 30)}
        display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0}
        display.expanded = None
        draws = []
        display.draw = lambda: draws.append(display.expanded)

        display.handle_mouse(5, 5, roundtable.curses.BUTTON1_CLICKED)
        self.assertEqual(display.expanded, "Codex")
        display.handle_mouse(5, 5, roundtable.curses.BUTTON1_CLICKED)
        self.assertIsNone(display.expanded)
        self.assertEqual(draws, ["Codex", None])

    def test_press_and_release_of_one_click_only_toggles_expanded_once(self):
        """Regression: with mouse-position reporting on, terminals commonly send a PRESSED event and
        a separate RELEASED event for one physical click. Treating both as a tap toggled expand on
        (press) then immediately back off (release) — looked like clicking only worked while held."""
        display = roundtable.Display.__new__(roundtable.Display)
        display.hitboxes = {"codex": (2, 2, 8, 30)}
        display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0}
        display.expanded = None
        display.draw = lambda: None

        display.handle_mouse(5, 5, roundtable.curses.BUTTON1_PRESSED)
        self.assertIsNone(display.expanded)  # press alone must not toggle
        display.handle_mouse(5, 5, roundtable.curses.BUTTON1_RELEASED)
        self.assertEqual(display.expanded, "Codex")  # release completes exactly one toggle

    def test_suppress_focus_reporting_writes_the_disable_sequence(self):
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdout", buffer):
            roundtable.suppress_focus_reporting()
        self.assertEqual(buffer.getvalue(), "\x1b[?1004l")

    def test_suppress_focus_reporting_swallows_a_broken_stdout(self):
        class BrokenStdout:
            def write(self, _):
                raise OSError("broken pipe")

        with mock.patch.object(sys, "stdout", BrokenStdout()):
            roundtable.suppress_focus_reporting()  # must not raise

    def test_curses_entry_points_suppress_focus_reporting_before_reading_input(self):
        """Regression: terminals that report focus-in/out send ESC [ I / ESC [ O on every alt-tab.
        Every text box here treats a bare Escape as cancel, so without disabling that reporting mode,
        switching focus away and back could look like the user hit Escape and silently end the run."""
        import linecache
        import importlib
        importlib.reload(roundtable)
        linecache.clearcache()
        for name in ("read_options_ui", "read_objective_ui", "run_tui"):
            with self.subTest(entry_point=name):
                source = inspect.getsource(getattr(roundtable, name))
                self.assertIn("suppress_focus_reporting()", source)

    def test_expand_keys_toggle_and_collapse_key_clears(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.expanded = None
        display.draw = lambda: None

        display.s = mock.Mock()
        display.s.getch = mock.Mock(side_effect=[ord("2"), -1])
        display.poll_input()
        self.assertEqual(display.expanded, "Claude")

        display.s.getch = mock.Mock(side_effect=[27, -1])
        display.poll_input()
        self.assertIsNone(display.expanded)

    def test_expand_key_pressed_twice_collapses(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.expanded = None
        display.draw = lambda: None
        display.s = mock.Mock()
        display.s.getch = mock.Mock(side_effect=[ord("1"), -1])
        display.poll_input()
        self.assertEqual(display.expanded, "Codex")
        display.s.getch = mock.Mock(side_effect=[ord("1"), -1])
        display.poll_input()
        self.assertIsNone(display.expanded)

    def test_expanded_panel_keyboard_scrolling_and_bounds(self):
        display = make_test_display(h=30)
        display.expanded = "Console"
        display.console.extend(("phase", f"event {i}") for i in range(80))
        display.s.getch = mock.Mock(side_effect=[
            roundtable.curses.KEY_UP,
            roundtable.curses.KEY_PPAGE,
            roundtable.curses.KEY_DOWN,
            roundtable.curses.KEY_HOME,
            roundtable.curses.KEY_END,
            -1,
        ])
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.poll_input()
        self.assertEqual(display.scroll["Console"], 0)

        display.s.getch = mock.Mock(side_effect=[roundtable.curses.KEY_HOME, -1])
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.poll_input()
        self.assertEqual(display.scroll["Console"], 60)
        self.assertIn("↑60", display.s.text())
        self.assertIn("event 0", display.s.text())

    def test_scroll_keys_do_nothing_without_an_expanded_panel(self):
        display = make_test_display()
        display.s.getch = mock.Mock(side_effect=[roundtable.curses.KEY_UP, -1])
        with mock.patch.object(display, "draw") as draw:
            display.poll_input()
        self.assertEqual(display.scroll["Console"], 0)
        draw.assert_not_called()

    def test_draw_expanded_reuses_agent_panel_and_shows_full_content(self):
        class Screen:
            def __init__(self, h, w):
                self.h, self.w = h, w
                self.grid = [[" "] * w for _ in range(h)]

            def getmaxyx(self):
                return self.h, self.w

            def erase(self):
                self.grid = [[" "] * self.w for _ in range(self.h)]

            def addnstr(self, y, x, text, n, attr=0):
                for i, ch in enumerate(text[:n]):
                    if 0 <= y < self.h and 0 <= x + i < self.w:
                        self.grid[y][x + i] = ch

            def refresh(self):
                pass

            def text(self):
                return "\n".join("".join(row).rstrip() for row in self.grid)

        display = roundtable.Display.__new__(roundtable.Display)
        display.s = Screen(40, 140)
        long_answer = "Codex says: " + ("word " * 400)
        display.session = roundtable.Session("Goal", "/tmp", 0, "now",
                                             [roundtable.Turn("Codex", "proposal", long_answer)])
        display.status = "Ready"
        display.activity = {}
        display.active = set()
        display.phase_completed = set()
        display.busy = False
        display.error = ""
        display.frame = 0
        display.monitor = mock.Mock(changes=[], truncated=False)
        display.started = time.monotonic()
        display.touch_mode = False
        display.hitboxes = {}
        display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0}
        display.usage_names = ("Codex", "Claude", "Antigravity")
        display.turn_times = {name: [] for name in display.usage_names}
        display.turn_outputs = {name: [] for name in display.usage_names}
        display.activity_pulses = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
        display.turn_start = {}
        display.console = roundtable.deque(maxlen=300)
        display.run_log = roundtable.RunLog(None)
        display.expanded = "Codex"

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()

        rendered = display.s.text()
        self.assertIn("CODEX", rendered)
        self.assertIn("collapses", rendered)
        # A far wider wrap than the normal ~1/3-width column fits many more "word"s per line.
        self.assertIn("word " * 15, rendered)

    def test_cycle_console_filter_wraps_through_all_filters(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.console_filter = 0
        display.draw = lambda: None
        seen = []
        for _ in range(len(roundtable.CONSOLE_FILTERS) + 1):
            seen.append(roundtable.CONSOLE_FILTERS[display.console_filter][0])
            display.cycle_console_filter()
        self.assertEqual(seen[0], "key events")
        self.assertEqual(seen, seen[:len(roundtable.CONSOLE_FILTERS)] + [seen[0]])

    def test_filtered_console_matches_the_active_filter(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.console = roundtable.deque([
            ("phase", "phase line"), ("tick", "tick line"), ("prompt", "prompt line"),
            ("retry", "retry line"), ("turn", "turn line"), ("error", "error line"),
        ])
        display.console_filter = 0  # key events: phase, retry, turn, error — no raw ticks
        label, entries = display._filtered_console()
        self.assertEqual(label, "key events")
        self.assertEqual({kind for kind, _ in entries}, {"phase", "retry", "turn", "error"})

        display.console_filter = 1  # all activity
        _, entries = display._filtered_console()
        self.assertEqual(len(entries), 6)

        display.console_filter = 2  # prompts only
        _, entries = display._filtered_console()
        self.assertEqual([text for _, text in entries], ["prompt line"])

    def test_click_on_console_toggles_expanded(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.hitboxes = {"console": (2, 2, 8, 30)}
        display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0, "Console": 0}
        display.expanded = None
        display.draw = lambda: None
        display.handle_mouse(5, 5, roundtable.curses.BUTTON1_CLICKED)
        self.assertEqual(display.expanded, "Console")
        display.handle_mouse(5, 5, roundtable.curses.BUTTON1_CLICKED)
        self.assertIsNone(display.expanded)

    def test_console_expand_and_filter_keys_via_poll_input(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.expanded = None
        display.console_filter = 0
        display.draw = lambda: None
        display.s = mock.Mock()
        display.s.getch = mock.Mock(side_effect=[ord("0"), -1])
        display.poll_input()
        self.assertEqual(display.expanded, "Console")

        display.s.getch = mock.Mock(side_effect=[ord("c"), -1])
        display.poll_input()
        self.assertEqual(display.console_filter, 1)

    def test_draw_console_default_filter_hides_ticks_until_cycled(self):
        display = make_test_display()
        display.log("Objective: Goal", kind="phase")
        display.log("[Codex] thinking", kind="tick")
        display.log("Something broke", kind="error")

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            rendered = display.s.text()
            self.assertIn("key events", rendered)
            self.assertIn("Objective: Goal", rendered)
            self.assertIn("Something broke", rendered)
            self.assertNotIn("[Codex] thinking", rendered)
            self.assertIn("console", display.hitboxes)

            display.cycle_console_filter()
            rendered = display.s.text()
            self.assertIn("all activity", rendered)
            self.assertIn("[Codex] thinking", rendered)

    def test_draw_expanded_console_shows_full_filtered_content(self):
        display = make_test_display()
        for i in range(5):
            display.log(f"tick number {i}", kind="tick")
        display.console_filter = 1  # all activity, so the ticks above are visible
        display.expanded = "Console"

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()

        rendered = display.s.text()
        self.assertIn("CONSOLE (expanded)", rendered)
        self.assertIn("all activity", rendered)
        self.assertIn("tick number 4", rendered)
        self.assertIn("collapses", rendered)
        self.assertIn("console", display.hitboxes)

    def test_console_filter_shows_filtered_count_transparency_and_hidden_summary(self):
        """Verify that compact and expanded console panels display filtered/total counts and hidden items."""
        display = make_test_display(h=48, w=160)
        display.log("Goal phase", kind="phase")
        display.log("[Codex] raw tick", kind="tick")
        display.log("Error event", kind="error")

        display.console_filter = 0  # key events: phase, error (2 of 3)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            rendered = display.s.text()
            self.assertIn("key events (2/3)", rendered)
            self.assertIn("1 hidden", rendered)

            display.expanded = "Console"
            display.draw()
            exp_rendered = display.s.text()
            self.assertIn("CONSOLE (expanded) · key events (2/3)", exp_rendered)

    def test_compact_console_honors_scroll_offset(self):
        """Regression: wheel/swipe updated scroll['Console'] but the non-expanded panel always
        sliced the latest N entries, so scrolling the compact console looked like a no-op."""
        display = make_test_display(h=48, w=160)
        # Unique markers so a partial render is unambiguous.
        for i in range(40):
            display.log(f"console-marker-{i:02d}", kind="phase")
        display.console_filter = 0  # key events includes phase

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.scroll["Console"] = 0
            display.draw()
            latest = display.s.text()
            self.assertIn("console-marker-39", latest)
            self.assertNotIn("console-marker-00", latest)

            display.scroll["Console"] = 25
            display.draw()
            scrolled = display.s.text()
            # Scrolling back must reveal older markers and drop the newest ones.
            self.assertNotIn("console-marker-39", scrolled)
            self.assertTrue(
                any(f"console-marker-{i:02d}" in scrolled for i in range(0, 20)),
                scrolled,
            )

    def test_compact_console_title_shows_scroll_offset(self):
        """Compact Console gave no visual cue that scroll["Console"] had moved you away from the
        live tail, unlike CODE MONITOR's title, which already showed a "· ↑N" suffix."""
        display = make_test_display(h=48, w=160)
        for i in range(40):
            display.log(f"console-marker-{i:02d}", kind="phase")
        display.console_filter = 0  # key events includes phase

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.scroll["Console"] = 0
            display.draw()
            top, left, _bottom, right = display.hitboxes["console"]
            title_row = "".join(display.s.grid[top])[left:right + 1]
            self.assertNotIn("↑", title_row)

            display.scroll["Console"] = 25
            display.draw()
            top, left, _bottom, right = display.hitboxes["console"]
            title_row = "".join(display.s.grid[top])[left:right + 1]
            self.assertIn(f"↑{display.scroll['Console']}", title_row)

    def test_compact_final_title_shows_scroll_offset(self):
        """Compact Final (TASK OUTCOME) gave no visual cue that scroll["Final"] had moved you away
        from the live tail, unlike CODE MONITOR's title, which already showed a "· ↑N" suffix."""
        display = make_test_display(h=48, w=160)
        display.session.final = "\n".join(f"final-marker-{i:02d}" for i in range(60))

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.scroll["Final"] = 0
            display.draw()
            top, left, _bottom, right = display.hitboxes["final"]
            title_row = "".join(display.s.grid[top])[left:right + 1]
            self.assertNotIn("↑", title_row)
            self.assertIn("TASK OUTCOME", title_row)

            display.scroll["Final"] = 20
            display.draw()
            top, left, _bottom, right = display.hitboxes["final"]
            title_row = "".join(display.s.grid[top])[left:right + 1]
            self.assertIn(f"↑{display.scroll['Final']}", title_row)
            self.assertIn("TASK OUTCOME", title_row)

    def test_compact_console_title_stays_inside_panel_border(self):
        """At the 72-column floor, long console filter names used to paint over the right border."""
        display = make_test_display(h=48, w=72)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            for filt in range(len(roundtable.CONSOLE_FILTERS)):
                display.console_filter = filt
                display.draw()
                self.assertIn("console", display.hitboxes, f"filter={filt}")
                top, left, _bottom, right = display.hitboxes["console"]
                row = "".join(display.s.grid[top])
                self.assertEqual(
                    row[right], "╮",
                    f"filter={filt} corrupted right border: {row[left:right + 1]!r}",
                )
                # Title text must still identify the panel.
                self.assertTrue(
                    "CONSOLE" in row[left:right + 1] or "»" in row[left:right + 1],
                    row[left:right + 1],
                )

    def test_code_monitor_honors_mouse_scroll_offset(self):
        display = make_test_display(h=48, w=72)
        display.monitor.changes = [
            roundtable.CodeChange("modified", f"code-marker-{i:02d}.py") for i in range(20)
        ]
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            latest = display.s.text()
            self.assertIn("code-marker-19.py", latest)
            self.assertNotIn("code-marker-00.py", latest)

            top, left, _, _ = display.hitboxes["code"]
            # Use a real wheel bit (not 1): state 1 is BUTTON1_RELEASED and would expand
            # the Code panel now that it is clickable like Final/Console.
            # Create a mock for curses with the required constant if it doesn't exist
            import curses
            button4_val = getattr(curses, "BUTTON4_PRESSED", 0)
            if button4_val == 0:
                # If BUTTON4_PRESSED is not available in test environment, use a mock value
                button4_val = 64  # Common value for BUTTON4_PRESSED
            display.handle_mouse(left + 1, top + 1, button4_val)
            scrolled = display.s.text()

        self.assertEqual(display.scroll["Code"], 3)
        self.assertIn("↑3", scrolled)
        self.assertNotIn("code-marker-19.py", scrolled)
        self.assertIn("code-marker-16.py", scrolled)
        _, _, _, right = display.hitboxes["code"]
        self.assertEqual(display.s.grid[top][right], "╮")

    def test_status_line_is_shortened_when_idle(self):
        """Idle dashboard used to hard-clip a long status·workspace via addnstr mid-string;
        shorten with an ellipsis instead so the path doesn't look truncated mid-segment."""
        display = make_test_display(h=30, w=72)
        display.busy = False
        display.status = "Ready with a particularly verbose phase description"
        display.session = roundtable.Session(
            "Goal",
            "/home/user/very/long/path/to/a/workspace/directory/name",
            0, "now", [],
        )
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
        row = "".join(display.s.grid[3]).rstrip()
        self.assertIn("…", row)
        # Must not leave a raw mid-path stump without ellipsis (e.g. ".../wor").
        self.assertLessEqual(len(row.strip()), 72)

    def test_draw_footer_displays_error_message_when_set(self):
        """When display.error is non-empty, draw() surfaces it in the footer."""
        display = make_test_display(h=30, w=100)
        display.error = "CRITICAL: Connection lost to remote worker node"
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=True):
            display.draw()
        footer_row = "".join(display.s.grid[29]).rstrip()
        self.assertIn("CRITICAL: Connection lost", footer_row)

    def test_agent_and_final_panel_scroll_handles_uninitialized_key(self):
        """Rendering agent or final panels with a missing scroll key uses default offset 0 without raising KeyError."""
        display = make_test_display(h=30, w=100)
        display.scroll = {}  # Empty scroll dict
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            display._agent_panel(5, 1, 10, 30, "CustomAgent", "★", "Custom", 1)
        self.assertEqual(display.scroll.get("CustomAgent"), 0)
        self.assertEqual(display.scroll.get("Final"), 0)


    def test_touch_hit_targets_and_panel_scrolling(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.hitboxes = {"send": (10, 2, 10, 10), "codex": (2, 2, 8, 30)}
        display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0}
        display.draw = lambda: None
        self.assertEqual(display.handle_mouse(5, 10, roundtable.curses.BUTTON1_CLICKED), "send")
        display.handle_mouse(5, 5, roundtable.curses.BUTTON4_PRESSED)
        self.assertEqual(display.scroll["Codex"], 3)

    def test_parallel_touch_cancel_propagates_to_every_agent(self):
        class WaitingAgent(roundtable.Agent):
            stopped = 0
            lock = threading.Lock()

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                on_tick("started")
                while not self.cancel_event.is_set():
                    time.sleep(0.01)
                with self.lock:
                    type(self).stopped += 1
                return self.name

        with tempfile.TemporaryDirectory() as td:
            agents = [(name, WaitingAgent(name, Path(td)))
                      for name in ("Codex", "Claude", "Antigravity")]
            session = roundtable.Session("Cancel", td, 0, "now", [])
            with self.assertRaises(KeyboardInterrupt):
                roundtable._run_parallel_phase(
                    session, agents, "proposal",
                    lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
                    lambda *_: None, "working")
            self.assertEqual(WaitingAgent.stopped, 3)
            self.assertTrue(all(agent.cancel_event is None for _, agent in agents))

    def test_phase_cancellation_does_not_clear_a_replacement_event(self):
        agent = roundtable.MockAgent("Codex", Path("/tmp"))
        replacement = threading.Event()
        with roundtable._phase_cancellation([("Codex", agent)]):
            agent.cancel_event = replacement
        self.assertIs(agent.cancel_event, replacement)

    def test_run_preflight_staggers_agent_start_times(self):
        # Regression coverage for the actual mechanism (setUp patches AGENT_SPAWN_STAGGER_SECONDS
        # to 0 for every other test, so this needs its own explicit override to observe it at all).
        start_times: dict[str, float] = {}

        class TimestampingAgent(roundtable.MockAgent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                start_times[self.name] = time.monotonic()
                return super().run(prompt, on_tick, cancel_event, no_edit)

        with tempfile.TemporaryDirectory() as td:
            agents = [(name, TimestampingAgent(name, Path(td)))
                      for name in ("Codex", "Claude", "Antigravity")]
            roundtable.run_preflight(agents, lambda *_: None, lambda *_: None, stagger=0.2)
            ordered = [start_times[name] for name in ("Codex", "Claude", "Antigravity")]
            self.assertEqual(ordered, sorted(ordered))
            self.assertGreaterEqual(ordered[1] - ordered[0], 0.15)
            self.assertGreaterEqual(ordered[2] - ordered[1], 0.15)

    def test_run_parallel_phase_staggers_agent_start_times(self):
        start_times: dict[str, float] = {}

        class TimestampingAgent(roundtable.MockAgent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                start_times[self.name] = time.monotonic()
                return super().run(prompt, on_tick, cancel_event, no_edit)

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Stagger check", td, 0, "now", [])
            agents = [(name, TimestampingAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working", stagger=0.2)
            ordered = [start_times[name] for name in ("Codex", "Claude", "Antigravity")]
            self.assertEqual(ordered, sorted(ordered))
            self.assertGreaterEqual(ordered[1] - ordered[0], 0.15)
            self.assertGreaterEqual(ordered[2] - ordered[1], 0.15)

    def test_staggered_parallel_phase_can_cancel_agents_before_they_launch(self):
        calls: list[str] = []

        class DoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                calls.append(self.name)
                return "Implemented and tested.\nTASK STATUS: complete"

        class DelayedAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                calls.append(self.name)
                return "This delayed CLI should never start"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Finish quickly", td, 0, "now", [])
            agents = [("Codex", DoneAgent("Codex", workspace)),
                      ("Claude", DelayedAgent("Claude", workspace)),
                      ("Antigravity", DelayedAgent("Antigravity", workspace))]
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                task_status_check=True, stagger=0.5,
            )
            self.assertEqual(calls, ["Codex"])
            self.assertEqual([turn.speaker for turn in session.turns], ["Codex"])

    def test_parallel_speed_samples_exclude_stagger_wait(self):
        observed: dict[str, float] = {}

        class EqualSpeedAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                started = time.monotonic()
                time.sleep(0.02)
                observed[self.name] = time.monotonic() - started
                return self.name

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Measure fairly", td, 0, "now", [])
            agents = [(name, EqualSpeedAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            speeds: dict[str, list[float]] = {}
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                agent_speed=speeds, stagger=0.2,
            )
            self.assertEqual(set(speeds), {"Codex", "Claude", "Antigravity"})
            # Compare the recorded sample with the time actually spent inside run(), rather than
            # assuming a sleeping thread always gets rescheduled within a fixed wall-time ceiling.
            # If stagger wait leaked into the samples, later agents would differ by 0.2/0.4s.
            for name, samples in speeds.items():
                self.assertAlmostEqual(samples[0], observed[name], delta=0.05)

    def test_zero_stagger_still_runs_concurrently(self):
        # stagger=0 (what setUp uses for every other test) must not become sequential -- it should
        # just skip the extra sleep, not stop agents from genuinely overlapping.
        class ConcurrentAgent(roundtable.Agent):
            lock = threading.Lock()
            active = 0
            maximum = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                with self.lock:
                    type(self).active += 1
                    type(self).maximum = max(type(self).maximum, type(self).active)
                time.sleep(0.05)
                with self.lock:
                    type(self).active -= 1
                return self.name

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Stagger check", td, 0, "now", [])
            agents = [(name, ConcurrentAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working", stagger=0)
            self.assertEqual(ConcurrentAgent.maximum, 3)

    def test_run_preflight_passes_with_healthy_agents(self):
        with tempfile.TemporaryDirectory() as td:
            agents = [(name, roundtable.MockAgent(name, Path(td)))
                      for name in ("Codex", "Claude", "Antigravity")]
            messages = []
            roundtable.run_preflight(agents, lambda *_: None,
                                     lambda active, message: messages.append((tuple(active), message)))
            self.assertEqual(messages[0], (("Codex", "Claude", "Antigravity"),
                                           "Running a preliminary system check"))
            self.assertEqual(messages[-1], ((), "System check complete"))

    def test_run_preflight_raises_with_every_broken_agent_named(self):
        with tempfile.TemporaryDirectory() as td:
            agents = [(name, FailingAgent(name, Path(td)))
                      for name in ("Codex", "Claude", "Antigravity")]
            with self.assertRaises(RuntimeError) as raised:
                roundtable.run_preflight(agents, lambda *_: None, lambda *_: None)
            message = str(raised.exception)
            for name in ("Codex", "Claude", "Antigravity"):
                self.assertIn(name, message)
            self.assertIn("boom: simulated failure", message)

    def test_run_preflight_reports_timeout_without_hanging(self):
        class HangingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                while not self.cancel_event.is_set():
                    time.sleep(0.01)
                raise RuntimeError(f"{self.name} cancelled")

        with tempfile.TemporaryDirectory() as td:
            agents = [("Codex", HangingAgent("Codex", Path(td)))]
            start = time.monotonic()
            with self.assertRaises(RuntimeError) as raised:
                roundtable.run_preflight(agents, lambda *_: None, lambda *_: None, timeout=0.1)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 2.0)
            self.assertIn("timed out", str(raised.exception))

    def test_run_preflight_allows_usage_limited_agent_to_wait_during_task(self):
        class LimitedAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                raise RuntimeError(
                    "Claude exited with status 1\n"
                    "You've hit your session limit · resets 5:30pm (America/Chicago)")

        with tempfile.TemporaryDirectory() as td:
            ticks = []
            roundtable.run_preflight(
                [("Claude", LimitedAgent("Claude", Path(td)))],
                lambda name, line: ticks.append((name, line)), lambda *_: None)
        self.assertTrue(any("usage-limited; will wait" in line for _, line in ticks))

    def test_agents_exchange_and_synthesize(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Solve it", td, 1, "now", [])
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None)
            self.assertEqual([t.speaker for t in session.turns],
                             list(roundtable.AGENT_NAMES) + list(roundtable.AGENT_NAMES) + ["Final"])
            self.assertTrue(session.final)
            for turn in session.turns[:-1]:
                self.assertTrue(turn.content.endswith(f"Signed: {turn.speaker}"))
            final_signers = session.final.rsplit("Signed by: ", 1)[-1].split(", ")
            self.assertEqual(set(final_signers), set(roundtable.AGENT_NAMES))

    def test_prompt_contains_other_agent(self):
        turns = [roundtable.Turn("Claude", "proposal", "Use a queue")]
        prompt = roundtable.prompt_for("Build a worker", turns, "review 1", "Codex")
        self.assertIn("Use a queue", prompt)
        self.assertIn("Build a worker", prompt)

    def test_parallel_phase_prepares_shared_prompt_context_once(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                "Build a worker", td, 0, "now",
                [roundtable.Turn("User", "context", "Keep it small")])
            agents = [(name, roundtable.MockAgent(name, Path(td)))
                      for name in roundtable.AGENT_NAMES]
            with mock.patch.object(
                    roundtable, "prepare_prompt_context",
                    wraps=roundtable.prepare_prompt_context) as prepare:
                roundtable._run_parallel_phase(
                    session, agents, "proposal", lambda *_: None, lambda *_: None, "Working")
        self.assertEqual(prepare.call_count, 1)

    def test_save_both_formats(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0, "now",
                                         [roundtable.Turn("Codex", "proposal", "Answer")], "Final")
            json_path, md_path = roundtable.save_session(session, Path(td))
            self.assertTrue(json_path.exists())
            self.assertIn("Final answer", md_path.read_text())

    def test_run_log_writes_lines_with_elapsed_prefix_and_flushes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "activity.log"
            run_log = roundtable.RunLog(path)
            run_log.write("phase", "Agents are developing solutions in parallel")
            run_log.write("prompt", "[Codex] PROMPT:\nmulti\nline\ntext")
            run_log.close()
            content = path.read_text()
            self.assertIn("PHASE   Agents are developing solutions in parallel", content)
            self.assertIn("PROMPT  [Codex] PROMPT:", content)
            self.assertIn("PROMPT  multi", content)
            self.assertIn("PROMPT  line", content)

    def test_run_log_with_no_path_is_a_silent_no_op(self):
        run_log = roundtable.RunLog(None)
        run_log.write("phase", "should not raise")
        run_log.close()

    def test_run_log_creates_missing_parent_directories(self):
        """A nested log path under a not-yet-created tree should still open and write."""
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "nested" / "run" / "activity.log"
            self.assertFalse(nested.parent.exists())
            run_log = roundtable.RunLog(nested)
            run_log.write("tick", "created parents")
            run_log.close()
            self.assertTrue(nested.is_file())
            self.assertIn("TICK    created parents", nested.read_text())

    def test_run_log_handles_file_errors_gracefully(self):
        """Open/write OSErrors degrade to a silent no-op; they must not crash the run."""
        with tempfile.TemporaryDirectory() as td:
            # path is an existing directory — open("a") raises IsADirectoryError (OSError).
            blocked = Path(td) / "not_a_file"
            blocked.mkdir()
            run_log = roundtable.RunLog(blocked)
            self.assertIsNone(run_log._handle)
            run_log.write("tick", "must not raise")
            run_log.close()

            # Mid-run write failure also degrades rather than raising into agent phases.
            good = Path(td) / "ok.log"
            run_log = roundtable.RunLog(good)
            self.assertIsNotNone(run_log._handle)
            with mock.patch.object(run_log._handle, "write", side_effect=OSError("disk full")):
                run_log.write("error", "simulated write failure")
            self.assertIsNone(run_log._handle)
            run_log.write("tick", "still a no-op after drop")
            run_log.close()

    def test_run_context_logs_reproducibility_details_but_not_environment(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "activity.log"
            run_log = roundtable.RunLog(path)
            args = roundtable.argparse.Namespace(
                output_dir=td, plain=True, mock=True, collab="parallel",
                synthesizer="rotate", synthesis_passes=3, reasoning_effort="auto",
                balance_load=True, task_status_check=False, reassign_idle=True,
                skip_preflight=True, preflight_timeout=25, extended_preflight=False,
                touch_mode=False, debug=False)
            agent = roundtable.Agent("Codex", Path(td), "test-model")
            session = roundtable.Session("Sensitive objective", td, 1, "now", [], restart_count=2)
            with mock.patch.dict(os.environ, {"ROUND_TABLE_TEST_SECRET": "must-not-appear"}):
                roundtable.log_run_context(
                    run_log, args, session, [agent], resumed=True,
                    completed_phases={"proposal"})
            run_log.close()
            logged = path.read_text()
        self.assertIn('"objective_sha256"', logged)
        self.assertIn('"test-model"', logged)
        self.assertIn('"completed_phases"', logged)
        self.assertIn('"proposal"', logged)
        self.assertIn('"roundtable_source"', logged)
        self.assertIn('"restart_count": 2', logged)
        self.assertIn('"workspace_git"', logged)
        self.assertIn("environment variables and credentials intentionally omitted", logged)
        self.assertNotIn("ROUND_TABLE_TEST_SECRET", logged)
        self.assertNotIn("must-not-appear", logged)

    def test_agent_streams_every_burst_output_line_and_logs_lifecycle(self):
        class BurstAgent(roundtable.Agent):
            def command(self, prompt, output_file=None, no_edit=False):
                return [
                    sys.executable, "-c",
                    "print('alpha'); print('beta'); print('gamma')",
                    prompt,
                ]

        with tempfile.TemporaryDirectory() as td:
            agent = BurstAgent("Claude", Path(td))
            ticks = []
            diagnostics = []
            agent.log_diagnostic = diagnostics.append
            answer = agent.run("private prompt text", ticks.append)
        self.assertEqual(answer.splitlines(), ["alpha", "beta", "gamma"])
        self.assertTrue({"alpha", "beta", "gamma"}.issubset(set(ticks)))
        joined = "\n".join(diagnostics)
        self.assertIn("launch cwd=", joined)
        self.assertIn("<prompt chars=19 sha256=", joined)
        self.assertNotIn("private prompt text", joined)
        self.assertIn("started pid=", joined)
        self.assertIn("captured_lines=3", joined)
        self.assertIn("completed successfully", joined)

    def test_log_path_for_pairs_with_transcript_stem(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0, "2026-01-01T00:00:00.000001+00:00", [])
            json_path, _ = roundtable.save_session(session, Path(td))
            log_path = roundtable.log_path_for(session, Path(td))
            self.assertEqual(log_path.stem, json_path.stem)
            self.assertEqual(log_path.suffix, ".log")

    def test_artifact_paths_for_share_one_stamp(self):
        session = roundtable.Session("Goal", "/ws", 0, "2026-01-01T00:00:00.000001+00:00", [])
        json_path, md_path, log_path = roundtable.artifact_paths_for(session, Path("/out"))
        self.assertEqual({json_path.stem, md_path.stem, log_path.stem}, {json_path.stem})
        self.assertEqual((json_path.suffix, md_path.suffix, log_path.suffix), (".json", ".md", ".log"))

    def test_config_summary_reports_active_non_default_settings(self):
        """Flags that change behavior (collab mode, effort, checks, elevated agents) must be
        readable from a single line instead of requiring a trip into the private disk log."""
        args = roundtable.argparse.Namespace(
            collab="sequential", reasoning_effort="high", synthesis_passes=3,
            balance_load=True, task_status_check=False, reassign_idle=True,
            dead_code_check=True, elevated=["grok", "codex", "codex"], mock=False)
        summary = roundtable.config_summary(args)
        self.assertIn("collab=sequential", summary)
        self.assertIn("effort=high", summary)
        self.assertIn("synthesis-passes=3", summary)
        self.assertIn("checks=balance-load, reassign-idle, dead-code", summary)
        self.assertIn("elevated=codex, grok", summary)
        self.assertNotIn("MOCK", summary)

    def test_config_summary_defaults_are_quiet_and_mock_is_flagged(self):
        args = roundtable.argparse.Namespace(
            collab="parallel", reasoning_effort="auto", synthesis_passes=6,
            balance_load=False, task_status_check=False, reassign_idle=False,
            dead_code_check=False, elevated=[], mock=False)
        summary = roundtable.config_summary(args)
        self.assertIn("checks=none", summary)
        self.assertIn("elevated=none", summary)
        self.assertNotIn("MOCK", summary)

        args.mock = True
        mock_summary = roundtable.config_summary(args)
        self.assertTrue(mock_summary.startswith("⚠ MOCK"))
        self.assertIn("Config: collab=parallel", mock_summary)

    def test_config_summary_treats_elevated_all_as_all_agents(self):
        args = roundtable.argparse.Namespace(
            collab="parallel", reasoning_effort="auto", synthesis_passes=6,
            balance_load=False, task_status_check=False, reassign_idle=False,
            dead_code_check=False, elevated=["all"], mock=False)
        self.assertIn("elevated=all", roundtable.config_summary(args))

    def test_config_summary_tolerates_a_sparse_namespace(self):
        """run_tui's test doubles construct Namespaces with only the fields they exercise --
        config_summary must not crash when reasoning_effort/synthesis_passes/elevated are absent."""
        args = roundtable.argparse.Namespace(collab="parallel")
        summary = roundtable.config_summary(args)
        self.assertIn("effort=auto", summary)
        self.assertIn("synthesis-passes=6", summary)
        self.assertIn("elevated=none", summary)

    def test_plain_mode_prints_config_summary_at_start(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            argv = ["roundtable", "Solve it", "--mock", "--plain", "--skip-preflight",
                    "-r", "0", "-C", td, "--output-dir", str(out), "--collab", "sequential"]
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                self.assertEqual(roundtable.main(), 0)
            printed = stdout.getvalue()
            self.assertIn("Config: collab=sequential", printed)
            self.assertIn("⚠ MOCK", printed)

    def test_plain_mode_success_prints_transcript_and_log_paths(self):
        """A successful run must tell the operator where both the human-readable transcript and
        the full diagnostic log (prompts, retries, exit codes) landed -- without it, the only way
        to find them was to already know the --output-dir default."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            argv = ["roundtable", "Solve it", "--mock", "--plain", "--skip-preflight",
                    "-r", "0", "-C", td, "--output-dir", str(out)]
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                self.assertEqual(roundtable.main(), 0)
            printed = stdout.getvalue()
            transcript = next(Path(out).glob("*.md"))
            log = next(Path(out).glob("*.log"))
            self.assertIn(f"Transcript: {transcript}", printed)
            self.assertIn(f"Log: {log}", printed)

    def test_plain_mode_failure_prints_checkpoint_paths_when_a_turn_was_saved(self):
        """Once at least one turn exists, a mid-run failure still checkpoints a transcript --
        the operator needs its path (and the log's) on stderr, not just a bare error message."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"

            def fake_conduct(session, *_args, **_kwargs):
                session.turns.append(roundtable.Turn("Codex", "proposal", "partial work"))
                raise RuntimeError("boom: simulated mid-run failure")

            argv = ["roundtable", "Solve it", "--mock", "--plain", "--skip-preflight",
                    "-r", "0", "-C", td, "--output-dir", str(out)]
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "conduct", side_effect=fake_conduct), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(roundtable.main(), 1)
            printed = stderr.getvalue()
            self.assertIn("could not complete", printed)
            transcript = next(Path(out).glob("*.md"))
            log = next(Path(out).glob("*.log"))
            self.assertIn(f"Transcript: {transcript}", printed)
            self.assertIn(f"Log: {log}", printed)

    def test_plain_mode_failure_prints_log_path_with_no_turns_saved(self):
        """A failure before any turn exists has no transcript to checkpoint, but the activity log
        was opened before conduct() ever ran -- its path must still be reported, not silently
        dropped just because there's no Transcript: line to go with it."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"

            def fake_conduct(_session, *_args, **_kwargs):
                raise RuntimeError("boom: simulated startup failure")

            argv = ["roundtable", "Solve it", "--mock", "--plain", "--skip-preflight",
                    "-r", "0", "-C", td, "--output-dir", str(out)]
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "conduct", side_effect=fake_conduct), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(roundtable.main(), 1)
            printed = stderr.getvalue()
            self.assertIn("could not complete", printed)
            log = next(Path(out).glob("*.log"))
            self.assertIn(f"Log: {log}", printed)
            self.assertNotIn("Transcript:", printed)
            self.assertEqual(list(Path(out).glob("*.md")), [])

    def test_plain_mode_cancel_with_no_turns_yet_skips_paths(self):
        """Cancelling before any turn exists means nothing was ever saved -- the message must stay
        a bare 'Cancelled.' rather than pointing at files that don't exist."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"

            def fake_conduct(_session, *_args, **_kwargs):
                raise KeyboardInterrupt

            argv = ["roundtable", "Solve it", "--mock", "--plain", "--skip-preflight",
                    "-r", "0", "-C", td, "--output-dir", str(out)]
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "conduct", side_effect=fake_conduct), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(roundtable.main(), 130)
            printed = stderr.getvalue()
            self.assertEqual(printed.strip(), "Cancelled.")

    def test_display_log_prompt_keeps_console_short_but_logs_full_prompt_to_disk(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "activity.log"
            display = roundtable.Display.__new__(roundtable.Display)
            display.started = time.monotonic()
            display.console = roundtable.deque(maxlen=300)
            display.run_log = roundtable.RunLog(path)

            long_prompt = "SYSTEM BRIEF\n" + ("detail " * 200)
            display.log_prompt("Codex", long_prompt)
            display.run_log.close()

            kind, summary = display.console[-1]
            self.assertEqual(kind, "prompt")
            self.assertLess(len(summary), 200)
            logged = path.read_text()
            self.assertIn("detail detail detail", logged)
            self.assertGreater(len(logged), len(long_prompt) * 0.9)

    def test_conduct_logs_every_prompt_including_synthesis(self):
        with tempfile.TemporaryDirectory() as td:
            logged: list[tuple[str, str]] = []
            session = roundtable.Session("Solve it", td, 1, "now", [])
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None,
                               lambda *_: None, synthesizer="claude",
                               log_prompt=lambda name, p: logged.append((name, p)))
            names_logged = [name for name, _ in logged]
            # proposal + review round 1 + one relay step in the final merge, for every agent
            for name in roundtable.AGENT_NAMES:
                self.assertEqual(names_logged.count(name), 3)
            final_prompts = [p for name, p in logged if name == "Claude"]
            self.assertTrue(any("final editor" in p.lower() for p in final_prompts))

    def test_followup_cycle_preserves_consensus_and_focuses_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                "Build it", td, 0, "2026-01-01T00:00:00.000001+00:00",
                [roundtable.Turn("Final", "consensus", "First answer"),
                 roundtable.Turn("User", "follow-up", "Now add search")], "First answer")
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents,
                               lambda *_: None, lambda *_: None,
                               followup=True)
            self.assertEqual([t.phase for t in session.turns if t.speaker == "Final"],
                             ["consensus", "consensus"])
            self.assertIn("followup-proposal", [t.phase for t in session.turns])
            prompt = roundtable.prompt_for(session.objective, session.turns,
                                           "followup-proposal", "Codex")
            self.assertIn("latest 'User — follow-up'", prompt)
            self.assertIn("Now add search", prompt)

    def test_repeated_saves_use_same_paths_and_accumulate(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0,
                                         "2026-01-01T00:00:00.000001+00:00", [])
            first = roundtable.save_session(session, Path(td))
            session.turns.append(roundtable.Turn("User", "follow-up", "More"))
            second = roundtable.save_session(session, Path(td))
            self.assertEqual(first, second)
            self.assertIn("More", second[1].read_text())

    def test_failed_atomic_save_preserves_previous_session(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0,
                                         "2026-01-01T00:00:00.000001+00:00", [])
            json_path, _ = roundtable.save_session(session, Path(td))
            before = json_path.read_text()
            session.turns.append(roundtable.Turn("User", "follow-up", "More"))
            with mock.patch.object(roundtable.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    roundtable.save_session(session, Path(td))
            self.assertEqual(json_path.read_text(), before)
            self.assertFalse(list(Path(td).glob(".*.tmp")))

    def test_load_session_round_trips_saved_json(self):
        with tempfile.TemporaryDirectory() as td:
            original = roundtable.Session(
                "Goal", td, 2, "2026-01-01T00:00:00.000001+00:00",
                [roundtable.Turn("Final", "consensus", "First answer")], "First answer")
            json_path, _ = roundtable.save_session(original, Path(td))
            loaded = roundtable.load_session(json_path)
            self.assertEqual(loaded, original)

    def test_resume_plain_adds_followup_and_reuses_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            original = roundtable.Session(
                "Original goal", td, 0, "2026-01-01T00:00:00.000001+00:00",
                [roundtable.Turn("Final", "consensus", "First answer")], "First answer")
            json_path, _ = roundtable.save_session(original, Path(td))
            argv = ["roundtable", "Add retries", "--resume", str(json_path),
                    "--plain", "--mock"]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            resumed = roundtable.load_session(json_path)
            self.assertEqual(resumed.objective, "Original goal")
            self.assertIn(roundtable.Turn("User", "follow-up", "Add retries"), resumed.turns)
            self.assertIn("followup-proposal", [turn.phase for turn in resumed.turns])

    def test_resume_plain_interactive_requires_followup(self):
        with tempfile.TemporaryDirectory() as td:
            original = roundtable.Session(
                "Original goal", td, 0, "2026-01-01T00:00:00.000001+00:00",
                [roundtable.Turn("Final", "consensus", "First answer")], "First answer")
            json_path, _ = roundtable.save_session(original, Path(td))
            argv = ["roundtable", "--resume", str(json_path), "--plain", "--mock"]
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys.stdin, "isatty", return_value=True), \
                 contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                roundtable.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("requires follow-up text", stderr.getvalue())
            self.assertEqual(roundtable.load_session(json_path), original)

    def test_load_session_rejects_malformed_turns(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text(json.dumps({"objective": "Goal", "workspace": td, "rounds": 1,
                                        "started_at": "now", "turns": [{}]}))
            with self.assertRaisesRegex(ValueError, "turn 0"):
                roundtable.load_session(path)

    def test_load_session_rejects_malformed_queued_prompts_instead_of_losing_them(self):
        with tempfile.TemporaryDirectory() as td:
            base = {"objective": "Goal", "workspace": td, "rounds": 1,
                    "started_at": "now", "turns": []}
            path = Path(td) / "bad.json"
            for queued_prompts in ("retry this", ["valid", 3]):
                with self.subTest(queued_prompts=queued_prompts):
                    path.write_text(json.dumps({**base, "queued_prompts": queued_prompts}))
                    with self.assertRaisesRegex(ValueError, "queued_prompts"):
                        roundtable.load_session(path)

    def test_load_session_keeps_compatibility_with_sessions_without_queued_prompts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.json"
            path.write_text(json.dumps({
                "objective": "Goal", "workspace": td, "rounds": 1,
                "started_at": "now", "turns": [],
            }))
            self.assertEqual(roundtable.load_session(path).queued_prompts, [])

    def test_load_session_keeps_compatibility_with_sessions_without_restart_count(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.json"
            path.write_text(json.dumps({
                "objective": "Goal", "workspace": td, "rounds": 1,
                "started_at": "now", "turns": [],
            }))
            self.assertEqual(roundtable.load_session(path).restart_count, 0)

    def test_load_session_round_trips_restart_count(self):
        with tempfile.TemporaryDirectory() as td:
            original = roundtable.Session(
                "Goal", td, 2, "2026-01-01T00:00:00.000001+00:00",
                [roundtable.Turn("Final", "consensus", "First answer")], "First answer",
                restart_count=3)
            json_path, _ = roundtable.save_session(original, Path(td))
            loaded = roundtable.load_session(json_path)
            self.assertEqual(loaded.restart_count, 3)
            self.assertEqual(loaded, original)

    def test_load_session_rejects_negative_or_non_int_restart_count(self):
        with tempfile.TemporaryDirectory() as td:
            base = {"objective": "Goal", "workspace": td, "rounds": 1,
                    "started_at": "now", "turns": []}
            path = Path(td) / "bad.json"
            for restart_count in (-1, "2", True):
                with self.subTest(restart_count=restart_count):
                    path.write_text(json.dumps({**base, "restart_count": restart_count}))
                    with self.assertRaisesRegex(ValueError, "restart_count"):
                        roundtable.load_session(path)

    def test_plain_mode_reports_agent_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0",
                    "-C", td, "--output-dir", str(Path(td) / "out")]
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "Agent", FailingAgent), \
                 contextlib.redirect_stderr(stderr):
                code = roundtable.main()
            self.assertEqual(code, 1)
            self.assertIn("boom: simulated failure", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            log_path = next((Path(td) / "out").glob("*.log"))
            self.assertIn(f"Log: {log_path}", stderr.getvalue())
            log = log_path.read_text()
            self.assertIn("Traceback (most recent call last)", log)
            self.assertIn("boom: simulated failure", log)

    def test_antigravity_command_is_sandboxed_and_noninteractive(self):
        agent = roundtable.Agent("Antigravity", Path("/tmp/work"), "antigravity-model")
        command = agent.command("Solve this")
        self.assertEqual(command[:3], ["agy", "--print", "Solve this"])
        self.assertIn("accept-edits", command)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[-2:], ["--model", "antigravity-model"])

    def test_aider_command_uses_repo_context_without_automatic_commits(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / ".git").mkdir()
            (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            agent = roundtable.Agent("Aider", workspace, "mistral/codestral-latest")
            command = agent.command("Solve this")
        self.assertEqual(command[:3], ["aider", "--message", "Solve this"])
        self.assertIn("--yes-always", command)
        self.assertNotIn("--no-git", command)
        self.assertIn("--no-auto-commits", command)
        self.assertIn("--no-dirty-commits", command)
        self.assertIn("--no-gitignore", command)
        self.assertIn("--no-suggest-shell-commands", command)
        self.assertNotIn("--edit-format", command)
        self.assertEqual(command[-2:], ["--model", "mistral/codestral-latest"])

    def test_aider_command_does_not_create_git_in_a_non_repository_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            agent = roundtable.Agent("Aider", Path(td))
            self.assertIn("--no-git", agent.command("Solve this"))

    def test_aider_command_bounds_each_api_call_with_a_timeout(self):
        # Observed in practice: Aider's own default (unbounded) hung 45+ minutes on a single API
        # call after a malformed provider response, even with --edit-format ask already active --
        # this is a separate failure mode from the edit-reflection loop, one level lower (LiteLLM's
        # own response parsing), so it needs its own fix regardless of no_edit.
        agent = roundtable.Agent("Aider", Path("/tmp/work"))
        command = agent.command("Solve this")
        self.assertIn("--timeout", command)
        self.assertEqual(command[command.index("--timeout") + 1], "180")

    def test_aider_command_disables_playwright_side_trips(self):
        agent = roundtable.Agent("Aider", Path("/tmp/work"))
        self.assertIn("--disable-playwright", agent.command("Solve this"))

    def test_aider_no_edit_uses_ask_mode_to_avoid_the_edit_reflection_loop(self):
        # Verified in practice: without --edit-format ask, a synthesis-phase prompt (prose only,
        # but often quoting code from another agent's proposal) can make Aider mistake that quote
        # for a malformed edit attempt, burning up to 3 retries (its own hard cap) re-sending the
        # full transcript to the model each time -- 900+ seconds observed against a slow provider.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / ".git").mkdir()
            (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            agent = roundtable.Agent("Aider", workspace, "mistral/codestral-latest")
            command = agent.command("Write a summary", no_edit=True)
        self.assertIn("--edit-format", command)
        self.assertEqual(command[command.index("--edit-format") + 1], "ask")
        self.assertIn("--no-git", command)

    def test_other_agents_ignore_no_edit_since_they_have_no_edit_reflection_loop(self):
        for name in ("Codex", "Claude", "Antigravity", "Grok", "Qwen"):
            agent = roundtable.Agent(name, Path("/tmp/work"))
            self.assertEqual(agent.command("Solve this"), agent.command("Solve this", no_edit=True))

    def test_grok_command_is_noninteractive_with_claude_style_permission_modes(self):
        agent = roundtable.Agent("Grok", Path("/tmp/work"), "grok-4")
        command = agent.command("Solve this")
        self.assertEqual(command[:3], ["grok", "-p", "Solve this"])
        self.assertIn("--output-format", command)
        self.assertIn("acceptEdits", command)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[-2:], ["-m", "grok-4"])

    def test_qwen_command_is_noninteractive_and_never_defaults_to_container_sandbox(self):
        agent = roundtable.Agent("Qwen", Path("/tmp/work"), "qwen3-coder")
        command = agent.command("Solve this")
        self.assertEqual(command[:3], ["qwen", "-p", "Solve this"])
        self.assertIn("auto-edit", command)
        self.assertNotIn("--sandbox", command)
        # Required for OPENAI_API_KEY/OPENAI_BASE_URL env vars to be honored at all -- without it,
        # a valid key still fails with a misleading "Invalid API-key provided" error.
        self.assertIn("--auth-type", command)
        self.assertIn("openai", command)
        # Measured ~36% faster in practice (13.0s -> 8.4s for a trivial reply); see
        # QWEN_SAFE_MODE_BANNER for why this is safe to always pass.
        self.assertIn("--safe-mode", command)
        self.assertEqual(command[-2:], ["-m", "qwen3-coder"])

    def test_qwen_safe_mode_banner_is_stripped_from_the_captured_answer(self):
        banner = ("⚠ SAFE MODE — all customizations disabled (hooks, extensions, skills, MCP "
                  "servers, QWEN.md). Restart without --safe-mode to resume normal operation.")
        self.assertEqual(roundtable.QWEN_SAFE_MODE_BANNER.sub("", f"{banner}\nOK").strip(), "OK")
        self.assertEqual(roundtable.QWEN_SAFE_MODE_BANNER.sub("", f"OK\n{banner}").strip(), "OK")
        # A real answer that happens to start with a similar-looking word must survive untouched.
        self.assertEqual(roundtable.QWEN_SAFE_MODE_BANNER.sub("", "SAFE MODE is a design pattern"),
                         "SAFE MODE is a design pattern")

    def test_qwen_yolo_warning_is_recognized_without_matching_real_answers(self):
        warning = ("Warning: running headless with --yolo / approval-mode=yolo and no sandbox. "
                   "All tool calls auto-execute. Enable a sandbox or silence this notice.")
        self.assertEqual(roundtable.QWEN_YOLO_WARNING.sub("", f"{warning}\nOK").strip(), "OK")
        self.assertEqual(roundtable.QWEN_YOLO_WARNING.sub("", f"OK\n{warning}").strip(), "OK")
        self.assertEqual(roundtable.QWEN_YOLO_WARNING.sub("", "Warning: review this code"),
                         "Warning: review this code")

    def test_explicit_reasoning_effort_uses_each_supported_clis_native_flag(self):
        expected = {
            "Codex": ("-c", 'model_reasoning_effort="high"'),
            "Claude": ("--effort", "high"),
            "Antigravity": ("--effort", "high"),
            "Aider": ("--reasoning-effort", "high"),
            "Grok": ("--reasoning-effort", "high"),
        }
        for name, (flag, value) in expected.items():
            with self.subTest(name=name):
                agent = roundtable.Agent(name, Path("/tmp/work"))
                agent.reasoning_effort = "high"
                command = agent.command("Solve this")
                self.assertIn(flag, command)
                self.assertEqual(command[command.index(flag) + 1], value)

        qwen = roundtable.Agent("Qwen", Path("/tmp/work"))
        qwen.reasoning_effort = "high"
        self.assertNotIn("--reasoning-effort", qwen.command("Solve this"))

    def test_auto_reasoning_effort_uses_phase_hint_without_risky_aider_inference(self):
        for name in ("Codex", "Claude", "Antigravity", "Grok"):
            with self.subTest(name=name):
                agent = roundtable.Agent(name, Path("/tmp/work"))
                agent.suggested_effort = "medium"
                self.assertTrue(any("medium" in argument for argument in agent.command("Solve this")))

        aider = roundtable.Agent("Aider", Path("/tmp/work"), "mistral/codestral-latest")
        aider.suggested_effort = "medium"
        self.assertNotIn("--reasoning-effort", aider.command("Solve this"))

    def test_preflight_auto_effort_is_low_and_restores_agent_state(self):
        class RecordingAgent(roundtable.Agent):
            def __init__(self):
                super().__init__("Claude", Path("/tmp/work"))
                self.commands = []

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                self.commands.append(self.command(prompt, no_edit=no_edit))
                return "OK"

        agent = RecordingAgent()
        ready, _ = roundtable.preflight_check(
            "Claude", agent, lambda *_: None, threading.Event(), 1.0)
        self.assertTrue(ready)
        self.assertEqual(agent.commands[0][agent.commands[0].index("--effort") + 1], "low")
        self.assertIsNone(agent.suggested_effort)
        self.assertIsNone(agent.cancel_event)

    def test_synthesis_auto_effort_is_medium_and_restores_agent_state(self):
        class RecordingAgent(roundtable.Agent):
            def __init__(self):
                super().__init__("Grok", Path("/tmp/work"))
                self.commands = []

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                self.commands.append(self.command(prompt, no_edit=no_edit))
                return "Completed\n\nFailed / incomplete\n\nNone"

        agent = RecordingAgent()
        session = roundtable.Session("Solve it", "/tmp/work", 0, "now", [])
        roundtable.synthesize(session, [("Grok", agent)], lambda *_: None, lambda *_: None)
        command = agent.commands[0]
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "medium")
        self.assertIsNone(agent.suggested_effort)

    def test_elevated_agents_swap_in_each_clis_permission_bypass_flag(self):
        codex = roundtable.Agent("Codex", Path("/tmp/work"), elevated=True)
        claude = roundtable.Agent("Claude", Path("/tmp/work"), elevated=True)
        antigravity = roundtable.Agent("Antigravity", Path("/tmp/work"), elevated=True)
        aider = roundtable.Agent("Aider", Path("/tmp/work"), elevated=True)
        grok = roundtable.Agent("Grok", Path("/tmp/work"), elevated=True)
        qwen = roundtable.Agent("Qwen", Path("/tmp/work"), elevated=True)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex.command("Solve this"))
        self.assertNotIn("--sandbox", codex.command("Solve this"))
        self.assertIn("--dangerously-skip-permissions", claude.command("Solve this"))
        self.assertNotIn("--permission-mode", claude.command("Solve this"))
        self.assertIn("--dangerously-skip-permissions", antigravity.command("Solve this"))
        self.assertNotIn("--sandbox", antigravity.command("Solve this"))
        self.assertIn("--suggest-shell-commands", aider.command("Solve this"))
        self.assertNotIn("--no-suggest-shell-commands", aider.command("Solve this"))
        self.assertIn("bypassPermissions", grok.command("Solve this"))
        self.assertNotIn("--sandbox", grok.command("Solve this"))
        self.assertIn("yolo", qwen.command("Solve this"))
        self.assertNotIn("--sandbox", qwen.command("Solve this"))

    def test_non_elevated_agents_stay_sandboxed_by_default(self):
        codex = roundtable.Agent("Codex", Path("/tmp/work"))
        claude = roundtable.Agent("Claude", Path("/tmp/work"))
        antigravity = roundtable.Agent("Antigravity", Path("/tmp/work"))
        aider = roundtable.Agent("Aider", Path("/tmp/work"))
        grok = roundtable.Agent("Grok", Path("/tmp/work"))
        self.assertIn("--no-suggest-shell-commands", aider.command("Solve this"))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", codex.command("Solve this"))
        self.assertIn("--sandbox", codex.command("Solve this"))
        self.assertNotIn("--dangerously-skip-permissions", claude.command("Solve this"))
        self.assertIn("--permission-mode", claude.command("Solve this"))
        self.assertNotIn("bypassPermissions", grok.command("Solve this"))
        self.assertIn("acceptEdits", grok.command("Solve this"))
        self.assertNotIn("--dangerously-skip-permissions", antigravity.command("Solve this"))
        self.assertIn("--sandbox", antigravity.command("Solve this"))

    def test_elevated_flag_resolves_per_agent_and_via_all(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--elevated", "antigravity"]
            captured = {}

            class RecordingAgent(roundtable.MockAgent):
                def __init__(self, name, workspace, model=None, elevated=False, debug=False):
                    super().__init__(name, workspace, model, debug=debug)
                    captured[name] = elevated

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "MockAgent", RecordingAgent):
                roundtable.main()
            self.assertEqual(captured, {"Codex": False, "Claude": False, "Antigravity": True,
                                        "Aider": False, "Grok": False, "Qwen": False})

    def test_task_status_check_flag_reaches_conduct(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--task-status-check"]
            captured = {}
            real_conduct = roundtable.conduct

            def recording_conduct(*args, **kwargs):
                captured.update(kwargs)
                return real_conduct(*args, **kwargs)

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "conduct", recording_conduct):
                roundtable.main()
            self.assertTrue(captured.get("task_status_check"))

    def test_reassign_idle_flag_reaches_conduct(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--reassign-idle"]
            captured = {}
            real_conduct = roundtable.conduct

            def recording_conduct(*args, **kwargs):
                captured.update(kwargs)
                return real_conduct(*args, **kwargs)

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "conduct", recording_conduct):
                roundtable.main()
            self.assertTrue(captured.get("reassign_idle"))

    def test_self_flag_points_workspace_at_roundtables_own_source(self):
        # --self makes self_checkpoint() hash the real, live roundtable.py on disk (not a fixture);
        # if anything edits that file between the pre-conduct hash and the post-phase checkpoint --
        # plausible with concurrent editors, and fatal here since this is the file under test --
        # SelfRestartRequired fires for real. restart_self() must stay mocked so that never reaches
        # the actual os.execv() and replaces this test process.
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "restart_self"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted((Path(td) / "out").glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            self.assertEqual(Path(session.workspace), Path(roundtable.__file__).resolve().parent)

    def test_self_restart_increments_and_persists_restart_count(self):
        """A --self run that hits SelfRestartRequired bumps session.restart_count before saving, so
        the process that resumes (and the operator reading the dashboard) knows this run has
        already replaced itself, instead of every restart looking like a fresh first pass."""
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "self_checkpoint",
                                   return_value=mock.Mock(side_effect=roundtable.SelfRestartRequired)), \
                 mock.patch.object(roundtable, "restart_self"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted((Path(td) / "out").glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            self.assertEqual(session.restart_count, 1)

    def test_self_flag_suffixes_the_note_so_the_real_objective_leads(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "-C", td, "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "restart_self"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted((Path(td) / "out").glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            self.assertTrue(session.objective.startswith("Improve the console panel"))
            self.assertIn(roundtable.SELF_EDIT_NOTE, session.objective)
            self.assertIn("run `python3 -m unittest test_roundtable`", session.objective)

    def test_create_self_test_sandbox_copies_source_files(self):
        with tempfile.TemporaryDirectory() as workspace_dir, \
             tempfile.TemporaryDirectory() as output_dir:
            workspace = Path(workspace_dir)
            (workspace / "roundtable.py").write_text("print('rt')")
            (workspace / "test_roundtable.py").write_text("print('tests')")
            (workspace / "README.md").write_text("# Readme")
            (workspace / "AGENT_PROMPTS.md").write_text("# Prompts")
            sandbox = roundtable.create_self_test_sandbox(workspace, Path(output_dir))
            self.assertEqual(sandbox, Path(output_dir) / "self-test-sandbox")
            self.assertEqual((sandbox / "roundtable.py").read_text(), "print('rt')")
            self.assertEqual((sandbox / "test_roundtable.py").read_text(), "print('tests')")
            self.assertEqual((sandbox / "README.md").read_text(), "# Readme")
            self.assertEqual((sandbox / "AGENT_PROMPTS.md").read_text(), "# Prompts")

    def test_self_flag_suffixes_note_on_resumed_session(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            session = roundtable.Session("Initial request", td, 1, "2026-01-01T00:00:00Z", [])
            paths = roundtable.save_session(session, output_dir)
            argv = ["roundtable", "--resume", str(paths[0]), "--self", "--plain", "-r", "0",
                    "--mock", "Resumed follow-up request"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "restart_self"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted(output_dir.glob("*.json"))[-1]
            resumed_session = roundtable.load_session(saved)
            user_turns = [t for t in resumed_session.turns if t.speaker == "User"]
            self.assertEqual(len(user_turns), 1)
            last_turn = user_turns[0]
            self.assertTrue(last_turn.content.startswith("Resumed follow-up request"))
            self.assertIn(roundtable.SELF_EDIT_NOTE, last_turn.content)
            self.assertIn("run `python3 -m unittest test_roundtable`", last_turn.content)

    def test_create_self_test_sandbox_refreshes_on_repeat_calls(self):
        with tempfile.TemporaryDirectory() as workspace_dir, \
             tempfile.TemporaryDirectory() as output_dir:
            workspace = Path(workspace_dir)
            (workspace / "roundtable.py").write_text("version 1")
            roundtable.create_self_test_sandbox(workspace, Path(output_dir))
            (workspace / "roundtable.py").write_text("version 2")
            sandbox = roundtable.create_self_test_sandbox(workspace, Path(output_dir))
            self.assertEqual((sandbox / "roundtable.py").read_text(), "version 2")

    def test_create_self_test_sandbox_resolves_relative_output_from_launcher_cwd(self):
        with tempfile.TemporaryDirectory() as launcher_dir, \
             tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "roundtable.py").write_text("print('rt')")
            original_cwd = os.getcwd()
            try:
                os.chdir(launcher_dir)
                sandbox = roundtable.create_self_test_sandbox(workspace, Path(".roundtable"))
            finally:
                os.chdir(original_cwd)
            self.assertTrue(sandbox.is_absolute())
            self.assertEqual(sandbox, Path(launcher_dir) / ".roundtable" / "self-test-sandbox")
            self.assertEqual((sandbox / "roundtable.py").read_text(), "print('rt')")

    def test_create_self_test_sandbox_skips_missing_source_files(self):
        with tempfile.TemporaryDirectory() as workspace_dir, \
             tempfile.TemporaryDirectory() as output_dir:
            sandbox = roundtable.create_self_test_sandbox(Path(workspace_dir), Path(output_dir))
            self.assertTrue(sandbox.is_dir())
            self.assertFalse((sandbox / "roundtable.py").exists())

    def test_create_self_test_sandbox_removes_stale_files_when_source_missing(self):
        with tempfile.TemporaryDirectory() as workspace_dir, \
             tempfile.TemporaryDirectory() as output_dir:
            workspace = Path(workspace_dir)
            (workspace / "roundtable.py").write_text("print('rt')")
            (workspace / "README.md").write_text("# Keep then drop")
            sandbox = roundtable.create_self_test_sandbox(workspace, Path(output_dir))
            self.assertTrue((sandbox / "README.md").is_file())
            (workspace / "README.md").unlink()
            (workspace / "roundtable.py").write_text("print('rt2')")
            sandbox = roundtable.create_self_test_sandbox(workspace, Path(output_dir))
            self.assertEqual((sandbox / "roundtable.py").read_text(), "print('rt2')")
            self.assertFalse((sandbox / "README.md").exists())

    def test_self_flag_note_points_agents_at_a_real_sandbox_copy(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "--output-dir", str(output_dir)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "restart_self"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted(output_dir.glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            sandbox = output_dir / "self-test-sandbox"
            self.assertIn(str(sandbox), session.objective)
            self.assertIn("--mock --plain --skip-preflight", session.objective)
            self.assertIn("--synthesis-passes 1 -r 0", session.objective)
            self.assertTrue((sandbox / "roundtable.py").is_file())
            self.assertTrue((sandbox / "test_roundtable.py").is_file())
            self.assertTrue((sandbox / "README.md").is_file())

    def test_explicit_workspace_flag_overrides_self(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "-C", td, "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "restart_self"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted((Path(td) / "out").glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            self.assertEqual(Path(session.workspace), Path(td).resolve())

    def test_skip_preflight_flag_bypasses_check(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--skip-preflight"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "run_preflight") as mock_preflight, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
                mock_preflight.assert_not_called()

    def test_preflight_timeout_flag_propagates_to_run_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--preflight-timeout", "15.5"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "run_preflight") as mock_preflight, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
                mock_preflight.assert_called_once()
                self.assertEqual(mock_preflight.call_args[1].get("timeout"), 15.5)

    def test_preflight_timeout_rejects_nonpositive_and_nonfinite_values(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), mock.patch.object(
                    sys, "argv", ["roundtable", "Solve it", "--preflight-timeout", value]), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    roundtable.main()
                self.assertEqual(raised.exception.code, 2)

    def test_preflight_timeout_defaults_to_extended(self):
        # --extended-preflight defaults on: real agents with healthy but slow startup have been
        # observed exceeding the tighter 25s default with nothing actually wrong.
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "run_preflight") as mock_preflight, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
                self.assertEqual(mock_preflight.call_args[1].get("timeout"),
                                 roundtable.EXTENDED_PREFLIGHT_TIMEOUT_SECONDS)

    def test_no_extended_preflight_flag_uses_the_tighter_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--no-extended-preflight"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "run_preflight") as mock_preflight, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
                self.assertEqual(mock_preflight.call_args[1].get("timeout"),
                                 roundtable.DEFAULT_PREFLIGHT_TIMEOUT_SECONDS)

    def test_extended_preflight_flag_uses_the_longer_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--extended-preflight"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "run_preflight") as mock_preflight, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
                self.assertEqual(mock_preflight.call_args[1].get("timeout"),
                                 roundtable.EXTENDED_PREFLIGHT_TIMEOUT_SECONDS)

    def test_explicit_preflight_timeout_overrides_extended_flag(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--extended-preflight",
                    "--preflight-timeout", "12.5"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "run_preflight") as mock_preflight, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
                self.assertEqual(mock_preflight.call_args[1].get("timeout"), 12.5)

    def test_extended_preflight_flag_survives_restart_arguments(self):
        args = roundtable.build_parser().parse_args([
            "Goal", "--self", "--plain", "--mock", "-r", "0", "--extended-preflight",
            "--output-dir", "saved",
        ])
        command = roundtable.restart_arguments(args, Path("saved/session.json"), True)
        self.assertIn("--extended-preflight", command)
        self.assertNotIn("--no-extended-preflight", command)

    def test_no_extended_preflight_survives_restart_arguments(self):
        args = roundtable.build_parser().parse_args([
            "Goal", "--self", "--plain", "--mock", "-r", "0", "--no-extended-preflight",
            "--output-dir", "saved",
        ])
        command = roundtable.restart_arguments(args, Path("saved/session.json"), True)
        self.assertIn("--no-extended-preflight", command)
        self.assertNotIn("--extended-preflight", command)

    def test_preflight_timeout_survives_restart_arguments(self):
        args = roundtable.build_parser().parse_args([
            "Goal", "--self", "--plain", "--mock", "-r", "0", "--preflight-timeout", "12.5",
            "--output-dir", "saved",
        ])
        command = roundtable.restart_arguments(args, Path("saved/session.json"), False)
        self.assertIn("--preflight-timeout", command)
        self.assertEqual(command[command.index("--preflight-timeout") + 1], "12.5")

    def test_restart_arguments_preserve_run_configuration(self):
        args = roundtable.build_parser().parse_args([
            "Goal", "--self", "--plain", "--mock", "-r", "2", "-C", "custom_ws", "--collab", "mixed",
            "--synthesizer", "claude", "--synthesis-passes", "1", "--balance-load",
            "--task-status-check", "--reassign-idle", "--dead-code-check", "--elevated", "codex",
            "--codex-model", "model-a", "--reasoning-effort", "high", "--output-dir", "saved",
        ])
        command = roundtable.restart_arguments(args, Path("saved/session.json"), True)
        self.assertEqual(command[:2], [sys.executable, str(Path(roundtable.__file__).resolve())])
        self.assertIn("--continue-after-restart", command)
        self.assertIn("followup", command)
        for option in ("--plain", "--mock", "--balance-load", "--task-status-check",
                       "--reassign-idle", "--dead-code-check", "--skip-preflight",
                       "--extended-preflight"):
            self.assertIn(option, command)
        self.assertEqual(command[command.index("--collab") + 1], "mixed")
        self.assertEqual(command[command.index("--synthesizer") + 1], "claude")
        self.assertEqual(command[command.index("--synthesis-passes") + 1], "1")
        self.assertEqual(command[command.index("--codex-model") + 1], "model-a")
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "high")
        self.assertEqual(command[command.index("--elevated") + 1], "codex")
        self.assertEqual(command[command.index("--rounds") + 1], "2")
        self.assertEqual(command[command.index("--workspace") + 1], "custom_ws")
        self.assertIn("--self", command)

    def test_restart_arguments_omits_self_flag_when_self_is_false(self):
        args = roundtable.build_parser().parse_args([
            "Goal", "--plain", "--mock", "-r", "0", "--output-dir", "saved",
        ])
        command = roundtable.restart_arguments(args, Path("saved/session.json"), False)
        self.assertNotIn("--self", command)

    def test_source_fingerprint_changes_when_file_content_changes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.py"
            path.write_text("a = 1\n")
            first = roundtable.source_fingerprint(path)
            self.assertEqual(first, roundtable.source_fingerprint(path))
            path.write_text("a = 2\n")
            self.assertNotEqual(first, roundtable.source_fingerprint(path))

    def test_source_fingerprint_missing_file_returns_empty_digest(self):
        missing = Path(tempfile.gettempdir()) / "roundtable-missing-source-does-not-exist.py"
        self.assertFalse(missing.exists())
        self.assertEqual(roundtable.source_fingerprint(missing), "")

    def test_self_checkpoint_disabled_never_raises(self):
        check = roundtable.self_checkpoint(False)
        check()  # no-op regardless of any real file changes

    def test_self_checkpoint_enabled_raises_once_source_changes(self):
        with tempfile.TemporaryDirectory() as td:
            fake_source = Path(td) / "roundtable.py"
            fake_source.write_text("a = 1\n")
            with mock.patch.object(roundtable.Path, "resolve", return_value=fake_source):
                check = roundtable.self_checkpoint(True)
                check()  # unchanged: no raise
                fake_source.write_text("a = 2\n")
                with self.assertRaises(roundtable.SelfRestartRequired):
                    check()

    def test_self_checkpoint_raises_when_source_becomes_unreadable(self):
        with tempfile.TemporaryDirectory() as td:
            fake_source = Path(td) / "roundtable.py"
            fake_source.write_text("a = 1\n")
            with mock.patch.object(roundtable.Path, "resolve", return_value=fake_source):
                check = roundtable.self_checkpoint(True)
                check()
                fake_source.unlink()
                with self.assertRaises(roundtable.SelfRestartRequired):
                    check()

    def test_self_edit_during_synthesis_checkpoints_completed_consensus(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                "Improve restart behavior", td, 0, "2026-01-01T00:00:00+00:00", [])
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            checkpoints = 0

            def changed_during_synthesis():
                nonlocal checkpoints
                checkpoints += 1
                if checkpoints == 2:
                    raise roundtable.SelfRestartRequired

            with self.assertRaises(roundtable.SelfRestartRequired):
                roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                                   checkpoint=changed_during_synthesis)

            self.assertTrue(session.final)
            self.assertEqual([turn.phase for turn in session.turns if turn.speaker == "Final"],
                             ["consensus"])

    def test_restart_continuation_skips_completed_consensus(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                "Improve restart behavior", td, 0, "2026-01-01T00:00:00+00:00",
                [roundtable.Turn("Codex", "proposal", "done"),
                 roundtable.Turn("Final", "consensus", "Completed\n\nDone")],
                "Completed\n\nDone")
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]

            with mock.patch.object(roundtable, "synthesize",
                                   side_effect=AssertionError("consensus ran twice")):
                roundtable.conduct(
                    session, *agents, lambda *_: None, lambda *_: None,
                    completed_phases={"proposal", "consensus"},
                )

            self.assertEqual([turn.phase for turn in session.turns if turn.speaker == "Final"],
                             ["consensus"])
            self.assertEqual(session.final, "Completed\n\nDone")

    def test_conduct_restart_vote_majority_now_restarts_at_review_round_1(self):
        """A --self session that changes source during the proposal phase defers the restart into
        review round 1 so agents can vote, instead of restarting immediately and silently."""
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                f"Improve gui\n\n{roundtable.SELF_EDIT_NOTE}", td, 1, "now", [])
            votes = {"Codex": "now", "Claude": "now", "Antigravity": "now",
                    "Aider": "later", "Grok": "later", "Qwen": "now"}
            agents = {name: RestartVotingAgent(name, Path(td), votes[name])
                     for name in roundtable.AGENT_NAMES}
            checkpoint = mock.Mock(side_effect=roundtable.SelfRestartRequired)
            status_messages: list[str] = []

            def capture_status(_active, message: str) -> None:
                status_messages.append(message)

            with mock.patch.object(
                    roundtable, "source_fingerprint", side_effect=["baseline", "changed"]):
                with self.assertRaises(roundtable.SelfRestartRequired):
                    roundtable.conduct(
                        session, *(agents[name] for name in roundtable.AGENT_NAMES),
                        lambda *_: None, capture_status, checkpoint=checkpoint)
            codex_prompts = agents["Codex"].received_prompts
            self.assertEqual(len(codex_prompts), 2)
            self.assertNotIn("RESTART:", codex_prompts[0])  # proposal: nothing pending yet
            self.assertIn("`RESTART: now`", codex_prompts[1])  # review 1: vote requested
            checkpoint.assert_called_once()
            tally_lines = [m for m in status_messages if m.startswith("RESTART vote:")]
            self.assertEqual(len(tally_lines), 1)
            self.assertIn("now=4 later=2 → restarting now", tally_lines[0])
            self.assertIn("now: Codex, Claude, Antigravity, Qwen", tally_lines[0])
            self.assertIn("later: Aider, Grok", tally_lines[0])

    def test_conduct_restart_vote_majority_later_defers_one_more_round(self):
        """A 'later' majority gets exactly one grace phase: round 2 runs with no further vote hint,
        and its ordinary checkpoint restarts for real regardless of round 1's outcome."""
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                f"Improve gui\n\n{roundtable.SELF_EDIT_NOTE}", td, 2, "now", [])
            votes = {"Codex": "later", "Claude": "later", "Antigravity": "later",
                    "Aider": "now", "Grok": "now", "Qwen": "later"}
            agents = {name: RestartVotingAgent(name, Path(td), votes[name])
                     for name in roundtable.AGENT_NAMES}
            checkpoint = mock.Mock(side_effect=roundtable.SelfRestartRequired)
            with mock.patch.object(
                    roundtable, "source_fingerprint", side_effect=["baseline", "changed"]):
                with self.assertRaises(roundtable.SelfRestartRequired):
                    roundtable.conduct(
                        session, *(agents[name] for name in roundtable.AGENT_NAMES),
                        lambda *_: None, lambda *_: None, checkpoint=checkpoint)
            codex_prompts = agents["Codex"].received_prompts
            self.assertEqual(len(codex_prompts), 3)  # proposal, review 1 (vote), review 2 (no vote)
            self.assertNotIn("`RESTART: now`", codex_prompts[0])
            self.assertIn("`RESTART: now`", codex_prompts[1])  # review 1: the hint itself
            # review 2's prompt naturally quotes round 1's votes as transcript history; only the
            # *hint* asking for a fresh vote must be absent, not the bare marker text.
            self.assertNotIn("`RESTART: now`", codex_prompts[2])
            checkpoint.assert_called_once()

    def test_conduct_restart_vote_majority_later_with_no_further_round_forces_restart(self):
        """A 'later' vote at the only scheduled review round still restarts afterward -- the grace
        period is bounded even when there is no round 2 to force it naturally."""
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                f"Improve gui\n\n{roundtable.SELF_EDIT_NOTE}", td, 1, "now", [])
            agents = {name: RestartVotingAgent(name, Path(td), "later")
                     for name in roundtable.AGENT_NAMES}
            checkpoint = mock.Mock(side_effect=roundtable.SelfRestartRequired)
            with mock.patch.object(
                    roundtable, "source_fingerprint", side_effect=["baseline", "changed"]):
                with self.assertRaises(roundtable.SelfRestartRequired):
                    roundtable.conduct(
                        session, *(agents[name] for name in roundtable.AGENT_NAMES),
                        lambda *_: None, lambda *_: None, checkpoint=checkpoint)
            checkpoint.assert_called_once()

    def test_conduct_restart_vote_skipped_for_a_non_self_session(self):
        """An ordinary (non --self) session must keep the original immediate, unconditional restart
        -- no vote hint, no deferral -- even if source_fingerprint happens to differ."""
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Improve gui", td, 1, "now", [])
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            checkpoint = mock.Mock(side_effect=roundtable.SelfRestartRequired)
            with mock.patch.object(
                    roundtable, "source_fingerprint", side_effect=["baseline", "changed"]):
                with self.assertRaises(roundtable.SelfRestartRequired):
                    roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                                       checkpoint=checkpoint)
            checkpoint.assert_called_once()

    def test_conduct_restart_vote_skipped_with_zero_review_rounds(self):
        """With no review round to defer into, a --self session restarts immediately, same as the
        non-self case -- there is nothing to vote in."""
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                f"Improve gui\n\n{roundtable.SELF_EDIT_NOTE}", td, 0, "now", [])
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            checkpoint = mock.Mock(side_effect=roundtable.SelfRestartRequired)
            with mock.patch.object(
                    roundtable, "source_fingerprint", side_effect=["baseline", "changed"]):
                with self.assertRaises(roundtable.SelfRestartRequired):
                    roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                                       checkpoint=checkpoint)
            checkpoint.assert_called_once()

    def test_conduct_restart_vote_no_op_when_source_unchanged(self):
        """No change detected: ordinary checkpoint() runs as always, no vote hint anywhere."""
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                f"Improve gui\n\n{roundtable.SELF_EDIT_NOTE}", td, 1, "now", [])
            agents = {name: RestartVotingAgent(name, Path(td), "now")
                     for name in roundtable.AGENT_NAMES}
            checkpoint = mock.Mock()
            with mock.patch.object(
                    roundtable, "source_fingerprint", side_effect=["same", "same", "same"]):
                roundtable.conduct(
                    session, *(agents[name] for name in roundtable.AGENT_NAMES),
                    lambda *_: None, lambda *_: None, checkpoint=checkpoint)
            for prompt in agents["Codex"].received_prompts:
                self.assertNotIn("RESTART:", prompt)
            # Ordinary, unconditional checkpoints: after proposal, after review 1, and after
            # synthesis -- none of them gated on a vote since nothing changed.
            self.assertEqual(checkpoint.call_count, 3)

    def test_apply_option_key_toggles_by_number_and_enter_confirms(self):
        values = {"elevated": False, "balance_load": False, "task_status_check": False}
        values, cursor, done = roundtable.apply_option_key(values, "1", 0)
        self.assertFalse(done)
        self.assertEqual(cursor, 0)
        self.assertTrue(values["elevated"])
        values, cursor, done = roundtable.apply_option_key(values, "1", cursor)
        self.assertFalse(values["elevated"])
        out_of_range = str(len(roundtable.OPTION_TOGGLES) + 1)
        values, cursor, done = roundtable.apply_option_key(values, out_of_range, cursor)  # no-op
        self.assertFalse(done)
        self.assertEqual(values, {"elevated": False, "balance_load": False,
                                  "task_status_check": False})
        values, cursor, done = roundtable.apply_option_key(values, "\n", cursor)
        self.assertTrue(done)

    def test_apply_option_key_toggles_skip_preflight(self):
        values = {"elevated": False, "balance_load": False, "task_status_check": False, "self": False, "skip_preflight": False}
        values, cursor, done = roundtable.apply_option_key(values, "5", 0)
        self.assertFalse(done)
        self.assertEqual(cursor, 4)
        self.assertTrue(values["skip_preflight"])

    def test_apply_option_key_escape_and_q_confirm_without_toggling(self):
        values = {"elevated": True, "balance_load": False, "task_status_check": False}
        result, cursor, done = roundtable.apply_option_key(values, "\x1b", 0)
        self.assertTrue(done)
        self.assertEqual(result, values)
        result, cursor, done = roundtable.apply_option_key(values, "q", 0)
        self.assertTrue(done)
        self.assertEqual(result, values)

    def test_apply_option_key_cursor_navigation_and_space_toggle(self):
        defaults = {name: False for name, _ in roundtable.OPTION_TOGGLES}
        # Start at cursor 0 ("elevated"), navigate down to cursor 1 ("balance_load")
        values, cursor, done = roundtable.apply_option_key(defaults, roundtable.curses.KEY_DOWN, 0)
        self.assertEqual(cursor, 1)
        self.assertFalse(done)
        # Toggle option at cursor 1 ("balance_load") with Space key
        values, cursor, done = roundtable.apply_option_key(values, " ", cursor)
        self.assertTrue(values["balance_load"])
        self.assertEqual(cursor, 1)
        # Navigate up with 'k' to wraparound or previous
        values, cursor, done = roundtable.apply_option_key(values, "k", cursor)
        self.assertEqual(cursor, 0)

    def test_options_summary_counts_enabled_flags(self):
        values = {name: False for name, _ in roundtable.OPTION_TOGGLES}
        self.assertEqual(roundtable.options_summary(values), "none on")
        values["elevated"] = True
        values["debug"] = True
        self.assertEqual(roundtable.options_summary(values), "2 on")

    def test_objective_editor_stats_reports_chars_and_lines(self):
        self.assertEqual(roundtable.objective_editor_stats(""), "empty")
        self.assertEqual(roundtable.objective_editor_stats("x"), "1 char · 1 line")
        self.assertEqual(roundtable.objective_editor_stats("ab\ncd"), "5 chars · 2 lines")

    def test_read_options_ui_draws_summary_checkboxes_and_continue(self):
        """Options chrome: header count, checkbox glyphs, reverse focus, Continue button."""
        screen = FakeScreen(30, 100)
        screen.get_wch = lambda: "\n"  # continue immediately after first paint
        defaults = {name: False for name, _ in roundtable.OPTION_TOGGLES}
        defaults["elevated"] = True
        with mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable.curses, "color_pair", return_value=0):
            result = roundtable.read_options_ui(screen, defaults)
        text = screen.text()
        self.assertTrue(result["elevated"])
        self.assertIn("1 on", text)
        self.assertIn("☑", text)
        self.assertIn("☐", text)
        self.assertIn("Continue", text)
        self.assertIn("OPTIONS", text)

    def test_agent_panel_title_shows_scroll_offset_cue(self):
        """Agent panes get the same · ↑N title cue as Console/Final when scrolled back."""
        long = "\n".join(f"line {i}" for i in range(40))
        display = make_test_display(
            h=40, w=120,
            turns=[roundtable.Turn("Codex", "proposal", long)],
        )
        display.scroll["Codex"] = 12
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        # Expanded? No — compact panel title should still show the cue.
        self.assertIn("↑", display.s.text())
        # Clamp may shrink the requested offset; whatever is kept must appear in the title band.
        self.assertGreater(display.scroll["Codex"], 0)
        self.assertIn(f"↑{display.scroll['Codex']}", display.s.text())

    def test_scrolled_agent_marks_new_live_activity_until_returning_to_tail(self):
        display = make_test_display(h=30, w=140)
        display.busy = True
        display.active = {"Codex"}
        display.scroll["Codex"] = 2
        display.unread = {name: 0 for name in display.scroll}
        display.work_activity["Codex"].extend(
            f"Reading older-{index}.py" for index in range(12))

        with mock.patch.object(display, "draw"), mock.patch.object(display, "poll_input"):
            display.tick("Codex", "Reading file roundtable.py")

        self.assertEqual(display.unread["Codex"], 1)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0):
            display._agent_panel(5, 1, 12, 50, "Codex", "◇", "OpenAI coding agent", 0)
        self.assertIn("+1 new", "".join(display.s.grid[5]))
        display._agent_panel(18, 1, 10, 22, "Codex", "◇", "OpenAI coding agent", 0)
        self.assertIn("+1 new", "".join(display.s.grid[18]))

        display.focused_panel = "Codex"
        with mock.patch.object(display, "draw"):
            self.assertTrue(display.scroll_expanded(roundtable.curses.KEY_END))
        self.assertEqual(display.scroll["Codex"], 0)
        self.assertEqual(display.unread["Codex"], 0)

    def test_scrolled_console_counts_only_events_visible_in_current_filter(self):
        display = make_test_display(h=48, w=160)
        display.scroll["Console"] = 2
        display.unread = {name: 0 for name in display.scroll}
        display.console_filter = 0  # key events; raw tick chatter is hidden

        display.log("raw provider chatter", kind="tick")
        display.log("phase changed", kind="phase")
        self.assertEqual(display.unread["Console"], 1)

        display.focused_panel = "Console"
        with mock.patch.object(display, "draw"):
            display.scroll_expanded(roundtable.curses.KEY_END)
        self.assertEqual(display.unread["Console"], 0)

    def test_console_filter_change_clears_filter_specific_unread_count(self):
        display = make_test_display(h=48, w=160)
        display.unread = {name: 0 for name in display.scroll}
        display.unread["Console"] = 3
        with mock.patch.object(display, "draw"):
            display.cycle_console_filter()
        self.assertEqual(display.console_filter, 1)
        self.assertEqual(display.unread["Console"], 0)

    def test_agent_panel_subtitle_shows_latest_dibs_claim(self):
        """Panel subtitle swaps the static lab label for the agent's latest DIBS claim."""
        display = make_test_display(
            h=40, w=160,
            turns=[
                roundtable.Turn("Codex", "proposal", "DIBS: the auth flow\nImplemented auth."),
                roundtable.Turn("Claude", "proposal", "DIBS: the docs\nWrote README notes."),
                # Later turn overrides Codex's claim; Claude's first claim remains.
                roundtable.Turn("Codex", "review 1", "DIBS: the retry logic\nFixed backoff."),
            ],
        )
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("DIBS: the retry logic", rendered)
        self.assertIn("DIBS: the docs", rendered)
        self.assertNotIn("DIBS: the auth flow", rendered)
        # Agents without a claim keep the static lab subtitle.
        self.assertIn("xAI coding agent", rendered)

    def test_expanded_agent_panel_subtitle_shows_dibs_claim(self):
        """Expanded agent view uses the same DIBS subtitle as the compact pane."""
        display = make_test_display(
            h=40, w=120,
            turns=[roundtable.Turn("Grok", "proposal", "DIBS: panel dibs display\nLanded it.")],
        )
        display.expanded = "Grok"
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        self.assertIn("DIBS: panel dibs display", display.s.text())

    def test_read_options_ui_mouse_click_toggles_option_and_continues(self):
        screen = FakeScreen(30, 80)
        events = [
            (roundtable.curses.KEY_MOUSE, (0, 3, 4, 0, roundtable.curses.BUTTON1_CLICKED)),
            (roundtable.curses.KEY_MOUSE, (0, 3, 5 + len(roundtable.OPTION_TOGGLES), 0, roundtable.curses.BUTTON1_CLICKED)),
        ]
        def fake_get_wch():
            key, mouse_evt = events.pop(0)
            screen._mouse_event = mouse_evt
            return key

        screen.get_wch = fake_get_wch
        defaults = {name: False for name, _ in roundtable.OPTION_TOGGLES}
        with mock.patch.object(roundtable.curses, "getmouse", side_effect=lambda: screen._mouse_event):
            result = roundtable.read_options_ui(screen, defaults)
        self.assertTrue(result["elevated"])

    def test_read_options_ui_ctrl_c_raises_keyboard_interrupt(self):
        """Belt-and-suspenders fallback matching Display.poll_input's `key == 3` check: if this
        terminal doesn't deliver Ctrl-C as a real SIGINT while blocked in get_wch(), the byte
        itself must still cancel instead of being silently swallowed as ordinary input."""
        screen = FakeScreen(30, 80)
        screen.get_wch = lambda: "\x03"
        defaults = {name: False for name, _ in roundtable.OPTION_TOGGLES}
        with self.assertRaises(KeyboardInterrupt):
            roundtable.read_options_ui(screen, defaults)

    def test_read_objective_ui_ctrl_c_raises_keyboard_interrupt(self):
        screen = FakeScreen(30, 80)
        screen.get_wch = lambda: "\x03"
        screen.move = lambda *_args: None
        with mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             self.assertRaises(KeyboardInterrupt):
            roundtable.read_objective_ui(screen, Path("/tmp"))

    def test_read_followup_ui_ctrl_c_raises_keyboard_interrupt(self):
        display = make_test_display()
        display.draw_followup = lambda editor: None
        display.s.get_wch = lambda: "\x03"
        display.s.nodelay = lambda *_args: None
        with self.assertRaises(KeyboardInterrupt):
            roundtable.read_followup_ui(display.s, display)

    def test_wait_for_agent_availability_formats_reset_time_portably(self):
        agent = roundtable.MockAgent("Codex", Path("/tmp"))
        ticks = []
        cancel = threading.Event()
        now = datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
        detail = "resets 1:30pm (UTC)"
        # Production deliberately waits through the cancellation event rather than time.sleep so
        # Ctrl-C can wake it immediately. Bypass that primitive without changing the code path.
        with mock.patch.object(cancel, "wait", return_value=False):
            roundtable._wait_for_agent_availability(
                agent, ticks.append, cancel, detail=detail, clock=lambda: now
            )
        self.assertTrue(any("waiting until 1:30PM" in t for t in ticks))

    def test_main_options_screen_only_overrides_toggles_that_were_actually_flipped(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--mock", "-C", td, "--elevated", "codex"]
            captured = {}

            def fake_wrapper(func, *wrapper_args, **_):
                if func is roundtable.read_options_ui:
                    defaults = wrapper_args[0]
                    captured["defaults"] = defaults
                    # Only override the toggle(s) actually meant to flip in this test; every other
                    # key rides through unchanged, so this test doesn't rot as OPTION_TOGGLES grows.
                    result = dict(defaults)
                    result["balance_load"] = True
                    return result
                if func is roundtable.run_tui:
                    captured["args"] = wrapper_args[0]
                    return 0
                raise AssertionError(f"unexpected curses.wrapper target: {func}")

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(roundtable.curses, "wrapper", side_effect=fake_wrapper):
                self.assertEqual(roundtable.main(), 0)
            # Check by key rather than the whole dict, so this doesn't need editing every time a new
            # toggle is added — test_every_store_true_opt_in_flag_has_a_matching_options_screen_toggle
            # already guards that every toggle is present at all.
            self.assertEqual(captured["defaults"]["elevated"], True)
            self.assertFalse(captured["defaults"]["balance_load"])
            self.assertFalse(captured["defaults"]["task_status_check"])
            self.assertFalse(captured["defaults"]["self"])
            result_args = captured["args"]
            self.assertEqual(result_args.elevated, ["codex"])  # left untouched, so preserved as-is
            self.assertTrue(result_args.balance_load)  # toggled on in the fake screen
            self.assertFalse(result_args.self)  # left untouched
            self.assertFalse(result_args.task_status_check)

    def test_main_reports_a_clean_cancel_from_the_options_screen(self):
        """Ctrl-C during the pre-session options screen has no session/run_log to fall back on
        (both are built later); main() must still exit 130 with a plain message instead of letting
        the exception escape to __main__ as a raw traceback."""
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--mock", "-C", td]

            def fake_wrapper(func, *_wrapper_args, **_):
                if func is roundtable.read_options_ui:
                    raise KeyboardInterrupt
                raise AssertionError(f"unexpected curses.wrapper target: {func}")

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(roundtable.curses, "wrapper", side_effect=fake_wrapper), \
                 contextlib.redirect_stderr(io.StringIO()) as captured_stderr:
                self.assertEqual(roundtable.main(), 130)
            self.assertIn("Cancelled", captured_stderr.getvalue())

    def test_main_reports_a_clean_cancel_from_the_objective_screen(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "--mock", "-C", td, "--skip-preflight"]

            def fake_wrapper(func, *_wrapper_args, **_):
                if func is roundtable.read_options_ui:
                    return {name: False for name, _ in roundtable.OPTION_TOGGLES}
                if func is roundtable.read_objective_ui:
                    raise KeyboardInterrupt
                raise AssertionError(f"unexpected curses.wrapper target: {func}")

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(roundtable.curses, "wrapper", side_effect=fake_wrapper), \
                 contextlib.redirect_stderr(io.StringIO()) as captured_stderr:
                self.assertEqual(roundtable.main(), 130)
            self.assertIn("Cancelled", captured_stderr.getvalue())

    def test_main_skips_the_options_toggle_screen_on_a_self_restart_continuation(self):
        """restart_arguments() already rebuilds the full invocation from the flags the user chose
        before the source changed, so re-showing the interactive toggle screen on the resulting
        --continue-after-restart relaunch would stop an otherwise-unattended --self restart on
        input the user has no way to know it's waiting for."""
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Task", td, 1, "now",
                                         [roundtable.Turn("Codex", "proposal", "Partial work")])
            session_path, _ = roundtable.save_session(session, Path(td))
            argv = ["roundtable", "--resume", str(session_path),
                    "--continue-after-restart", "initial", "--self", "--mock",
                    "--skip-preflight"]
            captured = {}

            def fake_wrapper(func, *wrapper_args, **_):
                if func is roundtable.read_options_ui:
                    raise AssertionError("toggle screen must not run on a --self restart")
                if func is roundtable.run_tui:
                    captured["args"] = wrapper_args[0]
                    return 0
                raise AssertionError(f"unexpected curses.wrapper target: {func}")

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(roundtable.curses, "wrapper", side_effect=fake_wrapper):
                self.assertEqual(roundtable.main(), 0)
            self.assertTrue(captured["args"].self)

    def test_tui_mode_prints_transcript_and_log_paths_after_completion(self):
        """run_tui can't print while curses still owns the terminal, so main() must do it once
        curses.wrapper has restored the screen -- otherwise a fullscreen run leaves the operator
        with no on-screen record of where the transcript/log ended up after the alt-screen clears."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"

            def fake_wrapper(func, *wrapper_args, **_):
                if func is roundtable.read_options_ui:
                    return dict(wrapper_args[0])
                if func is roundtable.run_tui:
                    # Mirrors run_tui's real order: the RunLog is opened before any turn happens,
                    # so by the time a transcript exists on disk, its paired .log already does too.
                    session = wrapper_args[1]
                    run_log = roundtable.RunLog(roundtable.log_path_for(session, out))
                    session.final = "Done"
                    session.turns.append(roundtable.Turn("Final", "consensus", "Done"))
                    roundtable.save_session(session, out)
                    run_log.close()
                    return 0
                raise AssertionError(f"unexpected curses.wrapper target: {func}")

            argv = ["roundtable", "Solve it", "--mock", "-C", td, "--output-dir", str(out)]
            stdout = io.StringIO()
            # redirect_stdout must be entered before the sys.stdout.isatty patch below -- patching
            # sys.stdout.isatty first would attach to the real stdout, and the redirected StringIO
            # (whose own isatty() defaults to False) would then push main() onto the --plain path.
            with contextlib.redirect_stdout(stdout), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(roundtable.curses, "wrapper", side_effect=fake_wrapper):
                self.assertEqual(roundtable.main(), 0)
            printed = stdout.getvalue()
            transcript = next(out.glob("*.md"))
            log = next(out.glob("*.log"))
            self.assertIn(f"Transcript: {transcript}", printed)
            self.assertIn(f"Log: {log}", printed)

    def test_tui_mode_prints_nothing_extra_when_no_session_was_ever_saved(self):
        """A cancel before the first save (handled inside run_tui itself, e.g. Ctrl-C on the
        objective screen) must not have main() print paths to files that don't exist."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"

            def fake_wrapper(func, *wrapper_args, **_):
                if func is roundtable.read_options_ui:
                    return dict(wrapper_args[0])
                if func is roundtable.run_tui:
                    return 130
                raise AssertionError(f"unexpected curses.wrapper target: {func}")

            argv = ["roundtable", "Solve it", "--mock", "-C", td, "--output-dir", str(out)]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(roundtable.curses, "wrapper", side_effect=fake_wrapper):
                self.assertEqual(roundtable.main(), 130)
            self.assertEqual(stdout.getvalue(), "")

    def test_list_agents_reports_found_and_missing_clis(self):
        """list_agents() must reflect real shutil.which lookups for every AGENT_EXECUTABLES entry,
        using the same mapping verify_clis/log_run_context rely on so they can't drift apart."""
        def fake_which(executable):
            return f"/usr/bin/{executable}" if executable in ("codex", "claude") else None

        with mock.patch.object(roundtable.shutil, "which", side_effect=fake_which):
            report = roundtable.list_agents()
        lines = report.splitlines()
        self.assertEqual(len(lines), len(roundtable.AGENT_NAMES))
        self.assertIn("Codex", lines[0])
        self.assertIn("/usr/bin/codex", lines[0])
        claude_line = next(line for line in lines if line.startswith("Claude"))
        self.assertIn("/usr/bin/claude", claude_line)
        aider_line = next(line for line in lines if line.startswith("Aider"))
        self.assertIn("not found", aider_line)

    def test_main_list_agents_flag_prints_and_exits_without_starting_a_session(self):
        """--list-agents must be a pure query: no curses, no preflight, no workspace/session setup --
        it should work even without a TTY, unlike the interactive options screen."""
        argv = ["roundtable", "--list-agents"]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), \
             mock.patch.object(roundtable, "verify_clis") as verify_mock, \
             mock.patch.object(roundtable.curses, "wrapper") as wrapper_mock:
            self.assertEqual(roundtable.main(), 0)
        verify_mock.assert_not_called()
        wrapper_mock.assert_not_called()
        printed = stdout.getvalue()
        for name in roundtable.AGENT_NAMES:
            self.assertIn(name, printed)

    def test_verify_clis_uses_the_canonical_agent_executables_mapping(self):
        """Missing-CLI errors must name the actual executables from AGENT_EXECUTABLES, not a
        second hardcoded list that could silently fall out of sync with it."""
        def fake_which(executable):
            return None if executable == "aider" else f"/usr/bin/{executable}"

        with mock.patch.object(roundtable.shutil, "which", side_effect=fake_which):
            with self.assertRaises(SystemExit) as cm:
                roundtable.verify_clis(mock=False)
        self.assertIn("aider", str(cm.exception))

    def test_agent_commands_use_the_canonical_executable_mapping(self):
        """The roster advertised in prompts and preflight must be the roster actually launched."""
        original = dict(roundtable.AGENT_EXECUTABLES)
        try:
            for name in roundtable.AGENT_NAMES:
                roundtable.AGENT_EXECUTABLES[name] = f"test-{original[name]}"
                agent = roundtable.Agent(name, Path("/tmp"))
                self.assertEqual(agent.command("Solve this")[0], f"test-{original[name]}")
                roundtable.AGENT_EXECUTABLES[name] = original[name]
        finally:
            roundtable.AGENT_EXECUTABLES.clear()
            roundtable.AGENT_EXECUTABLES.update(original)

    def test_agent_roster_stays_aligned_across_names_executables_ui_and_spinners(self):
        """The six AIs must agree everywhere they are named: display order, CLI map, TUI panels,
        and spinner keys. Adding a seventh agent without updating all of these is a real bug."""
        names = roundtable.AGENT_NAMES
        self.assertEqual(names, ("Codex", "Claude", "Antigravity", "Aider", "Grok", "Qwen"))
        self.assertEqual(tuple(roundtable.AGENT_EXECUTABLES), names)
        self.assertEqual(
            dict(roundtable.AGENT_EXECUTABLES),
            {"Codex": "codex", "Claude": "claude", "Antigravity": "agy",
             "Aider": "aider", "Grok": "grok", "Qwen": "qwen"},
        )
        self.assertEqual(roundtable.Display.PANEL_NAMES[:6], names)
        self.assertEqual(set(roundtable.AGENT_SPINNERS), set(names))
        self.assertEqual(len(roundtable.ROLE_HINTS_BY_SLOT), len(names))
        self.assertEqual(set(roundtable.role_hints_for("roster-lock")), set(names))

    def test_roster_awareness_names_self_and_every_peer_from_canonical_maps(self):
        """Each roster agent must know its own name/CLI and every peer; non-agents get nothing."""
        for speaker in roundtable.AGENT_NAMES:
            note = roundtable.roster_awareness(speaker)
            self.assertIn(f"You are {speaker}", note)
            self.assertIn(f"`{roundtable.AGENT_EXECUTABLES[speaker]}`", note)
            self.assertIn(f"one of {len(roundtable.AGENT_NAMES)} members", note)
            for peer in roundtable.AGENT_NAMES:
                if peer == speaker:
                    continue
                self.assertIn(peer, note)
                self.assertIn(f"`{roundtable.AGENT_EXECUTABLES[peer]}`", note)
        self.assertEqual(roundtable.roster_awareness("User"), "")
        self.assertEqual(roundtable.roster_awareness("Final"), "")
        self.assertEqual(roundtable.roster_awareness(""), "")

    def test_prompt_for_and_reassignment_inject_roster_self_awareness(self):
        """Working turns must tell agents who they are and who else sits at the table so
        discovery of the other AIs does not require re-reading roundtable.py every turn."""
        for speaker in roundtable.AGENT_NAMES:
            prompt = roundtable.prompt_for("Goal", [], "proposal", speaker)
            self.assertIn(f"You are {speaker}", prompt)
            self.assertIn(f"`{roundtable.AGENT_EXECUTABLES[speaker]}`", prompt)
            for peer in roundtable.AGENT_NAMES:
                if peer != speaker:
                    self.assertIn(peer, prompt)
            reassigned = roundtable.reassignment_prompt(
                "Goal", [], "proposal", speaker, {"Claude"} if speaker != "Claude" else {"Codex"})
            self.assertIn(f"You are {speaker}", reassigned)
        # Non-roster speakers keep SYSTEM_BRIEF but do not get the roster block.
        user_prompt = roundtable.prompt_for("Goal", [], "proposal", "User")
        self.assertNotIn("You are User", user_prompt)
        self.assertNotIn("Do not invent additional members", user_prompt)
        # With no speaker named, final/refine stay prose-only (backward-compatible public API).
        final = roundtable.final_prompt("Goal", [])
        self.assertNotIn("Do not invent additional members", final)

    def test_named_synthesis_and_dead_code_prompts_keep_roster_self_awareness(self):
        """Role changes must not make a known agent lose the identity block after review."""
        prompts = (
            roundtable.final_prompt("Goal", [], speaker="Claude"),
            roundtable.refine_prompt("Goal", [], "Draft", speaker="Grok"),
            roundtable.dead_code_check_prompt("Goal", [], speaker="Qwen"),
        )
        for prompt, speaker in zip(prompts, ("Claude", "Grok", "Qwen")):
            self.assertIn(f"You are {speaker}", prompt)
            self.assertIn("Do not invent additional members", prompt)
        # Direct/public helper use remains backward-compatible when no executing agent is known.
        self.assertNotIn("Do not invent additional members", roundtable.final_prompt("Goal", []))

    def test_synthesize_and_dead_code_check_pass_speaker_into_logged_prompts(self):
        """Call-site wiring: synthesis/dead-code must inject roster identity for the agent
        actually running the turn, not only when helpers are called with speaker= in unit tests.
        A regression that drops speaker=name at the call site would re-blind the agent mid-role-
        change while the helper-level tests above still pass."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session(
                "Solve it", td, 0, "now",
                [roundtable.Turn("Codex", "proposal", "Initial idea")])
            order = [
                ("Claude", roundtable.MockAgent("Claude", workspace)),
                ("Grok", roundtable.MockAgent("Grok", workspace)),
            ]
            logged: list[tuple[str, str]] = []
            roundtable.synthesize(
                session, order, lambda *_: None, lambda *_: None,
                log_prompt=lambda name, prompt: logged.append((name, prompt)))
        self.assertEqual([name for name, _ in logged], ["Claude", "Grok"])
        draft_prompt = logged[0][1]
        refine_prompt = logged[1][1]
        self.assertIn("You are Claude", draft_prompt)
        self.assertIn("`claude`", draft_prompt)
        self.assertIn("Grok", draft_prompt)
        self.assertIn("`grok`", draft_prompt)
        self.assertIn("Do not invent additional members", draft_prompt)
        self.assertIn("You are Grok", refine_prompt)
        self.assertIn("`grok`", refine_prompt)
        self.assertIn("Claude", refine_prompt)
        # Refiner must not be told it is the drafter.
        self.assertNotIn("You are Claude", refine_prompt)

        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0, "now", [])
            agent = roundtable.MockAgent("Qwen", Path(td))
            logged = []
            roundtable.run_dead_code_check(
                session, "Qwen", agent, lambda *_: None, lambda *_: None,
                log_prompt=lambda name, prompt: logged.append((name, prompt)))
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0][0], "Qwen")
        self.assertIn("You are Qwen", logged[0][1])
        self.assertIn("`qwen`", logged[0][1])
        self.assertIn("Do not invent additional members", logged[0][1])

    def test_roster_awareness_edge_cases_for_tiny_rosters(self):
        """Sole-member and two-member wording must stay grammatical if the roster shrinks in tests
        or a future fork; production always has six agents but the helper is table-driven."""
        real_names = roundtable.AGENT_NAMES
        real_execs = dict(roundtable.AGENT_EXECUTABLES)
        try:
            roundtable.AGENT_NAMES = ("Solo",)
            roundtable.AGENT_EXECUTABLES.clear()
            roundtable.AGENT_EXECUTABLES["Solo"] = "solo"
            sole = roundtable.roster_awareness("Solo")
            self.assertIn("You are Solo", sole)
            self.assertIn("`solo`", sole)
            self.assertIn("sole member", sole)
            self.assertNotIn("other members", sole)

            roundtable.AGENT_NAMES = ("Alpha", "Beta")
            roundtable.AGENT_EXECUTABLES.clear()
            roundtable.AGENT_EXECUTABLES.update({"Alpha": "alpha", "Beta": "beta"})
            pair = roundtable.roster_awareness("Alpha")
            self.assertIn("You are Alpha", pair)
            self.assertIn("`alpha`", pair)
            self.assertIn("one of 2 members", pair)
            self.assertIn("The other member is Beta (`beta`).", pair)
            self.assertNotIn("The other members are", pair)
        finally:
            roundtable.AGENT_NAMES = real_names
            roundtable.AGENT_EXECUTABLES.clear()
            roundtable.AGENT_EXECUTABLES.update(real_execs)

    def test_every_store_true_opt_in_flag_has_a_matching_options_screen_toggle(self):
        """Guards against parser flags added to the parser without a matching entry in OPTION_TOGGLES,
        so the interactive menu silently falls behind."""
        parser = roundtable.build_parser()
        parser_flags = {
            action.dest
            for action in parser._actions
            if isinstance(action, roundtable.argparse._StoreTrueAction)
            and action.help != roundtable.argparse.SUPPRESS
            and action.dest not in ("plain", "list_agents", "install")
        }
        toggle_names = {name for name, _ in roundtable.OPTION_TOGGLES}
        self.assertTrue(parser_flags <= toggle_names, f"Missing toggles for: {parser_flags - toggle_names}")

    def test_install_flag_invokes_installer_main(self):
        with mock.patch("install.main", return_value=0) as mock_install_main, \
             mock.patch("sys.argv", ["roundtable", "--install"]):
            exit_code = roundtable.main()
            self.assertEqual(exit_code, 0)
            mock_install_main.assert_called_once_with([])

    def test_install_flag_forwards_installer_arguments(self):
        with mock.patch("install.main", return_value=0) as mock_install_main, \
             mock.patch("sys.argv", ["roundtable", "--install", "--dry-run", "--skip-clis"]):
            exit_code = roundtable.main()
            self.assertEqual(exit_code, 0)
            mock_install_main.assert_called_once_with(["--dry-run", "--skip-clis"])

    def test_install_cli_entry_is_defined_before_curses_import(self):
        """`python3 roundtable.py --install` must not require curses (stock Windows)."""
        source = Path(roundtable.__file__).read_text(encoding="utf-8")
        gate = 'if __name__ == "__main__" and "--install" in sys.argv[1:]:'
        self.assertIn(gate, source)
        self.assertLess(source.index(gate), source.index("\nimport curses\n"))
        self.assertIn("def _run_install_from_cli", source)
        self.assertLess(
            source.index("def _run_install_from_cli"),
            source.index("\nimport curses\n"),
        )

    def test_run_install_from_cli_dry_run_forwards_flags(self):
        # Same helper the script uses when launched as `python3 roundtable.py --install ...`.
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            code = roundtable._run_install_from_cli(
                ["--install", "--dry-run", "--skip-clis", "--bin-dir", str(bin_dir)])
            self.assertEqual(code, 0)
            self.assertFalse(bin_dir.exists())  # dry-run must not create bin_dir


    def test_self_toggle_flips_workspace_to_roundtables_own_source(self):
        # cwd must differ from roundtable's own source dir, or a broken --self would still resolve
        # to the right path by accident (the test process's real cwd may already be that directory).
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--mock"]  # no -C: self should decide the workspace
            captured = {}

            def fake_wrapper(func, *wrapper_args, **_):
                if func is roundtable.read_options_ui:
                    defaults = wrapper_args[0]
                    result = dict(defaults)
                    result["self"] = True
                    return result
                if func is roundtable.run_tui:
                    captured["args"], captured["session"] = wrapper_args[0], wrapper_args[1]
                    return 0
                raise AssertionError(f"unexpected curses.wrapper target: {func}")

            original_cwd = os.getcwd()
            os.chdir(td)
            try:
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(sys.stdin, "isatty", return_value=True), \
                     mock.patch.object(sys.stdout, "isatty", return_value=True), \
                     mock.patch.object(roundtable.curses, "wrapper", side_effect=fake_wrapper):
                    self.assertEqual(roundtable.main(), 0)
            finally:
                os.chdir(original_cwd)
            self.assertTrue(captured["args"].self)
            self.assertEqual(Path(captured["session"].workspace),
                             Path(roundtable.__file__).resolve().parent)
            self.assertNotEqual(Path(captured["session"].workspace), Path(td).resolve())

    def test_sparkline_scales_values_and_pads_missing_history(self):
        self.assertEqual(roundtable.sparkline([], 4), "····")
        self.assertEqual(roundtable.sparkline([5, 5, 5], 5), "··███")
        self.assertEqual(roundtable.sparkline([1, 2, 3, 4], 4), "▁▃▅█")

    def test_activity_sparkline_buckets_recent_pulses_by_age(self):
        now = 100.0
        pulses = [99.9, 96.0, 88.0]
        result = roundtable.activity_sparkline(pulses, 4, window=8.0, now=now)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[-1], "█")
        self.assertEqual(roundtable.activity_sparkline([], 4, window=8.0, now=now), "▁▁▁▁")

    def test_completion_estimator_waits_for_evidence_then_counts_down(self):
        now = [0.0]
        estimator = roundtable.CompletionEstimator(5, clock=lambda: now[0])
        self.assertIsNone(estimator.remaining_seconds())

        now[0] = 10.0
        estimator.complete(1)
        self.assertEqual(estimator.remaining_seconds(), 40.0)
        now[0] = 13.0
        self.assertEqual(estimator.remaining_seconds(), 37.0)

    def test_completion_estimator_excludes_provider_limit_waits(self):
        now = [0.0]
        estimator = roundtable.CompletionEstimator(5, clock=lambda: now[0])
        now[0] = 10.0
        estimator.complete(1)

        now[0] = 12.0
        estimator.pause_for_provider("Claude")
        now[0] = 1012.0
        self.assertEqual(estimator.remaining_seconds(), 38.0)
        estimator.resume_provider("Claude")
        now[0] = 1020.0
        estimator.complete(1)

        # Two measured units took 10s each after the 1,000s provider pause was removed.
        self.assertEqual(estimator.observed_seconds, 20.0)
        self.assertEqual(estimator.remaining_seconds(), 30.0)

    def test_completion_estimate_is_coarse_and_human_readable(self):
        self.assertEqual(roundtable.format_completion_estimate(1), "est. ~5s left")
        self.assertEqual(roundtable.format_completion_estimate(61), "est. ~2m left")
        self.assertEqual(roundtable.format_completion_estimate(3601), "est. ~1h 1m left")

    def test_phase_work_units_reflect_parallel_and_sequential_wall_time(self):
        self.assertEqual(roundtable.phase_work_units(roundtable._run_parallel_phase), 1)
        self.assertEqual(roundtable.phase_work_units(roundtable._run_sequential_phase),
                         len(roundtable.AGENT_NAMES))

    def test_tick_records_usage_history_for_completed_turns(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.session = roundtable.Session("Goal", "/tmp", 0, "now", [])
        display.usage_names = ("Codex", "Claude", "Antigravity")
        display.turn_times = {name: [] for name in display.usage_names}
        display.turn_outputs = {name: [] for name in display.usage_names}
        display.activity_pulses = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
        display.turn_start = {"Codex": time.monotonic() - 1.5}
        display._known_turn_count = 0
        display.frame = 0
        display.activity = {}
        display.started = time.monotonic()
        display.console = roundtable.deque(maxlen=300)
        display.run_log = roundtable.RunLog(None)
        display.expanded = None
        display.monitor = mock.Mock(refresh=lambda: None)
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.session.turns.append(roundtable.Turn("Codex", "proposal", "a short answer"))

        display.tick("Codex", "")

        self.assertEqual(display.turn_outputs["Codex"], [len("a short answer")])
        self.assertEqual(len(display.turn_times["Codex"]), 1)
        self.assertGreater(display.turn_times["Codex"][0], 1.0)
        self.assertEqual(len(display.activity_pulses["Codex"]), 1)
        self.assertEqual(display._known_turn_count, 1)
        self.assertEqual(display.console[-1][0], "turn")
        self.assertIn("Codex · proposal", display.console[-1][1])

    def _make_tick_only_display(self):
        """A Display wired up just enough to call tick() without touching real curses draw/input."""
        display = roundtable.Display.__new__(roundtable.Display)
        display.session = roundtable.Session("Goal", "/tmp", 0, "now", [])
        display.usage_names = ("Codex", "Claude", "Antigravity", "Aider", "Grok", "Qwen")
        display.turn_times = {name: [] for name in display.usage_names}
        display.turn_outputs = {name: [] for name in display.usage_names}
        display.activity_pulses = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
        display.work_activity = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
        display.work_reads = {name: 0 for name in display.usage_names}
        display.work_execs = {name: 0 for name in display.usage_names}
        display.work_writes = {name: 0 for name in display.usage_names}
        display.usage_percent = {}
        display.retry_state = {}
        display.turn_start = {}
        display._known_turn_count = 0
        display.frame = 0
        display.activity = {}
        display.started = time.monotonic()
        display.console = roundtable.deque(maxlen=300)
        display.run_log = roundtable.RunLog(None)
        display.expanded = None
        display.monitor = mock.Mock(refresh=lambda: None)
        display.draw = lambda: None
        display.poll_input = lambda: None
        return display

    def test_parse_usage_gauge_picks_up_a_self_reported_percentage(self):
        display = self._make_tick_only_display()
        display.tick("Codex", "You've used 93% of your usage limit")
        self.assertEqual(display.usage_percent["Codex"], 93.0)
        display.tick("Codex", "still working on it")  # a non-matching line must not clear it
        self.assertEqual(display.usage_percent["Codex"], 93.0)

    def test_parse_usage_gauge_pins_100_on_hit_and_clears_on_recovery(self):
        display = self._make_tick_only_display()
        display.tick("Claude", "temporarily unavailable: You've hit your session limit")
        self.assertEqual(display.usage_percent["Claude"], 100.0)
        self.assertEqual(display.console[-1][0], "retry")
        display.tick("Claude", "agent available again — retrying the original task")
        self.assertNotIn("Claude", display.usage_percent)
        self.assertEqual(display.console[-1][0], "retry")

    def test_tick_elevates_transient_retry_to_key_event(self):
        display = self._make_tick_only_display()
        display.tick(
            "Codex",
            "failed (timeout) — retrying once in 3s (attempt 2/2)")
        self.assertEqual(display.console[-1][0], "retry")
        display.console_filter = 0
        _label, entries = display._filtered_console()
        self.assertEqual(entries[-1][0], "retry")

    def test_agent_panel_shows_the_usage_gauge_when_known(self):
        display = make_test_display()
        display.usage_percent["Antigravity"] = 93.0
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
        self.assertIn("93% used", display.s.text())

    def test_agent_panel_omits_the_gauge_when_no_signal_has_been_seen(self):
        display = make_test_display()
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
        self.assertNotIn("% used", display.s.text())

    def test_update_status_logs_phase_message_changes_once(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.active = set()
        display.status = "Ready"
        display.turn_start = {}
        display.phase_completed = set()
        display.phase_failed = set()
        display.activity = {}
        display.started = time.monotonic()
        display.console = roundtable.deque(maxlen=300)
        display.run_log = roundtable.RunLog(None)
        display.expanded = None

        display.update_status(["Codex", "Claude"], "Agents are developing solutions in parallel")
        display.update_status(["Codex"], "Agents are developing solutions in parallel")
        display.update_status([], "Agents are developing solutions in parallel")

        phase_logs = [text for kind, text in display.console if kind == "phase"]
        self.assertEqual(len(phase_logs), 1)
        self.assertEqual(display.phase_completed, {"Codex", "Claude"})
        self.assertIn("Claude", display.turn_start)

    def test_update_status_preserves_progress_across_sequential_agent_swaps(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.active = set()
        display.status = "Ready"
        display.turn_start = {}
        display.phase_completed = set()
        display.phase_failed = set()
        display.activity = {}
        display.scroll = {"Codex": 0, "Claude": 0}
        display.work_activity = {"Codex": roundtable.deque(), "Claude": roundtable.deque()}
        display.started = time.monotonic()
        display.console = roundtable.deque(maxlen=300)
        display.run_log = roundtable.RunLog(None)

        display.update_status(["Codex"], "Agents are reviewing in sequence")
        display.update_status(["Claude"], "Agents are reviewing in sequence")
        self.assertEqual(display.phase_completed, {"Codex"})

        display.update_status(["Claude"], "Claude is refining the final answer")
        self.assertEqual(display.phase_completed, set())
        self.assertEqual(display.phase_failed, set())

    def test_update_status_does_not_count_already_failed_agents_as_done(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.active = {"Codex", "Claude"}
        display.status = "Agents are reviewing in parallel"
        display.turn_start = {}
        display.phase_completed = set()
        display.phase_failed = {"Codex"}
        display.activity = {}
        display.started = time.monotonic()
        display.console = roundtable.deque(maxlen=300)
        display.run_log = roundtable.RunLog(None)

        # Coordinator empties the active set after both futures settle; Codex was already
        # demoted to phase_failed and must not reappear under "done".
        display.update_status([], "Agents are reviewing in parallel")
        self.assertEqual(display.phase_completed, {"Claude"})
        self.assertEqual(display.phase_failed, {"Codex"})

    def test_busy_status_line_shows_current_phase_progress_counts(self):
        display = make_test_display(h=30, w=140)
        display.busy = True
        display.status = "Agents are reviewing in parallel"
        display.active = {"Codex", "Claude"}
        display.phase_completed = {"Aider"}
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()

        status_row = "".join(display.s.grid[3])
        self.assertIn("2 working", status_row)
        self.assertIn("1 done", status_row)

    def test_phase_failure_is_not_counted_as_done_and_shows_on_status(self):
        display = make_test_display(h=30, w=140)
        display.busy = True
        display.status = "Agents are reviewing in parallel"
        display.active = {"Codex"}
        display.phase_completed = {"Claude", "Aider"}
        display.phase_failed = {"Aider"}
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()

        status_row = "".join(display.s.grid[3])
        self.assertIn("1 working", status_row)
        self.assertIn("1 done", status_row)  # Claude only; Aider demoted
        self.assertIn("1 failed", status_row)
        self.assertNotIn("2 done", status_row)

        roster = "".join(display.s.grid[4])
        self.assertIn("✗", roster)

    def test_tick_elevates_phase_drop_to_error_and_tracks_failure(self):
        display = make_test_display()
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.phase_completed = {"Codex"}
        display.monitor.refresh = lambda: None

        display.tick("Codex", "dropped from this phase after a failure: boom")

        self.assertIn("Codex", display.phase_failed)
        self.assertNotIn("Codex", display.phase_completed)
        error_logs = [text for kind, text in display.console if kind == "error"]
        self.assertTrue(any("dropped from this phase" in text for text in error_logs), error_logs)

    def test_tick_elevates_task_complete_signal_to_phase_kind(self):
        display = make_test_display()
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None

        display.tick("Claude", "marked the task complete — skipping Aider this phase")

        phase_logs = [text for kind, text in display.console if kind == "phase"]
        self.assertTrue(any("marked the task complete" in text for text in phase_logs), phase_logs)
        tick_logs = [text for kind, text in display.console if kind == "tick"]
        self.assertFalse(any("marked the task complete" in text for text in tick_logs))

    def test_failed_agent_panel_shows_failure_state(self):
        display = make_test_display(h=30, w=160)
        display.busy = True
        display.active = set()
        display.phase_failed = {"Codex"}
        display.activity["Codex"] = "dropped from this phase after a failure: timeout"
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
        # Panel text wraps; flatten whitespace so a multi-line wrap still matches.
        flat = " ".join(display.s.text().split())
        self.assertIn("✗ failed", flat)
        # Other six-column panes occur between Codex's wrapped rows in the full screen buffer.
        self.assertIn("dropped from this", flat)
        self.assertIn("failure: timeout", flat)

    def test_status_line_shows_operation_transparency_during_processing(self):
        """The status line provides clear visibility into what's happening during processing."""
        display = make_test_display(h=30, w=140)
        display.busy = True
        display.status = "Processing agent responses"
        display.active = {"Codex", "Claude", "Aider"}
        display.phase_completed = {"Antigravity"}

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()

        status_row = "".join(display.s.grid[3])
        self.assertIn("3 working", status_row)
        self.assertIn("1 done", status_row)
        self.assertIn("Processing agent responses", status_row)

    def test_draw_shows_console_panel_when_terminal_is_tall_enough(self):
        class Screen:
            def __init__(self, h, w):
                self.h, self.w = h, w
                self.grid = [[" "] * w for _ in range(h)]

            def getmaxyx(self):
                return self.h, self.w

            def erase(self):
                self.grid = [[" "] * self.w for _ in range(self.h)]

            def addnstr(self, y, x, text, n, attr=0):
                for i, ch in enumerate(text[:n]):
                    if 0 <= y < self.h and 0 <= x + i < self.w:
                        self.grid[y][x + i] = ch

            def refresh(self):
                pass

            def text(self):
                return "\n".join("".join(row) for row in self.grid)

        def make_display(h, w):
            display = roundtable.Display.__new__(roundtable.Display)
            display.s = Screen(h, w)
            display.session = roundtable.Session("Goal", "/tmp", 0, "now", [])
            display.status = "Ready"
            display.activity = {}
            display.active = set()
            display.phase_completed = set()
            display.busy = False
            display.error = ""
            display.frame = 0
            display.monitor = mock.Mock(changes=[], truncated=False)
            display.started = time.monotonic()
            display.touch_mode = False
            display.hitboxes = {}
            display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Aider": 0, "Grok": 0,
                              "Qwen": 0, "Final": 0}
            display.usage_names = ("Codex", "Claude", "Antigravity", "Aider", "Grok", "Qwen")
            display.turn_times = {name: [] for name in display.usage_names}
            display.turn_outputs = {name: [] for name in display.usage_names}
            display.activity_pulses = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
            display.turn_start = {}
            display.console = roundtable.deque(maxlen=300)
            display.run_log = roundtable.RunLog(None)
            display.expanded = None
            return display

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            tall = make_display(48, 160)
            tall.draw()
            self.assertIn("CONSOLE", tall.s.text())
            self.assertIn("CODE MONITOR", tall.s.text())

            short = make_display(24, 160)
            short.draw()
            self.assertNotIn("CONSOLE", short.s.text())
            self.assertIn("CODE MONITOR", short.s.text())

    def test_balanced_columns_fill_width_with_even_gaps(self):
        columns = roundtable.balanced_columns(121, 3, gap=2)
        self.assertEqual(columns, [(1, 39), (42, 38), (82, 38)])
        self.assertEqual(columns[-1][0] + columns[-1][1], 120)

    def test_format_duration_compact_forms(self):
        self.assertEqual(roundtable.format_duration(0), "0:00")
        self.assertEqual(roundtable.format_duration(65), "1:05")
        self.assertEqual(roundtable.format_duration(3600 + 2 * 60 + 3), "1:02:03")
        self.assertEqual(roundtable.format_duration(-5), "0:00")

    def test_dashboard_hint_prioritizes_actions_and_reveals_add_prompt(self):
        compact = roundtable.dashboard_hint(67, touch_mode=False, busy=True)
        self.assertLessEqual(len(compact), 67)
        self.assertIn("ctrl+c cancel", compact)
        self.assertIn("i add prompt", compact)
        self.assertIn("1-6/f/0", compact)
        self.assertIn("? help", compact)
        self.assertNotIn("…", compact)

        wide = roundtable.dashboard_hint(120, touch_mode=False, busy=False)
        self.assertIn("c filter", wide)
        self.assertIn("click panel", wide)
        self.assertIn("1-6/f/0/m", wide)
        self.assertIn("transcript autosaved", wide)
        self.assertNotIn("add prompt", wide)

    def test_expanded_hint_keeps_collapse_and_scroll_help_at_minimum_width(self):
        compact = roundtable.expanded_hint(67, touch_mode=False)
        self.assertLessEqual(len(compact), 67)
        self.assertIn("Esc/q collapse", compact)
        self.assertIn("1-6/f/0/", compact)  # Updated to include the 'm' panel option
        self.assertIn("↑/↓", compact)
        self.assertIn("scroll", compact)
        self.assertNotIn("…", compact)

    def test_code_change_summary_formats_kind_counts(self):
        empty = roundtable.code_change_summary([])
        self.assertEqual(empty, "")
        changes = [
            roundtable.CodeChange("added", "a.py"),
            roundtable.CodeChange("added", "b.py"),
            roundtable.CodeChange("modified", "c.py"),
            roundtable.CodeChange("deleted", "d.py"),
        ]
        self.assertEqual(roundtable.code_change_summary(changes), "+2 ~1 −1")

    def test_code_monitor_expands_via_m_key_and_click(self):
        """Code Monitor was scroll-only; m/M and click must expand like Final/Console."""
        display = make_test_display(h=30, w=100)
        display.monitor.changes = [
            roundtable.CodeChange("modified", f"file-{i}.py") for i in range(12)
        ]
        display.s.getch = mock.Mock(side_effect=[ord("m"), -1])
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.poll_input()
            self.assertEqual(display.expanded, "Code")
            display.draw()
            rendered = display.s.text()
        self.assertIn("CODE MONITOR (expanded)", rendered)
        self.assertIn("file-11.py", rendered)
        # Second m collapses.
        display.s.getch = mock.Mock(side_effect=[ord("m"), -1])
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.poll_input()
        self.assertIsNone(display.expanded)

        display.expanded = None
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
            top, left, _, _ = display.hitboxes["code"]
            display.handle_mouse(left + 1, top + 1, roundtable.curses.BUTTON1_CLICKED)
        self.assertEqual(display.expanded, "Code")

    def test_compact_code_title_shows_kind_summary(self):
        display = make_test_display(h=48, w=160)
        display.monitor.changes = [
            roundtable.CodeChange("added", "new.py"),
            roundtable.CodeChange("modified", "edit.py"),
            roundtable.CodeChange("deleted", "gone.py"),
        ]
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        self.assertIn("+1", display.s.text())
        self.assertIn("~1", display.s.text())
        self.assertIn("−1", display.s.text())

    def test_agent_grid_uses_two_rows_when_tall_enough(self):
        # 2*8 + 1 gap = 17 is the minimum for 2×3; short areas stay 1×6.
        cols, short = roundtable.agent_grid(120, 16, 6, top=5)
        self.assertEqual(cols, 6)
        self.assertEqual(len(short), 6)
        self.assertEqual(len({y for y, _x, _h, _w in short}), 1)

        cols, tall = roundtable.agent_grid(120, 17, 6, top=5)
        self.assertEqual(cols, 3)
        self.assertEqual(len(tall), 6)
        rows = sorted({y for y, _x, _h, _w in tall})
        self.assertEqual(len(rows), 2)
        # Wider panels: three columns beat six at the same terminal width.
        self.assertGreater(tall[0][3], short[0][3])

    def test_draw_uses_two_row_agent_grid_on_tall_terminals(self):
        display = make_test_display(h=48, w=160)
        display.busy = True
        display.active = {"Codex", "Grok"}
        display.phase_completed = {"Claude"}
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        # Two distinct panel header rows (not a single six-wide strip).
        header_rows = [i for i, row in enumerate(display.s.grid)
                       if sum(1 for ch in row if ch == "╭") >= 3]
        self.assertGreaterEqual(len(header_rows), 2, display.s.text())
        # Second row hosts Aider/Grok/Qwen.
        self.assertIn("AIDER", display.s.text())
        self.assertIn("GROK", display.s.text())
        self.assertIn("QWEN", display.s.text())
        # Roster strip: short Antigravity label + working/done marks.
        roster = "".join(display.s.grid[4])
        self.assertIn("Anti", roster)
        self.assertIn("●", roster)  # active
        self.assertIn("✓", roster)  # responded

    def test_draw_header_shows_turns_and_elapsed_even_with_battery(self):
        display = make_test_display(h=25, w=120)
        display.session.turns = [
            roundtable.Turn("Codex", "proposal", "hello"),
            roundtable.Turn("Claude", "proposal", "world"),
        ]
        display.started = time.monotonic() - 125  # ~2:05
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value="🔋 80%"):
            display.draw()
        header = "".join(display.s.grid[0])
        self.assertIn("2 turns", header)
        self.assertIn("2:05", header)
        self.assertIn("80%", header)

    def test_draw_header_shows_self_mode_badge_and_cleans_objective(self):
        """Header displays '⚡ self' badge and strips SELF_EDIT_NOTE boilerplate from objective line."""
        display = make_test_display(h=25, w=120)
        display.session.objective = f"improve self gui\n\n{roundtable.SELF_EDIT_NOTE}\n\nNote: sandbox"
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        header = "".join(display.s.grid[0])
        self.assertIn("⚡ self", header)
        obj_line = "".join(display.s.grid[2])
        self.assertIn("⚡ improve self gui", obj_line)
        self.assertNotIn(roundtable.SELF_EDIT_NOTE, obj_line)

    def test_draw_header_shows_restart_count_after_self_edit_restarts(self):
        """Header appends '↻N' once a --self run has restarted itself, so the badge reflects the
        run's own edit-and-reload history instead of always looking like a first pass."""
        display = make_test_display(h=25, w=120)
        display.session.objective = f"improve self gui\n\n{roundtable.SELF_EDIT_NOTE}"
        display.session.restart_count = 2
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        header = "".join(display.s.grid[0])
        self.assertIn("⚡ self ↻2", header)

    def test_session_is_self_and_clean_self_objective_helpers(self):
        plain = roundtable.Session("fix a bug", "/tmp", 0, "now", [])
        self.assertFalse(roundtable.session_is_self(plain))
        self.assertEqual(roundtable.clean_self_objective(plain.objective), "fix a bug")
        self.assertIsNone(roundtable.self_sandbox_path(plain))

        with_note = roundtable.Session(
            f"improve gui\n\n{roundtable.SELF_EDIT_NOTE}\n\n"
            "A throwaway copy of the current source is kept at `/tmp/out/self-test-sandbox`, refreshed",
            "/tmp", 0, "now", [])
        self.assertTrue(roundtable.session_is_self(with_note))
        self.assertEqual(roundtable.clean_self_objective(with_note.objective), "improve gui")
        self.assertEqual(roundtable.self_sandbox_path(with_note), "/tmp/out/self-test-sandbox")

        # Follow-up-only self note (resume with --self on a non-self session).
        followup_only = roundtable.Session("original goal", "/tmp", 0, "now", [
            roundtable.Turn(
                "User", "follow-up",
                f"keep going\n\n{roundtable.SELF_EDIT_NOTE}\n\n"
                "A throwaway copy of the current source is kept at `/var/sandbox`, refreshed"),
        ])
        self.assertTrue(roundtable.session_is_self(followup_only))
        self.assertEqual(roundtable.self_sandbox_path(followup_only), "/var/sandbox")

    def test_main_does_not_auto_enable_self_from_workspace_contents(self):
        """A plain (non---self) run must stay ordinary even when -C happens to point at a
        directory that contains test_roundtable.py -- e.g. the roundtable repo itself. Self mode
        is opt-in via --self only; merely running from that directory must never silently append
        SELF_EDIT_NOTE to the objective or spin up a self-test sandbox."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "test_roundtable.py").write_text("# fixture suite\n")
            (workspace / "roundtable.py").write_text("print('fixture')\n")
            out = workspace / "out"
            argv = [
                "roundtable", "ordinary task", "--plain", "-r", "0", "--mock",
                "--skip-preflight", "-C", str(workspace), "--output-dir", str(out),
            ]
            with mock.patch.object(sys, "argv", argv), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted(out.glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            self.assertFalse(roundtable.session_is_self(session))
            self.assertNotIn(roundtable.SELF_EDIT_NOTE, session.objective)
            self.assertFalse((out / "self-test-sandbox").exists())

    def test_restart_vote_and_task_status_prompts_keep_roster_self_awareness(self):
        """RESTART_VOTE / TASK_STATUS ride prompt_for; identity must not drop when those hints attach.
        (verify_self_edit_turn is not a prompt builder — it only ticks unittest results — so it needs
        no roster injection.)"""
        for speaker in ("Grok", "Claude"):
            voted = roundtable.prompt_for(
                "Goal", [], "review", speaker, restart_vote_pending=True)
            statused = roundtable.prompt_for(
                "Goal", [], "review", speaker, task_status_check=True)
            for prompt in (voted, statused):
                self.assertIn(f"You are {speaker}", prompt)
                self.assertIn(f"`{roundtable.AGENT_EXECUTABLES[speaker]}`", prompt)
                self.assertIn("Do not invent additional members", prompt)
            self.assertIn("RESTART: now", voted)
            self.assertIn("RESTART: later", voted)
            self.assertIn("TASK STATUS: complete", statused)

    def test_draw_status_line_shows_self_sandbox_path(self):
        """Status line surfaces the smoke-test sandbox path advertised in the self note."""
        display = make_test_display(h=25, w=160)
        sandbox = "/home/user/.roundtable/self-test-sandbox"
        display.session.objective = (
            f"improve self\n\n{roundtable.SELF_EDIT_NOTE}\n\n"
            f"A throwaway copy of the current source is kept at `{sandbox}`, refreshed each time")
        display.session.workspace = "/home/user/roundtable"
        display.status = "Ready"
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        status = "".join(display.s.grid[3])
        self.assertIn("⚡", status)
        # Full path may be shortened; the distinctive leaf should still be visible.
        self.assertIn("self-test-sandbox", status)

    def test_dangerous_options_includes_self_and_elevated(self):
        self.assertIn("elevated", roundtable.DANGEROUS_OPTIONS)
        self.assertIn("self", roundtable.DANGEROUS_OPTIONS)

    def test_create_self_test_sandbox_records_per_file_errors(self):
        """A single unreadable source must not abort the refresh; it is recorded instead."""
        with tempfile.TemporaryDirectory() as workspace_dir, \
             tempfile.TemporaryDirectory() as output_dir:
            workspace = Path(workspace_dir)
            (workspace / "roundtable.py").write_text("print('ok')")
            (workspace / "README.md").write_text("# ok")
            bad = workspace / "test_roundtable.py"
            bad.write_text("broken")
            # Make the source unreadable so copy2 fails without requiring special root perms.
            os.chmod(bad, 0)
            errors: list[str] = []
            try:
                sandbox = roundtable.create_self_test_sandbox(
                    workspace, Path(output_dir), errors)
            finally:
                os.chmod(bad, 0o644)
            self.assertTrue(sandbox.is_dir())
            self.assertEqual((sandbox / "roundtable.py").read_text(), "print('ok')")
            self.assertTrue(any("test_roundtable.py" in msg for msg in errors), errors)

    def test_create_self_test_sandbox_raises_when_output_not_writable(self):
        with tempfile.TemporaryDirectory() as workspace_dir, \
             tempfile.TemporaryDirectory() as output_dir:
            workspace = Path(workspace_dir)
            (workspace / "roundtable.py").write_text("x")
            out = Path(output_dir)
            os.chmod(out, 0o555)
            try:
                with self.assertRaises(OSError):
                    roundtable.create_self_test_sandbox(workspace, out)
            finally:
                os.chmod(out, 0o755)

    def test_self_verification_command_is_none_outside_a_self_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(roundtable.self_verification_command(td))

    def test_self_verification_command_targets_the_real_test_module(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_roundtable.py").write_text("# stand-in")
            self.assertEqual(
                roundtable.self_verification_command(td),
                [sys.executable, "-m", "unittest", "test_roundtable", "-q"])

    def test_run_self_verification_is_none_outside_a_self_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(roundtable.subprocess, "run") as run:
                self.assertIsNone(roundtable.run_self_verification(td))
            run.assert_not_called()

    def test_run_self_verification_reports_pass_with_real_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_roundtable.py").write_text("# stand-in")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="Ran 333 tests in 45.0s\n\nOK\n")
            with mock.patch.object(roundtable.subprocess, "run", return_value=completed) as run:
                passed, detail = roundtable.run_self_verification(td)
            self.assertTrue(passed)
            self.assertIn("OK", detail)
            self.assertEqual(run.call_args.kwargs["cwd"], td)

    def test_run_self_verification_reports_failure_detail(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_roundtable.py").write_text("# stand-in")
            completed = subprocess.CompletedProcess(
                args=[], returncode=1,
                stdout="", stderr="Ran 333 tests in 45.0s\n\nFAILED (failures=1)\n")
            with mock.patch.object(roundtable.subprocess, "run", return_value=completed):
                passed, detail = roundtable.run_self_verification(td)
            self.assertFalse(passed)
            self.assertIn("FAILED", detail)

    def test_run_self_verification_handles_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_roundtable.py").write_text("# stand-in")
            with mock.patch.object(
                    roundtable.subprocess, "run",
                    side_effect=roundtable.subprocess.TimeoutExpired(cmd="x", timeout=1)):
                passed, detail = roundtable.run_self_verification(td, timeout=1)
            self.assertFalse(passed)
            self.assertIn("timed out", detail)

    def test_self_source_fingerprint_is_stable_and_content_sensitive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "roundtable.py").write_text("alpha")
            (root / "test_roundtable.py").write_text("beta")
            (root / "README.md").write_text("gamma")
            first = roundtable.self_source_fingerprint(td)
            self.assertEqual(first, roundtable.self_source_fingerprint(td))
            (root / "roundtable.py").write_text("alpha-changed")
            self.assertNotEqual(first, roundtable.self_source_fingerprint(td))
            # Restoring identical bytes restores the same fingerprint (content, not mtime).
            (root / "roundtable.py").write_text("alpha")
            self.assertEqual(first, roundtable.self_source_fingerprint(td))

    def test_run_self_verification_reuses_cache_when_source_is_unchanged(self):
        """Parallel --self phases would otherwise spawn one full suite per agent against identical
        files; the fingerprint cache must make the second call a pure reuse."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_roundtable.py").write_text("# stand-in")
            (Path(td) / "roundtable.py").write_text("# code")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="Ran 1 test in 0.0s\n\nOK\n")
            with mock.patch.object(roundtable.subprocess, "run", return_value=completed) as run:
                first = roundtable.run_self_verification(td)
                second = roundtable.run_self_verification(td)
            self.assertEqual(first, (True, "Ran 1 test in 0.0s · OK"))
            self.assertEqual(second, first)
            self.assertEqual(run.call_count, 1)

    def test_run_self_verification_reruns_after_source_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "test_roundtable.py").write_text("# stand-in")
            (root / "roundtable.py").write_text("v1")
            pass_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="OK\n")
            fail_result = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="FAILED\n")
            with mock.patch.object(
                    roundtable.subprocess, "run",
                    side_effect=[pass_result, fail_result]) as run:
                first = roundtable.run_self_verification(td)
                (root / "roundtable.py").write_text("v2")
                second = roundtable.run_self_verification(td)
            self.assertEqual(first, (True, "OK"))
            self.assertEqual(second, (False, "FAILED"))
            self.assertEqual(run.call_count, 2)

    def test_run_self_verification_does_not_cache_timeouts(self):
        """A load-induced timeout must not stick as a FAIL once the source is still the same."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_roundtable.py").write_text("# stand-in")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="OK\n")
            with mock.patch.object(
                    roundtable.subprocess, "run",
                    side_effect=[
                        roundtable.subprocess.TimeoutExpired(cmd="x", timeout=1),
                        completed,
                    ]) as run:
                first = roundtable.run_self_verification(td, timeout=1)
                second = roundtable.run_self_verification(td, timeout=1)
            self.assertEqual(first, (False, "verification timed out after 1s"))
            self.assertEqual(second, (True, "OK"))
            self.assertEqual(run.call_count, 2)

    def test_run_self_verification_serializes_concurrent_callers(self):
        """Two agents finishing a parallel phase at once must not thrash two suite runs when the
        source is unchanged — one runs, the other waits and reuses."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_roundtable.py").write_text("# stand-in")
            (Path(td) / "roundtable.py").write_text("# code")
            started = threading.Event()
            release = threading.Event()
            calls = {"n": 0}

            def slow_run(*_args, **_kwargs):
                calls["n"] += 1
                started.set()
                # Hold the suite long enough that the second caller is waiting on the lock.
                self.assertTrue(release.wait(2.0))
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr="OK\n")

            results: list[tuple[bool, str] | None] = [None, None]

            def worker(index: int) -> None:
                results[index] = roundtable.run_self_verification(td)

            with mock.patch.object(roundtable.subprocess, "run", side_effect=slow_run) as run:
                t1 = threading.Thread(target=worker, args=(0,))
                t2 = threading.Thread(target=worker, args=(1,))
                t1.start()
                self.assertTrue(started.wait(2.0))
                t2.start()
                # Give the second thread a moment to block on the verification lock.
                time.sleep(0.05)
                release.set()
                t1.join(2.0)
                t2.join(2.0)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertEqual(results[0], (True, "OK"))
            self.assertEqual(results[1], (True, "OK"))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(calls["n"], 1)

    def test_verify_self_edit_turn_is_a_noop_for_non_self_sessions(self):
        session = roundtable.Session("Ordinary goal", "/tmp", 0, "now", [])
        agent = roundtable.Agent("Codex", Path("/tmp"))
        ticks = []
        with mock.patch.object(roundtable.subprocess, "run") as run:
            roundtable.verify_self_edit_turn(session, agent, ticks.append)
        run.assert_not_called()
        self.assertEqual(ticks, [])

    def test_verify_self_edit_turn_is_a_noop_for_a_mock_agent(self):
        """--mock exists to test orchestration without any real invocations; a MockAgent's
        simulated content has no genuine code change behind it to verify."""
        session = roundtable.Session(
            f"Fix the GUI\n\n{roundtable.SELF_EDIT_NOTE}", "/tmp", 0, "now", [])
        agent = roundtable.MockAgent("Codex", Path("/tmp"))
        ticks = []
        with mock.patch.object(roundtable.subprocess, "run") as run:
            roundtable.verify_self_edit_turn(session, agent, ticks.append)
        run.assert_not_called()
        self.assertEqual(ticks, [])

    def test_verify_self_edit_turn_ticks_pass_and_fail(self):
        session = roundtable.Session(
            f"Fix the GUI\n\n{roundtable.SELF_EDIT_NOTE}", "/tmp", 0, "now", [])
        agent = roundtable.Agent("Codex", Path("/tmp"))
        ticks = []
        with mock.patch.object(roundtable, "run_self_verification", return_value=(True, "OK")):
            roundtable.verify_self_edit_turn(session, agent, ticks.append)
        self.assertEqual(len(ticks), 1)
        self.assertIn("PASS", ticks[0])
        self.assertIn("OK", ticks[0])

        ticks.clear()
        with mock.patch.object(
                roundtable, "run_self_verification",
                return_value=(False, "FAILED (failures=1)")):
            roundtable.verify_self_edit_turn(session, agent, ticks.append)
        self.assertEqual(len(ticks), 1)
        self.assertIn("FAIL", ticks[0])

    def test_run_parallel_phase_verifies_each_agents_turn_in_a_self_session(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session(
                f"Fix the GUI\n\n{roundtable.SELF_EDIT_NOTE}", td, 0, "now", [])
            agents = [(name, roundtable.MockAgent(name, workspace))
                     for name in ("Codex", "Claude")]
            with mock.patch.object(
                    roundtable, "verify_self_edit_turn") as verify:
                roundtable._run_parallel_phase(
                    session, agents, "proposal", lambda *_: None, lambda *_: None, "Working")
            self.assertEqual(verify.call_count, 2)

    def test_run_parallel_phase_skips_verification_for_an_ordinary_session(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Ordinary goal", td, 0, "now", [])
            agents = [(name, roundtable.MockAgent(name, workspace))
                     for name in ("Codex", "Claude")]
            with mock.patch.object(roundtable.subprocess, "run") as run:
                roundtable._run_parallel_phase(
                    session, agents, "proposal", lambda *_: None, lambda *_: None, "Working")
            run.assert_not_called()

    def test_run_parallel_phase_skips_real_verification_for_mock_agents_in_a_self_session(self):
        """Regression: a --self session run with --mock (as several main()-level --self tests do,
        e.g. test_self_flag_points_workspace_at_roundtables_own_source) must never trigger a real
        subprocess -- otherwise a fast, MockAgent-only test recursively spawns the real test suite
        once per agent and turns a sub-second test into a multi-minute one."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "test_roundtable.py").write_text("# stand-in")
            session = roundtable.Session(
                f"Fix the GUI\n\n{roundtable.SELF_EDIT_NOTE}", td, 0, "now", [])
            agents = [(name, roundtable.MockAgent(name, workspace))
                     for name in ("Codex", "Claude")]
            with mock.patch.object(roundtable.subprocess, "run") as run:
                roundtable._run_parallel_phase(
                    session, agents, "proposal", lambda *_: None, lambda *_: None, "Working")
            run.assert_not_called()

    def test_run_sequential_phase_verifies_each_agents_turn_in_a_self_session(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session(
                f"Fix the GUI\n\n{roundtable.SELF_EDIT_NOTE}", td, 0, "now", [])
            agents = [(name, roundtable.MockAgent(name, workspace))
                     for name in ("Codex", "Claude")]
            with mock.patch.object(roundtable, "verify_self_edit_turn") as verify:
                roundtable._run_sequential_phase(
                    session, agents, "proposal", lambda *_: None, lambda *_: None, "Working")
            self.assertEqual(verify.call_count, 2)

    def test_conduct_verifies_after_the_dead_code_check_step(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                f"Fix the GUI\n\n{roundtable.SELF_EDIT_NOTE}", td, 0, "now",
                [roundtable.Turn("Codex", "proposal", "done"),
                 roundtable.Turn("Final", "consensus", "Completed\n\nDone")],
                "Completed\n\nDone")
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            with mock.patch.object(roundtable, "verify_self_edit_turn") as verify:
                roundtable.conduct(
                    session, *agents, lambda *_: None, lambda *_: None,
                    dead_code_check=True, completed_phases={"proposal"})
            verify.assert_called_once()

    def test_draw_keeps_single_row_agent_grid_when_short(self):
        """At the 72×25 floor, agent area is too short for 2×3 — stay one row of six."""
        display = make_test_display(h=25, w=72)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        header_row = display.s.grid[5]
        border_cols = [i for i, ch in enumerate(header_row) if ch in "╭╮"]
        self.assertEqual(len(border_cols), 12, display.s.text())

    def test_followup_editor_reserves_its_own_bottom_band(self):
        class Screen:
            def getmaxyx(self):
                return 30, 100

            def move(self, *_args):
                pass

            def refresh(self):
                pass

        display = roundtable.Display.__new__(roundtable.Display)
        display.s = Screen()
        reserved = []
        boxes = []
        display.draw = lambda reserved_bottom=0: reserved.append(reserved_bottom)
        display._box = lambda y, x, height, width, color=0: boxes.append(
            (y, x, height, width))
        display._put = lambda *_args, **_kwargs: None

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0):
            display.draw_followup(roundtable.LineEditor("Follow up"))

        self.assertEqual(reserved, [7])
        self.assertEqual(boxes, [(23, 1, 6, 97)])

    def test_agents_work_concurrently_but_turn_order_is_stable(self):
        class ConcurrentAgent(roundtable.Agent):
            lock = threading.Lock()
            active = 0
            maximum = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                with self.lock:
                    type(self).active += 1
                    type(self).maximum = max(type(self).maximum, type(self).active)
                time.sleep(0.08)
                with self.lock:
                    type(self).active -= 1
                return self.name

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Parallel task", td, 0, "now", [])
            agents = [ConcurrentAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None)
            self.assertEqual(ConcurrentAgent.maximum, len(roundtable.AGENT_NAMES))
            self.assertEqual([turn.speaker for turn in session.turns],
                             list(roundtable.AGENT_NAMES) + ["Final"])

    def test_scope_hint_flags_agents_meaningfully_slower_than_fastest(self):
        self.assertEqual(roundtable.scope_hint("Codex", {}), "")
        self.assertEqual(roundtable.scope_hint("Codex", {"Codex": [], "Claude": [10]}), "")
        speeds = {"Codex": [10.0], "Claude": [10.5], "Antigravity": [30.0]}
        self.assertEqual(roundtable.scope_hint("Codex", speeds), "")
        self.assertEqual(roundtable.scope_hint("Claude", speeds), "")
        self.assertIn("tightly scoped", roundtable.scope_hint("Antigravity", speeds))
        # Sub-150ms difference should be ignored to prevent false triggers from GIL/scheduling jitter
        jitter_speeds = {"Codex": [0.02], "Claude": [0.05]}
        self.assertEqual(roundtable.scope_hint("Claude", jitter_speeds), "")

    def test_run_parallel_phase_scopes_the_agent_slow_in_an_earlier_phase(self):
        delays = {"Codex": 0.02, "Claude": 0.02, "Antigravity": 0.32}
        seen_prompts: dict[str, str] = {}

        class StaggeredAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(delays[self.name])
                seen_prompts[self.name] = prompt
                return f"{self.name} done"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Balance task", td, 0, "now", [])
            agents = [(name, StaggeredAgent(name, workspace)) for name in delays]
            agent_speed: dict[str, list[float]] = {}
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working",
                                           agent_speed=agent_speed)
            self.assertEqual(set(agent_speed), set(delays))
            # First phase: no history yet, nobody is scoped down.
            self.assertNotIn("tightly scoped", seen_prompts["Antigravity"])

            seen_prompts.clear()
            roundtable._run_parallel_phase(session, agents, "review 1", lambda *_: None,
                                           lambda *_: None, "Working",
                                           agent_speed=agent_speed)
            self.assertIn("tightly scoped", seen_prompts["Antigravity"])
            self.assertNotIn("tightly scoped", seen_prompts["Codex"])
            self.assertNotIn("tightly scoped", seen_prompts["Claude"])

    def test_run_parallel_phase_without_agent_speed_never_scopes(self):
        seen_prompts: dict[str, str] = {}

        class SlowAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(0.05 if self.name == "Antigravity" else 0.01)
                seen_prompts[self.name] = prompt
                return f"{self.name} done"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("No balance", td, 0, "now", [])
            agents = [(name, SlowAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working")
            roundtable._run_parallel_phase(session, agents, "review 1", lambda *_: None,
                                           lambda *_: None, "Working")
            self.assertNotIn("tightly scoped", seen_prompts["Antigravity"])

    def test_conduct_balance_load_scopes_slower_agent_in_review_round(self):
        delays = {"Codex": 0.02, "Claude": 0.02, "Antigravity": 0.32, "Aider": 0.02, "Grok": 0.02,
                 "Qwen": 0.02}
        seen_prompts: list[tuple[str, str]] = []

        class StaggeredAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(delays[self.name])
                return f"{self.name} done"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Balance conduct", td, 1, "now", [])
            agents = [StaggeredAgent(name, workspace) for name in delays]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                               balance_load=True,
                               log_prompt=lambda name, p: seen_prompts.append((name, p)))
            antigravity_prompt = next(p for name, p in seen_prompts
                                      if name == "Antigravity" and "review 1):" in p)
            codex_prompt = next(p for name, p in seen_prompts
                               if name == "Codex" and "review 1):" in p)
            self.assertIn("tightly scoped", antigravity_prompt)
            self.assertNotIn("tightly scoped", codex_prompt)

    def test_run_sequential_phase_lets_each_agent_build_on_the_last(self):
        seen_prompts = {}

        class RelayAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                seen_prompts[self.name] = prompt
                return f"{self.name} says hi"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Relay task", td, 0, "now", [])
            agents = [(name, RelayAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_sequential_phase(session, agents, "proposal", lambda *_: None,
                                             lambda *_: None, "Relaying")
            self.assertNotIn("Codex says hi", seen_prompts["Codex"])
            self.assertIn("Codex says hi", seen_prompts["Claude"])
            self.assertIn("Claude says hi", seen_prompts["Antigravity"])
            self.assertIn("live sequential relay", seen_prompts["Claude"])
            self.assertEqual([t.speaker for t in session.turns], ["Codex", "Claude", "Antigravity"])

    def test_conduct_sequential_collab_never_overlaps_agents(self):
        class LockStepAgent(roundtable.Agent):
            lock = threading.Lock()
            active = 0
            maximum = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                with self.lock:
                    type(self).active += 1
                    type(self).maximum = max(type(self).maximum, type(self).active)
                time.sleep(0.03)
                with self.lock:
                    type(self).active -= 1
                return self.name

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Sequential task", td, 0, "now", [])
            agents = [LockStepAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None, collab="sequential")
            self.assertEqual(LockStepAgent.maximum, 1)
            self.assertEqual([t.speaker for t in session.turns],
                             list(roundtable.AGENT_NAMES) + ["Final"])

    def test_conduct_mixed_collab_alternates_relay_and_parallel_rounds(self):
        class TrackingAgent(roundtable.Agent):
            lock = threading.Lock()
            active = 0
            maxima = []

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                with self.lock:
                    type(self).active += 1
                time.sleep(0.03)
                with self.lock:
                    type(self).maxima.append(type(self).active)
                    type(self).active -= 1
                return self.name

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Mixed task", td, 2, "now", [])
            agents = [TrackingAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None, collab="mixed")
            phases = [t.phase for t in session.turns]
            self.assertIn("review 1", phases)
            self.assertIn("review 2", phases)
            n = len(roundtable.AGENT_NAMES)
            round1_speakers = [t.speaker for t in session.turns if t.phase == "review 1"]
            round2_speakers = [t.speaker for t in session.turns if t.phase == "review 2"]
            self.assertEqual(round1_speakers, list(roundtable.AGENT_NAMES))
            self.assertEqual(set(round2_speakers), set(roundtable.AGENT_NAMES))
            # maxima order: proposal (parallel), review 1 (sequential relay), review 2 (parallel)
            proposal_maxima, review1_maxima, review2_maxima = (
                TrackingAgent.maxima[0:n], TrackingAgent.maxima[n:2 * n],
                TrackingAgent.maxima[2 * n:3 * n])
            self.assertTrue(any(v > 1 for v in proposal_maxima))
            self.assertEqual(review1_maxima, [1] * n)
            self.assertTrue(any(v > 1 for v in review2_maxima))

    def test_conduct_reports_eta_only_after_observing_a_completed_phase(self):
        class TimedAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(0.01)
                return self.name

        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Estimate this", td, 0, "now", [])
            agents = [TimedAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            statuses = []
            roundtable.conduct(
                session, *agents, lambda *_: None,
                lambda active, message: statuses.append((tuple(active), message)),
                synthesis_passes=2)

        proposal_messages = [message for active, message in statuses
                             if active and "developing solutions" in message]
        synthesis_messages = [message for active, message in statuses
                              if active and "final answer" in message]
        self.assertTrue(proposal_messages)
        self.assertNotIn("est.", proposal_messages[0])
        self.assertTrue(synthesis_messages)
        self.assertTrue(all("est." in message for message in synthesis_messages))

    def test_conduct_reports_exact_pipeline_step_on_each_active_operation(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Show pipeline progress", td, 1, "now", [])
            agents = [
                roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES
            ]
            statuses = []
            roundtable.conduct(
                session, *agents, lambda *_: None,
                lambda active, message: statuses.append((tuple(active), message)),
                synthesis_passes=2, stagger=0)

        active_messages = []
        for active, message in statuses:
            if active and (not active_messages or message != active_messages[-1]):
                active_messages.append(message)
        self.assertEqual(len(active_messages), 4)
        for index, message in enumerate(active_messages, 1):
            self.assertTrue(message.startswith(f"Step {index}/4 · "), message)

    def test_pick_synthesizer_rotates_by_objective_and_honors_explicit_choice(self):
        with tempfile.TemporaryDirectory() as td:
            agents = {name: roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES}
            claude = agents["Claude"]
            session_a = roundtable.Session("Objective A", td, 0, "now", [])
            session_b = roundtable.Session("A very different objective", td, 0, "now", [])
            rotated_a = roundtable.pick_synthesizer("rotate", session_a, *agents.values())[0]
            rotated_a_again = roundtable.pick_synthesizer("rotate", session_a, *agents.values())[0]
            rotated_b = roundtable.pick_synthesizer("rotate", session_b, *agents.values())[0]
            self.assertEqual(rotated_a, rotated_a_again)
            self.assertIn(rotated_a, roundtable.AGENT_NAMES)
            self.assertIn(rotated_b, roundtable.AGENT_NAMES)
            forced = roundtable.pick_synthesizer("claude", session_a, *agents.values())
            self.assertEqual(forced, ("Claude", claude))

    def test_synthesis_order_includes_everyone_starting_with_the_chosen_drafter(self):
        with tempfile.TemporaryDirectory() as td:
            agents = {name: roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES}
            session = roundtable.Session("Objective", td, 0, "now", [])
            order = roundtable.synthesis_order("claude", session, *agents.values())
            self.assertEqual([name for name, _ in order][0], "Claude")
            self.assertEqual({name for name, _ in order}, set(roundtable.AGENT_NAMES))
            self.assertEqual(len(order), len(roundtable.AGENT_NAMES))
            # Stable for the same objective, but the trailing order need not match agent-list order.
            again = roundtable.synthesis_order("claude", session, *agents.values())
            self.assertEqual([name for name, _ in order], [name for name, _ in again])

    def test_synthesis_order_limits_passes_without_changing_the_drafter(self):
        with tempfile.TemporaryDirectory() as td:
            agents = {name: roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES}
            claude = agents["Claude"]
            session = roundtable.Session("Objective", td, 0, "now", [])
            full = roundtable.synthesis_order("claude", session, *agents.values())
            fast = roundtable.synthesis_order("claude", session, *agents.values(), 1)
            self.assertEqual(fast, full[:1])
            self.assertEqual(fast[0], ("Claude", claude))
            self.assertEqual(
                len(roundtable.synthesis_order("claude", session, *agents.values(), 0)), 1)
            self.assertEqual(
                len(roundtable.synthesis_order("claude", session, *agents.values(), 10)),
                len(roundtable.AGENT_NAMES))

    def test_synthesis_order_preferred_first_overrides_chosen_drafter(self):
        with tempfile.TemporaryDirectory() as td:
            agents = {name: roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES}
            session = roundtable.Session("Objective", td, 0, "now", [])
            order = roundtable.synthesis_order(
                "claude", session, *agents.values(), preferred_first="Grok")
            self.assertEqual(order[0][0], "Grok")
            self.assertEqual({name for name, _ in order}, set(roundtable.AGENT_NAMES))
            # Unknown or empty preferred names fall back to --synthesizer selection.
            fallback = roundtable.synthesis_order(
                "claude", session, *agents.values(), preferred_first="NotAnAgent")
            self.assertEqual(fallback[0][0], "Claude")

    def test_synthesize_relays_a_draft_through_every_agent_in_order(self):
        seen_prompts: list[tuple[str, str]] = []

        class RecordingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                seen_prompts.append((self.name, prompt))
                return f"{self.name}'s version"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now",
                                         [roundtable.Turn("Codex", "proposal", "Initial idea")])
            order = [("Claude", RecordingAgent("Claude", workspace)),
                    ("Codex", RecordingAgent("Codex", workspace)),
                    ("Antigravity", RecordingAgent("Antigravity", workspace))]
            statuses = []
            result = roundtable.synthesize(session, order, lambda *_: None,
                                           lambda active, message: statuses.append((tuple(active), message)))
            self.assertEqual(
                result, "Antigravity's version\n\nSigned by: Claude, Codex, Antigravity")
            self.assertEqual([name for name, _ in seen_prompts], ["Claude", "Codex", "Antigravity"])
            self.assertIn("final editor", seen_prompts[0][1].lower())
            self.assertIn("CURRENT DRAFT FINAL ANSWER", seen_prompts[1][1])
            self.assertIn("Claude's version", seen_prompts[1][1])
            self.assertIn("Codex's version", seen_prompts[2][1])
            self.assertEqual(statuses[0], (("Claude",), "Claude is drafting the final answer"))
            self.assertEqual(statuses[1], (("Codex",), "Codex is refining the final answer"))
            self.assertEqual(statuses[-1], ((), "Final answer complete"))

    def test_synthesize_keeps_latest_when_refiner_repeats_structured_answer(self):
        class RepeatingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                return (
                    "## Completed\n\n- 10 tests passed.\n\n## Failed / incomplete\n\nNone.\n\n"
                    "## Completed\n\n- 11 tests passed.\n\n## Failed / incomplete\n\nNone."
                )

        with tempfile.TemporaryDirectory() as td:
            agent = RepeatingAgent("Claude", Path(td))
            session = roundtable.Session("Goal", td, 0, "now", [])
            ticks = []
            result = roundtable.synthesize(
                session, [("Claude", agent)],
                lambda name, line: ticks.append((name, line)), lambda *_: None)

        self.assertEqual(result.count("## Completed"), 1)
        self.assertNotIn("10 tests passed", result)
        self.assertIn("11 tests passed", result)
        self.assertIn(
            ("Claude", "discarded an earlier duplicated final-answer block"), ticks)

    def test_synthesize_keeps_last_good_draft_when_a_refiner_fails(self):
        class RefiningAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if self.name == "Antigravity":
                    raise RuntimeError(
                        "Antigravity exited with status 1\ntimeout waiting for response")
                return f"{self.name}'s version"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            order = [
                ("Claude", RefiningAgent("Claude", workspace)),
                ("Antigravity", RefiningAgent("Antigravity", workspace)),
                ("Codex", RefiningAgent("Codex", workspace)),
            ]
            ticks = []
            completed = []
            result = roundtable.synthesize(
                session, order, lambda name, line: ticks.append((name, line)),
                lambda *_: None, step_complete=completed.append)
        self.assertEqual(result, "Codex's version\n\nSigned by: Claude, Codex")
        self.assertIn(
            ("Antigravity", "refinement skipped after failure: timeout waiting for response"),
            ticks)
        self.assertEqual(completed, [1, 1, 1])

    def test_sign_agent_work_does_not_duplicate_an_existing_signature(self):
        self.assertEqual(
            roundtable.sign_agent_work("Codex", "Finished.\n\nSigned: Codex"),
            "Finished.\n\nSigned: Codex")

    def test_failed_refiner_is_absent_from_final_signature(self):
        class Agent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if self.name == "Aider":
                    raise RuntimeError("Aider exited with status 1\nprovider failed")
                return f"{self.name} result"

        with tempfile.TemporaryDirectory() as td:
            order = [(name, Agent(name, Path(td))) for name in ("Claude", "Aider")]
            result = roundtable.synthesize(
                roundtable.Session("Goal", td, 0, "now", []),
                order, lambda *_: None, lambda *_: None)
        self.assertTrue(result.endswith("Signed by: Claude"))
        self.assertNotIn("Signed by: Claude, Aider", result)

    def test_synthesize_still_retries_and_fails_when_the_initial_drafter_fails(self):
        class FailingAgent(roundtable.Agent):
            attempts = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).attempts += 1
                raise RuntimeError("Claude exited with status 1\ntimeout waiting for response")

        with tempfile.TemporaryDirectory() as td:
            agent = FailingAgent("Claude", Path(td))
            with mock.patch.object(roundtable.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "timeout waiting for response"):
                    roundtable.synthesize(
                        roundtable.Session("Goal", td, 0, "now", []),
                        [("Claude", agent)], lambda *_: None, lambda *_: None)
        self.assertEqual(FailingAgent.attempts, 2)

    def test_synthesis_renders_unchanged_transcript_once_for_all_passes(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session(
                "Solve it", td, 0, "now",
                [roundtable.Turn("Codex", "proposal", "Initial idea")])
            order = [
                ("Claude", roundtable.MockAgent("Claude", workspace)),
                ("Codex", roundtable.MockAgent("Codex", workspace)),
                ("Grok", roundtable.MockAgent("Grok", workspace)),
            ]
            with mock.patch.object(
                    roundtable, "transcript", wraps=roundtable.transcript) as render:
                roundtable.synthesize(
                    session, order, lambda *_: None, lambda *_: None)
        self.assertEqual(render.call_count, 1)

    def test_dead_code_check_prompt_asks_for_call_site_search_not_prose(self):
        prompt = roundtable.dead_code_check_prompt("Goal", [])
        self.assertIn("dead code", prompt.lower())
        self.assertIn("call site", prompt.lower())
        self.assertIn("test suite", prompt.lower())
        self.assertIn("Goal", prompt)

    def test_run_dead_code_check_appends_a_turn_and_reports_completion(self):
        class FoundNothingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                return "Searched the diff for unused functions; found nothing to remove."

        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0, "now", [])
            agent = FoundNothingAgent("Codex", Path(td))
            statuses = []
            roundtable.run_dead_code_check(
                session, "Codex", agent, lambda *_: None,
                lambda active, message: statuses.append((tuple(active), message)))
        self.assertEqual(len(session.turns), 1)
        self.assertEqual(session.turns[0].speaker, "Codex")
        self.assertEqual(session.turns[0].phase, "dead-code-check")
        self.assertIn("found nothing to remove", session.turns[0].content)
        self.assertIn(("Codex",), [names for names, _ in statuses])
        self.assertIn(((), "Dead-code check complete"), statuses)

    def test_run_dead_code_check_failure_is_soft_and_appends_no_turn(self):
        class FailingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                raise RuntimeError("Codex exited with status 1\ntimeout waiting for response")

        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0, "now", [])
            agent = FailingAgent("Codex", Path(td))
            ticks = []
            with mock.patch.object(roundtable.time, "sleep"):
                roundtable.run_dead_code_check(
                    session, "Codex", agent, lambda name, line: ticks.append((name, line)),
                    lambda *_: None)
        self.assertEqual(session.turns, [])
        self.assertTrue(
            any("dead-code check skipped after failure" in line for _, line in ticks), ticks)

    def test_conduct_runs_dead_code_check_before_synthesis_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                               dead_code_check=True)
        phases = [turn.phase for turn in session.turns]
        self.assertIn("dead-code-check", phases)
        self.assertLess(phases.index("dead-code-check"), phases.index("consensus"))

    def test_conduct_skips_dead_code_check_already_completed_on_resume(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                "Goal", td, 0, "now",
                [roundtable.Turn("Codex", "proposal", "done"),
                 roundtable.Turn("Codex", "dead-code-check", "already checked; nothing to remove")])
            agents = [roundtable.MockAgent(name, Path(td)) for name in roundtable.AGENT_NAMES]
            with mock.patch.object(
                    roundtable, "run_dead_code_check",
                    side_effect=AssertionError("dead-code check ran twice")):
                roundtable.conduct(
                    session, *agents, lambda *_: None, lambda *_: None, dead_code_check=True,
                    completed_phases={"proposal", "dead-code-check"})
        self.assertEqual(
            [turn.phase for turn in session.turns].count("dead-code-check"), 1)

    def test_role_hints_by_slot_has_exactly_one_role_per_agent(self):
        """Guards against ROLE_HINTS_BY_SLOT and AGENT_NAMES drifting out of sync -- role_hints_for
        zips them together, which silently truncates rather than erroring if the counts differ
        (e.g. a new agent CLI added without a matching new role, or a role hint deleted), leaving
        an agent with no role hint in its prompt or a role permanently unassigned."""
        self.assertEqual(len(roundtable.ROLE_HINTS_BY_SLOT), len(roundtable.AGENT_NAMES))
        self.assertEqual(len(set(roundtable.ROLE_HINTS_BY_SLOT)), len(roundtable.ROLE_HINTS_BY_SLOT))

    def test_prompt_for_includes_each_agents_role_hint(self):
        hints = roundtable.role_hints_for("Goal")
        codex_prompt = roundtable.prompt_for("Goal", [], "proposal", "Codex")
        claude_prompt = roundtable.prompt_for("Goal", [], "proposal", "Claude")
        antigravity_prompt = roundtable.prompt_for("Goal", [], "proposal", "Antigravity")
        self.assertIn(hints["Codex"], codex_prompt)
        self.assertIn(hints["Claude"], claude_prompt)
        self.assertIn(hints["Antigravity"], antigravity_prompt)
        self.assertNotIn(hints["Codex"], claude_prompt)

    def test_role_hints_rotate_across_agents_by_objective_but_stay_stable_per_objective(self):
        mappings = [roundtable.role_hints_for(f"Objective {i}") for i in range(12)]
        for mapping in mappings:
            self.assertEqual(set(mapping), set(roundtable.AGENT_NAMES))
            self.assertEqual(set(mapping.values()), set(roundtable.ROLE_HINTS_BY_SLOT))
        self.assertEqual(roundtable.role_hints_for("Same objective"),
                         roundtable.role_hints_for("Same objective"))
        codex_hints = {mapping["Codex"] for mapping in mappings}
        self.assertGreater(len(codex_hints), 1)  # Codex isn't stuck in the same lane every time

    def test_extract_dibs_keeps_only_each_agents_latest_claim(self):
        turns = [
            roundtable.Turn("Codex", "proposal", "DIBS: the auth flow\nDid the work."),
            roundtable.Turn("Claude", "proposal", "DIBS: the docs\nWrote the docs."),
            roundtable.Turn("Codex", "review 1", "DIBS: the retry logic\nSwitched focus."),
            roundtable.Turn("Antigravity", "proposal", "No dibs line here."),
        ]
        self.assertEqual(roundtable.extract_dibs(turns),
                         {"Codex": "the retry logic", "Claude": "the docs"})

    def test_extract_dibs_handles_markdown_formatting(self):
        turns = [
            roundtable.Turn("Codex", "proposal", "**DIBS: the auth flow**\nDid the work."),
            roundtable.Turn("Claude", "proposal", "`DIBS: the docs`\nWrote the docs."),
            roundtable.Turn("Antigravity", "proposal", "### DIBS: the UI widgets\nDrafted widgets."),
            roundtable.Turn("Aider", "proposal", "- DIBS: the backoff logic\nImplemented backoff."),
        ]
        self.assertEqual(roundtable.extract_dibs(turns),
                         {"Codex": "the auth flow", "Claude": "the docs",
                          "Antigravity": "the UI widgets", "Aider": "the backoff logic"})

    def test_extract_dibs_does_not_allocate_a_lowercase_copy_before_regex_search(self):
        class NoLowerCopy(str):
            def lower(self):
                raise AssertionError("extract_dibs should use its case-insensitive regex directly")

        turns = [
            roundtable.Turn("Codex", "proposal", NoLowerCopy("DIBS: the parser\nWorking")),
        ]
        self.assertEqual(roundtable.extract_dibs(turns), {"Codex": "the parser"})

    def test_prompt_for_tells_an_agent_what_others_have_already_claimed(self):
        turns = [roundtable.Turn("Codex", "proposal", "DIBS: the auth flow\nDid the work.")]
        claude_prompt = roundtable.prompt_for("Goal", turns, "review 1", "Claude")
        codex_prompt = roundtable.prompt_for("Goal", turns, "review 1", "Codex")
        self.assertIn("Codex has dibs on the auth flow", claude_prompt)
        self.assertNotIn("has dibs on", codex_prompt)  # an agent isn't told about its own claim
        self.assertIn("DIBS: <short claim>", claude_prompt)  # instructed to state its own, too

    def test_prompt_for_has_no_claims_note_when_nobody_has_claimed_anything(self):
        prompt = roundtable.prompt_for("Goal", [], "proposal", "Codex")
        self.assertNotIn("Already claimed", prompt)
        self.assertIn("DIBS: <short claim>", prompt)

    def test_reassignment_prompt_names_who_is_still_working_and_what_they_claimed(self):
        turns = [roundtable.Turn("Claude", "proposal", "DIBS: the frontend\nWorking on it.")]
        prompt = roundtable.reassignment_prompt("Goal", turns, "proposal", "Codex",
                                                {"Claude", "Antigravity"})
        self.assertIn("Antigravity, Claude", prompt)
        self.assertIn("Claude has dibs on the frontend", prompt)
        self.assertIn("pick up a different", prompt)
        self.assertIn("help", prompt)

    def test_spinner_frame_differs_by_agent_and_cycles(self):
        codex_frames = {roundtable.spinner_frame("Codex", i) for i in range(12)}
        claude_frames = {roundtable.spinner_frame("Claude", i) for i in range(12)}
        antigravity_frames = {roundtable.spinner_frame("Antigravity", i) for i in range(12)}
        self.assertGreater(len(codex_frames), 1)
        self.assertGreater(len(claude_frames), 1)
        self.assertGreater(len(antigravity_frames), 1)
        self.assertTrue(codex_frames.isdisjoint(claude_frames))
        self.assertTrue(codex_frames.isdisjoint(antigravity_frames))
        self.assertEqual(roundtable.spinner_frame("Codex", 0),
                         roundtable.spinner_frame("Codex", len(roundtable.AGENT_SPINNERS["Codex"][0])))
        # Verify speed divisor advances frame correctly (Claude divisor is 3, Codex divisor is 1)
        self.assertEqual(roundtable.spinner_frame("Claude", 0), "·")
        self.assertEqual(roundtable.spinner_frame("Claude", 1), "·")
        self.assertEqual(roundtable.spinner_frame("Claude", 2), "·")
        self.assertEqual(roundtable.spinner_frame("Claude", 3), "✢")
        self.assertEqual(roundtable.spinner_frame("Codex", 0), "◌")
        self.assertEqual(roundtable.spinner_frame("Codex", 1), "○")

    def test_parallel_phase_reports_agents_completing_independently(self):
        delays = {"Codex": 0.02, "Claude": 0.12, "Antigravity": 0.22}

        class StaggeredAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(delays[self.name])
                return self.name

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Parallel task", td, 0, "now", [])
            agents = [(name, StaggeredAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            active_states = []
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None,
                lambda active, _message: active_states.append(tuple(active)), "Working",
            )
            self.assertEqual(active_states[0], ("Codex", "Claude", "Antigravity"))
            self.assertIn(("Claude", "Antigravity"), active_states)
            self.assertIn(("Antigravity",), active_states)
            self.assertEqual(active_states[-1], ())

    def test_parallel_phase_ticks_a_completion_line_when_each_agent_finishes(self):
        delays = {"Codex": 0.02, "Claude": 0.12, "Antigravity": 0.22}

        class StaggeredAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(delays[self.name])
                return self.name

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Parallel task", td, 0, "now", [])
            agents = [(name, StaggeredAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            ticks = []
            roundtable._run_parallel_phase(
                session, agents, "proposal",
                lambda speaker, line: ticks.append((speaker, line)),
                lambda *_: None, "Working",
            )
            finished = [(speaker, line) for speaker, line in ticks if "finished this phase" in line]
            self.assertEqual([speaker for speaker, _ in finished], ["Codex", "Claude", "Antigravity"])
            # Claude normally remains pending when Codex's completion is observed, but a busy test
            # runner may deliver both completed futures in one polling cycle. Antigravity's wider
            # delay is the stable contract this assertion needs to cover.
            self.assertIn("waiting on Antigravity", finished[0][1])
            self.assertIn("waiting on nothing else", finished[-1][1])

    def test_signals_task_complete_matches_marker_near_end_of_text(self):
        self.assertFalse(roundtable.signals_task_complete(""))
        self.assertTrue(roundtable.signals_task_complete("All done.\nTASK STATUS: complete"))
        self.assertTrue(roundtable.signals_task_complete("All done.\ntask status: COMPLETE  "))
        self.assertTrue(roundtable.signals_task_complete("All done.\nTASK STATUS: complete\n\n"))
        self.assertTrue(roundtable.signals_task_complete("All done.\n`TASK STATUS: complete`"))
        self.assertTrue(roundtable.signals_task_complete("All done.\n**TASK STATUS: complete**"))
        self.assertTrue(roundtable.signals_task_complete("All done.\nTASK STATUS: complete."))
        self.assertFalse(roundtable.signals_task_complete("All done.\nTASK STATUS: in-progress"))
        self.assertFalse(roundtable.signals_task_complete("Still working on it."))
        buried = "TASK STATUS: complete" + ("x" * 400)
        self.assertFalse(roundtable.signals_task_complete(buried))

    def test_signals_task_complete_only_accepts_the_final_marker(self):
        self.assertFalse(roundtable.signals_task_complete(
            "I considered TASK STATUS: complete, but verification remains.\n"
            "TASK STATUS: in-progress"
        ))
        self.assertFalse(roundtable.signals_task_complete(
            "TASK STATUS: complete\nVerification still pending."
        ))

    def test_stored_signed_turn_preserves_task_complete_signal(self):
        turn = roundtable.Turn(
            "Codex", "proposal",
            roundtable.sign_agent_work("Codex", "Done and verified.\nTASK STATUS: complete"),
        )
        self.assertTrue(roundtable.turn_signals_task_complete(turn))
        self.assertFalse(roundtable.turn_signals_task_complete(roundtable.Turn(
            "Codex", "proposal",
            roundtable.sign_agent_work("Codex", "Still working.\nTASK STATUS: in-progress"),
        )))

    def test_prompt_for_includes_task_status_hint_only_when_requested(self):
        plain = roundtable.prompt_for("Goal", [], "proposal", "Codex")
        checked = roundtable.prompt_for("Goal", [], "proposal", "Codex", task_status_check=True)
        self.assertNotIn("TASK STATUS", plain)
        self.assertIn("TASK STATUS: complete", checked)

    def test_run_parallel_phase_stops_other_agents_once_one_signals_task_complete(self):
        class WaitingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                while not self.cancel_event.is_set():
                    time.sleep(0.01)
                raise RuntimeError(f"{self.name} cancelled")

        class DoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                return "Wrote the file and verified it.\nTASK STATUS: complete"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Physics demo", td, 0, "now", [])
            agents = [("Codex", DoneAgent("Codex", workspace)),
                     ("Claude", WaitingAgent("Claude", workspace)),
                     ("Antigravity", WaitingAgent("Antigravity", workspace))]
            ticks = []
            roundtable._run_parallel_phase(
                session, agents, "proposal",
                lambda speaker, line: ticks.append((speaker, line)),
                lambda *_: None, "Working", task_status_check=True,
            )
            self.assertEqual([turn.speaker for turn in session.turns], ["Codex"])
            stopped = [(speaker, line) for speaker, line in ticks if "stopped early" in line]
            self.assertEqual({speaker for speaker, _ in stopped}, {"Claude", "Antigravity"})
            self.assertTrue(all("Codex already completed" in line for _, line in stopped))

    def test_run_parallel_phase_without_task_status_check_ignores_the_marker(self):
        class DoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(0.02)
                return "Done.\nTASK STATUS: complete"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, DoneAgent(name, workspace)) for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working")
            self.assertEqual({turn.speaker for turn in session.turns}, {"Codex", "Claude", "Antigravity"})

    def test_run_parallel_phase_records_completion_from_the_last_finisher(self):
        """Regression: remaining==empty must not prevent completed_by from being returned.

        The last agent to finish has no peers left to cancel, but conduct still needs the
        completer's name so it can skip further reviews and prefer that agent as drafter.
        """
        others_finished = threading.Event()
        finished_count = [0]
        finished_lock = threading.Lock()

        class SlowDoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if self.name == "Claude":
                    # Wait for both peers instead of racing a sleep duration against theirs, so
                    # this test can't flip under system load (e.g. a busy CI box or, as here, other
                    # agents' CLI processes sharing the machine).
                    others_finished.wait(timeout=5)
                    return "Claude finished last.\nTASK STATUS: complete"
                with finished_lock:
                    finished_count[0] += 1
                    if finished_count[0] == 2:
                        others_finished.set()
                return f"{self.name} still working — not done yet."

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Last finisher", td, 0, "now", [])
            agents = [(name, SlowDoneAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            ticks = []
            completed_by = roundtable._run_parallel_phase(
                session, agents, "proposal",
                lambda speaker, line: ticks.append((speaker, line)),
                lambda *_: None, "Working", task_status_check=True, stagger=0,
            )
            self.assertEqual(completed_by, "Claude")
            self.assertEqual({turn.speaker for turn in session.turns},
                             {"Codex", "Claude", "Antigravity"})
            self.assertTrue(any(speaker == "Claude" and "marked the task complete" in line
                                for speaker, line in ticks))

    def test_run_parallel_phase_records_completion_when_every_agent_finishes_together(self):
        """Same-poll finish of all agents used to leave remaining empty before any check ran."""
        class InstantDoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                return f"{self.name} done.\nTASK STATUS: complete"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("All at once", td, 0, "now", [])
            agents = [(name, InstantDoneAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            completed_by = roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                task_status_check=True, stagger=0,
            )
            self.assertEqual(completed_by, "Codex")
            self.assertEqual({turn.speaker for turn in session.turns},
                             {"Codex", "Claude", "Antigravity"})

    def test_reassign_idle_gives_a_finished_agent_extra_work_while_others_run(self):
        calls = {"Codex": 0}

        class MixedAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if self.name == "Codex":
                    calls["Codex"] += 1
                    return "Codex extra content" if calls["Codex"] > 1 else "Codex proposal content"
                time.sleep(0.2)
                return f"{self.name} proposal content"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, MixedAgent(name, workspace)) for name in ("Codex", "Claude", "Antigravity")]
            ticks = []
            roundtable._run_parallel_phase(
                session, agents, "proposal",
                lambda speaker, line: ticks.append((speaker, line)),
                lambda *_: None, "Working", reassign_idle=True,
            )
            phases = [(t.speaker, t.phase) for t in session.turns]
            self.assertIn(("Codex", "proposal · extra"), phases)
            self.assertEqual({t.phase for t in session.turns if t.speaker != "Codex"}, {"proposal"})
            self.assertEqual(calls["Codex"], 2)
            self.assertTrue(any("picking up extra work" in line for _, line in ticks))

    def test_reassign_idle_starts_only_one_bonus_per_phase(self):
        """Later finishers stay idle once the first bonus is underway — concurrent extras burn
        tokens and risk colliding workspace edits without shortening wall-clock further."""
        calls = {"Codex": 0, "Claude": 0}
        bonus_speakers: list[str] = []

        class StaggeredAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if "pick up a different" in prompt or "· extra" in prompt:
                    bonus_speakers.append(self.name)
                    return f"{self.name} extra content"
                if self.name == "Codex":
                    calls["Codex"] += 1
                    return "Codex proposal content"
                if self.name == "Claude":
                    time.sleep(0.05)
                    calls["Claude"] += 1
                    return "Claude proposal content"
                time.sleep(0.25)
                return f"{self.name} proposal content"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, StaggeredAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity", "Aider")]
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                reassign_idle=True, stagger=0,
            )
            extra_turns = [t for t in session.turns if t.phase.endswith("· extra")]
            self.assertEqual(len(extra_turns), 1, session.turns)
            # First finisher among the fast pair gets the sole bonus; set iteration order is
            # not load-bearing — only that later finishers do not start a second one.
            self.assertEqual(len(bonus_speakers), 1)
            self.assertIn(bonus_speakers[0], ("Codex", "Claude"))
            self.assertEqual(calls["Claude"] + calls["Codex"], 2)  # each primary once

    def test_reassign_idle_skips_bonus_when_only_one_primary_remains(self):
        """With only two agents, the first finisher always sees a single remaining primary — a
        bonus would almost always be cancelled mid-flight after paying full startup cost."""
        calls = {"Codex": 0}

        class PairAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if "pick up a different" in prompt or "· extra" in prompt:
                    calls["Codex"] += 10  # should never happen
                    return "unexpected bonus"
                if self.name == "Codex":
                    calls["Codex"] += 1
                    return "Codex proposal content"
                time.sleep(0.2)
                return f"{self.name} proposal content"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, PairAgent(name, workspace)) for name in ("Codex", "Claude")]
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                reassign_idle=True, stagger=0,
            )
            self.assertEqual(calls["Codex"], 1)  # primary only
            self.assertEqual({t.phase for t in session.turns}, {"proposal"})
            self.assertFalse(any("extra" in t.phase for t in session.turns))

    def test_reassign_idle_bonus_uses_low_suggested_effort(self):
        efforts: list[str | None] = []

        class EffortProbeAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if "pick up a different" in prompt or "· extra" in prompt:
                    efforts.append(self.suggested_effort)
                    return "bonus"
                if self.name == "Codex":
                    return "Codex primary"
                time.sleep(0.2)
                return f"{self.name} primary"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, EffortProbeAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                reassign_idle=True, stagger=0,
            )
            self.assertEqual(efforts, ["low"])
            self.assertIsNone(agents[0][1].suggested_effort)

    def test_reassign_idle_off_by_default_leaves_a_finished_agent_idle(self):
        calls = {"Codex": 0}

        class MixedAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if self.name == "Codex":
                    calls["Codex"] += 1
                    return "Codex proposal content"
                time.sleep(0.1)
                return f"{self.name} proposal content"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, MixedAgent(name, workspace)) for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working")
            self.assertEqual(calls["Codex"], 1)
            self.assertEqual({t.phase for t in session.turns}, {"proposal"})

    def test_reassign_idle_does_not_apply_to_the_agent_that_declared_task_complete(self):
        class WaitingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                while not self.cancel_event.is_set():
                    time.sleep(0.01)
                raise RuntimeError(f"{self.name} cancelled")

        class DoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                return "Wrote the file and verified it.\nTASK STATUS: complete"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [("Codex", DoneAgent("Codex", workspace)),
                     ("Claude", WaitingAgent("Claude", workspace)),
                     ("Antigravity", WaitingAgent("Antigravity", workspace))]
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                task_status_check=True, reassign_idle=True,
            )
            self.assertEqual([t.phase for t in session.turns], ["proposal"])  # no "· extra" turn

    def test_reassign_idle_discards_a_bonus_attempt_that_does_not_finish_in_time(self):
        class HangingBonusAgent(roundtable.Agent):
            calls = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).calls += 1
                if type(self).calls == 1:
                    return "Codex proposal content"
                while not self.cancel_event.is_set():
                    time.sleep(0.01)
                raise RuntimeError("Codex cancelled")

        HangingBonusAgent.calls = 0

        class SlowAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                time.sleep(0.2)
                return f"{self.name} proposal content"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [("Codex", HangingBonusAgent("Codex", workspace)),
                     ("Claude", SlowAgent("Claude", workspace)),
                     ("Antigravity", SlowAgent("Antigravity", workspace))]
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working", reassign_idle=True)
            # The bonus attempt got cancelled once the round closed, so no "· extra" turn lands —
            # but the phase still completes normally instead of hanging on it.
            self.assertEqual({t.phase for t in session.turns}, {"proposal"})
            self.assertEqual({t.speaker for t in session.turns}, {"Codex", "Claude", "Antigravity"})

    def test_conduct_lets_agents_skipped_by_task_status_check_review_next_round(self):
        class DoneOnceAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if f"YOUR TURN ({self.name}, proposal)" in prompt:
                    if self.name == "Codex":
                        return "Wrote the file and verified it.\nTASK STATUS: complete"
                    while not self.cancel_event.is_set():
                        time.sleep(0.01)
                    raise RuntimeError(f"{self.name} cancelled")
                return f"{self.name} contribution"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Physics demo", td, 1, "now", [])
            agents = [DoneOnceAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                               synthesizer="codex", task_status_check=True)
            proposal_speakers = [t.speaker for t in session.turns if t.phase == "proposal"]
            review_speakers = [t.speaker for t in session.turns if t.phase == "review 1"]
            self.assertEqual(proposal_speakers, ["Codex"])
            self.assertEqual(set(review_speakers), set(roundtable.AGENT_NAMES))

    def test_resumed_conduct_does_not_repeat_reviews_after_checkpointed_completion(self):
        class RecordingAgent(roundtable.Agent):
            review_phases: list[str] = []

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                for phase in ("review 1", "review 2", "review 3"):
                    if f", {phase})" in prompt:
                        type(self).review_phases.append(phase)
                        break
                return f"{self.name} contribution"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            completed = roundtable.Turn(
                "Codex", "proposal",
                roundtable.sign_agent_work(
                    "Codex", "Implemented and verified.\nTASK STATUS: complete"),
            )
            session = roundtable.Session("Goal", td, 3, "now", [completed])
            agents = [RecordingAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            RecordingAgent.review_phases = []
            roundtable.conduct(
                session, *agents, lambda *_: None, lambda *_: None,
                synthesizer="codex", synthesis_passes=1, task_status_check=True,
                completed_phases={"proposal"}, stagger=0,
            )
            self.assertEqual(set(RecordingAgent.review_phases), {"review 1"})
            self.assertEqual(len(RecordingAgent.review_phases), len(roundtable.AGENT_NAMES))
            self.assertFalse(any(turn.phase in {"review 2", "review 3"}
                                 for turn in session.turns))

    def test_resumed_conduct_still_runs_one_verification_review_after_a_mid_review_completion(self):
        """Regression: the checkpointed-completion reconstruction in conduct() must count phases
        that ran *before* the completing phase (no decrement) separately from phases after it
        (each one spends one unit of the one-verification-review budget). A completion recorded on
        "review 1" rather than "proposal" exercises that ordering: proposal must not consume the
        budget (it precedes the completion), review 2 is the one allowed verification round, and
        review 3 must still be skipped."""
        class RecordingAgent(roundtable.Agent):
            review_phases: list[str] = []

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                for phase in ("review 1", "review 2", "review 3"):
                    if f", {phase})" in prompt:
                        type(self).review_phases.append(phase)
                        break
                return f"{self.name} contribution"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proposal_turns = [
                roundtable.Turn(name, "proposal", roundtable.sign_agent_work(name, "Proposed."))
                for name in roundtable.AGENT_NAMES
            ]
            review_turns = [
                roundtable.Turn(
                    name, "review 1",
                    roundtable.sign_agent_work(
                        name, "Implemented and verified.\nTASK STATUS: complete"
                        if name == "Codex" else "Reviewed."))
                for name in roundtable.AGENT_NAMES
            ]
            session = roundtable.Session(
                "Goal", td, 3, "now", proposal_turns + review_turns)
            agents = [RecordingAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            RecordingAgent.review_phases = []
            roundtable.conduct(
                session, *agents, lambda *_: None, lambda *_: None,
                synthesizer="codex", synthesis_passes=1, task_status_check=True,
                completed_phases={"proposal", "review 1"}, stagger=0,
            )
            self.assertEqual(set(RecordingAgent.review_phases), {"review 2"})
            self.assertEqual(len(RecordingAgent.review_phases), len(roundtable.AGENT_NAMES))
            self.assertFalse(any(turn.phase == "review 3" for turn in session.turns))

    def test_conduct_synthesis_survives_a_task_status_check_cancellation_earlier_in_the_run(self):
        """Regression: a task_status_check completion sets a shared cancel_event on every agent to
        stop the round. Real Agent.run() falls back to self.cancel_event when no override is passed
        (agent.run(prompt, on_tick) with no third arg) — if that Event is never cleared, the next
        such call (synthesize(), which used to call agent.run this way) saw an already-cancelled
        Event and aborted the whole run before producing anything."""
        class RealisticAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                cancel_event = cancel_event or self.cancel_event
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError(f"{self.name} cancelled")
                if f"YOUR TURN ({self.name}, proposal)" in prompt:
                    if self.name == "Codex":
                        return "Done and verified.\nTASK STATUS: complete"
                    while not self.cancel_event.is_set():
                        time.sleep(0.01)
                    raise RuntimeError(f"{self.name} cancelled")
                return f"{self.name} says: final text"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Physics demo", td, 0, "now", [])
            agents = [RealisticAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                               synthesizer="codex", task_status_check=True)
            self.assertTrue(session.final)
            self.assertEqual([t.phase for t in session.turns], ["proposal", "consensus"])

    def test_conduct_skips_remaining_reviews_after_verified_completion(self):
        """After TASK STATUS: complete + one verification review, later configured reviews are cut."""
        class DoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if f"YOUR TURN ({self.name}, proposal)" in prompt:
                    if self.name == "Codex":
                        return "Implemented and verified.\nTASK STATUS: complete"
                    while not self.cancel_event.is_set():
                        time.sleep(0.01)
                    raise RuntimeError(f"{self.name} cancelled")
                return f"{self.name} review contribution"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Multi-review goal", td, 3, "now", [])
            agents = [DoneAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            statuses: list[str] = []
            roundtable.conduct(
                session, *agents, lambda *_: None,
                lambda _active, message: statuses.append(message),
                synthesizer="codex", synthesis_passes=1, task_status_check=True, stagger=0,
            )
            phases = {turn.phase for turn in session.turns}
            self.assertIn("proposal", phases)
            self.assertIn("review 1", phases)
            self.assertNotIn("review 2", phases)
            self.assertNotIn("review 3", phases)
            self.assertTrue(any("skipping remaining review" in message for message in statuses))
            self.assertTrue(any(message.startswith("Step 1/5 · ") for message in statuses))
            self.assertTrue(any(message.startswith("Step 2/5 · ") for message in statuses))
            # Once reviews 2 and 3 are abandoned, the final pass is exactly the last remaining step.
            self.assertTrue(any(message.startswith("Step 3/3 · ") for message in statuses))

    def test_conduct_trims_synthesis_and_prefers_completer_after_early_complete(self):
        """When the objective is already done, synthesis is draft+one-refine and the completer drafts."""
        synthesis_speakers: list[str] = []

        class DoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if "CURRENT DRAFT FINAL ANSWER" in prompt or "You are the final editor" in prompt:
                    synthesis_speakers.append(self.name)
                    return f"{self.name} final"
                if f"YOUR TURN ({self.name}, proposal)" in prompt:
                    if self.name == "Claude":
                        return "Claude finished the work.\nTASK STATUS: complete"
                    while not self.cancel_event.is_set():
                        time.sleep(0.01)
                    raise RuntimeError(f"{self.name} cancelled")
                return f"{self.name} contribution"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Early complete synth", td, 0, "now", [])
            agents = [DoneAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            statuses: list[str] = []
            roundtable.conduct(
                session, *agents, lambda *_: None,
                lambda _active, message: statuses.append(message),
                synthesizer="rotate", synthesis_passes=6, task_status_check=True, stagger=0,
            )
            self.assertEqual(synthesis_speakers[0], "Claude")
            self.assertEqual(len(synthesis_speakers),
                             roundtable.EARLY_COMPLETE_SYNTHESIS_PASSES)
            self.assertTrue(any("shorter final synthesis" in message for message in statuses))
            self.assertIn("Claude", session.final)

    def test_run_with_retry_recovers_from_a_transient_failure(self):
        class FlakyAgent(roundtable.Agent):
            attempts = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).attempts += 1
                if type(self).attempts == 1:
                    raise RuntimeError(f"{self.name} exited with status 1\ntimeout waiting for response")
                return "recovered on retry"

        agent = FlakyAgent("Codex", Path("/tmp"))
        ticks = []
        with mock.patch.object(threading.Event, "wait", return_value=False) as wait_mock:
            result = roundtable._run_with_retry(agent, "prompt", ticks.append)
        self.assertEqual(result, "recovered on retry")
        self.assertEqual(FlakyAgent.attempts, 2)
        wait_mock.assert_called_with(roundtable.RETRY_BACKOFF_SECONDS)
        retry_tick = next(t for t in ticks if "retrying once" in t)
        self.assertIn(f"in {roundtable.RETRY_BACKOFF_SECONDS:g}s", retry_tick)
        self.assertIn("(attempt 2/2)", retry_tick)

    def test_run_with_retry_tells_agent_to_check_progress_before_a_transient_retry(self):
        class FlakyAgent(roundtable.Agent):
            prompts = []

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).prompts.append(prompt)
                if len(type(self).prompts) == 1:
                    raise RuntimeError(f"{self.name} exited with status 1\ntimeout")
                return "recovered"

        agent = FlakyAgent("Codex", Path("/tmp"))
        # Backoff uses cancel_event.wait so a task_status_check cancel can interrupt it; patch that
        # path rather than time.sleep, which is no longer the wait mechanism.
        with mock.patch.object(threading.Event, "wait", return_value=False):
            roundtable._run_with_retry(agent, "original prompt", lambda _: None)
        self.assertEqual(FlakyAgent.prompts[0], "original prompt")
        self.assertIn(roundtable.RERUN_PROGRESS_NOTE, FlakyAgent.prompts[1])
        self.assertTrue(FlakyAgent.prompts[1].startswith("original prompt"))

    def test_run_with_retry_raises_if_the_retry_also_fails(self):
        class AlwaysFailsAgent(roundtable.Agent):
            attempts = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).attempts += 1
                raise RuntimeError(f"{self.name} exited with status 1\nstill broken")

        agent = AlwaysFailsAgent("Codex", Path("/tmp"))
        with mock.patch.object(roundtable.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "still broken"):
                roundtable._run_with_retry(agent, "prompt", lambda _: None)
        self.assertEqual(AlwaysFailsAgent.attempts, 2)

    def test_run_with_retry_can_disable_transient_retries(self):
        class AlwaysFailsAgent(roundtable.Agent):
            attempts = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).attempts += 1
                raise RuntimeError(f"{self.name} exited with status 1\nstill broken")

        agent = AlwaysFailsAgent("Codex", Path("/tmp"))
        with mock.patch.object(roundtable.time, "sleep") as sleep_mock:
            with self.assertRaisesRegex(RuntimeError, "still broken"):
                roundtable._run_with_retry(
                    agent, "prompt", lambda _: None, transient_retries=0)
        self.assertEqual(AlwaysFailsAgent.attempts, 1)
        sleep_mock.assert_not_called()

    def test_run_with_retry_never_retries_a_deliberate_cancellation(self):
        class CancelledAgent(roundtable.Agent):
            attempts = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).attempts += 1
                raise RuntimeError(f"{self.name} cancelled")

        agent = CancelledAgent("Codex", Path("/tmp"))
        with mock.patch.object(roundtable.time, "sleep") as sleep_mock:
            with self.assertRaisesRegex(RuntimeError, "Codex cancelled"):
                roundtable._run_with_retry(agent, "prompt", lambda _: None)
        self.assertEqual(CancelledAgent.attempts, 1)  # no retry for an intentional stop
        sleep_mock.assert_not_called()

    def test_run_with_retry_waits_for_usage_limit_then_reasks_original_task(self):
        class LimitedThenReadyAgent(roundtable.Agent):
            task_prompts = []
            probes = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if prompt == roundtable.PREFLIGHT_PROMPT:
                    type(self).probes += 1
                    if type(self).probes == 1:
                        raise roundtable.UsageLimitError(
                            "Claude unavailable: You've hit your session limit · resets 5:30pm")
                    return "OK"
                type(self).task_prompts.append(prompt)
                if len(type(self).task_prompts) == 1:
                    raise roundtable.UsageLimitError(
                        "Claude unavailable: You've hit your session limit · resets 5:30pm")
                return "completed original task"

        agent = LimitedThenReadyAgent("Claude", Path("/tmp"))
        ticks = []
        with mock.patch.object(threading.Event, "wait", return_value=False) as wait_mock:
            result = roundtable._run_with_retry(agent, "original task", ticks.append)
        self.assertEqual(result, "completed original task")
        self.assertEqual(len(LimitedThenReadyAgent.task_prompts), 2)
        self.assertEqual(LimitedThenReadyAgent.probes, 2)
        self.assertEqual(wait_mock.call_count, 2)
        self.assertTrue(any("agent available again" in tick for tick in ticks))
        self.assertEqual(LimitedThenReadyAgent.task_prompts[0], "original task")
        self.assertIn(roundtable.RERUN_PROGRESS_NOTE, LimitedThenReadyAgent.task_prompts[1])
        self.assertTrue(LimitedThenReadyAgent.task_prompts[1].startswith("original task"))

    def test_usage_limit_wait_stops_when_round_is_cancelled(self):
        class LimitedAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                raise roundtable.UsageLimitError("Codex unavailable: rate limit exceeded")

        agent = LimitedAgent("Codex", Path("/tmp"))
        cancel_event = threading.Event()
        cancel_event.set()
        with self.assertRaisesRegex(RuntimeError, "Codex cancelled"):
            roundtable._run_with_retry(agent, "original task", lambda _: None, cancel_event)

    def test_retry_backoff_aborts_immediately_on_cancellation(self):
        class TransientFailAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                raise RuntimeError("transient connection error")

        agent = TransientFailAgent("Codex", Path("/tmp"))
        cancel_event = threading.Event()

        def _cancel_later():
            time.sleep(0.01)
            cancel_event.set()

        threading.Thread(target=_cancel_later).start()
        with self.assertRaisesRegex(RuntimeError, "Codex cancelled"):
            roundtable._run_with_retry(agent, "task", lambda _: None, cancel_event)

    def test_usage_limit_detector_matches_provider_message_without_generic_limit_word(self):
        self.assertIn("session limit", roundtable.usage_limit_detail(
            "You've hit your session limit · resets 5:30pm (America/Chicago)"))
        self.assertIsNone(roundtable.usage_limit_detail(
            "Please limit this implementation to the requested scope."))

    def test_usage_percent_used_parses_used_and_remaining_phrasings(self):
        self.assertEqual(roundtable.usage_percent_used(
            "You've used 93% of your usage limit"), 93.0)
        self.assertEqual(roundtable.usage_percent_used("used 42%"), 42.0)
        self.assertEqual(roundtable.usage_percent_used(
            "7% remaining until your session limit resets"), 93.0)
        self.assertEqual(roundtable.usage_percent_used("20% left"), 80.0)
        self.assertIsNone(roundtable.usage_percent_used("Reading app.py"))

    def test_usage_percent_used_clamps_to_the_valid_range(self):
        self.assertEqual(roundtable.usage_percent_used("used 150%"), 100.0)
        self.assertEqual(roundtable.usage_percent_used("150% remaining"), 0.0)

    def test_parse_reset_time_returns_todays_time_when_still_upcoming(self):
        now = datetime(2026, 7, 20, 17, 0)
        reset_at = roundtable.parse_reset_time("You've hit your session limit · resets 5:30pm", now)
        self.assertEqual((reset_at.year, reset_at.month, reset_at.day), (2026, 7, 20))
        self.assertEqual((reset_at.hour, reset_at.minute), (17, 30))

    def test_parse_reset_time_rolls_over_to_tomorrow_when_time_has_passed(self):
        now = datetime(2026, 7, 20, 18, 0)
        reset_at = roundtable.parse_reset_time("resets 5:30pm", now)
        self.assertEqual((reset_at.year, reset_at.month, reset_at.day), (2026, 7, 21))
        self.assertEqual((reset_at.hour, reset_at.minute), (17, 30))

    def test_parse_reset_time_honors_named_timezone(self):
        now = datetime(2026, 7, 20, 17, 0, tzinfo=ZoneInfo("America/Chicago"))
        reset_at = roundtable.parse_reset_time("resets 5:30pm (America/Chicago)", now)
        self.assertEqual(reset_at.tzinfo.key, "America/Chicago")
        self.assertEqual((reset_at.hour, reset_at.minute), (17, 30))

    def test_parse_reset_time_returns_none_without_a_recognizable_time(self):
        now = datetime(2026, 7, 20, 17, 0)
        self.assertIsNone(roundtable.parse_reset_time("rate limit exceeded, please retry", now))
        self.assertIsNone(roundtable.parse_reset_time("resets 25:99pm", now))
        self.assertIsNone(roundtable.parse_reset_time(
            "resets 5:30pm (Not/A_Real_Timezone)", now))

    def test_wait_for_agent_availability_sleeps_until_reset_time_instead_of_polling(self):
        class ReadyAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                return "OK"

        agent = ReadyAgent("Claude", Path("/tmp"))
        now = datetime(2026, 7, 20, 17, 0).astimezone()
        ticks = []
        with mock.patch.object(threading.Event, "wait", return_value=False) as wait_mock:
            roundtable._wait_for_agent_availability(
                agent, ticks.append, threading.Event(),
                "You've hit your session limit · resets 5:30pm", clock=lambda: now)
        waited = wait_mock.call_args.args[0]
        self.assertAlmostEqual(waited, 30 * 60 + roundtable.RESET_TIME_BUFFER_SECONDS, delta=1)
        self.assertTrue(any("waiting until" in tick for tick in ticks))

    def test_wait_for_agent_availability_falls_back_to_polling_without_a_reset_time(self):
        class ReadyAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                return "OK"

        agent = ReadyAgent("Claude", Path("/tmp"))
        ticks = []
        with mock.patch.object(threading.Event, "wait", return_value=False) as wait_mock:
            roundtable._wait_for_agent_availability(
                agent, ticks.append, threading.Event(), "rate limit exceeded, no reset given")
        wait_mock.assert_called_once_with(roundtable.AVAILABILITY_CHECK_SECONDS)
        self.assertTrue(any("polling every 30s" in tick for tick in ticks))

    def test_wait_for_agent_availability_only_announces_method_once_across_polls(self):
        class FlakyThenReadyAgent(roundtable.Agent):
            probes = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).probes += 1
                if type(self).probes < 3:
                    raise roundtable.UsageLimitError("still rate limited, no reset given")
                return "OK"

        agent = FlakyThenReadyAgent("Claude", Path("/tmp"))
        ticks = []
        with mock.patch.object(threading.Event, "wait", return_value=False):
            roundtable._wait_for_agent_availability(
                agent, ticks.append, threading.Event(), "rate limit exceeded, no reset given")
        self.assertEqual(FlakyThenReadyAgent.probes, 3)
        announce_count = sum(1 for tick in ticks if "polling every" in tick)
        self.assertEqual(announce_count, 1)
        self.assertNotIn(True, ["checking whether" in tick for tick in ticks])
        self.assertNotIn(True, ["still unavailable" in tick for tick in ticks])

    def test_run_parallel_phase_recovers_a_primary_agent_that_fails_once(self):
        class FlakyAgent(roundtable.Agent):
            attempts = 0

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if self.name != "Antigravity":
                    return f"{self.name} content"
                type(self).attempts += 1
                if type(self).attempts == 1:
                    raise RuntimeError("Antigravity exited with status 1\ntimeout waiting for response")
                return "Antigravity content after retry"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, FlakyAgent(name, workspace)) for name in ("Codex", "Claude", "Antigravity")]
            with mock.patch.object(roundtable.time, "sleep"):
                roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                               lambda *_: None, "Working")
            self.assertEqual({t.speaker: t.content for t in session.turns}["Antigravity"],
                             roundtable.sign_agent_work("Antigravity", "Antigravity content after retry"))

    def test_run_parallel_phase_drops_an_agent_that_fails_after_retries_instead_of_crashing(self):
        # Regression test: a single agent exhausting its retry budget (e.g. a persistent CLI
        # timeout) must not crash the whole phase and discard peers that already finished —
        # this is what happened to a real run when Antigravity failed twice near the end of a
        # phase after Codex, Claude, and Qwen had already completed successfully.
        class PersistentlyFlakyAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if self.name == "Antigravity":
                    raise RuntimeError("Antigravity exited with status 1\ntimeout waiting for response")
                return f"{self.name} content"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, PersistentlyFlakyAgent(name, workspace))
                     for name in ("Codex", "Claude", "Antigravity")]
            with mock.patch.object(roundtable.time, "sleep"):
                roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                               lambda *_: None, "Working")
            turns_by_speaker = {t.speaker: t.content for t in session.turns}
            self.assertNotIn("Antigravity", turns_by_speaker)
            self.assertEqual(turns_by_speaker["Codex"],
                             roundtable.sign_agent_work("Codex", "Codex content"))
            self.assertEqual(turns_by_speaker["Claude"],
                             roundtable.sign_agent_work("Claude", "Claude content"))

    def test_save_stems_include_microseconds(self):
        with tempfile.TemporaryDirectory() as td:
            one = roundtable.Session("Goal", td, 0, "2026-01-01T00:00:00.000001+00:00", [])
            two = roundtable.Session("Goal", td, 0, "2026-01-01T00:00:00.000002+00:00", [])
            self.assertNotEqual(roundtable.save_session(one, Path(td))[0],
                                roundtable.save_session(two, Path(td))[0])

    def test_parse_work_activity_increments_counters(self):
        display = make_test_display()
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None

        # Test reads
        display.tick("Codex", "view_file('roundtable.py')")
        display.tick("Codex", "read file contents")
        # should not double-count a result/returned line
        display.tick("Codex", "view_file returned success")
        self.assertEqual(display.work_reads["Codex"], 2)

        # Test writes
        display.tick("Claude", "replace_file_content at line 5")
        display.tick("Claude", "wrote file: README.md")
        display.tick("Claude", "edit_file returned success")
        self.assertEqual(display.work_writes["Claude"], 2)

        # Test execs
        display.tick("Antigravity", "run_command: python3 -m unittest")
        display.tick("Antigravity", "bash command execution")
        display.tick("Antigravity", "run_command exited with code 0")
        self.assertEqual(display.work_execs["Antigravity"], 2)

        # Specific "verb file" phrases still count; bare prose verbs must not.
        display.tick("Grok", "open file path/to/x.py")
        display.tick("Grok", "modify file path/to/x.py")
        self.assertEqual(display.work_reads["Grok"], 1)
        self.assertEqual(display.work_writes["Grok"], 1)

    def test_parse_work_activity_ignores_prose_verbs(self):
        """Bare words like run/check/change/do must not inflate the panel counters.

        An earlier pattern expansion matched almost every progress line; keep the matchers
        tool-ish so Reads/Execs/Writes stay meaningful in the agent panel.
        """
        display = make_test_display()
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None

        for line in (
            "I will check the review and do the run next",
            "planning to implement the change and apply the patch",
            "looking at how to perform the call",
            "trigger the process and scan the tree",
        ):
            display.tick("Codex", line)
        self.assertEqual(display.work_reads["Codex"], 0)
        self.assertEqual(display.work_writes["Codex"], 0)
        self.assertEqual(display.work_execs["Codex"], 0)

    def test_agent_panel_keeps_usage_in_compact_state_when_it_fits(self):
        """When full '● working · 95% used' is too long, still show '● work · 95%' if possible."""
        display = make_test_display(h=25, w=120)
        display.busy = True
        display.active = {"Codex"}
        display.usage_percent["Codex"] = 95.0
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
        rendered = display.s.text()
        # At 120 cols the full label is still too wide for a six-agent row, so the compact
        # form must retain the percentage rather than dropping the gauge entirely.
        self.assertIn("● work · 95%", rendered)
        self.assertNotIn("● working · 95% used", rendered)

    def test_draw_renders_work_monitoring_counters(self):
        display = make_test_display(h=30, w=240)
        display.work_reads["Codex"] = 12
        display.work_execs["Codex"] = 8
        display.work_writes["Codex"] = 4

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            rendered = display.s.text()
            self.assertIn("Reads: 12", rendered)
            self.assertIn("Execs: 8", rendered)
            self.assertIn("Writes: 4", rendered)

    def test_draw_renders_active_tickers_next_to_agent_names(self):
        display = make_test_display(h=30, w=240)
        display.busy = True
        display.active = {"Codex", "Claude", "Antigravity"}
        display.frame = 0
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            rendered = display.s.text()
            # For Codex (frame=0 is ◌)
            self.assertIn("CODEX ◌", rendered)
            # For Claude (frame=0 is ·)
            self.assertIn("CLAUDE ·", rendered)
            # For Antigravity (frame=0 is ⠁)
            self.assertIn("ANTIGRAVITY ⠁", rendered)

    def test_draw_does_not_render_tickers_when_inactive(self):
        display = make_test_display(h=30, w=240)
        display.busy = False
        display.active = {"Codex", "Claude", "Antigravity"}
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            rendered = display.s.text()
            self.assertNotIn("CODEX ◌", rendered)
            self.assertNotIn("CLAUDE ·", rendered)
            self.assertNotIn("ANTIGRAVITY ⠁", rendered)
            self.assertIn("CODEX", rendered)
            self.assertIn("CLAUDE", rendered)
            self.assertIn("ANTIGRAVITY", rendered)

    def test_draw_renders_ticker_only_for_active_agent(self):
        display = make_test_display(h=30, w=120)
        display.busy = True
        display.active = {"Claude"}
        display.frame = 3
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
            rendered = display.s.text()
            self.assertIn("CLAUDE ✢", rendered)
            self.assertNotIn("CODEX ●", rendered)
            self.assertNotIn("ANTIGRAVITY ⡀", rendered)

    def test_draw_animates_active_tickers_over_frames(self):
        display = make_test_display(h=30, w=240)
        display.busy = True
        display.active = {"Codex", "Claude", "Antigravity"}

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            # Test frame 1: Codex should advance (frame 1 -> ○), Claude remains (divisor 3 -> ·), Antigravity advances (frame 1 -> ⠂)
            display.frame = 1
            display.draw()
            rendered = display.s.text()
            self.assertIn("CODEX ○", rendered)
            self.assertIn("CLAUDE ·", rendered)
            self.assertIn("ANTIGRAVITY ⠂", rendered)

            # Test frame 3: Codex advances (3 % 6 = 3 -> ●), Claude advances (3 // 3 = 1 -> ✢), Antigravity advances (3 % 8 = 3 -> ⡀)
            display.frame = 3
            display.draw()
            rendered = display.s.text()
            self.assertIn("CODEX ●", rendered)
            self.assertIn("CLAUDE ✢", rendered)
            self.assertIn("ANTIGRAVITY ⡀", rendered)

    def test_spinner_frame_fallback_for_unknown_agent(self):
        # Unknown agent should fall back to default spinner frames and speed divisor 1
        default_frames = roundtable.DEFAULT_SPINNER_FRAMES
        for i in range(len(default_frames) * 2):
            expected = default_frames[i % len(default_frames)]
            self.assertEqual(roundtable.spinner_frame("UnknownAgent", i), expected)

    def test_ticker_never_overruns_panel_border_at_minimum_width(self):
        # 72 columns is the smallest width draw() renders full panels at (below it,
        # a "Terminal too small" message shows instead). At that width, four columns
        # leave each agent's "NAME + ticker" header with little to no slack, so any
        # future longer name/spinner frame would silently bleed into the next panel's
        # border if the header text weren't clipped to the panel's own width.
        display = make_test_display(h=25, w=72)
        display.busy = True
        display.active = set(roundtable.AGENT_NAMES)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            for frame in range(max(len(f) for f, _ in roundtable.AGENT_SPINNERS.values())):
                display.frame = frame
                display.draw()
                header_row = display.s.grid[5]
                # Every box-drawing corner char from _box must survive untouched; if a
                # header overran its column it would clobber one of these.
                border_cols = [i for i, ch in enumerate(header_row) if ch in "╭╮"]
                self.assertEqual(len(border_cols), 12, display.s.text())

    def test_agent_panel_rows_never_overrun_borders_at_minimum_width(self):
        display = make_test_display(h=25, w=72)
        display.busy = True
        display.active = set(roundtable.AGENT_NAMES)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()

        for name in roundtable.AGENT_NAMES:
            top, left, bottom, right = display.hitboxes[name.lower()]
            with self.subTest(name=name):
                self.assertEqual(display.s.grid[top][right], "╮")
                for row in range(top + 1, bottom):
                    self.assertEqual(display.s.grid[row][right], "│", display.s.text())
                self.assertEqual(display.s.grid[bottom][right], "╯")
        rendered = display.s.text()
        self.assertIn("● work", rendered)
        self.assertNotIn("-0.0s", rendered)

    def test_agent_header_is_clipped_to_panel_width(self):
        display = make_test_display()
        display.busy = True
        display.active = {"Antigravity"}
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "spinner_frame", return_value="X" * 50):
            display.draw()
        y, x, _, x2 = display.hitboxes["antigravity"]
        panel_row = display.s.grid[y]
        # The header write must stop before the panel's own right border column,
        # regardless of how long the name/ticker combination turns out to be.
        self.assertEqual(panel_row[x2], "╮")

    def test_session_queued_prompts_default_and_load(self):
        session = roundtable.Session("test obj", "/tmp", 1, "now", [])
        self.assertEqual(session.queued_prompts, [])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jpath, _ = roundtable.save_session(session, tmp_path)
            loaded = roundtable.load_session(jpath)
            self.assertEqual(loaded.queued_prompts, [])

    def test_interrupt_queues_prompt_and_acknowledges_on_rotate_line(self):
        display = make_test_display()
        display.busy = True
        display.active = {"Codex"}
        with mock.patch("roundtable.read_followup_ui", return_value="Add error handling"), \
             mock.patch.object(display, "draw"):
            display.trigger_interrupt()
        self.assertEqual(display.session.queued_prompts, ["Add error handling"])
        self.assertIn("[Codex] Acknowledged queued task: Add error handling", display.status)
        self.assertEqual(display.activity["Codex"], "Acknowledged queued task: Add error handling")

    def test_trigger_interrupt_restores_hidden_cursor_on_close(self):
        """read_followup_ui turns the terminal cursor on (curs_set(1)) so the user can see where
        they're typing; trigger_interrupt must hide it again on the way out, or the blinking cursor
        leaks into the plain dashboard view for the rest of the session."""
        display = make_test_display()
        display.busy = True
        display.active = {"Codex"}
        with mock.patch("roundtable.read_followup_ui", return_value=""), \
             mock.patch.object(display, "draw"), \
             mock.patch.object(roundtable.curses, "curs_set") as mock_curs_set:
            display.trigger_interrupt()
        mock_curs_set.assert_called_once_with(0)

    def test_queued_prompt_acknowledgement_rotates_active_agents(self):
        display = make_test_display()
        display.active = {"Antigravity", "Codex", "Claude"}
        with mock.patch.object(display, "draw"):
            display.acknowledge_queued_prompt("first")
            first = display.status
            display.acknowledge_queued_prompt("second")
            second = display.status
            display.acknowledge_queued_prompt("third")
            third = display.status
        self.assertIn("[Codex]", first)
        self.assertIn("[Claude]", second)
        self.assertIn("[Antigravity]", third)

    def test_busy_dashboard_exposes_clickable_add_prompt_control(self):
        display = make_test_display()
        display.busy = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()
        self.assertIn("ADD PROMPT [i]", display.s.text())
        y, x, y2, x2 = display.hitboxes["interrupt"]
        self.assertEqual(display.handle_mouse(
            x, y, roundtable.curses.BUTTON1_CLICKED), "interrupt")
        self.assertEqual(y, y2)
        self.assertGreater(x2, x)

    def test_poll_input_i_key_triggers_interrupt(self):
        display = make_test_display()
        display.s.getch = mock.MagicMock(side_effect=[ord("i"), -1])
        with mock.patch.object(display, "trigger_interrupt") as mock_interrupt:
            display.poll_input()
            mock_interrupt.assert_called_once()

    def test_drain_queued_prompts_appends_turns(self):
        session = roundtable.Session("test obj", "/tmp", 1, "now", [])
        session.queued_prompts.extend([
            "Refactor authentication module",
            "Then update its documentation",
        ])
        original_queue = session.queued_prompts
        drained = roundtable.drain_queued_prompts(session)
        self.assertTrue(drained)
        self.assertEqual(len(session.turns), 2)
        self.assertEqual(session.turns[0].speaker, "User")
        self.assertEqual(session.turns[0].phase, "follow-up")
        self.assertEqual(session.turns[0].content, "Refactor authentication module")
        self.assertEqual(session.turns[1].content, "Then update its documentation")
        self.assertIs(session.queued_prompts, original_queue)
        self.assertEqual(session.queued_prompts, [])

    def test_run_phase_drains_queued_prompt_before_dispatch(self):
        """A prompt queued mid-round (via trigger_interrupt) must reach the very next phase, not
        just the next full round -- that's what makes the interrupt actually interrupt."""
        session = roundtable.Session("Task", "/tmp", 1, "now", [])
        session.queued_prompts.append("Also handle logging")
        captured = {}

        def stub_runner(session, agents, phase, tick, status, message, log_prompt):
            captured["phase"] = phase
            captured["turns"] = list(session.turns)

        roundtable._run_phase(stub_runner, session, [], "review 1", lambda *_: None,
                              lambda *_: None, "Working", lambda *_: None, None)
        self.assertEqual(captured["phase"], "followup-review 1")
        self.assertEqual(captured["turns"][-1].content, "Also handle logging")
        self.assertEqual(session.queued_prompts, [])

    def test_run_phase_leaves_phase_name_alone_when_nothing_queued(self):
        session = roundtable.Session("Task", "/tmp", 1, "now", [])
        captured = {}

        def stub_runner(session, agents, phase, tick, status, message, log_prompt):
            captured["phase"] = phase

        roundtable._run_phase(stub_runner, session, [], "review 1", lambda *_: None,
                              lambda *_: None, "Working", lambda *_: None, None)
        self.assertEqual(captured["phase"], "review 1")

    def test_run_phase_does_not_double_prefix_an_already_followup_phase(self):
        session = roundtable.Session("Task", "/tmp", 1, "now", [])
        session.queued_prompts.append("More context")
        captured = {}

        def stub_runner(session, agents, phase, tick, status, message, log_prompt):
            captured["phase"] = phase

        roundtable._run_phase(stub_runner, session, [], "followup-proposal", lambda *_: None,
                              lambda *_: None, "Working", lambda *_: None, None)
        self.assertEqual(captured["phase"], "followup-proposal")

    def test_run_tui_drains_queued_prompts_at_round_end(self):
        session = roundtable.Session("Task", "/tmp", 1, "now", [])
        args = roundtable.argparse.Namespace(
            output_dir="/tmp", skip_preflight=True, preflight_timeout=5,
            touch_mode=False, collab="parallel", synthesizer="rotate",
            balance_load=False, task_status_check=False, reassign_idle=False,
            dead_code_check=False)
        prompts_at_dispatch = []

        def stub_conduct(active_session, *_args, **_kwargs):
            prompts_at_dispatch.append([
                turn.content for turn in active_session.turns if turn.speaker == "User"])
            active_session.turns.append(roundtable.Turn("Final", "consensus", "done"))
            if len(prompts_at_dispatch) == 1:
                # Model an interrupt arriving after conduct()'s pre-synthesis drain point.
                active_session.queued_prompts.append("Post-synthesis request")

        test_display = make_test_display()
        test_display.session = session
        stdscr = mock.MagicMock()
        with mock.patch("roundtable.Display", return_value=test_display), \
             mock.patch("roundtable.conduct", side_effect=stub_conduct), \
             mock.patch("roundtable.read_followup_ui", return_value=""), \
             mock.patch("roundtable.save_session", return_value=("/tmp/s.json", "/tmp/s.md")), \
             mock.patch("roundtable.finalize_agent_prompt_file") as finalize_board, \
             mock.patch("roundtable.suppress_focus_reporting"):
            ret = roundtable.run_tui(stdscr, args, session, None, None, None, None, None, None,
                                     resumed=False)

        self.assertEqual(ret, 0)
        self.assertEqual(prompts_at_dispatch, [[], ["Post-synthesis request"]])
        self.assertEqual(session.queued_prompts, [])
        finalize_board.assert_called_once()

    def test_run_tui_logs_config_summary_to_the_console_at_start(self):
        """The GUI console must echo the run's active configuration up front, not just the private
        disk log -- so an operator watching the dashboard can see it without leaving the app."""
        session = roundtable.Session("Task", "/tmp", 1, "now", [])
        args = roundtable.argparse.Namespace(
            output_dir="/tmp", skip_preflight=True, preflight_timeout=5,
            touch_mode=False, collab="mixed", synthesizer="rotate",
            balance_load=False, task_status_check=False, reassign_idle=False,
            dead_code_check=False, reasoning_effort="auto", synthesis_passes=6,
            elevated=[], mock=False)

        def stub_conduct(active_session, *_args, **_kwargs):
            active_session.turns.append(roundtable.Turn("Final", "consensus", "done"))

        test_display = make_test_display()
        test_display.session = session
        stdscr = mock.MagicMock()
        with mock.patch("roundtable.Display", return_value=test_display), \
             mock.patch("roundtable.conduct", side_effect=stub_conduct), \
             mock.patch("roundtable.read_followup_ui", return_value=""), \
             mock.patch("roundtable.save_session", return_value=("/tmp/s.json", "/tmp/s.md")), \
             mock.patch("roundtable.finalize_agent_prompt_file"), \
             mock.patch("roundtable.suppress_focus_reporting"):
            ret = roundtable.run_tui(stdscr, args, session, None, None, None, None, None, None,
                                     resumed=False)

        self.assertEqual(ret, 0)
        console_text = " ".join(text for _kind, text in test_display.console)
        self.assertIn("Config: collab=mixed", console_text)

    def test_run_tui_self_restart_preserves_active_followup_state(self):
        session = roundtable.Session("Task", "/tmp", 0, "now", [])
        args = roundtable.argparse.Namespace(
            output_dir="/tmp", skip_preflight=True, preflight_timeout=5,
            touch_mode=False, collab="parallel", synthesizer="rotate",
            balance_load=False, task_status_check=False, reassign_idle=False,
            dead_code_check=False, synthesis_passes=3)
        calls = 0

        def stub_conduct(active_session, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise roundtable.SelfRestartRequired
            active_session.turns.append(roundtable.Turn("Final", "consensus", "done"))

        test_display = make_test_display()
        test_display.session = session
        stdscr = mock.MagicMock()
        session_path = Path("/tmp/session.json")
        with mock.patch("roundtable.Display", return_value=test_display), \
             mock.patch("roundtable.conduct", side_effect=stub_conduct), \
             mock.patch("roundtable.read_followup_ui", return_value="Refine the result"), \
             mock.patch("roundtable.save_session",
                        return_value=(session_path, Path("/tmp/session.md"))), \
             mock.patch("roundtable.restart_self") as mock_restart, \
             mock.patch("roundtable.finalize_agent_prompt_file") as finalize_board, \
             mock.patch("roundtable.curses.endwin"), \
             mock.patch("roundtable.suppress_focus_reporting"):
            ret = roundtable.run_tui(
                stdscr, args, session, None, None, None, None, None, None, resumed=False,
                checkpoint=lambda: None)

        self.assertEqual(ret, 0)
        self.assertEqual(calls, 1)
        mock_restart.assert_called_once_with(args, session_path, False)
        finalize_board.assert_not_called()

    def test_run_tui_restart_continuation_skips_the_followup_prompt(self):
        """Regression: main() passes resumed=True for a --continue-after-restart followup relaunch
        (it reuses the followup flag for both), and the last turn before such a restart is always
        an agent's, not the user's -- so without gating on completed_phases too, this looked
        exactly like a genuine --resume missing its follow-up text and blocked on read_followup_ui
        before conduct() ever ran, silently hanging an unattended --self restart."""
        session = roundtable.Session("Task", "/tmp", 1, "now", [
            roundtable.Turn("User", "follow-up", "Do the thing"),
            roundtable.Turn("Codex", "followup-proposal", "Partial work"),
        ])
        args = roundtable.argparse.Namespace(
            output_dir="/tmp", skip_preflight=True, preflight_timeout=5,
            touch_mode=False, collab="parallel", synthesizer="rotate",
            balance_load=False, task_status_check=False, reassign_idle=False,
            dead_code_check=False, synthesis_passes=3)
        captured = {}

        def stub_conduct(active_session, *_args, **kwargs):
            captured["completed_phases"] = kwargs.get("completed_phases")
            active_session.turns.append(roundtable.Turn("Final", "consensus", "done"))

        test_display = make_test_display()
        test_display.session = session
        stdscr = mock.MagicMock()
        with mock.patch("roundtable.Display", return_value=test_display), \
             mock.patch("roundtable.conduct", side_effect=stub_conduct), \
             mock.patch("roundtable.read_followup_ui", return_value="") as mock_followup_ui, \
             mock.patch("roundtable.save_session", return_value=("/tmp/s.json", "/tmp/s.md")), \
             mock.patch("roundtable.suppress_focus_reporting"):
            ret = roundtable.run_tui(
                stdscr, args, session, None, None, None, None, None, None, resumed=True,
                completed_phases={"followup-proposal"})

        self.assertEqual(ret, 0)
        self.assertEqual(captured.get("completed_phases"), {"followup-proposal"})
        # The only call is the legitimate post-completion "what's next" prompt, not a blocking
        # one before conduct() ran.
        mock_followup_ui.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "process-group cancellation is POSIX-specific")
    def test_agent_cancellation_stops_the_whole_subprocess_group(self):
        agent = roundtable.Agent("Claude", Path("/tmp"))
        cancelled = threading.Event()
        cancelled.set()
        proc = mock.Mock(pid=4321, stdout=[])
        proc.poll.return_value = None
        proc.wait.return_value = 0

        with mock.patch.object(roundtable.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(roundtable.os, "killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "Claude cancelled"):
                agent.run("task", lambda _line: None, cancelled)

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(proc.pid, roundtable.signal.SIGTERM)
        proc.terminate.assert_not_called()

    def test_agent_cancellation_does_not_log_it_as_an_unexpected_error(self):
        """Regression: the broad except-Exception cleanup handler added around the subprocess
        loop must not relabel a deliberate cancellation's own RuntimeError as an "unexpected
        error" -- the cancellation branch just above it already logs its own, more specific
        reason, so the generic label would be actively misleading in a --debug log."""
        agent = roundtable.Agent("Claude", Path("/tmp"))
        cancelled = threading.Event()
        cancelled.set()
        proc = mock.Mock(pid=4321, stdout=[])
        proc.poll.return_value = None
        proc.wait.return_value = 0
        logged: list[str] = []
        agent.log_diagnostic = logged.append

        with mock.patch.object(roundtable.subprocess, "Popen", return_value=proc), \
             mock.patch.object(roundtable.os, "killpg"):
            with self.assertRaisesRegex(RuntimeError, "Claude cancelled"):
                agent.run("task", lambda _line: None, cancelled)

        self.assertFalse(any("unexpected error" in line for line in logged), logged)

    def test_agent_run_caps_ticked_lines_but_keeps_the_full_answer(self):
        """Regression: a real run once had a single turn print roughly 100k lines (a CLI
        re-printing a large file across tool calls), and on_tick was called once per line with
        no limit -- each call chains into a log write + curses redraw, so the whole run's
        UI/log pipeline stalled for the better part of an hour draining a backlog that carried
        no new information. Ticking is now capped; every line is still captured for the answer."""
        agent = roundtable.Agent("Codex", Path("/tmp"))
        line_count = roundtable.Agent.TICK_LINE_CAP + 500
        lines = [f"line {i}\n" for i in range(line_count)]
        proc = mock.Mock(pid=1234, stdout=iter(lines))
        poll_calls = {"n": 0}

        def fake_poll():
            poll_calls["n"] += 1
            return None if poll_calls["n"] < 3 else 0

        proc.poll.side_effect = fake_poll
        proc.wait.return_value = 0
        proc.returncode = 0
        ticked: list[str] = []

        with mock.patch.object(roundtable.subprocess, "Popen", return_value=proc), \
             mock.patch.object(roundtable.time, "sleep"):
            answer = agent.run("task", ticked.append)

        self.assertEqual(answer, "".join(lines).strip())
        real_ticks = [line for line in ticked if line]
        self.assertLessEqual(len(real_ticks), roundtable.Agent.TICK_LINE_CAP + 1)
        self.assertTrue(any("capped at" in line for line in real_ticks), real_ticks[-3:])

    def test_reassign_idle_prompt_includes_same_phase_finished_turns(self):
        """A mid-phase · extra prompt must see co-agents who already finished this phase.

        session.turns is only updated after the phase ends, so without folding in-flight
        finished_results into the reassignment transcript, same-round DIBS claims are invisible.
        Only one bonus runs per phase, so this uses two fast finishers in the same poll (so both
        land in finished_results before the bonus is built) plus two slow agents to keep
        remaining ≥ 2.
        """
        bonus_prompts: list[str] = []
        # Synchronize the two fast primaries so they land in the same just_finished poll
        # batch; finished_results is filled for the whole batch before any bonus is built.
        fast_pair = threading.Barrier(2, timeout=2.0)

        class TrackingAgent(roundtable.Agent):
            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                if "· extra" in prompt or "pick up a different" in prompt:
                    bonus_prompts.append(prompt)
                    return f"{self.name} extra content"
                if self.name == "Claude":
                    fast_pair.wait()
                    return "DIBS: the frontend\nClaude proposal content"
                if self.name == "Codex":
                    fast_pair.wait()
                    return "Codex proposal content"
                # Stay busy long enough for the reassignment to run.
                deadline = time.monotonic() + 0.6
                while time.monotonic() < deadline:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    time.sleep(0.02)
                return f"{self.name} proposal content"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            # Codex is first so the same-poll completion batch assigns it the bonus; Claude's
            # co-agent result must still be present in that bonus prompt.
            agents = [(name, TrackingAgent(name, workspace))
                      for name in ("Codex", "Claude", "Antigravity", "Aider")]
            roundtable._run_parallel_phase(
                session, agents, "proposal", lambda *_: None, lambda *_: None, "Working",
                reassign_idle=True, stagger=0,
            )

        self.assertTrue(bonus_prompts, "a reassignment prompt should have been issued")
        self.assertEqual(len(bonus_prompts), 1)
        joined = "\n".join(bonus_prompts)
        self.assertIn("Claude has dibs on the frontend", joined)
        self.assertIn("Claude proposal content", joined)

    def test_ensure_agent_prompt_file_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            path = roundtable.ensure_agent_prompt_file(workspace)
            self.assertTrue(path.is_file())
            content = path.read_text(encoding="utf-8")
            self.assertIn("append-only", content.lower())
            self.assertIn("## From <agent> to <agent or all>", content)

    def test_ensure_agent_prompt_file_does_not_overwrite_existing_entries(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            path = workspace / roundtable.AGENT_PROMPT_FILE
            path.write_text("existing peer note", encoding="utf-8")
            roundtable.ensure_agent_prompt_file(workspace)
            self.assertEqual(path.read_text(encoding="utf-8"), "existing peer note")

    def test_finalize_agent_prompt_file_archives_without_resetting_the_board(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            board = roundtable.ensure_agent_prompt_file(workspace)
            board.write_text(
                f"{roundtable.AGENT_PROMPT_TEMPLATE}\n## From Codex to all — result\nPassed.\n",
                encoding="utf-8")
            log_path = workspace / "run.log"
            run_log = roundtable.RunLog(log_path)
            roundtable.finalize_agent_prompt_file(workspace, run_log)
            run_log.close()
            # Archiving is diagnostic-only now; the reset happens when the next fresh run starts
            # (start_agent_prompt_file), not here, so a hard kill that skips this function can't
            # leave the next run's reset undone.
            self.assertIn("## From Codex to all — result", board.read_text(encoding="utf-8"))
            logged = log_path.read_text(encoding="utf-8")
            self.assertIn("From Codex to all — result", logged)

    def test_reset_agent_prompt_file_does_not_create_a_missing_board(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            self.assertFalse(roundtable.reset_agent_prompt_file(workspace))
            self.assertFalse((workspace / roundtable.AGENT_PROMPT_FILE).exists())

    def test_start_agent_prompt_file_fresh_resets_existing_content(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            path = workspace / roundtable.AGENT_PROMPT_FILE
            path.write_text("stale content from a hard-killed earlier run", encoding="utf-8")
            roundtable.start_agent_prompt_file(workspace, fresh=True)
            self.assertEqual(path.read_text(encoding="utf-8"), roundtable.AGENT_PROMPT_TEMPLATE)

    def test_start_agent_prompt_file_fresh_creates_missing_board(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            roundtable.start_agent_prompt_file(workspace, fresh=True)
            path = workspace / roundtable.AGENT_PROMPT_FILE
            self.assertEqual(path.read_text(encoding="utf-8"), roundtable.AGENT_PROMPT_TEMPLATE)

    def test_start_agent_prompt_file_not_fresh_preserves_existing_content(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            path = workspace / roundtable.AGENT_PROMPT_FILE
            path.write_text("peer notes from before this --self restart", encoding="utf-8")
            roundtable.start_agent_prompt_file(workspace, fresh=False)
            self.assertEqual(path.read_text(encoding="utf-8"),
                             "peer notes from before this --self restart")

    def test_start_agent_prompt_file_not_fresh_creates_missing_board(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            roundtable.start_agent_prompt_file(workspace, fresh=False)
            path = workspace / roundtable.AGENT_PROMPT_FILE
            self.assertEqual(path.read_text(encoding="utf-8"), roundtable.AGENT_PROMPT_TEMPLATE)

    def test_conduct_resets_the_board_for_a_fresh_run_but_not_a_restart(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            path = workspace / roundtable.AGENT_PROMPT_FILE
            path.write_text("leftover board content", encoding="utf-8")

            # completed_phases set: a --self restart continuing an in-progress run must not
            # touch peer notes already left on the board.
            session = roundtable.Session(
                "Goal", td, 0, "now",
                [roundtable.Turn("Codex", "proposal", "done"),
                 roundtable.Turn("Final", "consensus", "Completed\n\nDone")],
                "Completed\n\nDone")
            agents = [roundtable.MockAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                               completed_phases={"proposal", "consensus"})
            self.assertEqual(path.read_text(encoding="utf-8"), "leftover board content")

            # completed_phases=None: a brand-new run (or a plain --resume) must start clean, in
            # case the previous run was killed before it could archive/reset anything itself.
            path.write_text("leftover board content", encoding="utf-8")
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [roundtable.MockAgent(name, workspace) for name in roundtable.AGENT_NAMES]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None,
                               completed_phases=None)
            self.assertEqual(path.read_text(encoding="utf-8"), roundtable.AGENT_PROMPT_TEMPLATE)

    def test_prompt_for_includes_agent_prompt_board_hint_for_agents_only(self):
        prompt = roundtable.prompt_for("Objective", [], "proposal", "Codex")
        self.assertIn(roundtable.AGENT_PROMPT_FILE, prompt)
        user_prompt = roundtable.prompt_for("Objective", [], "proposal", "User")
        self.assertNotIn(roundtable.AGENT_PROMPT_FILE, user_prompt)

    def test_extract_agent_prompt_entries(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            self.assertEqual(roundtable.extract_agent_prompt_entries(workspace), "")
            roundtable.ensure_agent_prompt_file(workspace)
            self.assertEqual(roundtable.extract_agent_prompt_entries(workspace), "")
            board_path = workspace / roundtable.AGENT_PROMPT_FILE
            entry_text = "## From Antigravity to all — testing\nHello from test."
            board_path.write_text(roundtable.AGENT_PROMPT_TEMPLATE + "\n" + entry_text, encoding="utf-8")
            self.assertEqual(roundtable.extract_agent_prompt_entries(workspace), entry_text)

    def test_extract_agent_prompt_entries_caps_context_and_keeps_newest_content(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            board_path = workspace / roundtable.AGENT_PROMPT_FILE
            oldest = "o" * 20
            newest = "n" * roundtable.AGENT_PROMPT_CONTEXT_CHARS
            board_path.write_text(
                roundtable.AGENT_PROMPT_TEMPLATE + oldest + newest, encoding="utf-8")

            entries = roundtable.extract_agent_prompt_entries(workspace)

            self.assertIn("20 older characters omitted", entries)
            self.assertNotIn(oldest, entries)
            self.assertTrue(entries.endswith(newest))

    def test_prompt_for_includes_active_agent_prompt_board_entries(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            roundtable.ensure_agent_prompt_file(workspace)
            board_path = workspace / roundtable.AGENT_PROMPT_FILE
            entry_text = "## From Antigravity to all — testing\nHello from test."
            board_path.write_text(roundtable.AGENT_PROMPT_TEMPLATE + "\n" + entry_text, encoding="utf-8")
            ctx = roundtable.prepare_prompt_context("Objective", [], workspace=workspace)
            self.assertEqual(ctx.prompt_board_entries, entry_text)
            prompt = roundtable.prompt_for("Objective", [], "proposal", "Codex", context=ctx)
            self.assertIn("Active prompt board entries (AGENT_PROMPTS.md):", prompt)
            self.assertIn(entry_text, prompt)


    def test_prompt_for_includes_restart_vote_hint_for_agents_only_when_pending(self):
        prompt = roundtable.prompt_for(
            "Objective", [], "review 1", "Codex", restart_vote_pending=True)
        self.assertIn("RESTART: now", prompt)
        self.assertIn("RESTART: later", prompt)
        no_vote_prompt = roundtable.prompt_for(
            "Objective", [], "review 1", "Codex", restart_vote_pending=False)
        self.assertNotIn("RESTART:", no_vote_prompt)
        user_prompt = roundtable.prompt_for(
            "Objective", [], "review 1", "User", restart_vote_pending=True)
        self.assertNotIn("RESTART:", user_prompt)

    def test_tally_restart_votes_majority_now(self):
        turns = [
            roundtable.Turn("Codex", "review 1", "work\n\nRESTART: now — fix is small\n\nSigned: Codex"),
            roundtable.Turn("Claude", "review 1", "work\n\nRESTART: later — want more polish\n\nSigned: Claude"),
            roundtable.Turn("Grok", "review 1", "work\n\n**RESTART: now** — agreed\n\nSigned: Grok"),
        ]
        self.assertEqual(roundtable.tally_restart_votes(turns, "review 1"), "now")
        self.assertEqual(
            roundtable.extract_restart_votes(turns, "review 1"),
            {"Codex": "now", "Claude": "later", "Grok": "now"},
        )

    def test_tally_restart_votes_majority_later(self):
        turns = [
            roundtable.Turn("Codex", "review 1", "work\n\nRESTART: later — keep going\n\nSigned: Codex"),
            roundtable.Turn("Claude", "review 1", "work\n\n`RESTART: later` — same\n\nSigned: Claude"),
            roundtable.Turn("Grok", "review 1", "work\n\nRESTART: now — disagree\n\nSigned: Grok"),
        ]
        self.assertEqual(roundtable.tally_restart_votes(turns, "review 1"), "later")

    def test_tally_restart_votes_tie_and_no_votes_favor_now(self):
        tied = [
            roundtable.Turn("Codex", "review 1", "RESTART: now — a\n\nSigned: Codex"),
            roundtable.Turn("Claude", "review 1", "RESTART: later — b\n\nSigned: Claude"),
        ]
        self.assertEqual(roundtable.tally_restart_votes(tied, "review 1"), "now")
        no_votes = [roundtable.Turn("Codex", "review 1", "just work, no vote\n\nSigned: Codex")]
        self.assertEqual(roundtable.tally_restart_votes(no_votes, "review 1"), "now")
        self.assertEqual(roundtable.extract_restart_votes(no_votes, "review 1"), {})

    def test_tally_restart_votes_ignores_other_phases(self):
        turns = [roundtable.Turn("Codex", "review 2", "RESTART: later — wrong phase\n\nSigned: Codex")]
        self.assertEqual(roundtable.tally_restart_votes(turns, "review 1"), "now")

    def test_extract_restart_votes_latest_per_agent_not_stackable(self):
        """One agent with two turns in the same phase keeps only its latest vote — no double count."""
        turns = [
            roundtable.Turn("Codex", "review 1", "first\n\nRESTART: later — early\n\nSigned: Codex"),
            roundtable.Turn("Claude", "review 1", "work\n\nRESTART: later — also\n\nSigned: Claude"),
            roundtable.Turn("Codex", "review 1", "second\n\nRESTART: now — changed mind\n\nSigned: Codex"),
        ]
        self.assertEqual(
            roundtable.extract_restart_votes(turns, "review 1"),
            {"Codex": "now", "Claude": "later"},
        )
        # Without per-agent dedupe this would be later=2 now=1 → "later".
        self.assertEqual(roundtable.tally_restart_votes(turns, "review 1"), "now")

    def test_format_restart_vote_summary_lists_sides_and_action(self):
        turns = [
            roundtable.Turn("Codex", "review 1", "RESTART: now — fix\n\nSigned: Codex"),
            roundtable.Turn("Claude", "review 1", "RESTART: later — polish\n\nSigned: Claude"),
            roundtable.Turn("Grok", "review 1", "RESTART: now — agree\n\nSigned: Grok"),
        ]
        summary = roundtable.format_restart_vote_summary(turns, "review 1")
        self.assertIn("RESTART vote: now=2 later=1 → restarting now", summary)
        self.assertIn("now: Codex, Grok", summary)
        self.assertIn("later: Claude", summary)
        later_majority = [
            roundtable.Turn("Codex", "review 1", "RESTART: later\n\nSigned: Codex"),
            roundtable.Turn("Claude", "review 1", "RESTART: later\n\nSigned: Claude"),
            roundtable.Turn("Grok", "review 1", "RESTART: now\n\nSigned: Grok"),
        ]
        later_summary = roundtable.format_restart_vote_summary(later_majority, "review 1")
        self.assertIn("→ deferring restart by one phase", later_summary)
        empty = roundtable.format_restart_vote_summary([], "review 1")
        self.assertIn("now=0 later=0 → restarting now", empty)
        self.assertIn("no votes cast", empty)

    def test_self_checkpoint_ignores_sibling_files_next_to_loaded_program(self):
        """Restart only tracks Path(__file__); tests/docs next to it must not force execv."""
        with tempfile.TemporaryDirectory() as td:
            fake_source = Path(td) / "roundtable.py"
            sibling_test = Path(td) / "test_roundtable.py"
            sibling_readme = Path(td) / "README.md"
            fake_source.write_text("a = 1\n")
            sibling_test.write_text("def test_x(): pass\n")
            sibling_readme.write_text("# Doc\n")
            with mock.patch.object(roundtable.Path, "resolve", return_value=fake_source):
                check = roundtable.self_checkpoint(True)
                check()
                sibling_test.write_text("def test_x(): assert True\n")
                sibling_readme.write_text("# Changed\n")
                check()  # auxiliary edits alone must not restart
                fake_source.write_text("a = 2\n")
                with self.assertRaises(roundtable.SelfRestartRequired):
                    check()

    def test_poll_input_handles_resize_event(self):
        """The real input path redraws and moves focus off a panel hidden by the new layout."""
        display = make_test_display(h=48, w=120)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
            self.assertIn("console", display.hitboxes)
            display.focused_panel = "Console"
            display.s.resize(25, 120)  # compact outcome band omits Console
            display.s.getch = mock.Mock(side_effect=[roundtable.curses.KEY_RESIZE, -1])
            display.poll_input()
        self.assertNotIn("console", display.hitboxes)
        self.assertEqual(display.focused_panel, "Code")

    def test_resize_clears_focus_when_terminal_is_too_small(self):
        display = make_test_display(h=30, w=100)
        display.focused_panel = "Codex"
        display.s.resize(19, 100)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.handle_resize()
        self.assertIsNone(display.focused_panel)
        self.assertEqual(display.hitboxes, {})
        self.assertIn("Terminal too small", display.s.text())

    def test_resize_preserves_expanded_panel(self):
        display = make_test_display(h=30, w=100)
        display.expanded = "Final"
        display.focused_panel = "Final"
        display.s.resize(40, 140)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.handle_resize()
        self.assertEqual(display.expanded, "Final")
        self.assertEqual(display.focused_panel, "Final")
        self.assertIn("TASK OUTCOME (expanded)", display.s.text())

    def test_display_help_modal_toggle_and_drawing(self):
        """Test that ? key toggles show_help modal overlay and renders shortcut help."""
        display = make_test_display(h=30, w=100)
        self.assertFalse(display.show_help)
        display.show_help = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("KEYBOARD SHORTCUTS & HELP", rendered)
        self.assertIn("Expand / collapse Agent", rendered)

        # ? toggles help on, then getch returns -1 so poll_input exits (nodelay style).
        display.show_help = False
        keys = iter([ord("?"), -1])
        display.s.getch = lambda: next(keys)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.poll_input()
        self.assertTrue(display.show_help)

    def test_help_modal_draws_over_expanded_panel(self):
        """? must still show the help overlay while a panel is full-screen.

        draw() used to early-return after _draw_expanded without painting the modal, so the
        help key and touch HELP button appeared to do nothing until the panel was collapsed.
        """
        display = make_test_display(h=30, w=100)
        display.expanded = "Codex"
        display.show_help = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("KEYBOARD SHORTCUTS & HELP", rendered)
        # Expanded chrome remains underneath the modal (title is split by the overlay box).
        self.assertEqual(display.expanded, "Codex")
        self.assertIn("Esc/q collapse", rendered)

    def test_help_modal_shows_overflow_indicator_on_short_terminal(self):
        """At a short-but-supported terminal height the shortcut list no longer fits.

        Regression check: shortcuts[:available_rows] used to just drop the tail silently, so a
        user on a 72x20 terminal (the documented minimum) would never learn Esc/q or ?/h exist.
        The modal must now say how many entries are hidden instead of truncating quietly.
        """
        display = make_test_display(h=20, w=100)
        display.show_help = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("more", rendered)
        more_row = next(i for i, row in enumerate(display.s.grid) if "more" in "".join(row))
        footer_row = next(
            i for i, row in enumerate(display.s.grid) if "Press any key" in "".join(row))
        self.assertLess(more_row, footer_row)
        # With the addition of operational transparency info, the help modal now contains
        # more items, so some items may still be visible even when truncated
        # The key is that the "more" indicator appears

    def test_help_modal_clears_dashboard_underlay(self):
        """Help overlay must opaque-fill its interior so panel chrome does not bleed through.

        _box only draws the frame. Without an interior wipe, the status line, agent titles, and
        panel corners stayed visible between shortcut rows (e.g. 'odTab/Shift-Tab', 'Ready ·').
        """
        display = make_test_display(h=20, w=72)
        display.show_help = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        # Status and idle agent copy were painted before the modal; they must not remain inside it.
        rendered = display.s.text()
        self.assertIn("KEYBOARD SHORTCUTS & HELP", rendered)
        self.assertNotIn("Ready ·", rendered)
        self.assertNotIn("Waiting for the shared task", rendered)
        self.assertNotIn("odTab", rendered)
        more_line = next("".join(row) for row in display.s.grid if "more" in "".join(row))
        # Overflow notice sits on a wiped content row, not mixed into a leftover panel border.
        self.assertRegex(more_line, r"│\s+\+\d+ more")

    def test_help_modal_shows_all_shortcuts_when_terminal_tall_enough(self):
        display = make_test_display(h=30, w=100)
        display.show_help = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertNotIn("more", rendered)
        self.assertIn("Toggle this Help overlay modal", rendered)

    def test_help_modal_documents_mouse_click_and_wheel_support(self):
        """The help overlay previously described keyboard shortcuts only, leaving mouse click
        (expand a panel) and wheel scroll (page a panel) completely undiscoverable to a
        non-touch, mouse-enabled terminal user."""
        display = make_test_display(h=30, w=100)
        display.show_help = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("Click / wheel", rendered)

    def test_keyboard_focus_cycles_and_enter_expands_selected_panel(self):
        display = make_test_display(h=30, w=100)
        keys = iter([9, 9, roundtable.curses.KEY_BTAB, 10, -1])
        display.s.getch = lambda: next(keys)
        with mock.patch.object(display, "draw") as draw:
            display.poll_input()
        self.assertEqual(display.focused_panel, "Codex")
        self.assertEqual(display.expanded, "Codex")
        self.assertEqual(draw.call_count, 4)

    def test_shift_tab_starts_focus_at_last_panel(self):
        display = make_test_display(h=30, w=100)
        with mock.patch.object(display, "draw"):
            display.move_panel_focus(-1)
        self.assertEqual(display.focused_panel, "Console")

    def test_focused_panel_title_is_visually_marked(self):
        display = make_test_display(h=30, w=100)
        display.focused_panel = "Codex"
        calls = []
        original_put = display._put

        def record_put(y, x, text, attr=0):
            calls.append((text, attr))
            original_put(y, x, text, attr)

        display._put = record_put
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        codex_titles = [(text, attr) for text, attr in calls if "CODEX" in text]
        self.assertTrue(codex_titles)
        self.assertTrue(codex_titles[0][1] & roundtable.curses.A_REVERSE)

    def test_touch_mode_draws_tappable_help_button(self):
        """Touch users have no "?" key, so draw() must register a real help hitbox.

        Regression check: handle_mouse() reads self.hitboxes["help"], but nothing populated
        it -- the overlay was completely unreachable from a touchscreen.
        """
        display = make_test_display(h=30, w=100)
        display.touch_mode = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        self.assertIn("help", display.hitboxes)
        self.assertIn("HELP", display.s.text())

        top, left, bottom, right = display.hitboxes["help"]
        self.assertFalse(display.show_help)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.handle_mouse(left, top, roundtable.curses.BUTTON1_CLICKED)
        self.assertTrue(display.show_help)

    def test_touch_header_controls_do_not_overlap_brand_or_session_metadata(self):
        """At the 72-column floor, a battery summary used to push STOP over the session text
        and HELP over the brand because each header item was positioned independently."""
        display = make_test_display(h=30, w=72)
        display.touch_mode = True
        display.busy = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary",
                               return_value="Battery 100% · charging"):
            display.draw()

        header = "".join(display.s.grid[0])
        self.assertIn("◈  ROUNDTABLE", header)
        self.assertIn("? HELP", header)
        self.assertIn("■ STOP", header)
        self.assertIn("turns", header)
        help_box = display.hitboxes["help"]
        stop_box = display.hitboxes["stop"]
        self.assertLess(help_box[3], stop_box[1])

    def test_touch_mode_help_modal_shows_gestures_not_keyboard_shortcuts(self):
        """The help overlay is keyboard-shortcut text by default; touch users need gestures."""
        display = make_test_display(h=30, w=100)
        display.touch_mode = True
        display.show_help = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("TOUCH CONTROLS & HELP", rendered)
        self.assertIn("Tap panel", rendered)
        self.assertNotIn("PgUp/PgDn", rendered)

    def test_dashboard_hint_touch_mode_mentions_help_button(self):
        wide = roundtable.dashboard_hint(120, touch_mode=True, busy=True)
        self.assertIn("tap ? for help", wide)

    def test_tick_elevates_retry_and_usage_limit_events_to_retry_kind(self):
        """Retries and provider waits must use kind=retry and stay in the default filter."""
        display = make_test_display(h=30, w=140)
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None

        display.tick(
            "Codex",
            f"failed (timeout) — retrying once in {roundtable.RETRY_BACKOFF_SECONDS:g}s "
            f"(attempt 2/2)")
        self.assertEqual(display.console[-1][0], "retry")
        self.assertIn("retrying once in", display.console[-1][1])

        display.tick(
            "Claude",
            "usage limit reached — waiting until 5:30PM before checking availability")
        self.assertEqual(display.console[-1][0], "retry")

        display.console_filter = 0
        _label, entries = display._filtered_console()
        kinds = {kind for kind, _ in entries}
        self.assertIn("retry", kinds)

    def test_display_parse_retry_state_and_panel_badge(self):
        display = make_test_display(h=30, w=140)
        display.update_status(["Codex"], "Working…")
        display.busy = True
        display.turn_start["Codex"] = time.monotonic()
        display.parse_retry_state(
            "Codex",
            f"failed (API error) — retrying once in {roundtable.RETRY_BACKOFF_SECONDS:g}s "
            f"(attempt 2/2)")
        self.assertEqual(display.retry_state.get("Codex"), "retrying")

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("↻ retrying", rendered)
        status_row = "".join(display.s.grid[3])
        self.assertIn("1 retrying", status_row)
        roster = "".join(display.s.grid[4])
        self.assertIn("↻", roster)

        # Rate-limit stall is a separate badge + status count.
        display.parse_retry_state(
            "Codex", "temporarily unavailable: You've hit your session limit")
        self.assertEqual(display.retry_state.get("Codex"), "rate limited")
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        flat = " ".join(display.s.text().split())
        # At w=140 each agent column is narrow, so the panel uses the compact form
        # ("⏳ limited" / "⏳ limited Ns"); the internal state key stays "rate limited" above.
        self.assertIn("⏳", flat)
        self.assertRegex(flat, r"limited(?:\s+\d+s)?")
        self.assertIn("1 limited", "".join(display.s.grid[3]))

        # Normal progress after a transient retry clears the retry badge.
        display.parse_retry_state(
            "Codex",
            f"failed (timeout) — retrying once in {roundtable.RETRY_BACKOFF_SECONDS:g}s "
            f"(attempt 2/2)")
        display.parse_retry_state("Codex", "Reading roundtable.py")
        self.assertNotIn("Codex", display.retry_state)

        # Completing a turn also clears any remaining stall state.
        display.parse_retry_state(
            "Codex", "temporarily unavailable: You've hit your session limit")
        display.session.turns.append(roundtable.Turn("Codex", "proposal", "done"))
        display.draw = lambda: None
        display.poll_input = lambda: None
        display.monitor.refresh = lambda: None
        display.tick("Codex", "finished")
        self.assertNotIn("Codex", display.retry_state)

    def test_retry_state_clears_on_new_phase(self):
        display = make_test_display()
        display.retry_state = {"Codex": "retrying", "Claude": "rate limited"}
        display.update_status(["Codex"], "Agents are reviewing in parallel")
        self.assertEqual(display.retry_state, {})

    def test_display_header_shows_mock_badge_when_mock_mode_active(self):
        display = make_test_display()
        display.mock = True
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
        rendered = display.s.text()
        self.assertIn("🧪 mock", rendered)

    def test_run_tui_logs_preflight_skipped_to_ui_console(self):
        args = roundtable.build_parser().parse_args(["--mock", "--plain", "--skip-preflight", "goal"])
        session = roundtable.Session("goal", "/tmp", 1, "now", [])
        mock_ui = mock.Mock()
        mock_ui.tick = lambda n, l: None
        with mock.patch.object(roundtable, "Display", return_value=mock_ui), \
             mock.patch.object(roundtable, "conduct"):
            roundtable.run_tui(mock.Mock(), args, session, mock.Mock(), mock.Mock(),
                              mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        mock_ui.log.assert_any_call("Preflight skipped by configuration", kind="phase")

    def test_display_scroll_focused_panel_when_unexpanded(self):
        display = make_test_display()
        display.expanded = None
        display.focused_panel = "Console"
        # The default Console filter hides raw ticks, so use visible key-event entries here.
        display.console.extend([("phase", f"line {i}") for i in range(50)])
        self.assertEqual(display.scroll.get("Console"), 0)
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            handled = display.scroll_expanded(roundtable.curses.KEY_UP)
        self.assertTrue(handled)
        self.assertEqual(display.scroll.get("Console"), 1)

    def test_display_poll_input_mouse_motion_preserves_help_modal(self):
        display = make_test_display()
        display.show_help = True
        keys = iter([roundtable.curses.KEY_MOUSE, -1])
        display.s.getch = lambda: next(keys)
        with mock.patch.object(roundtable.curses, "getmouse", return_value=(0, 10, 10, 0, 0)), \
             mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.poll_input()
        self.assertTrue(display.show_help)

    def test_help_modal_blocks_wheel_scroll_on_underlying_panel(self):
        display = make_test_display()
        display.show_help = True
        display.hitboxes = {"codex": (2, 2, 8, 30)}
        display.scroll["Codex"] = 4
        display.draw = mock.Mock()

        display.handle_mouse(5, 5, roundtable.curses.BUTTON4_PRESSED)

        self.assertEqual(display.scroll["Codex"], 4)
        self.assertTrue(display.show_help)
        display.draw.assert_not_called()

    def test_help_modal_keeps_global_mouse_controls_actionable(self):
        display = make_test_display()
        display.show_help = True
        display.hitboxes = {
            "stop": (0, 20, 0, 29),
            "interrupt": (3, 60, 3, 78),
        }
        display.draw = mock.Mock()

        self.assertEqual(
            display.handle_mouse(20, 0, roundtable.curses.BUTTON1_CLICKED), "stop")
        self.assertEqual(
            display.handle_mouse(60, 3, roundtable.curses.BUTTON1_CLICKED), "interrupt")
        self.assertTrue(display.show_help)
        display.draw.assert_not_called()

    def test_display_put_handles_negative_x_coordinate(self):
        display = make_test_display()
        with mock.patch.object(display.s, "addnstr") as mock_addnstr:
            display._put(5, -2, "test")
            mock_addnstr.assert_not_called()

    def test_agent_run_handles_empty_prompt_gracefully(self):
        """Test that Agent.run properly validates inputs and raises appropriate errors."""
        agent = roundtable.Agent("Codex", Path("/tmp"))
        with self.assertRaises(ValueError) as cm:
            agent.run("", lambda x: None)
        self.assertIn("cannot run with empty prompt", str(cm.exception))

    def test_agent_run_handles_process_launch_failure(self):
        """Test that Agent.run properly handles process launch failures."""
        agent = roundtable.Agent("Codex", Path("/tmp"))
        with mock.patch.object(roundtable.subprocess, "Popen") as mock_popen:
            mock_popen.side_effect = OSError("Command not found")
            with self.assertRaises(RuntimeError) as cm:
                agent.run("test prompt", lambda x: None)
            self.assertIn("failed to start process", str(cm.exception))

    def test_gui_focused_panel_highlights_box_border(self):
        """draw() must actually bold the focused panel's border, not just leave focused_panel set.

        Regression check: the previous version of this test only asserted
        ``display.focused_panel == "Codex"``, which was true before draw() even ran and would
        pass even if the border-highlighting code were deleted entirely.
        """
        display = make_test_display(h=30, w=100)
        original_box = display._box

        def bold_box_count():
            calls = []
            display._box = lambda y, x, height, width, color=0: (
                calls.append(color), original_box(y, x, height, width, color))[-1]
            with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
                 mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
                 mock.patch.object(roundtable, "battery_summary", return_value=""):
                display.draw()
            display._box = original_box
            return sum(1 for color in calls if color & roundtable.curses.A_BOLD)

        display.focused_panel = None
        self.assertEqual(bold_box_count(), 0)

        for name in ("Codex", "Final", "Code", "Console"):
            display.focused_panel = name
            self.assertEqual(bold_box_count(), 1, f"expected exactly one bold border for {name}")

    def test_gui_expanded_panel_has_tappable_close_button(self):
        display = make_test_display(h=30, w=100)
        display.expanded = "Codex"
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
            self.assertIn("close", display.hitboxes)
            top, left, bottom, right = display.hitboxes["close"]
            display.handle_mouse(left, top, roundtable.curses.BUTTON1_CLICKED)
        self.assertIsNone(display.expanded)

    def test_gui_console_filter_hitbox_and_mouse_tap_cycles(self):
        display = make_test_display(h=40, w=100)
        display.console.append(("tick", "Test event"))
        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False), \
             mock.patch.object(roundtable, "battery_summary", return_value=""):
            display.draw()
            self.assertIn("console_filter", display.hitboxes)
            initial_filter = display.console_filter
            top, left, bottom, right = display.hitboxes["console_filter"]
            display.handle_mouse(left, top, roundtable.curses.BUTTON1_CLICKED)
            self.assertEqual(display.console_filter, (initial_filter + 1) % len(roundtable.CONSOLE_FILTERS))

    def test_agent_panel_shows_self_awareness_indicators(self):
        """Agent panels should show self-awareness indicators when in a self session."""
        # Create a session that simulates a self session
        turns = [roundtable.Turn("Codex", "test", "Test content")]
        session = roundtable.Session(
            objective=roundtable.SELF_EDIT_NOTE + " Test objective",
            workspace="/tmp/test",
            rounds=1,
            started_at="now",
            turns=turns
        )

        display = make_test_display(turns=turns)
        display.session = session  # Override with our self-aware session
        display.busy = True
        display.active = {"Codex"}

        with mock.patch.object(roundtable.curses, "color_pair", return_value=0), \
             mock.patch.object(roundtable.curses, "has_colors", return_value=False):
            display.draw()

        rendered = display.s.text()
        # Check that the self indicator (⚡) appears somewhere in the agent panel
        self.assertIn("⚡", rendered, "Self-awareness indicator should appear in agent panel for self sessions")


if __name__ == "__main__":
    unittest.main()
