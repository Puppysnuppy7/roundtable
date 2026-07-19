import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import roundtable


class FailingAgent(roundtable.Agent):
    def run(self, prompt, on_tick):
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
    display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0, "Console": 0}
    display.usage_names = ("Codex", "Claude", "Antigravity")
    display.turn_times = {name: [] for name in display.usage_names}
    display.turn_outputs = {name: [] for name in display.usage_names}
    display.activity_pulses = {name: roundtable.deque(maxlen=200) for name in display.usage_names}
    display.turn_start = {}
    display.console = roundtable.deque(maxlen=300)
    display.run_log = roundtable.RunLog(None)
    display.expanded = None
    display.console_filter = 0
    return display


class RoundtableTests(unittest.TestCase):
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

            def run(self, prompt, on_tick):
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
            def run(self, prompt, on_tick):
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

    def test_agents_exchange_and_synthesize(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session("Solve it", td, 1, "now", [])
            codex = roundtable.MockAgent("Codex", Path(td))
            claude = roundtable.MockAgent("Claude", Path(td))
            antigravity = roundtable.MockAgent("Antigravity", Path(td))
            roundtable.conduct(session, codex, claude, antigravity,
                               lambda *_: None, lambda *_: None)
            self.assertEqual([t.speaker for t in session.turns],
                             ["Codex", "Claude", "Antigravity", "Codex", "Claude",
                              "Antigravity", "Final"])
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
            codex = roundtable.MockAgent("Codex", Path(td))
            claude = roundtable.MockAgent("Claude", Path(td))
            antigravity = roundtable.MockAgent("Antigravity", Path(td))
            roundtable.conduct(session, codex, claude, antigravity, lambda *_: None,
                               lambda *_: None, synthesizer="claude",
                               log_prompt=lambda name, p: logged.append((name, p)))
            names_logged = [name for name, _ in logged]
            # proposal + review round 1 + one relay step in the final merge, for every agent
            self.assertEqual(names_logged.count("Codex"), 3)
            self.assertEqual(names_logged.count("Claude"), 3)
            self.assertEqual(names_logged.count("Antigravity"), 3)
            final_prompts = [p for name, p in logged if name == "Claude"]
            self.assertTrue(any("final editor" in p.lower() for p in final_prompts))

    def test_followup_cycle_preserves_consensus_and_focuses_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            session = roundtable.Session(
                "Build it", td, 0, "2026-01-01T00:00:00.000001+00:00",
                [roundtable.Turn("Final", "consensus", "First answer"),
                 roundtable.Turn("User", "follow-up", "Now add search")], "First answer")
            codex = roundtable.MockAgent("Codex", Path(td))
            claude = roundtable.MockAgent("Claude", Path(td))
            antigravity = roundtable.MockAgent("Antigravity", Path(td))
            roundtable.conduct(session, codex, claude, antigravity,
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

    def test_elevated_agents_swap_in_each_clis_permission_bypass_flag(self):
        codex = roundtable.Agent("Codex", Path("/tmp/work"), elevated=True)
        claude = roundtable.Agent("Claude", Path("/tmp/work"), elevated=True)
        antigravity = roundtable.Agent("Antigravity", Path("/tmp/work"), elevated=True)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex.command("Solve this"))
        self.assertNotIn("--sandbox", codex.command("Solve this"))
        self.assertIn("--dangerously-skip-permissions", claude.command("Solve this"))
        self.assertNotIn("--permission-mode", claude.command("Solve this"))
        self.assertIn("--dangerously-skip-permissions", antigravity.command("Solve this"))
        self.assertNotIn("--sandbox", antigravity.command("Solve this"))

    def test_non_elevated_agents_stay_sandboxed_by_default(self):
        codex = roundtable.Agent("Codex", Path("/tmp/work"))
        claude = roundtable.Agent("Claude", Path("/tmp/work"))
        antigravity = roundtable.Agent("Antigravity", Path("/tmp/work"))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", codex.command("Solve this"))
        self.assertIn("--sandbox", codex.command("Solve this"))
        self.assertNotIn("--dangerously-skip-permissions", claude.command("Solve this"))
        self.assertIn("--permission-mode", claude.command("Solve this"))
        self.assertNotIn("--dangerously-skip-permissions", antigravity.command("Solve this"))
        self.assertIn("--sandbox", antigravity.command("Solve this"))

    def test_elevated_flag_resolves_per_agent_and_via_all(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["roundtable", "Solve it", "--plain", "-r", "0", "--mock",
                    "-C", td, "--output-dir", str(Path(td) / "out"), "--elevated", "antigravity"]
            captured = {}

            class RecordingAgent(roundtable.MockAgent):
                def __init__(self, name, workspace, model=None, elevated=False):
                    super().__init__(name, workspace, model)
                    captured[name] = elevated

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(roundtable, "MockAgent", RecordingAgent):
                roundtable.main()
            self.assertEqual(captured, {"Codex": False, "Claude": False, "Antigravity": True})

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
            self.assertEqual(captured["defaults"], {"elevated": True, "balance_load": False,
                                                     "task_status_check": False, "self": False})
            result_args = captured["args"]
            self.assertEqual(result_args.elevated, ["codex"])  # left untouched, so preserved as-is
            self.assertTrue(result_args.balance_load)  # toggled on in the fake screen
            self.assertFalse(result_args.self)  # left untouched
            self.assertFalse(result_args.task_status_check)

    def test_every_store_true_opt_in_flag_has_a_matching_options_screen_toggle(self):
        """Guards against the exact gap --self shipped with: a new opt-in flag added to the parser
        without a matching entry in OPTION_TOGGLES, so the interactive menu silently falls behind."""
        parser_flags = {"balance_load", "task_status_check", "self"}  # store_true, user-facing
        toggle_names = {name for name, _ in roundtable.OPTION_TOGGLES}
        self.assertTrue(parser_flags <= toggle_names)

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
            display.scroll = {"Codex": 0, "Claude": 0, "Antigravity": 0, "Final": 0}
            display.usage_names = ("Codex", "Claude", "Antigravity")
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

            def run(self, prompt, on_tick):
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
            agents = [ConcurrentAgent(name, workspace)
                      for name in ("Codex", "Claude", "Antigravity")]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None)
            self.assertEqual(ConcurrentAgent.maximum, 3)
            self.assertEqual([turn.speaker for turn in session.turns],
                             ["Codex", "Claude", "Antigravity", "Final"])

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
            def run(self, prompt, on_tick):
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
            def run(self, prompt, on_tick):
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
        delays = {"Codex": 0.02, "Claude": 0.02, "Antigravity": 0.32}
        seen_prompts: list[tuple[str, str]] = []

        class StaggeredAgent(roundtable.Agent):
            def run(self, prompt, on_tick):
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
            def run(self, prompt, on_tick):
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

            def run(self, prompt, on_tick):
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
            agents = [LockStepAgent(name, workspace) for name in ("Codex", "Claude", "Antigravity")]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None, collab="sequential")
            self.assertEqual(LockStepAgent.maximum, 1)
            self.assertEqual([t.speaker for t in session.turns],
                             ["Codex", "Claude", "Antigravity", "Final"])

    def test_conduct_mixed_collab_alternates_relay_and_parallel_rounds(self):
        class TrackingAgent(roundtable.Agent):
            lock = threading.Lock()
            active = 0
            maxima = []

            def run(self, prompt, on_tick):
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
            agents = [TrackingAgent(name, workspace) for name in ("Codex", "Claude", "Antigravity")]
            roundtable.conduct(session, *agents, lambda *_: None, lambda *_: None, collab="mixed")
            phases = [t.phase for t in session.turns]
            self.assertIn("review 1", phases)
            self.assertIn("review 2", phases)
            round1_speakers = [t.speaker for t in session.turns if t.phase == "review 1"]
            round2_speakers = [t.speaker for t in session.turns if t.phase == "review 2"]
            self.assertEqual(round1_speakers, ["Codex", "Claude", "Antigravity"])
            self.assertEqual(set(round2_speakers), {"Codex", "Claude", "Antigravity"})
            # maxima order: proposal (parallel), review 1 (sequential relay), review 2 (parallel)
            proposal_maxima, review1_maxima, review2_maxima = (
                TrackingAgent.maxima[0:3], TrackingAgent.maxima[3:6], TrackingAgent.maxima[6:9])
            self.assertTrue(any(v > 1 for v in proposal_maxima))
            self.assertEqual(review1_maxima, [1, 1, 1])
            self.assertTrue(any(v > 1 for v in review2_maxima))

    def test_pick_synthesizer_rotates_by_objective_and_honors_explicit_choice(self):
        with tempfile.TemporaryDirectory() as td:
            codex = roundtable.MockAgent("Codex", Path(td))
            claude = roundtable.MockAgent("Claude", Path(td))
            antigravity = roundtable.MockAgent("Antigravity", Path(td))
            session_a = roundtable.Session("Objective A", td, 0, "now", [])
            session_b = roundtable.Session("A very different objective", td, 0, "now", [])
            rotated_a = roundtable.pick_synthesizer("rotate", session_a, codex, claude, antigravity)[0]
            rotated_a_again = roundtable.pick_synthesizer("rotate", session_a, codex, claude, antigravity)[0]
            rotated_b = roundtable.pick_synthesizer("rotate", session_b, codex, claude, antigravity)[0]
            self.assertEqual(rotated_a, rotated_a_again)
            self.assertIn(rotated_a, ("Codex", "Claude", "Antigravity"))
            self.assertIn(rotated_b, ("Codex", "Claude", "Antigravity"))
            forced = roundtable.pick_synthesizer("claude", session_a, codex, claude, antigravity)
            self.assertEqual(forced, ("Claude", claude))

    def test_synthesis_order_includes_all_three_starting_with_the_chosen_drafter(self):
        with tempfile.TemporaryDirectory() as td:
            codex = roundtable.MockAgent("Codex", Path(td))
            claude = roundtable.MockAgent("Claude", Path(td))
            antigravity = roundtable.MockAgent("Antigravity", Path(td))
            session = roundtable.Session("Objective", td, 0, "now", [])
            order = roundtable.synthesis_order("claude", session, codex, claude, antigravity)
            self.assertEqual([name for name, _ in order][0], "Claude")
            self.assertEqual({name for name, _ in order}, {"Codex", "Claude", "Antigravity"})
            self.assertEqual(len(order), 3)
            # Stable for the same objective, but the trailing pair need not match agent-list order.
            again = roundtable.synthesis_order("claude", session, codex, claude, antigravity)
            self.assertEqual([name for name, _ in order], [name for name, _ in again])

    def test_synthesize_relays_a_draft_through_every_agent_in_order(self):
        seen_prompts: list[tuple[str, str]] = []

        class RecordingAgent(roundtable.Agent):
            def run(self, prompt, on_tick):
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

    def test_parallel_phase_reports_agents_completing_independently(self):
        delays = {"Codex": 0.02, "Claude": 0.12, "Antigravity": 0.22}

        class StaggeredAgent(roundtable.Agent):
            def run(self, prompt, on_tick):
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
            def run(self, prompt, on_tick):
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
        self.assertFalse(roundtable.signals_task_complete("All done.\nTASK STATUS: in-progress"))
        self.assertFalse(roundtable.signals_task_complete("Still working on it."))
        buried = "TASK STATUS: complete" + ("x" * 400)
        self.assertFalse(roundtable.signals_task_complete(buried))

    def test_prompt_for_includes_task_status_hint_only_when_requested(self):
        plain = roundtable.prompt_for("Goal", [], "proposal", "Codex")
        checked = roundtable.prompt_for("Goal", [], "proposal", "Codex", task_status_check=True)
        self.assertNotIn("TASK STATUS", plain)
        self.assertIn("TASK STATUS: complete", checked)

    def test_run_parallel_phase_stops_other_agents_once_one_signals_task_complete(self):
        class WaitingAgent(roundtable.Agent):
            def run(self, prompt, on_tick):
                while not self.cancel_event.is_set():
                    time.sleep(0.01)
                raise RuntimeError(f"{self.name} cancelled")

        class DoneAgent(roundtable.Agent):
            def run(self, prompt, on_tick):
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
            def run(self, prompt, on_tick):
                time.sleep(0.02)
                return "Done.\nTASK STATUS: complete"

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            session = roundtable.Session("Goal", td, 0, "now", [])
            agents = [(name, DoneAgent(name, workspace)) for name in ("Codex", "Claude", "Antigravity")]
            roundtable._run_parallel_phase(session, agents, "proposal", lambda *_: None,
                                           lambda *_: None, "Working")
            self.assertEqual({turn.speaker for turn in session.turns}, {"Codex", "Claude", "Antigravity"})

    def test_conduct_lets_agents_skipped_by_task_status_check_review_next_round(self):
        class DoneOnceAgent(roundtable.Agent):
            def run(self, prompt, on_tick):
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
            codex = DoneOnceAgent("Codex", workspace)
            claude = DoneOnceAgent("Claude", workspace)
            antigravity = DoneOnceAgent("Antigravity", workspace)
            roundtable.conduct(session, codex, claude, antigravity, lambda *_: None, lambda *_: None,
                               synthesizer="codex", task_status_check=True)
            proposal_speakers = [t.speaker for t in session.turns if t.phase == "proposal"]
            review_speakers = [t.speaker for t in session.turns if t.phase == "review 1"]
            self.assertEqual(proposal_speakers, ["Codex"])
            self.assertEqual(set(review_speakers), {"Codex", "Claude", "Antigravity"})

    def test_save_stems_include_microseconds(self):
        with tempfile.TemporaryDirectory() as td:
            one = roundtable.Session("Goal", td, 0, "2026-01-01T00:00:00.000001+00:00", [])
            two = roundtable.Session("Goal", td, 0, "2026-01-01T00:00:00.000002+00:00", [])
            self.assertNotEqual(roundtable.save_session(one, Path(td))[0],
                                roundtable.save_session(two, Path(td))[0])


if __name__ == "__main__":
    unittest.main()
