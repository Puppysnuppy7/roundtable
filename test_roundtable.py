import contextlib
import inspect
import io
import json
import os
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

    def refresh(self):
        pass

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.grid)


def make_test_display(h=48, w=160, turns=None):
    """A Display wired up enough to call draw()/handle_mouse()/poll_input(), bypassing __init__."""
    display = roundtable.Display.__new__(roundtable.Display)
    display.s = FakeScreen(h, w)
    display.session = roundtable.Session("Goal", "/tmp", 0, "now", turns or [])
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
    display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Aider": 0, "Grok": 0, "Qwen": 0,
                      "Final": 0, "Console": 0}
    display.usage_names = ("Codex", "Claude", "Antigravity", "Aider", "Grok", "Qwen")
    display.turn_times = {name: [] for name in display.usage_names}
    display.turn_outputs = {name: [] for name in display.usage_names}
    display.activity_pulses = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
    display.work_activity = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
    display.work_reads = {name: 0 for name in display.usage_names}
    display.work_execs = {name: 0 for name in display.usage_names}
    display.work_writes = {name: 0 for name in display.usage_names}
    display.turn_start = {}
    display._known_turn_count = len(display.session.turns)
    display.console = roundtable.deque(maxlen=300)
    display.run_log = roundtable.RunLog(None)
    display.expanded = None
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

    def test_work_event_strips_terminal_codes_and_labels_common_operations(self):
        self.assertEqual(roundtable.work_event("\x1b[32mReading app.py\x1b[0m"),
                         "⌕ Reading app.py")
        self.assertEqual(roundtable.work_event("executing python3 -m unittest"),
                         "▶ executing python3 -m unittest")
        self.assertEqual(roundtable.work_event("Applying patch"), "✎ Applying patch")

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

    def test_line_editor_supports_cursor_and_multiline_layout(self):
        editor = roundtable.LineEditor("abcd")
        editor.handle_key(roundtable.curses.KEY_LEFT)
        editor.handle_key("X")
        editor.handle_key("\x0e")
        self.assertEqual(editor.text, "abcX\nd")
        lines, cursor_y, cursor_x = roundtable.editor_layout(editor, 4, 3)
        self.assertEqual(lines, ["abcX", "d"])
        self.assertEqual((cursor_y, cursor_x), (1, 0))

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
            ("turn", "turn line"), ("error", "error line"),
        ])
        display.console_filter = 0  # key events: phase, turn, error — no raw ticks
        label, entries = display._filtered_console()
        self.assertEqual(label, "key events")
        self.assertEqual({kind for kind, _ in entries}, {"phase", "turn", "error"})

        display.console_filter = 1  # all activity
        _, entries = display._filtered_console()
        self.assertEqual(len(entries), 5)

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

    def test_prompt_contains_other_agent(self):
        turns = [roundtable.Turn("Claude", "proposal", "Use a queue")]
        prompt = roundtable.prompt_for("Build a worker", turns, "review 1", "Codex")
        self.assertIn("Use a queue", prompt)
        self.assertIn("Build a worker", prompt)

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

    def test_log_path_for_pairs_with_transcript_stem(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Goal", td, 0, "2026-01-01T00:00:00.000001+00:00", [])
            json_path, _ = roundtable.save_session(session, Path(td))
            log_path = roundtable.log_path_for(session, Path(td))
            self.assertEqual(log_path.stem, json_path.stem)
            self.assertEqual(log_path.suffix, ".log")

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

    def test_antigravity_command_is_sandboxed_and_noninteractive(self):
        agent = roundtable.Agent("Antigravity", Path("/tmp/work"), "antigravity-model")
        command = agent.command("Solve this")
        self.assertEqual(command[:3], ["agy", "--print", "Solve this"])
        self.assertIn("accept-edits", command)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[-2:], ["--model", "antigravity-model"])

    def test_aider_command_is_noninteractive_and_never_touches_git(self):
        agent = roundtable.Agent("Aider", Path("/tmp/work"), "mistral/codestral-latest")
        command = agent.command("Solve this")
        self.assertEqual(command[:3], ["aider", "--message", "Solve this"])
        self.assertIn("--yes-always", command)
        self.assertIn("--no-git", command)
        self.assertIn("--no-suggest-shell-commands", command)
        self.assertNotIn("--edit-format", command)
        self.assertEqual(command[-2:], ["--model", "mistral/codestral-latest"])

    def test_aider_command_bounds_each_api_call_with_a_timeout(self):
        # Observed in practice: Aider's own default (unbounded) hung 45+ minutes on a single API
        # call after a malformed provider response, even with --edit-format ask already active --
        # this is a separate failure mode from the edit-reflection loop, one level lower (LiteLLM's
        # own response parsing), so it needs its own fix regardless of no_edit.
        agent = roundtable.Agent("Aider", Path("/tmp/work"))
        command = agent.command("Solve this")
        self.assertIn("--timeout", command)
        self.assertEqual(command[command.index("--timeout") + 1], "180")

    def test_aider_no_edit_uses_ask_mode_to_avoid_the_edit_reflection_loop(self):
        # Verified in practice: without --edit-format ask, a synthesis-phase prompt (prose only,
        # but often quoting code from another agent's proposal) can make Aider mistake that quote
        # for a malformed edit attempt, burning up to 3 retries (its own hard cap) re-sending the
        # full transcript to the model each time -- 900+ seconds observed against a slow provider.
        agent = roundtable.Agent("Aider", Path("/tmp/work"), "mistral/codestral-latest")
        command = agent.command("Write a summary", no_edit=True)
        self.assertIn("--edit-format", command)
        self.assertEqual(command[command.index("--edit-format") + 1], "ask")

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
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted((Path(td) / "out").glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            self.assertEqual(Path(session.workspace), Path(roundtable.__file__).resolve().parent)

    def test_self_flag_suffixes_the_note_so_the_real_objective_leads(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "-C", td, "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(roundtable.main(), 0)
            saved = sorted((Path(td) / "out").glob("*.json"))[-1]
            session = roundtable.load_session(saved)
            self.assertTrue(session.objective.startswith("Improve the console panel"))
            self.assertIn(roundtable.SELF_EDIT_NOTE, session.objective)
            self.assertIn("run `python3 -m unittest test_roundtable`", session.objective)

    def test_explicit_workspace_flag_overrides_self(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Improve the console panel", "--plain", "-r", "0", "--mock",
                    "--self", "-C", td, "--output-dir", str(Path(td) / "out")]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
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

    def test_restart_arguments_preserve_run_configuration(self):
        args = roundtable.build_parser().parse_args([
            "Goal", "--self", "--plain", "--mock", "-r", "2", "-C", "custom_ws", "--collab", "mixed",
            "--synthesizer", "claude", "--synthesis-passes", "1", "--balance-load",
            "--task-status-check", "--reassign-idle", "--elevated", "codex",
            "--codex-model", "model-a", "--output-dir", "saved",
        ])
        command = roundtable.restart_arguments(args, Path("saved/session.json"), True)
        self.assertEqual(command[:2], [sys.executable, str(Path(roundtable.__file__).resolve())])
        self.assertIn("--continue-after-restart", command)
        self.assertIn("followup", command)
        for option in ("--plain", "--mock", "--balance-load", "--task-status-check",
                       "--reassign-idle", "--skip-preflight"):
            self.assertIn(option, command)
        self.assertEqual(command[command.index("--collab") + 1], "mixed")
        self.assertEqual(command[command.index("--synthesizer") + 1], "claude")
        self.assertEqual(command[command.index("--synthesis-passes") + 1], "1")
        self.assertEqual(command[command.index("--codex-model") + 1], "model-a")
        self.assertEqual(command[command.index("--elevated") + 1], "codex")
        self.assertEqual(command[command.index("--rounds") + 1], "2")
        self.assertEqual(command[command.index("--workspace") + 1], "custom_ws")

    def test_source_fingerprint_changes_when_file_content_changes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.py"
            path.write_text("a = 1\n")
            first = roundtable.source_fingerprint(path)
            self.assertEqual(first, roundtable.source_fingerprint(path))
            path.write_text("a = 2\n")
            self.assertNotEqual(first, roundtable.source_fingerprint(path))

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

    def test_apply_option_key_toggles_by_number_and_enter_confirms(self):
        values = {"elevated": False, "balance_load": False, "task_status_check": False}
        values, done = roundtable.apply_option_key(values, "1")
        self.assertFalse(done)
        self.assertTrue(values["elevated"])
        values, done = roundtable.apply_option_key(values, "1")
        self.assertFalse(values["elevated"])
        values, done = roundtable.apply_option_key(values, "9")  # out of range: no-op
        self.assertFalse(done)
        self.assertEqual(values, {"elevated": False, "balance_load": False,
                                  "task_status_check": False})
        values, done = roundtable.apply_option_key(values, "\n")
        self.assertTrue(done)

    def test_apply_option_key_toggles_skip_preflight(self):
        values = {"elevated": False, "balance_load": False, "task_status_check": False, "self": False, "skip_preflight": False}
        values, done = roundtable.apply_option_key(values, "5")
        self.assertFalse(done)
        self.assertTrue(values["skip_preflight"])

    def test_apply_option_key_escape_and_q_confirm_without_toggling(self):
        values = {"elevated": True, "balance_load": False, "task_status_check": False}
        result, done = roundtable.apply_option_key(values, "\x1b")
        self.assertTrue(done)
        self.assertEqual(result, values)
        result, done = roundtable.apply_option_key(values, "q")
        self.assertTrue(done)
        self.assertEqual(result, values)

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

    def test_every_store_true_opt_in_flag_has_a_matching_options_screen_toggle(self):
        """Guards against parser flags added to the parser without a matching entry in OPTION_TOGGLES,
        so the interactive menu silently falls behind."""
        parser = roundtable.build_parser()
        parser_flags = {
            action.dest
            for action in parser._actions
            if isinstance(action, roundtable.argparse._StoreTrueAction)
            and action.help != roundtable.argparse.SUPPRESS
            and action.dest != "plain"
        }
        toggle_names = {name for name, _ in roundtable.OPTION_TOGGLES}
        self.assertTrue(parser_flags <= toggle_names, f"Missing toggles for: {parser_flags - toggle_names}")

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

    def test_update_status_logs_phase_message_changes_once(self):
        display = roundtable.Display.__new__(roundtable.Display)
        display.active = set()
        display.status = "Ready"
        display.turn_start = {}
        display.phase_completed = set()
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
            self.assertEqual(result, "Antigravity's version")
            self.assertEqual([name for name, _ in seen_prompts], ["Claude", "Codex", "Antigravity"])
            self.assertIn("final editor", seen_prompts[0][1].lower())
            self.assertIn("CURRENT DRAFT FINAL ANSWER", seen_prompts[1][1])
            self.assertIn("Claude's version", seen_prompts[1][1])
            self.assertIn("Codex's version", seen_prompts[2][1])
            self.assertEqual(statuses[0], (("Claude",), "Claude is drafting the final answer"))
            self.assertEqual(statuses[1], (("Codex",), "Codex is refining the final answer"))
            self.assertEqual(statuses[-1], ((), "Final answer complete"))

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
            self.assertIn("waiting on Antigravity, Claude", finished[0][1])
            self.assertIn("waiting on nothing else", finished[-1][1])

    def test_signals_task_complete_matches_marker_near_end_of_text(self):
        self.assertTrue(roundtable.signals_task_complete("All done.\nTASK STATUS: complete"))
        self.assertTrue(roundtable.signals_task_complete("All done.\ntask status: COMPLETE  "))
        self.assertTrue(roundtable.signals_task_complete("All done.\nTASK STATUS: complete\n\n"))
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
        with mock.patch.object(roundtable.time, "sleep") as sleep_mock:
            result = roundtable._run_with_retry(agent, "prompt", ticks.append)
        self.assertEqual(result, "recovered on retry")
        self.assertEqual(FlakyAgent.attempts, 2)
        sleep_mock.assert_called_once_with(roundtable.RETRY_BACKOFF_SECONDS)
        self.assertTrue(any("retrying once" in t for t in ticks))

    def test_run_with_retry_tells_agent_to_check_progress_before_a_transient_retry(self):
        class FlakyAgent(roundtable.Agent):
            prompts = []

            def run(self, prompt, on_tick, cancel_event=None, no_edit=False):
                type(self).prompts.append(prompt)
                if len(type(self).prompts) == 1:
                    raise RuntimeError(f"{self.name} exited with status 1\ntimeout")
                return "recovered"

        agent = FlakyAgent("Codex", Path("/tmp"))
        with mock.patch.object(roundtable.time, "sleep"):
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

    def test_usage_limit_detector_matches_provider_message_without_generic_limit_word(self):
        self.assertIn("session limit", roundtable.usage_limit_detail(
            "You've hit your session limit · resets 5:30pm (America/Chicago)"))
        self.assertIsNone(roundtable.usage_limit_detail(
            "Please limit this implementation to the requested scope."))

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
                             "Antigravity content after retry")

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
        session.queued_prompts.append("Refactor authentication module")
        drained = roundtable.drain_queued_prompts(session)
        self.assertTrue(drained)
        self.assertEqual(len(session.turns), 1)
        self.assertEqual(session.turns[0].speaker, "User")
        self.assertEqual(session.turns[0].phase, "follow-up")
        self.assertEqual(session.turns[0].content, "Refactor authentication module")
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
            balance_load=False, task_status_check=False, reassign_idle=False)
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
             mock.patch("roundtable.suppress_focus_reporting"):
            ret = roundtable.run_tui(stdscr, args, session, None, None, None, None, None, None,
                                     resumed=False)

        self.assertEqual(ret, 0)
        self.assertEqual(prompts_at_dispatch, [[], ["Post-synthesis request"]])
        self.assertEqual(session.queued_prompts, [])

    def test_run_tui_self_restart_preserves_active_followup_state(self):
        session = roundtable.Session("Task", "/tmp", 0, "now", [])
        args = roundtable.argparse.Namespace(
            output_dir="/tmp", skip_preflight=True, preflight_timeout=5,
            touch_mode=False, collab="parallel", synthesizer="rotate",
            balance_load=False, task_status_check=False, reassign_idle=False,
            synthesis_passes=3)
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
             mock.patch("roundtable.curses.endwin"), \
             mock.patch("roundtable.suppress_focus_reporting"):
            ret = roundtable.run_tui(
                stdscr, args, session, None, None, None, None, None, None, resumed=False,
                checkpoint=lambda: None)

        self.assertEqual(ret, 0)
        self.assertEqual(calls, 1)
        mock_restart.assert_called_once_with(args, session_path, False)

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

if __name__ == "__main__":
    unittest.main()
