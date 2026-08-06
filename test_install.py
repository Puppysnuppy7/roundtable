import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install
import roundtable


class InstallTests(unittest.TestCase):
    def test_local_agent_manifest_matches_roundtable_without_requiring_import_at_install_time(self):
        self.assertEqual(install.AGENT_EXECUTABLES, roundtable.AGENT_EXECUTABLES)
        self.assertEqual(install.AGENT_NAMES, roundtable.AGENT_NAMES)

    def test_cli_installers_and_requires_cover_exactly_the_agent_executables(self):
        executables = set(roundtable.AGENT_EXECUTABLES.values())
        self.assertEqual(set(install.CLI_INSTALLERS), executables)
        self.assertTrue(set(install.CLI_INSTALLER_REQUIRES) <= executables)
        for executable, command in install.CLI_INSTALLERS.items():
            if command is not None:
                self.assertIn(executable, install.CLI_INSTALLER_REQUIRES, executable)

    def test_default_bin_dir_prefers_local_bin_when_on_path(self):
        with tempfile.TemporaryDirectory() as home:
            other = Path(home) / "custom" / "bin"
            other.mkdir(parents=True)
            local_bin = Path(home) / ".local" / "bin"
            local_bin.mkdir(parents=True)
            with mock.patch.object(Path, "home", return_value=Path(home)), \
                 mock.patch.dict(os.environ, {"PATH": f"{other}{os.pathsep}{local_bin}"}):
                self.assertEqual(install.default_bin_dir(), local_bin)

    def test_default_bin_dir_falls_back_to_first_writable_home_path_entry(self):
        with tempfile.TemporaryDirectory() as home:
            other = Path(home) / "custom" / "bin"
            other.mkdir(parents=True)
            with mock.patch.object(Path, "home", return_value=Path(home)), \
                 mock.patch.dict(os.environ, {"PATH": str(other)}):
                self.assertEqual(install.default_bin_dir(), other)

    def test_default_bin_dir_defaults_to_local_bin_when_nothing_matches(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(Path, "home", return_value=Path(home)), \
                 mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
                self.assertEqual(install.default_bin_dir(), Path(home) / ".local" / "bin")

    def test_install_roundtable_symlink_creates_executable_link(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            message = install.install_roundtable_symlink(bin_dir, dry_run=False,
                                                          current=("linux", "x86_64"))
            target = bin_dir / "roundtable"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), install.REPO_ROOT / "roundtable.py")
            self.assertIn("linked", message)

    def test_install_roundtable_symlink_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            install.install_roundtable_symlink(bin_dir, dry_run=False, current=("linux", "x86_64"))
            second = install.install_roundtable_symlink(bin_dir, dry_run=False,
                                                         current=("linux", "x86_64"))
            self.assertIn("already linked", second)

    def test_install_roundtable_symlink_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            message = install.install_roundtable_symlink(bin_dir, dry_run=True,
                                                          current=("linux", "x86_64"))
            self.assertIn("would link", message)
            self.assertFalse(bin_dir.exists())

    def test_install_roundtable_symlink_falls_back_to_copy_when_symlinks_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            with mock.patch.object(Path, "symlink_to", side_effect=OSError("no symlink support")):
                message = install.install_roundtable_symlink(bin_dir, dry_run=False,
                                                              current=("linux", "x86_64"))
            target = bin_dir / "roundtable"
            self.assertFalse(target.is_symlink())
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), (install.REPO_ROOT / "roundtable.py").read_bytes())
            self.assertIn("copied", message)

    def test_install_roundtable_writes_windows_cmd_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            message = install.install_roundtable_symlink(
                bin_dir, dry_run=False, current=("windows", "x86_64"))
            target = bin_dir / "roundtable.cmd"
            self.assertTrue(target.is_file())
            self.assertIn(str(install.REPO_ROOT / "roundtable.py"), target.read_text())
            self.assertIn("%*", target.read_text())
            self.assertIn("installed launcher", message)

    def test_install_roundtable_preserves_unrelated_existing_command_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            target = bin_dir / "roundtable"
            target.write_text("someone else's command")
            with self.assertRaises(FileExistsError):
                install.install_roundtable_symlink(
                    bin_dir, dry_run=False, current=("linux", "x86_64"))
            self.assertEqual(target.read_text(), "someone else's command")

    def test_install_roundtable_force_replaces_existing_command(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            target = bin_dir / "roundtable"
            target.write_text("old command")
            install.install_roundtable_symlink(
                bin_dir, dry_run=False, force=True, current=("linux", "x86_64"))
            self.assertTrue(target.is_symlink())

    def test_install_roundtable_never_replaces_directory(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            target = bin_dir / "roundtable"
            target.mkdir(parents=True)
            with self.assertRaises(IsADirectoryError):
                install.install_roundtable_symlink(
                    bin_dir, dry_run=False, force=True, current=("linux", "x86_64"))

    def test_install_cli_reports_already_installed_without_running_anything(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/codex"), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Codex", "codex", dry_run=False)
        run.assert_not_called()
        self.assertIn("already installed", message)
        self.assertTrue(ok)

    def test_install_cli_reports_no_automated_installer_for_agy(self):
        with mock.patch.object(install.shutil, "which", return_value=None):
            message, ok = install.install_cli("Antigravity", "agy", dry_run=False,
                                              current=("linux", "x86_64"))
        self.assertIn("no automated installer", message)
        self.assertTrue(ok)  # informational, not a hard failure

    def test_install_cli_grok_installs_via_npm_with_full_arch_support(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Grok", "grok", dry_run=False,
                                              current=("linux", "arm64"))
        run.assert_called_once_with(["/usr/bin/npm", "install", "-g", "@xai-official/grok"],
                                    check=True)
        self.assertNotIn("no verified prebuilt binary", message)
        self.assertNotIn("observed shipping", message)
        self.assertTrue(ok)

    def test_current_platform_normalizes_known_architectures(self):
        cases = {
            "x86_64": "x86_64", "AMD64": "x86_64",
            "aarch64": "arm64", "arm64": "arm64",
            "armv7l": "arm32", "armv6l": "arm32",
        }
        for machine, expected_arch in cases.items():
            with mock.patch.object(install.platform, "machine", return_value=machine):
                _system, arch = install.current_platform()
                self.assertEqual(arch, expected_arch, machine)

    def test_npm_arch_support_only_covers_npm_installed_clis(self):
        npm_executables = {executable for executable, req in install.CLI_INSTALLER_REQUIRES.items()
                           if req == "npm"}
        self.assertEqual(set(install.NPM_ARCH_SUPPORT), npm_executables)

    def test_install_cli_flags_unsupported_architecture_but_still_attempts(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Qwen", "qwen", dry_run=False,
                                              current=("linux", "arm64"))
        run.assert_called_once()
        self.assertIn("no verified prebuilt binary", message)
        self.assertTrue(ok)  # attempt succeeded (mocked); arch note is informational

    def test_install_cli_no_architecture_warning_when_supported(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run"):
            message, ok = install.install_cli("Codex", "codex", dry_run=False,
                                              current=("linux", "arm64"))
        self.assertNotIn("no verified prebuilt binary", message)
        self.assertTrue(ok)

    def test_install_cli_reports_missing_requirement_without_running_installer(self):
        def fake_which(name):
            return None if name in ("codex", "npm") else f"/usr/bin/{name}"
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Codex", "codex", dry_run=False)
        run.assert_not_called()
        self.assertIn("needs `npm`", message)
        self.assertFalse(ok)

    def test_install_cli_dry_run_does_not_invoke_subprocess(self):
        def fake_which(name):
            return None if name == "codex" else f"/usr/bin/{name}"
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Codex", "codex", dry_run=True)
        run.assert_not_called()
        self.assertIn("would run", message)
        self.assertTrue(ok)

    def test_install_cli_runs_installer_and_reports_success(self):
        which_calls = []

        def fake_which(name):
            which_calls.append(name)
            if name == "npm":
                return "/usr/bin/npm"
            if name == "codex" and len(which_calls) > 2:
                return "/usr/local/bin/codex"
            return None

        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Codex", "codex", dry_run=False)
        run.assert_called_once_with(["/usr/bin/npm", "install", "-g", "@openai/codex"], check=True)
        self.assertIn("installed", message)
        self.assertTrue(ok)

    def test_install_cli_resolves_launcher_to_its_full_path_before_running(self):
        """Regression: on Windows, npm/pipx resolve to a .cmd/.exe wrapper that shutil.which()
        finds fine, but subprocess.run(shell=False) can't launch by the bare unqualified name
        ("npm") the way a shell can -- it needs the exact resolved path, extension included."""
        def fake_which(name):
            return r"C:\Program Files\nodejs\npm.CMD" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            install.install_cli("Codex", "codex", dry_run=False)
        called_command = run.call_args.args[0]
        self.assertEqual(called_command[0], r"C:\Program Files\nodejs\npm.CMD")
        self.assertNotEqual(called_command[0], "npm")

    def test_install_cli_runs_multi_step_installer_in_sequence(self):
        """Aider's entry isn't a single command: pipx installs aider-install, which is then run
        to do the actual (uv-resolved) aider install. Both steps must run, in order."""
        def fake_which(name):
            return f"/usr/bin/{name}" if name in ("pipx", "aider-install") else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Aider", "aider", dry_run=False)
        self.assertEqual(run.call_count, 2)
        first_call, second_call = run.call_args_list
        self.assertEqual(first_call.args[0], ["/usr/bin/pipx", "install", "aider-install"])
        self.assertEqual(second_call.args[0], ["/usr/bin/aider-install"])
        self.assertIn("installed", message)
        self.assertTrue(ok)

    def test_install_cli_multi_step_installer_dry_run_shows_both_steps(self):
        def fake_which(name):
            return "/usr/bin/pipx" if name == "pipx" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Aider", "aider", dry_run=True)
        run.assert_not_called()
        self.assertIn("pipx install aider-install", message)
        self.assertIn("aider-install", message)
        self.assertTrue(ok)

    def test_install_cli_multi_step_installer_stops_after_failing_step(self):
        def fake_which(name):
            return f"/usr/bin/{name}" if name in ("pipx", "aider-install") else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=install.subprocess.CalledProcessError(1, "pipx")) as run:
            message, ok = install.install_cli("Aider", "aider", dry_run=False)
        run.assert_called_once()
        self.assertIn("install failed", message)
        self.assertFalse(ok)

    def test_install_cli_reports_subprocess_failure(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=install.subprocess.CalledProcessError(1, "npm")):
            message, ok = install.install_cli("Codex", "codex", dry_run=False)
        self.assertIn("install failed", message)
        self.assertFalse(ok)

    def test_main_only_flag_restricts_cli_installs(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--dry-run", "--only", "Codex", "Aider"])
            body = "\n".join(printed)
            self.assertIn("Codex", body)
            self.assertIn("Aider", body)
            self.assertNotIn("Grok", body)

    def test_main_prints_detected_platform_banner(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--skip-clis", "--dry-run"])
            self.assertTrue(any(line.startswith("Detected platform: ") for line in printed))

    def test_main_warns_on_arm32(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch.object(install, "current_platform", return_value=("linux", "arm32")), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--skip-clis", "--dry-run"])
            self.assertTrue(any("32-bit ARM" in line for line in printed))

    def test_main_warns_on_windows(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch.object(install, "current_platform", return_value=("windows", "x86_64")), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--skip-clis", "--dry-run"])
            self.assertTrue(any("curses" in line for line in printed))

    def test_main_skip_clis_only_links_roundtable(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--skip-clis"])
            self.assertTrue((bin_dir / "roundtable").is_symlink())
            self.assertFalse(any("already installed" in line or "not found" in line for line in printed))

    def test_main_existing_command_returns_failure_and_explains_force(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td)
            (bin_dir / "roundtable").write_text("existing")
            stderr = []
            with mock.patch.object(install, "current_platform",
                                   return_value=("linux", "x86_64")), \
                 mock.patch("builtins.print") as printed:
                printed.side_effect = lambda *args, **kwargs: (
                    stderr.append(" ".join(map(str, args)))
                    if kwargs.get("file") is install.sys.stderr else None)
                result = install.main(["--bin-dir", str(bin_dir), "--skip-clis"])
            self.assertEqual(result, 1)
            self.assertTrue(any("--force" in line for line in stderr))

    def test_install_roundtable_copy_reinstall_is_idempotent_without_force(self):
        """Copy-fallback installs must re-run cleanly (message invites re-run after git pull)."""
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            with mock.patch.object(Path, "symlink_to", side_effect=OSError("no symlink support")):
                first = install.install_roundtable_symlink(
                    bin_dir, dry_run=False, current=("linux", "x86_64"))
                second = install.install_roundtable_symlink(
                    bin_dir, dry_run=False, current=("linux", "x86_64"))
            self.assertIn("copied", first)
            self.assertIn("already installed", second)
            target = bin_dir / "roundtable"
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), (install.REPO_ROOT / "roundtable.py").read_bytes())

    def test_install_roundtable_refreshes_stale_copy_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            target = bin_dir / "roundtable"
            # Minimal stale copy that still carries the recognizable marker.
            target.write_text(
                "#!/usr/bin/env python3\n"
                '"""Roundtable: a dependency-free terminal UI for collaborating coding agents."""\n'
                "# old\n",
                encoding="utf-8",
            )
            message = install.install_roundtable_symlink(
                bin_dir, dry_run=False, current=("linux", "x86_64"))
            self.assertIn("updated copy", message)
            self.assertEqual(target.read_bytes(), (install.REPO_ROOT / "roundtable.py").read_bytes())

    def test_install_roundtable_updates_stale_windows_launcher_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            target = bin_dir / "roundtable.cmd"
            target.write_text('@ "C:\\OldPython\\python.exe" "C:\\old\\roundtable.py" %*\r\n',
                              encoding="utf-8", newline="")
            message = install.install_roundtable_symlink(
                bin_dir, dry_run=False, current=("windows", "x86_64"))
            self.assertIn("updated launcher", message)
            text = target.read_text(encoding="utf-8")
            self.assertIn("roundtable.py", text)
            self.assertNotIn("OldPython", text)

    def test_main_returns_nonzero_when_cli_install_fails(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"

            def fake_install_cli(name, executable, dry_run, current=None):
                if executable == "codex":
                    return f"{name} install failed: boom", False
                return f"{name} already installed (/bin/{executable})", True

            with mock.patch.object(install, "install_cli", side_effect=fake_install_cli), \
                 mock.patch("builtins.print"):
                result = install.main(["--bin-dir", str(bin_dir), "--only", "Codex", "Aider"])
            self.assertEqual(result, 1)

    def test_main_returns_zero_when_only_informational_missing_clis(self):
        """A CLI with no automated installer (e.g. agy) must not make the whole install fail."""
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"

            def fake_install_cli(name, executable, dry_run, current=None):
                return f"{name} not found -- no automated installer here", True

            with mock.patch.object(install, "install_cli", side_effect=fake_install_cli), \
                 mock.patch("builtins.print"):
                result = install.main(["--bin-dir", str(bin_dir), "--only", "Grok"])
            self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
