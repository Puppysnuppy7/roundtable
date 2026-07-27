import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install
import roundtable


class InstallTests(unittest.TestCase):
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
            message = install.install_roundtable_symlink(bin_dir, dry_run=False)
            target = bin_dir / "roundtable"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), install.REPO_ROOT / "roundtable.py")
            self.assertIn("linked", message)

    def test_install_roundtable_symlink_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            install.install_roundtable_symlink(bin_dir, dry_run=False)
            second = install.install_roundtable_symlink(bin_dir, dry_run=False)
            self.assertIn("already linked", second)

    def test_install_roundtable_symlink_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            message = install.install_roundtable_symlink(bin_dir, dry_run=True)
            self.assertIn("would link", message)
            self.assertFalse(bin_dir.exists())

    def test_install_cli_reports_already_installed_without_running_anything(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/codex"), \
             mock.patch.object(install.subprocess, "run") as run:
            message = install.install_cli("Codex", "codex", dry_run=False)
        run.assert_not_called()
        self.assertIn("already installed", message)

    def test_install_cli_reports_no_automated_installer_for_agy_and_grok(self):
        with mock.patch.object(install.shutil, "which", return_value=None):
            for name, executable in (("Antigravity", "agy"), ("Grok", "grok")):
                message = install.install_cli(name, executable, dry_run=False)
                self.assertIn("no automated installer", message)

    def test_install_cli_reports_missing_requirement_without_running_installer(self):
        def fake_which(name):
            return None if name in ("codex", "npm") else f"/usr/bin/{name}"
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message = install.install_cli("Codex", "codex", dry_run=False)
        run.assert_not_called()
        self.assertIn("needs `npm`", message)

    def test_install_cli_dry_run_does_not_invoke_subprocess(self):
        def fake_which(name):
            return None if name == "codex" else f"/usr/bin/{name}"
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message = install.install_cli("Codex", "codex", dry_run=True)
        run.assert_not_called()
        self.assertIn("would run", message)

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
            message = install.install_cli("Codex", "codex", dry_run=False)
        run.assert_called_once_with(["npm", "install", "-g", "@openai/codex"], check=True)
        self.assertIn("installed", message)

    def test_install_cli_reports_subprocess_failure(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=install.subprocess.CalledProcessError(1, "npm")):
            message = install.install_cli("Codex", "codex", dry_run=False)
        self.assertIn("install failed", message)

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

    def test_main_skip_clis_only_links_roundtable(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--skip-clis"])
            self.assertTrue((bin_dir / "roundtable").is_symlink())
            self.assertFalse(any("already installed" in line or "not found" in line for line in printed))


if __name__ == "__main__":
    unittest.main()
