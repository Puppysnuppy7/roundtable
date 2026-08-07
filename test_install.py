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

    def test_install_cli_update_reruns_command_even_when_already_installed(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else "/usr/bin/codex"
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Codex", "codex", dry_run=False, update=True)
        run.assert_called_once_with(["/usr/bin/npm", "install", "-g", "@openai/codex"], check=True)
        self.assertIn("updated", message)
        self.assertTrue(ok)

    def test_install_cli_update_dry_run_says_would_update_not_install(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else "/usr/bin/codex"
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Codex", "codex", dry_run=True, update=True)
        run.assert_not_called()
        self.assertIn("would update", message)
        self.assertTrue(ok)

    def test_install_cli_without_update_skips_already_installed_without_running_anything(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/codex"), \
             mock.patch.object(install.subprocess, "run") as run:
            message, ok = install.install_cli("Codex", "codex", dry_run=False, update=False)
        run.assert_not_called()
        self.assertIn("already installed", message)
        self.assertTrue(ok)

    def test_install_cli_update_reports_already_installed_when_no_automated_installer(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/agy"):
            message, ok = install.install_cli("Antigravity", "agy", dry_run=False, update=True)
        self.assertIn("already installed", message)
        self.assertIn("no automated update available", message)
        self.assertTrue(ok)

    def test_install_cli_update_reports_already_installed_when_requirement_missing(self):
        def fake_which(name):
            return "/usr/bin/codex" if name == "codex" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which):
            message, ok = install.install_cli("Codex", "codex", dry_run=False, update=True)
        self.assertIn("already installed", message)
        self.assertIn("needs `npm` on PATH to auto-update", message)
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

    def test_ensure_windows_dependencies_is_a_noop_off_windows(self):
        with mock.patch.object(install.subprocess, "run") as run:
            message = install.ensure_windows_dependencies(dry_run=False, current=("linux", "x86_64"))
        run.assert_not_called()
        self.assertIsNone(message)

    def test_ensure_windows_dependencies_dry_run_touches_nothing(self):
        with mock.patch.object(install.subprocess, "run") as run:
            message = install.ensure_windows_dependencies(dry_run=True, current=("windows", "x86_64"))
        run.assert_not_called()
        self.assertIn("windows-curses", message)
        self.assertIn("tzdata", message)

    def test_ensure_windows_dependencies_installs_via_the_running_interpreter(self):
        with mock.patch.object(install.subprocess, "run") as run:
            message = install.ensure_windows_dependencies(dry_run=False, current=("windows", "x86_64"))
        run.assert_called_once_with(
            [install.sys.executable, "-m", "pip", "install", "windows-curses", "tzdata"], check=True)
        self.assertIn("installed", message)

    def test_ensure_windows_dependencies_reports_failure(self):
        with mock.patch.object(install.subprocess, "run",
                               side_effect=install.subprocess.CalledProcessError(1, "pip")):
            message = install.ensure_windows_dependencies(dry_run=False, current=("windows", "x86_64"))
        self.assertIn("failed", message)

    def test_refresh_windows_path_is_noop_off_windows(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
            install.refresh_windows_path(current=("linux", "x86_64"))
            self.assertEqual(os.environ["PATH"], "/usr/bin")

    def test_refresh_windows_path_is_noop_when_winreg_unavailable(self):
        with mock.patch.object(install, "winreg", None), \
             mock.patch.dict(os.environ, {"PATH": r"C:\existing"}, clear=False):
            install.refresh_windows_path(current=("windows", "x86_64"))
            self.assertEqual(os.environ["PATH"], r"C:\existing")

    def test_refresh_windows_path_merges_new_registry_entries(self):
        class FakeKey:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class FakeWinreg:
            HKEY_LOCAL_MACHINE = 1
            HKEY_CURRENT_USER = 2

            def OpenKey(self, hive, subkey):
                if hive == self.HKEY_LOCAL_MACHINE:
                    return FakeKey(r"C:\Windows;C:\Windows\System32")
                return FakeKey(r"C:\Users\User\.local\bin;C:\Program Files\nodejs")

            def QueryValueEx(self, key, name):
                return key.value, 1

        with mock.patch.object(install, "winreg", FakeWinreg()), \
             mock.patch.dict(os.environ, {"PATH": r"C:\existing"}, clear=False):
            install.refresh_windows_path(current=("windows", "x86_64"))
            dirs = os.environ["PATH"].split(";")
        self.assertEqual(dirs[0], r"C:\existing")
        self.assertIn(r"C:\Program Files\nodejs", dirs)
        self.assertIn(r"C:\Users\User\.local\bin", dirs)

    def test_refresh_windows_path_dedupes_case_insensitively(self):
        class FakeKey:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class FakeWinreg:
            HKEY_LOCAL_MACHINE = 1
            HKEY_CURRENT_USER = 2

            def OpenKey(self, hive, subkey):
                return FakeKey(r"c:\existing;C:\NewDir")

            def QueryValueEx(self, key, name):
                return key.value, 1

        with mock.patch.object(install, "winreg", FakeWinreg()), \
             mock.patch.dict(os.environ, {"PATH": r"C:\existing"}, clear=False):
            install.refresh_windows_path(current=("windows", "x86_64"))
            dirs = os.environ["PATH"].split(";")
        self.assertEqual(dirs.count(r"C:\existing"), 1)
        self.assertIn(r"C:\NewDir", dirs)

    def test_refresh_windows_path_tolerates_missing_registry_keys(self):
        class FakeWinreg:
            HKEY_LOCAL_MACHINE = 1
            HKEY_CURRENT_USER = 2

            def OpenKey(self, hive, subkey):
                raise OSError("key not found")

            def QueryValueEx(self, key, name):
                raise AssertionError("unreachable")

        with mock.patch.object(install, "winreg", FakeWinreg()), \
             mock.patch.dict(os.environ, {"PATH": r"C:\existing"}, clear=False):
            install.refresh_windows_path(current=("windows", "x86_64"))
            self.assertEqual(os.environ["PATH"], r"C:\existing")

    def test_refresh_windows_path_expands_registry_env_var_tokens(self):
        """Regression: Antigravity's own installer writes its PATH entry as the literal,
        unexpanded '%LOCALAPPDATA%\\agy\\bin' (a REG_EXPAND_SZ token) -- QueryValueEx returns
        that raw string as-is; only os.path.expandvars actually resolves it."""
        class FakeKey:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class FakeWinreg:
            HKEY_LOCAL_MACHINE = 1
            HKEY_CURRENT_USER = 2

            def OpenKey(self, hive, subkey):
                if hive == self.HKEY_CURRENT_USER:
                    return FakeKey(r"%LOCALAPPDATA%\agy\bin")
                raise OSError("not set")

            def QueryValueEx(self, key, name):
                return key.value, 2  # REG_EXPAND_SZ

        with mock.patch.object(install, "winreg", FakeWinreg()), \
             mock.patch.dict(os.environ,
                             {"PATH": r"C:\existing", "LOCALAPPDATA": r"C:\Users\User\AppData\Local"},
                             clear=False):
            install.refresh_windows_path(current=("windows", "x86_64"))
            dirs = os.environ["PATH"].split(";")
        self.assertIn(r"C:\Users\User\AppData\Local\agy\bin", dirs)
        self.assertNotIn(r"%LOCALAPPDATA%\agy\bin", dirs)

    def test_confirm_defaults_no_when_stdin_is_not_a_tty(self):
        with mock.patch.object(install.sys.stdin, "isatty", return_value=False), \
             mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            self.assertFalse(install._confirm("Update?"))

    def test_confirm_accepts_y_variants(self):
        with mock.patch.object(install.sys.stdin, "isatty", return_value=True):
            for reply in ("y", "Y", "yes", "YES", "  y  "):
                with mock.patch("builtins.input", return_value=reply):
                    self.assertTrue(install._confirm("Update?"), reply)

    def test_confirm_rejects_anything_else(self):
        with mock.patch.object(install.sys.stdin, "isatty", return_value=True):
            for reply in ("n", "no", "", "sure"):
                with mock.patch("builtins.input", return_value=reply):
                    self.assertFalse(install._confirm("Update?"), reply)

    def test_confirm_defaults_no_on_eof_or_interrupt(self):
        with mock.patch.object(install.sys.stdin, "isatty", return_value=True):
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertFalse(install._confirm("Update?"))
            with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
                self.assertFalse(install._confirm("Update?"))

    def test_update_package_managers_dry_run_lists_commands_without_running_them(self):
        def fake_which(name):
            return f"/usr/bin/{name}" if name in ("npm", "pipx") else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install.subprocess, "run") as run:
            messages, failed = install.update_package_managers(dry_run=True)
        run.assert_not_called()
        self.assertFalse(failed)
        joined = "\n".join(messages)
        self.assertIn("Python", joined)
        self.assertIn("pip install --upgrade pip", joined)
        self.assertIn("npm install -g npm@latest", joined)
        self.assertIn("pip install --upgrade pipx", joined)

    def test_update_package_managers_only_upgrades_whats_present(self):
        with mock.patch.object(install.shutil, "which", return_value=None), \
             mock.patch.object(install.subprocess, "run") as run:
            messages, failed = install.update_package_managers(dry_run=False)
        run.assert_called_once()  # just pip -- npm/pipx aren't on PATH here
        self.assertFalse(failed)
        self.assertTrue(any("pip upgraded" in m for m in messages))

    def test_update_package_managers_reports_a_failed_upgrade(self):
        with mock.patch.object(install.shutil, "which", return_value=None), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=install.subprocess.CalledProcessError(1, "pip")):
            messages, failed = install.update_package_managers(dry_run=False)
        self.assertTrue(failed)
        self.assertTrue(any("pip upgrade failed" in m for m in messages))

    def test_update_package_managers_explains_externally_managed_environment_without_failing(self):
        """Regression: found by actually running --update live on a real Debian-based box (the
        Pi) -- pip refuses to touch its own system install there (PEP 668), --user included. This
        is the OS correctly protecting itself, not a bug in this script, so it must not count as
        a failure -- just an honest, actionable explanation instead of a bare exit-status message."""
        error = install.subprocess.CalledProcessError(
            1, ["pip", "install", "--upgrade", "pip"],
            output="", stderr="error: externally-managed-environment\n\n"
                              "× This environment is externally managed\n")
        with mock.patch.object(install.shutil, "which", return_value=None), \
             mock.patch.object(install.subprocess, "run", side_effect=error):
            messages, failed = install.update_package_managers(dry_run=False)
        self.assertFalse(failed)
        self.assertTrue(any("externally managed" in m and "upgrade skipped" in m for m in messages))
        self.assertFalse(any("upgrade failed" in m for m in messages))

    def test_main_update_dry_run_shows_package_manager_plan_without_prompting(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch.object(install, "is_musl_libc", return_value=False), \
                 mock.patch.object(install, "install_cli", return_value=("Codex ok", True)), \
                 mock.patch("builtins.input", side_effect=AssertionError("should not prompt")), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--update", "--dry-run", "--only", "Codex"])
            self.assertTrue(any("pip install --upgrade pip" in line for line in printed))

    def test_main_update_skips_package_managers_when_declined(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            printed = []
            with mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch.object(install, "is_musl_libc", return_value=False), \
                 mock.patch.object(install, "install_cli", return_value=("Codex ok", True)), \
                 mock.patch.object(install, "_confirm", return_value=False), \
                 mock.patch.object(install.subprocess, "run") as run, \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                install.main(["--bin-dir", str(bin_dir), "--update", "--only", "Codex"])
            run.assert_not_called()
            self.assertTrue(any("skipped pip/npm/pipx update" in line for line in printed))

    def test_main_update_runs_package_managers_when_confirmed(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            with mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch.object(install, "is_musl_libc", return_value=False), \
                 mock.patch.object(install, "install_cli", return_value=("Codex ok", True)), \
                 mock.patch.object(install, "_confirm", return_value=True) as confirm, \
                 mock.patch.object(install.shutil, "which", return_value=None), \
                 mock.patch.object(install.subprocess, "run") as run, \
                 mock.patch("builtins.print"):
                install.main(["--bin-dir", str(bin_dir), "--update", "--only", "Codex"])
            confirm.assert_called_once()
            run.assert_called_once()  # just pip, since which() is mocked to find nothing else

    def test_main_install_without_update_never_touches_package_managers(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            with mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch.object(install, "is_musl_libc", return_value=False), \
                 mock.patch.object(install, "install_cli", return_value=("Codex ok", True)), \
                 mock.patch.object(install, "_confirm",
                                   side_effect=AssertionError("should not be asked")), \
                 mock.patch("builtins.print"):
                install.main(["--bin-dir", str(bin_dir), "--only", "Codex"])

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

    def test_is_wsl_false_off_linux_without_touching_proc_version(self):
        with mock.patch("builtins.open", side_effect=AssertionError("should not be reached")):
            self.assertFalse(install.is_wsl(current=("windows", "x86_64")))
            self.assertFalse(install.is_wsl(current=("darwin", "arm64")))

    def test_is_wsl_true_when_proc_version_names_microsoft(self):
        with mock.patch.object(install.Path, "read_text",
                               return_value="Linux version 5.15.90.1-microsoft-standard-WSL2"):
            self.assertTrue(install.is_wsl(current=("linux", "x86_64")))

    def test_is_wsl_false_on_bare_metal_linux(self):
        with mock.patch.object(install.Path, "read_text",
                               return_value="Linux version 6.1.0-amd64 (Debian)"):
            self.assertFalse(install.is_wsl(current=("linux", "x86_64")))

    def test_is_musl_libc_false_off_linux_without_shelling_out(self):
        with mock.patch.object(install.subprocess, "run",
                               side_effect=AssertionError("should not be reached")):
            self.assertFalse(install.is_musl_libc(current=("windows", "x86_64")))

    def test_is_musl_libc_true_via_musl_loader_file(self):
        with mock.patch.object(install.Path, "glob", return_value=[Path("libc.musl-x86_64.so.1")]):
            self.assertTrue(install.is_musl_libc(current=("linux", "x86_64")))

    def test_is_musl_libc_true_via_ldd_fallback(self):
        fake_result = mock.Mock(stdout="linux-vdso.so.1\n\t/lib/ld-musl-x86_64.so.1 (0x7f...)\n")
        with mock.patch.object(install.Path, "glob", return_value=[]), \
             mock.patch.object(install.subprocess, "run", return_value=fake_result):
            self.assertTrue(install.is_musl_libc(current=("linux", "x86_64")))

    def test_is_musl_libc_false_on_glibc(self):
        fake_result = mock.Mock(stdout="linux-vdso.so.1\n\t/lib/x86_64-linux-gnu/libc.so.6\n")
        with mock.patch.object(install.Path, "glob", return_value=[]), \
             mock.patch.object(install.subprocess, "run", return_value=fake_result):
            self.assertFalse(install.is_musl_libc(current=("linux", "x86_64")))

    def test_install_cli_notes_musl_when_arch_pair_is_otherwise_supported(self):
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which):
            message, ok = install.install_cli("Grok", "grok", dry_run=True,
                                              current=("linux", "x86_64"), musl=True)
        self.assertIn("musl", message)
        self.assertTrue(ok)

    def test_install_cli_does_not_probe_musl_itself(self):
        """musl is an explicit param, not auto-detected here -- is_musl_libc() shells out to ldd,
        and doing that once per agent (instead of once, in main()) would be six ldd calls for no
        reason, and would also collide with subprocess.run mocks in every other install_cli test."""
        def fake_which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.object(install.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install, "is_musl_libc",
                               side_effect=AssertionError("should not be called")):
            install.install_cli("Grok", "grok", dry_run=True, current=("linux", "x86_64"))

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
        self.assertIn("would install", message)
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
        self.assertEqual(first_call.args[0], ["/usr/bin/pipx", "install", "--force", "aider-install"])
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
        self.assertIn("pipx install --force aider-install", message)
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

    def test_main_update_flag_reaches_install_cli(self):
        seen_update_values = []

        def fake_install_cli(name, executable, dry_run, current=None, musl=False, update=False):
            seen_update_values.append(update)
            return f"{name} ok", True

        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            with mock.patch.object(install, "install_cli", side_effect=fake_install_cli), \
                 mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch("builtins.print"):
                install.main(["--bin-dir", str(bin_dir), "--update", "--only", "Codex"])
        self.assertEqual(seen_update_values, [True])

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

            def fake_install_cli(name, executable, dry_run, current=None, musl=False, update=False):
                if executable == "codex":
                    return f"{name} install failed: boom", False
                return f"{name} already installed (/bin/{executable})", True

            with mock.patch.object(install, "install_cli", side_effect=fake_install_cli), \
                 mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch("builtins.print"):
                result = install.main(["--bin-dir", str(bin_dir), "--only", "Codex", "Aider"])
            self.assertEqual(result, 1)

    def test_main_returns_zero_when_only_informational_missing_clis(self):
        """A CLI with no automated installer (e.g. agy) must not make the whole install fail."""
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"

            def fake_install_cli(name, executable, dry_run, current=None, musl=False, update=False):
                return f"{name} not found -- no automated installer here", True

            with mock.patch.object(install, "install_cli", side_effect=fake_install_cli), \
                 mock.patch.object(install, "current_platform", return_value=("linux", "x86_64")), \
                 mock.patch("builtins.print"):
                result = install.main(["--bin-dir", str(bin_dir), "--only", "Grok"])
            self.assertEqual(result, 0)

    def test_run_reports_unexpected_exception_instead_of_a_raw_traceback(self):
        stderr = []
        with mock.patch.object(install, "main", side_effect=RuntimeError("boom")), \
             mock.patch.object(install.sys, "stderr") as fake_stderr:
            fake_stderr.write.side_effect = lambda s: stderr.append(s)
            result = install.run([])
        self.assertEqual(result, 1)
        self.assertTrue(any("boom" in line for line in stderr))
        self.assertTrue(any("unexpected failure" in line for line in stderr))

    def test_run_reports_keyboard_interrupt_as_130(self):
        with mock.patch.object(install, "main", side_effect=KeyboardInterrupt), \
             mock.patch("builtins.print"):
            result = install.run([])
        self.assertEqual(result, 130)

    def test_run_passes_through_normal_exit_code(self):
        with mock.patch.object(install, "main", return_value=0):
            self.assertEqual(install.run([]), 0)


if __name__ == "__main__":
    unittest.main()
