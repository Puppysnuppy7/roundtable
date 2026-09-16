# Roundtable — handoff notes

Originally written by Claude (Sonnet 5), and updated by Codex, so a fresh session can pick this up
with full context. It will go stale; treat it as a snapshot, not a source of truth. Re-read
`git log`, the actual code, and the newest Roundtable log for anything that happened after this was
written.

**Tracked in git since 2026-09-16.** It used to be gitignored, which meant each machine kept its own
divergent copy: the Optiplex copy held an entire 2026-08-11 Windows section this one never had, and
the Pi -- the canonical repo host -- had no copy at all. That content is merged in below. Because
this file is now tracked, append to it and commit like any other change; don't keep machine-local
edits out of git, or the divergence starts over.

**Last updated:** 2026-07-24, after commit `4ee7044`, while a newer uncommitted `--self` run was
still active.

## What this project is

`/home/user/roundtable/roundtable.py` (~3660 lines) — a dependency-free, single-file, curses-based
terminal UI where **six** coding-agent CLIs collaborate on a task: `codex` (OpenAI), `claude`
(Anthropic), `agy`/Antigravity (Google), `aider` (model-agnostic, pinned to Mistral's Codestral by
default so it doesn't duplicate a lab-native agent), `grok` (xAI), and `qwen` (Alibaba) — five labs
plus one swappable, so no two agents run the same underlying model by default. Parallel/sequential/
mixed proposal+review rounds, a multi-agent final-answer relay, per-agent role rotation,
`DIBS:`-based work-claiming, a shared append-only `AGENT_PROMPTS.md` scratch board, live usage
graphs, a per-agent "used N% of usage limit" gauge, a filterable console panel, an interactive
startup options-toggle screen (skipped on a `--self` restart), and a `--self` mode for the agents to
edit roundtable's own source (with a throwaway `self-test-sandbox` copy so agents can smoke-test a
real invocation without touching the live shared workspace). It now also has phase-aware effort
hints, coarse evidence-based completion estimates, detailed reproducibility/lifecycle logging,
per-turn agent signatures, cancellable staggered starts/retry backoff, early-completion pruning, and
duplicate-final normalization. Full behavior is documented in `README.md` — read that before
assuming anything about current flags/behavior; it is kept in sync with committed code.

Test suite: `test_roundtable.py` (~3730 lines, **223 tests at committed HEAD**), run with
`python3 -m unittest test_roundtable` from `/home/user/roundtable`. Should always be green before
considering any change done. Commit `4ee7044` was independently verified with all 223 tests, a
terminating mock smoke run, `py_compile`, and `git diff --check`.

## Current state

- Git repo, 23 commits on `master`, committed HEAD is `4ee7044` (`Streamline multi-agent completion
  workflow`).
- **The working tree is not clean as of this update.** A newer live `--self` run, objective
  “Streamline and debug roundtable,” has modified `roundtable.py` and `test_roundtable.py` after
  `4ee7044` (currently 136 insertions / 33 deletions; `git diff --check` passes). These changes are
  unreviewed and uncommitted. Do not discard or commit them blindly. The active run log is
  `/home/user/.roundtable/roundtable-20260724-133624-398205.log`; it had reported 227 tests from an
  agent but had not reached final synthesis when this handoff was updated.
- Git identity for this repo: `Puppysnuppy <liammhoyer@gmail.com>`, set locally (not global) after
  explicit user confirmation. Don't change it without asking again.
- No remote is configured. All commits so far are local-only, which is why history has been
  rewritten more than once this session (splitting an accidentally-bundled commit) — safe to do
  again if needed, but always confirm with the user first per standard git-safety practice, and
  never do it if a remote/push exists.

Recent committed milestones:

- `4ee7044` — cancellable stagger/retry waits, early-completion review and synthesis pruning,
  completing-agent drafter preference, fairer load timing, batched progress events, duplicate-final
  normalization, and terminating sandbox smoke guidance.
- `e4f740a` — safer self-restart/sandbox behavior and Aider read-only context reduction.
- `b461371` — hardened agent runs, work signatures, and expanded diagnostics.
- `159879b` — agent-efficiency improvements and task-completion estimation.

## Incidents and lessons baked into the current code (read before touching --self)

- **Destructive concurrent-edit incident**: early in this project's life, nothing had ever been
  committed, and a `--self` run with multiple real (non-mock) agents got simultaneous write access
  to roundtable's own source with only advisory (`DIBS:`) conflict prevention — it reverted
  `roundtable.py`/`test_roundtable.py` to the last git commit, losing uncommitted work. Recovered by
  manual reconstruction verified against the test suite, then committed for the first time. Moral:
  **always keep working tree committed before a `--self` run**; DIBS is a hint agents are asked to
  follow, not an enforced lock.
- **Aider is the one CLI to watch closest.** It's LiteLLM-based (not a vendor-native client like the
  other five), which is the root cause of two distinct real bugs already fixed: (1) an
  edit-reflection loop mistaking quoted code in prose replies for malformed edits, burning 900+s on
  retries — fixed via `--edit-format ask` (the `no_edit` parameter) for non-editing turns; (2) a
  genuine 45-minute hang from an unbounded per-call timeout after a LiteLLM/Mistral response-parsing
  compatibility error — fixed via `--timeout 180`. Both were found by reading real `.roundtable/*.log`
  files, not by guessing from symptoms. Read-only Aider turns now also use `--no-git` so filename
  mentions cannot auto-add the repository to preflight/synthesis context, and all Aider turns use
  `--disable-playwright`. This reduced a measured preflight to 107 input tokens, but ordinary
  editing/review turns still auto-add mentioned source files because Aider exposes no supported
  switch to disable that behavior; recent self runs still showed 81–83k-token Aider editing turns.
  Keep normal editing turns repository-aware unless deliberately accepting a less capable
  analysis-only Aider.
- **Qwen has three auth gotchas**, all verified against the real CLI, not docs: needs
  `--auth-type openai` or env vars are silently ignored with a misleading "Invalid API-key" error;
  needs an explicit `-m <model>` (env var alone isn't enough); must never be passed `--sandbox`
  (hangs indefinitely without a container runtime — `--safe-mode` + `--approval-mode` alone is the
  right non-interactive posture).
- **Antigravity needed `"command(*)"` added to `~/.gemini/antigravity-cli/settings.json`'s
  `permissions.allow`** to pass headless preflight without `--dangerously-skip-permissions` — done
  and confirmed kept by the user; don't assume a fresh machine has this.
- **Resource contention**: launching all 6 agent subprocesses at once caused spurious preflight
  timeouts on constrained hardware. Fixed via `AGENT_SPAWN_STAGGER_SECONDS` (cancellable stagger
  inside worker threads, still fully concurrent overall). Tests patch this to `0.0` in `setUp` —
  see `RoundtableTests.setUp` in test_roundtable.py. A timing test was made deterministic in
  `4ee7044` by comparing the recorded sample to actual time inside `run()`, rather than assuming a
  sleeping thread will always be rescheduled within 150ms.
- **Smoke-test process leak**: the self-test prompt used to advertise `--mock` without `--plain`,
  leaving TUI mock processes waiting for input. The prompt and README now prescribe
  `--mock --plain --skip-preflight --synthesis-passes 1 -r 0`; three confirmed old orphans were
  terminated. Preserve the terminating flags in future examples.
- **Duplicated final answer**: Antigravity once emitted two complete final blocks with slightly
  different test timings. Exact-string dedupe was insufficient. `normalize_final_answer()` now
  keeps the latest structured `## Completed` block and logs when it discards an earlier duplicate.
- **Half-finished autonomous work gets deleted, not patched around.** A `--self` run once produced a
  ~360-line token-usage/ETA subsystem that was never wired into anything user-visible and had almost
  no test coverage — it was cut entirely rather than kept "for later." When reviewing `--self` output
  (or any agent's), check for dead code (`grep` for a new function's call sites outside its own
  definition and its test) before committing it.
- **Review `--self` diffs before committing, especially after multiple restarts in one run** — a
  commit was once accidentally made bundling a small intended fix together with legitimate but
  unreviewed work from an earlier restart, because `git add` was run without checking `git diff`
  first for pre-existing staged/unstaged changes. Caught after the fact and split via interactive
  rebase (safe since no remote existed). Always `git diff`/`git status` before `git add` when
  multiple `--self` restarts may have accumulated changes.

## Useful context for continuing work

- Real run logs land under the configured relative `--output-dir`, so location depends on the
  launcher cwd: commonly `~/.roundtable/*.log` when launched from `/home/user`, or
  `/home/user/roundtable/.roundtable/*.log` when launched from the repo. Search both. Logs are
  elapsed-time-prefixed and include full prompts plus lifecycle diagnostics; they are the best
  source of truth for what actually happened.
- Test doubles for `Display` bypass `__init__` via `Display.__new__(...)` and manually set
  attributes (see `make_test_display` helper and `FakeScreen` class near the top of
  `test_roundtable.py`) — curses can't be meaningfully unit-tested otherwise. New `Display` state
  should be read defensively (`getattr`/`.get()` with a default) wherever practical so old test
  fixtures don't need updating every time; `make_test_display` itself should still be kept current
  for state new tests will want to set directly.
- `OPTION_TOGGLES` (the startup numbered-toggle screen) must stay in sync with every `store_true` CLI
  flag — `test_every_store_true_opt_in_flag_has_a_matching_options_screen_toggle` guards this via
  introspection. Always append new toggles at the end, never insert in the middle (silently renumbers
  existing keyboard shortcuts).
- The user has a habit of running `roundtable --self` between conversation turns and then asking
  "check recent changes" / "read the log" — always check `git diff`/`git log` and the most recent
  `.roundtable/*.log` file first before assuming a report is about something already known.
- `--self` can restart the same PID with `execv`; a single log may contain several “Roundtable run
  started” sections with elapsed time resetting to zero. Treat those as process segments, not
  separate sessions. Completed-phase state and the completing agent are preserved across restart.
- `--task-status-check` now allows one verification review after an agent declares completion, then
  skips redundant reviews. Default rotating synthesis is capped to draft + one refinement after
  early completion, and prefers the completing agent as drafter; an explicit `--synthesizer` still
  wins.
- Dynamic-default pattern used for things tests need to override (e.g. `stagger: float | None =
  None`, resolved against a module constant inside the function body): lets tests pass an explicit
  override without needing the constant frozen at function-definition time. Follow this pattern for
  any new tunable that needs a fast value in tests and a slower real-world default.
- The project explicitly avoids inventing numbers a CLI never reported (see the usage-limit gauge's
  "sparse dict, no signal = no gauge" design, and `AGENT_PROMPTS.md`'s own "never invent exact
  usage" guidance to agents). Keep that discipline for any future usage/metrics work.

## How to resume

1. `cd /home/user/roundtable && git status && git log --oneline -20` — see what's changed since this
   was written. Expect a dirty self-run tree unless the active run was reviewed/committed later.
2. Check whether the newest `--self` process/run has finished before testing or editing. Inspect
   both output roots:
   `find ~/.roundtable /home/user/roundtable/.roundtable -maxdepth 1 -name '*.log' -printf '%T@ %p\n' | sort -nr | head`.
3. Review `git diff` in full. The current post-`4ee7044` changes are agent-authored and unreviewed.
   Preserve them while investigating.
4. `python3 -m unittest test_roundtable -q` — independently confirm green after the live run stops.
5. If there's an uncommitted diff, review it (`git diff`) before assuming it's safe — it may be a
   self-edit run's output that hasn't been checked yet, possibly spanning multiple restarts.
6. `README.md` is the authoritative behavior doc — skim it for anything that's changed shape since
   this file was written.


## Addendum (added by a separate Claude Code instance, reached over SSH, 2026-08-05 ~20:45 CDT)

Per governance in CLAUDE.md (not present when this handoff was last written) - this is informational only, no repo changes made, nothing here needs sign-off.

- The live session (PID 48155 at time of writing) is testing roundtable's installer across platforms: **Linux passed** in a clean Docker container. **Windows** is still mid-OOBE in a headless (`-display none`, intentional) QEMU VM (`~/vms/win10-test.qcow2`), actively writing to disk, not stuck.
- This matches the uncommitted `git status` diff (README.md, roundtable.py, test_roundtable.py modified; CLAUDE.md, PENDING_REVIEW.md untracked/new) seen at the same time - consistent with active install.py cross-platform work, not left over from something older.
- PENDING_REVIEW.md had no entries as of this addendum.
- Not verified independently: exact test counts, current commit-readiness. Re-check git log/status per this file's own "How to resume" section before treating anything above as still current.



## Addendum 2 (added by the same separate Claude Code instance, reached over SSH, 2026-08-06 ~00:15 CDT)

Cross-machine test suite comparison + a real, evidenced install.py fix for Aider. No repo changes made here - this is informational, for the primary session's judgment on whether to act on the install.py finding below.

### Cross-machine results

| Machine | Platform | Result |
|---|---|---|
| Yoga (this machine) | Linux x86_64, all 6 CLIs installed | 450/450 `test_roundtable` + `test_install` |
| Raspberry Pi 5 | Linux ARM64 (Debian 13/trixie) | 449/450 initially - the 1 failure (`test_plain_mode_reports_agent_failure_without_traceback`) is fully explained by 3 missing real CLI binaries (agy, aider, grok), not a code bug. After fixing Aider (see below), only Grok/Antigravity remain missing, both for documented/expected reasons. |
| Optiplex | Windows x86_64 (real hardware, not the stuck QEMU VM test) | 30/35 on `test_install` - 5 failures all cluster on symlink-related assertions (`is_symlink()`, `"would link"`, `"already linked"`, `is_file()`) vs. actual behavior of writing a `.cmd` launcher on Windows. Likely a test-suite gap (tests not updated for the new Windows-launcher path from the recent "Support Windows" commit), possibly also worth checking whether install.py should still attempt a real symlink when Developer Mode is enabled rather than always using the launcher. Not fixed here - flagging for review. |

The Yoga's own Windows-in-QEMU installer test (mentioned in Addendum 1) was killed after the VM appeared to hang (disk image growth flatlined to ~1.7MB/hour despite ~70% sustained CPU, and a `screendump` via the QEMU monitor socket showed a solid black screen). The real-hardware Optiplex result above supersedes it as the Windows data point.

### Aider install failure on Pi - root cause and fix (verified working)

`install.py`'s current Aider command (`pipx install aider-chat`) failed on the Pi with a `numpy==1.24.3` build error. Root-caused as follows, not an ARM64-specific issue:

- `aider-chat`'s true latest PyPI release is **0.86.2** (confirmed via the PyPI JSON API directly - a naive `pip index versions aider-chat` was misleading here because this Pi's pip.conf has `piwheels.org` as an extra-index-url, which appears to distort version resolution for this package).
- 0.86.2's actual `requires_dist` hard-pins **`numpy==1.26.4`** (exact, not a range) - confirmed via PyPI JSON metadata.
- numpy 1.26.4 predates Python 3.13 and has **no wheel for cp313 on any platform or architecture** (checked its full file listing - wheels stop at cp312). So `pip`/`pipx install aider-chat` is broken for *any* Python 3.13 environment, not just this Pi - would hit the same wall on the Optiplex or any other machine running Python 3.13.
- **Fix, verified working on the Pi just now**: aider's own currently-recommended install method sidesteps this entirely -
  ```
  pipx install aider-install
  aider-install
  ```
  This uses `uv`'s resolver instead of pip's, which correctly substitutes a compatible scipy/numpy stack rather than choking on aider-chat's exact pin. Result: `aider --version` → `aider 0.86.2`, fully working.

**Suggested (not applied) fix for install.py**: replace the Aider `pipx install aider-chat` command with the two-step `aider-install` bootstrap above. This is a real install.py code change and should go through the primary session's and user's normal review, not something applied by this SSH instance.

### Other Pi environment changes made (informational, not repo changes)

- Installed nodejs 22.x (via NodeSource; Debian's stock apt package was v20, too old for Claude Code/Qwen's `engine >=22` requirement) and configured a user-writable npm global prefix (`~/.npm-global`) to avoid `EACCES` on `npm install -g`.
- Installed `pipx` via apt.
- Result: Codex, Claude, and Qwen CLIs now genuinely installed and verified (`--version` runs clean) on the Pi, in addition to Aider above.
- Grok remains uninstallable (vendor ships x86_64-only Linux binaries, no ARM64 build exists - a hard platform wall, not fixable here). Antigravity remains manual-install-only, as already documented.



## Addendum 3 (added by the same SSH instance, 2026-08-06 ~00:30 CDT)

The user found updated vendor install pages for the two remaining CLIs previously documented as unfixable. Both assumptions in install.py/roundtable.py appear outdated:

### Antigravity (agy)

Official page (antigravity.google/download) confirms a scriptable installer exists, contradicting the "no publicly documented package-manager install command" assumption:
```
curl -fsSL https://antigravity.google/cli/install.sh | bash   # macOS/Linux
irm https://antigravity.google/cli/install.ps1 | iex           # Windows PowerShell
```
Page explicitly states **Linux ARM64 is supported** ("Download for ARM64" tarball), min requirements glibc >=2.28. **Not executed/verified** - piping a remote script to bash was blocked by this session's safety classifier (reasonably - unvetted remote script execution). Needs the user or the primary session to actually run and confirm it.

### Grok

Official docs (via search, x.ai/build itself returned 403 to WebFetch) confirm Grok Build now supports both x86_64 and aarch64 on Linux via:
```
curl -fsSL https://x.ai/cli/install.sh | bash
```
with an npm alternative: `npm install -g @xai-official/grok`. This directly contradicts roundtable's current code comment ("this vendor's Linux build has been observed shipping as an x86_64-only binary; unconfirmed for other architectures").

**Tested on the Pi just now**: `npm install -g @xai-official/grok` succeeded cleanly (3 packages added). Running `grok --version` to verify it actually executes was blocked by this session's safety classifier (first execution of a freshly-installed, unvetted beta binary) - so **install succeeded but runtime behavior is not yet confirmed**.

Note: Grok Build is described as "early beta" with access "tied to a specific xAI subscription" - worth confirming account/auth requirements before assuming a clean `--version` run will work even once someone executes it.

### Net effect if both pan out

If Antigravity's installer and Grok's npm package both check out, roundtable/install.py's architecture-support assumptions for both agents are stale and could be updated - potentially making all 6 CLIs installable on Linux ARM64 (combined with the Aider fix in Addendum 2), leaving no hard platform walls on this Pi at all. Someone should verify `grok --version` and the antigravity installer directly before updating install.py's messaging/logic, though.



---

## Addendum 4 (from Windows desktop, cross-machine test coordinator session, 2026-08-06)

Closing the loop on Addendum 3: both previously-unverified installs are now confirmed working on the Pi.

- **Grok**: confirmed running. `~/.npm-global/bin/grok --version` → `grok 0.2.118 (1e1687c1cf)`. (Note: not on PATH in non-interactive SSH sessions since `.bashrc` isn't sourced — use the full path or an interactive/login shell.)
- **Antigravity (agy)**: installer script downloaded to `/tmp/agy_install.sh`, manually reviewed in full (official Google Cloud Run-hosted binary distribution, SHA512 checksum verified against a signed manifest, no obfuscation), then executed. `~/.local/bin/agy --version` → `1.1.10`.

**Net effect**: all 6 agent CLIs (Codex, Claude, Qwen, Aider, Grok, Antigravity) are now installed and verified running on the Pi (aarch64/Linux). Combined with the Aider root-cause fix in Addendum 2, there are no remaining hard platform walls for this Pi. install.py's current messaging/architecture-support assumptions for Grok and Antigravity are stale and worth revisiting whenever this session picks the code back up (not changed here — only observed/verified from the outside, per this repo's own sign-off rules for source changes).

Also noticed this session is mid-development on a new "chat mode" feature (role hints + prompt variants) in `roundtable.py`/`test_roundtable.py` — left untouched. A cross-platform test-suite comparison (Yoga vs. Pi, same working-tree snapshot including this WIP) follows in the next handoff update once that run completes.


---

## Addendum 5 (from Windows desktop, cross-machine test coordinator session, 2026-08-06)

Ran the full test suite (`test_roundtable.py` + `test_install.py`, 450 tests) on this exact working-tree snapshot — including the in-progress "chat mode" feature you're mid-editing (uncommitted `roundtable.py`/`test_roundtable.py`/`README.md` changes) — on two machines:

| Machine | Arch | Result |
|---|---|---|
| Yoga (this machine) | x86_64 | 450/450 passing |
| Raspberry Pi 5 ("octopi") | aarch64 | 450/450 passing |

Both clean, no platform-specific failures. The only wrinkle on both machines was environmental, not a code bug: a non-interactive/non-login SSH session's `$PATH` doesn't pick up `~/.npm-global/bin` (only sourced from `.bashrc`, which non-login shells skip) — this made `verify_clis` report agent CLIs as "missing" until PATH was set explicitly for the test run. Nothing to fix in the repo; just a note in case it trips up CI or another remote session later.

Snapshot was taken via `tar --exclude='.git' --exclude='__pycache__' --exclude='.roundtable'` from `~/roundtable` on the Yoga and extracted fresh into the Pi's `~/roundtable-test`, replacing the earlier stale copy from before the Aider/Grok/Antigravity fixes.


---

## Addendum 6 (from Windows desktop, cross-machine test coordinator session, 2026-08-06)

Made actual code changes to install.py/test_install.py/README.md (not just observations this time — full test suite passes on all three machines below, confirmed before writing this). Left uncommitted so the primary session can review alongside the in-progress chat-mode work; nothing here touches `roundtable.py` or `test_roundtable.py`.

**install.py**:
- `CLI_INSTALLERS["grok"]`: `None` → `["npm", "install", "-g", "@xai-official/grok"]`. Verified end-to-end on the Pi (aarch64): install + `grok --version` both work.
- `CLI_INSTALLER_REQUIRES["grok"] = "npm"` added.
- `NPM_ARCH_SUPPORT["grok"]`: added, all six OS/arch combos (queried `@xai-official/grok`'s own `optionalDependencies`/`os`/`cpu` fields on the npm registry — it ships dedicated native packages for linux/darwin/win32 × x64/arm64, same pattern as codex/claude, no native-dep caveat like Qwen's).
- `NO_INSTALLER_ARCH_CAVEATS`: emptied (was grok-only; no longer applicable now grok has a real installer).
- Module docstring, `install_cli` docstring: updated to say "agy" only (was "agy/grok") wherever it described CLIs with no automated installer.
- **Antigravity (`agy`) deliberately left as `None`, not wired to the official installer.** Its installer is `curl -fsSL https://antigravity.google/cli/install.sh | bash` (or the PowerShell equivalent) — a remote-script execution, not a package-registry install like every other entry in `CLI_INSTALLERS`. That's a different trust/reversibility profile for something that would run silently by default for every future `install.py` user, so I left a comment explaining the gap and did not make that call myself. If you want it wired in, it's a one-line addition (`"agy": ["bash", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"]` + a `"bash"` entry in `CLI_INSTALLER_REQUIRES`), your call.

**test_install.py**:
- Replaced `test_install_cli_reports_no_automated_installer_for_agy_and_grok` with an agy-only version, and `test_install_cli_grok_caveat_only_shown_off_x86_64` (now-obsolete, caveat dict is empty) with `test_install_cli_grok_installs_via_npm_with_full_arch_support`.
- Also fixed the 5 tests that were failing on real Windows (found while testing on the Optiplex, unrelated to the grok/agy work): `test_install_roundtable_symlink_creates_executable_link`, `..._is_idempotent`, `..._dry_run_touches_nothing`, `..._falls_back_to_copy_when_symlinks_unsupported`, and `test_main_skip_clis_only_links_roundtable`. None of these were a real install.py bug — they called `install_roundtable_symlink`/`main` without pinning `current`/`current_platform` to POSIX, so on an actual Windows box `current_platform()` correctly took the `.cmd`-launcher branch and the tests' `target.is_symlink()` assertions failed. Pinned each to `current=("linux", "x86_64")` (or mocked `current_platform` for the `main()`-level test), matching the pattern the existing `test_install_roundtable_writes_windows_cmd_launcher` test already used for the Windows case.

**README.md**: updated the installer section to list Grok's new `npm install -g @xai-official/grok` command and narrowed the "no installer" callout to just Antigravity.

**Test results after these changes**, full suite (`test_roundtable` + `test_install`, 450 tests):

| Machine | Arch | Before | After |
|---|---|---|---|
| Yoga | x86_64 | 450/450 | 450/450 |
| Pi | aarch64 | 450/450 | 450/450 |
| Optiplex | x86_64 (Windows) | 30/35 | 35/35 |


---

## Addendum 7 (from Windows desktop, cross-machine test coordinator session, 2026-08-06)

First time `test_roundtable.py` (not just `test_install.py`) has been run on the Optiplex (Windows). Full suite (`test_roundtable` + `test_install`, 450 tests):

| Machine | Arch | Result |
|---|---|---|
| Yoga | x86_64 (Linux) | 450/450 |
| Pi | aarch64 (Linux) | 450/450 |
| Optiplex | x86_64 (Windows) | 439/450 (9 errors, 2 fails) |

**Fixed** (test-suite gap, same category as the existing `@unittest.skipUnless(os.name == "posix", ...)` on its sibling): `test_agent_cancellation_does_not_log_it_as_an_unexpected_error` unconditionally did `mock.patch.object(roundtable.os, "killpg")`, which fails on Windows because `os.killpg` doesn't exist there at all to patch. Confirmed via `roundtable.py:1187-1190` that real code already branches correctly (`os.killpg` on POSIX, `proc.send_signal(sig)` on Windows) — the mock was just copy-pasted scaffolding from the POSIX sibling test and isn't actually exercised on Windows either way. Fix: added `create=True` to the patch so it works whether or not the attribute exists, instead of narrowing coverage with another skip. Verified passing on all three machines now (450/450 across the board on the two Linux boxes; this specific test now passes on Windows too).

**Left unfixed — did not touch `roundtable.py`** (another Claude session has substantial uncommitted work there — the chat-mode feature — and patching around it risked a collision):

1. **Real bug** (6 of the 9 remaining errors, all the same root cause): `main()`'s final summary `print(summary, flush=True)` (`roundtable.py:5501`) raises `UnicodeEncodeError: 'charmap' codec can't encode character '⚠'` whenever the printed summary contains a ⚠ warning character. Stock Windows consoles default to cp1252, which has no mapping for U+26A0. This is a real crash any Windows user could hit outside a UTF-8 console (`chcp 65001`), not just a test artifact — affects `test_elevated_flag_resolves_per_agent_and_via_all`, `test_reassign_idle_flag_reaches_conduct`, `test_task_status_check_flag_reaches_conduct`, `test_plain_mode_cancel_with_no_turns_yet_skips_paths`, `test_plain_mode_failure_prints_checkpoint_paths_when_a_turn_was_saved`, `test_plain_mode_failure_prints_log_path_with_no_turns_saved`. (The `PermissionError` on temp-dir cleanup seen in each traceback is a secondary symptom — the log file handle is still open when the crash interrupts cleanup — not a separate bug.) Suggest either encoding the print with `errors="replace"`/normalizing to ASCII-safe fallback characters, or setting stdout to UTF-8 at startup on Windows.

2. **1 more error, `test_plain_mode_reports_agent_failure_without_traceback`**: not the encoding bug — this one's just the "missing CLI" PATH artifact seen elsewhere in this session (non-interactive/non-login shell PATH), not a real issue.

3. **Environment gap, not a code bug**: `test_parse_reset_time_honors_named_timezone` and `test_wait_for_agent_availability_formats_reset_time_portably` both fail with `ModuleNotFoundError: No module named 'tzdata'` — Windows' `zoneinfo` module has no bundled/system tz database (unlike Linux, which usually has one at `/usr/share/zoneinfo`) and needs the `tzdata` PyPI package to resolve *any* named zone, including `UTC`. Since adding `tzdata` would be a new runtime dependency, per this repo's own CLAUDE.md that needs your explicit sign-off — flagged, not added. Worth deciding whether real Windows users hitting the reset-time-parsing feature need this documented as a prerequisite, or whether it's worth a graceful fallback in the parsing code itself.

4. **Likely a test-only limitation, not a real functional gap**: `test_create_self_test_sandbox_raises_when_output_not_writable` and `test_create_self_test_sandbox_records_per_file_errors` both fail (`OSError not raised` / empty error list). These almost certainly simulate "unwritable" via POSIX-style `chmod` bits, which Windows doesn't enforce the same way for the owning user (real ACL-denied access would still raise correctly) — so this is probably about the test's simulation technique, not `create_self_test_sandbox`'s actual error handling. Didn't dig into the exact test body to confirm since it's in the same file as the WIP work; worth a quick look whenever that session is free.

Recommend prioritizing #1 (the UnicodeEncodeError) — it's the only one of these that's a plain, real, user-facing crash rather than an environment quirk or test artifact.


---

## Addendum 8 (from Windows desktop, cross-machine test coordinator session, 2026-08-06)

Fixes for the Windows failures found in Addendum 7. All applied directly to `roundtable.py`/`test_roundtable.py` (checked the live file hadn't changed since I last pulled it before each push, to avoid colliding with the chat-mode WIP still in progress in this same session — no conflicts). Full suite re-verified passing on Yoga (450/450) and Pi (450/450) after each change, not just Windows.

**Fixed in `roundtable.py`** (real bug): the plain/non-interactive-mode branch of `main()` (~line 5494) now does
```python
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass
```
right before its first `print()`. Root cause: `config_summary()` prepends a `⚠` (U+26A0) character in `--mock` mode, and stock Windows consoles default to the cp1252 codepage, which can't encode it — crashing the whole run with `UnicodeEncodeError` rather than printing the warning. This is a real, user-facing bug (not a test artifact): any Windows user hitting a warning-bearing summary outside a UTF-8 console (`chcp 65001`) would hit this. `errors="replace"` degrades gracefully to a replacement glyph instead of crashing, matching the `errors="replace"` idiom already used elsewhere in this file for I/O.

**Correcting my own Addendum 7**: I mischaracterized the `tzdata`/`ZoneInfo` failures as a `roundtable.py` dependency gap. On closer look, `parse_reset_time()` (roundtable.py:3896-3903) already catches `ZoneInfoNotFoundError` and safely returns `None` — production code was never broken. The two failing tests (`test_parse_reset_time_honors_named_timezone`, `test_wait_for_agent_availability_formats_reset_time_portably`) construct their own `now` fixture directly via `ZoneInfo(...)`, unguarded, before ever calling into the code under test — so the crash was in test setup, not `roundtable.py`. Since Windows genuinely has no bundled IANA tz database (needs the third-party `tzdata` package to resolve *any* named zone, even `"UTC"`), and there's no way to exercise "does a named zone resolve correctly" without one, I added a module-level `_HAS_TZDATA` probe and `@unittest.skipUnless(_HAS_TZDATA, ...)` on both tests — same shape as the existing POSIX-only skips, no new runtime dependency added anywhere.

**Also fixed in `test_roundtable.py`**: `test_create_self_test_sandbox_raises_when_output_not_writable` and `..._records_per_file_errors` simulate "unreadable"/"unwritable" via `os.chmod(path, 0)` / `os.chmod(path, 0o555)`. Windows' `os.chmod` only toggles the read-only *attribute* bit and doesn't stop the owning process from reading/writing the way POSIX mode bits do (Windows uses ACLs instead) — so the simulated permission denial silently didn't happen, not because `create_self_test_sandbox`'s own error handling is missing anything. Added `@unittest.skipUnless(os.name == "posix", ...)`, matching the existing pattern.

**Result on the Optiplex**: 444 passed, 5 skipped (all for the reasons above, all legitimate), 1 error — and that last one isn't a code or test issue either: `test_plain_mode_reports_agent_failure_without_traceback` doesn't mock CLI verification, so it needs all 6 real agent CLIs on PATH, and this particular Windows box was only ever set up for install.py/test_install.py testing (only `claude` is actually installed here; no Node/npm/pipx at all). Didn't install a full CLI roster + Node.js/pipx just to close this out — that's a bigger system change than "fix the Windows errors" calls for, and this box's role was always install-mechanics testing, not being a fully-populated agent runner like the Pi. Not a `roundtable.py` bug.

**Final tally, full suite (`test_roundtable` + `test_install`, 450 tests)**:

| Machine | Arch | Result |
|---|---|---|
| Yoga | x86_64 (Linux) | 450/450 |
| Pi | aarch64 (Linux) | 450/450 |
| Optiplex | x86_64 (Windows) | 444 passed, 5 skipped (legitimate), 1 error (this box's CLI roster is incomplete, not a bug) |


---

## Addendum 9 (from Windows desktop, cross-machine test coordinator session, 2026-08-06)

Closed out the Optiplex completely. Installed Node.js 22.14.0 (msiexec silent install) and pipx (via `python -m pip install --user pipx && python -m pipx ensurepath`), then ran the actual `install.py` for real:

- **Codex, Grok, Qwen**: all installed via npm cleanly once the WinError 2 fix (Addendum 8) resolved the launcher-path issue.
- **Aider**: installed via the new `aider-install` two-step path (Addendum 8) — `uv` pulled its own isolated Python 3.12 interpreter and resolved `numpy==1.26.4` without issue, sidestepping the Python 3.13 incompatibility entirely. `aider --version` → `aider 0.86.2`.
- **Antigravity**: downloaded and reviewed `install.ps1` (same official Google Cloud Run infra + SHA512 checksum verification as the Linux script, no obfuscation), ran it after you granted a permission rule for the specific command. Installed to `%LOCALAPPDATA%\agy\bin\agy.exe`, added to User PATH registry. `agy --version` → `1.1.10`, same version as the Pi.

All 6 CLIs (`codex 0.146.1`, `claude 2.1.222`, `agy 1.1.10`, `aider 0.86.2`, `grok 0.2.118`, `qwen 0.21.6`) now installed and verified running on the Optiplex.

**Final full-suite result (`test_roundtable` + `test_install`, 454 tests)**:

| Machine | Arch | Result |
|---|---|---|
| Yoga | x86_64 (Linux) | 454/454 |
| Pi | aarch64 (Linux) | 454/454 |
| Optiplex | x86_64 (Windows) | 449 passed, 5 skipped (legitimate platform limits), 0 errors, 0 failures |

The 5 skips on Windows are all accounted for and correct, not gaps: POSIX-only process-group cancellation, POSIX-only chmod-based permission simulation (×2), and no IANA tz database without the third-party `tzdata` package (×2) — none of these reflect a real `roundtable.py` limitation, per the analysis in Addendum 8.

This closes out the cross-platform installer/test-suite work from this session: all three machines are clean, all three run all 6 agent CLIs (well — Yoga and Pi always did; Optiplex now does too), and every fix along the way (Grok/Antigravity install commands, the Windows launcher-path bug, the Aider numpy pin, the killpg/tzdata/chmod test gaps) is in the actual `install.py`/`test_install.py`/`roundtable.py`/`test_roundtable.py`/`README.md` source, verified passing everywhere, and left uncommitted for you to review alongside the in-progress chat-mode work.

---

## Addendum 10 (from Windows desktop, cross-machine test coordinator session, 2026-08-06)

install.py now installs everything needed to actually run the full suite, not just the 6 agent CLIs: added `ensure_windows_dependencies()` (Addendum/PENDING_REVIEW.md has the sign-off) which auto-installs `windows-curses` and `tzdata` via `python -m pip` on Windows. Both were previously manual/warned-about-only prerequisites. Optiplex went from 449 passed + 5 skipped to 455 passed + 3 skipped (0 errors) as a result -- the remaining 3 skips are genuinely POSIX-only and unfixable by installing anything (process-group signaling, chmod-based permission simulation). Committed as `1342dff`.

Session closing out here. Full commit history from this session: `9f0e651` (pre-existing) -> `a981a89` (Grok/Antigravity installers + Windows subprocess/Aider fixes) -> `614b935` (chat mode + Windows runtime/test fixes) -> `1342dff` (windows-curses/tzdata). All three machines (Yoga, Pi, Optiplex) verified clean or as clean as platform allows.


---

## Addendum 11 (from Windows desktop, cross-machine test coordinator session, 2026-08-07) -- ROLE CHANGE, READ THIS FIRST

**The repo's primary/canonical location has moved to the Raspberry Pi**, per Liam's explicit direction. Summary for whoever (human or Claude session) reads this next:

- Pi (`~/roundtable` there) is now a proper `git clone` of `github.com/Puppysnuppy7/roundtable` (new remote -- didn't exist when earlier addenda in this file were written), with its own git identity and `gh`-authenticated push access.
- This Yoga machine's `~/roundtable` is untouched and fully in sync (`git pull` cleanly to `origin/master`), but is meant to become a testing device going forward, not where new primary work starts.
- I've also appended a note to this session's own persistent memory (`~/.claude/projects/-home-user/memory/project_roundtable.md`) with the same information, since a future session here might not open this file immediately.
- **This has not been confirmed with the live Yoga Claude session directly** -- there's no channel for an SSH-guest instance to message a live interactive session, so this is relayed via the two durable artifacts (this file + that memory note) instead. Liam may still want to tell that session in person or update `~/roundtable/CLAUDE.md`'s own primary-session designation, which currently doesn't reflect this change.

Everything else from this round of work (Addenda 1-10): Grok/Antigravity real installers, several genuine Windows bugs found by actually running the suite there (UnicodeEncodeError crash, subprocess WinError 2, stale-PATH via registry refresh in both install.py and roundtable.py's own main()), Aider's fix (aider-install, not pipx install aider-chat), and a new `roundtable --update` flag. Final commit on all three machines: `4c2d567`.


## Addendum 12 — Codex continuation, 2026-09-14 UTC

Resumed the latest Claude transcript (`61324e16-0742-4daf-b676-d422c35c36a0.jsonl`),
whose final authorized task was "Wire up" Muse and Kimi. Branch: `agents-roster`, based
on `ff78688`. Completed the inherited unfinished eight-agent integration in roundtable.py,
test_roundtable.py, install.py, and README.md.

- Muse and Kimi now participate in roster selection, commands, authentication setup,
  model overrides, restart arguments, synthesis, roles, and TUI panels/shortcuts/colors.
- Fixed the grid to use two balanced rows (2×4 for eight), preserving row height.
- Kimi respects configured model defaults. With KIMI_MODEL_API_KEY it supplies a missing
  KIMI_MODEL_NAME (explicit model or kimi-for-coding), without changing the parent environment.
- Kimi targets the newer Kimi Code `-p` interface; the older Python kimi-cli differs.
  Its headless mode uses automatic approval. Muse retains its sandbox unless elevated.
- Installer detects both but leaves installation to vendor instructions; neither binary is
  currently on PATH. No authenticated provider run was possible or claimed.
- Existing default synthesis budget remains six; `--synthesis-passes 8` includes all eight.
- Validation: 699/699 unittest tests passed; py_compile and git diff --check passed;
  terminating mock run with all eight and eight synthesis passes passed; installer dry run passed.
- Full suite log: /tmp/roundtable-verified-tests.log.
  Mock output: /tmp/roundtable-eight-smoke.log.

Next practical step: install/authenticate Muse and Kimi, then run a small real task with
`--agents muse,kimi` to verify each vendor's current CLI, permissions, and credentials.
The homelab handoff remains /home/user/handoff/HANDOFF.md; no infrastructure changed here.

## Addendum 13 — Claude, primary session, 2026-09-16

Resumed after a weekly-limit gap and found Addendum 12's work already done and pushed. Verified it
independently rather than taking the addendum's word:

- `python3 -m unittest test_roundtable test_install` → **699/699 OK** on yoga (85s), my own run.
- Working tree is identical to `fc3f944`, which is already on `origin/agents-roster`. Nothing of
  mine needed pushing; the earlier "push" request was satisfied by Codex, not by me.
- I briefly committed `CLAUDE.md` + `PENDING_REVIEW.md` by accident (`git add -A` swept up two
  long-untracked files under an inaccurate "tests not yet green" message). Undone with
  `git reset fc3f944` (mixed, unpushed commit, no history rewritten). Both are untracked again.

**Three decisions left for Liam — none block anything, and a green suite does not cover them:**

1. `--synthesis-passes` still defaults to **6** while the roster is now **8**, and the value is
   clamped by `min(passes, len(agents))`. At six agents the default meant *everyone*; at eight it
   silently leaves two agents out of the final relay, chosen by hash rotation. Codex documented
   this deliberately (the `choices` range does derive from `AGENT_NAMES`, so `-8` is accepted).
   Keep 6 as a cost default, or raise it to the roster size — a real call, not an oversight.
2. Resuming a **pre-`--agents` session** now yields eight agents, not six: `roundtable.py:2197`
   reads `data.get("roster", list(AGENT_NAMES))`, and the comment above it still says "absent
   means all six." Behavior is arguably right (resume picks up the current roster); the comment is
   now wrong either way.
3. `CLAUDE.md` and `PENDING_REVIEW.md` have **never been committed** (untracked since Aug 5 / Aug
   17, and not gitignored). So the Pi and Optiplex clones have never had the governance doc that
   Addendum 11 instructs incoming agents to read first. Worth committing as its own change.

Minor: several "all six" comments are now stale (`roundtable.py` 3301, 5020, 6292). The ones at
546, 5244, 5740 describe history correctly and should stay. `install.py:97` is about npm platform
combinations, not agents.

**Still the real gap, unchanged from Addendum 12:** neither `muse` nor `kimi` is on PATH here, so
no authenticated provider run has happened and none is claimed. Next step is user-side — install
and authenticate both, then a small real task with `--agents muse,kimi`.

Note for whoever picks this up: a `codex app-server --remote-control` daemon is live on yoga, so
this repo can have a concurrent writer. Read state from `git`, not from recollection of your edits.

### Addendum 13a — resolution of item 1, same session

Item 1 of the three above is **decided and committed** (`a05d792`, pushed to `agents-roster`):
`--synthesis-passes` **stays at 6**. Raising it to 8 would have been a silent per-run cost increase,
and Liam has repeatedly optimised roundtable runs for credit spend. The load-bearing fact -- which
the old help text did not say -- is that agents past the pass count **still contribute their work**,
because the drafter reads the whole transcript; they only miss a relay turn of their own. The help
and README now say exactly that, and show `default: 6 of 8`.

Same commit corrected the stale "six" comments (panel labels, early-complete synthesis note, `--self`
restart roster) and rewrote the pre-`--agents` resume comment at roundtable.py:2195 to state the real
behaviour: an absent roster resolves to today's `AGENT_NAMES`, so resuming an old session seats
agents added since it was saved. Suite re-run after the edits: **699/699 OK**.

Items 2 and 3 are folded in or still open: the resume comment (2) is fixed; **tracking `CLAUDE.md`
and `PENDING_REVIEW.md` (3) is still untouched and still needs Liam's word** -- both remain
untracked, so the Pi and Optiplex clones still lack the governance doc Addendum 11 points at.
Merging `agents-roster` (15 commits ahead of `master`) into `master` is likewise **not** done and
was not authorised.

> **Superseded the same day -- see Addendum 13b.** Liam authorised both ("merge and track"): the
> governance docs are tracked and `master` is merged. Do not act on the two "still open" items
> above.

### Addendum 13b — merged to master, and a real Windows bug it exposed, 2026-09-16

Liam authorised both open items ("merge and track"), so:

- `CLAUDE.md` + `PENDING_REVIEW.md` are **tracked** (`8fd1524`). The Pi and Optiplex each had their
  own *untracked* copies, which blocks a plain `git pull`; all three were byte-identical
  (`dd3716a0…` / `2bf5c7f0…`, verified by hash before touching anything), so replacing them lost
  nothing. **All three clones are synced.** The Optiplex clone is `C:\Users\User\roundtable` --
  that box's Windows user is `User`, not `zen22`.
- `agents-roster` **merged into `master`** as a fast-forward, matching this repo's linear history
  (it has zero merge commits; a `--no-ff` would have introduced the first).

**Pulling master to the Optiplex immediately turned up a real bug, and it was mine.** Those clones
had not been pulled since before `--detach` existed, so the whole `--detach`/`--status`/`--on`
feature had *never* had its tests run on Windows. Three failed there. One was the test's fault
(`/bin/sh` doesn't exist on Windows -- now skipped, same convention as the other POSIX-only skips).
The other two shared a genuine product defect:

> `_process_is_running` used `os.kill(pid, 0)`. On Windows that is **not** a signal check, it is an
> `OpenProcess` call, and Windows keeps an exited process's pid openable for as long as any handle
> to it survives. So it answered **True for processes that had already finished** -- meaning
> `--status` on Windows reported dead runs as `running`, the one thing `describe_run_state`'s own
> docstring says it must never do.

Confirmed by direct experiment on the Optiplex rather than from the docs: an exited child and a live
child **both** returned True from `os.kill`, while `GetExitCodeProcess` separated them cleanly
(`0` vs `STILL_ACTIVE`/259). Closing the handle first did *not* help. Fixed in `443af77` with a
Windows branch that asks for the exit code; access-denied is still treated as alive, matching the
POSIX `PermissionError` case.

I also disproved a scarier theory before acting on it: `os.kill(pid, 0)` does **not** terminate the
target on Python 3.13 Windows (signal 0 is special-cased). Worth knowing, because the old docs
warning that any signal maps to `TerminateProcess` would have implied `--status` killed live runs.

**Tri-machine verification, all at `443af77`:**

| Machine | Arch | Result |
|---|---|---|
| yoga | x86_64 Linux | 699/699 OK |
| pi | aarch64 Linux | 699/699 OK |
| optiplex | Windows | 699 OK, 7 skipped |

Lesson worth keeping: the Pi and Optiplex clones sitting many commits behind meant two platforms
had no coverage at all while the branch looked green. Pull the other clones *before* trusting a
green suite as cross-platform.

**Skip audit (Optiplex, verbose run).** All 7 are real platform limits, none hide coverage:
process-group cancellation (POSIX-specific), symlink semantics, and four permission tests that
Windows cannot express with mode bits (`os.chmod(0)` / `os.chmod(0o555)` don't block the owning
process there -- ACLs would), plus the `/bin/sh` remote-shell skip added above. Addendum 10's
figure of 3 skips predates `--detach`, the detached console, and stored-key locking; the four extra
skips arrived with those features and are all legitimate.


---

## Recovered from the Optiplex copy -- Windows state as of 2026-08-11

Preserved verbatim while consolidating the handoffs (2026-09-16). This section existed **only** in
`C:\Users\User\roundtable\HANDOFF.md` and was absent from the copy every later addendum was
written against, because the file was gitignored and each machine kept its own. It was authored on
the Windows desktop (it refers to `C:\Users\zen22\roundtable`), and the 2026-08-05 SSH addendum it
sat beside was already present here, so this is the only part that was actually missing.

Version numbers and test counts below are **frozen at 2026-08-11** and are long superseded -- the
suite is now 699 tests and the roster is eight agents. The durable parts are the findings: the Qwen
key's trailing `0x16`, the international DashScope base URL, and the WinGet `0x8a15000f` repair.

**Last updated:** 2026-08-11 on the current Windows machine (`C:\Users\zen22\roundtable`). The
July/Linux notes below remain useful history, but this section is the current operational state.

## 2026-08-11 current Windows state

- The local working tree is intentionally dirty and **not committed or pushed**. Preserve and
  review all existing changes before staging. Current work spans `README.md`, `install.py`,
  `roundtable.py`, `test_install.py`, and `test_roundtable.py`; this `HANDOFF.md` is ignored session
  scaffolding. The Optiplex copies of `README.md`, `install.py`, `test_install.py`, and the previous
  handoff were imported before editing so its newer user-owned work was not overwritten.
- Roundtable auth/preflight fixes now in the local source:
  - explicitly stored keys override stale inherited environment values;
  - hidden-input key paste cleanup removes invisible edge control characters (the live Qwen key
    had a trailing `0x16` that made Node reject the Authorization header);
  - Aider's harmless Windows prompt-toolkit warning is filtered, and preflight accepts Aider's
    standalone `OK` among its normal version/model/token banners only after checking the full
    response for authentication failures;
  - the live Qwen credential is valid only against international DashScope, so the Optiplex stored
    `OPENAI_BASE_URL` is `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`.
- The installer is now a complete installer rather than a launcher plus partial CLI installer:
  - installs/checks Python runtime packages (`windows-curses`, `tzdata`), Git, Node.js/npm, pipx,
    and all six CLIs;
  - Antigravity uses Google's official HTTPS installer downloaded to a temporary file (not a
    remote-script pipe);
  - Windows uses WinGet for Git/Node and repairs the signed Microsoft `source.msix` automatically
    on `0x8a15000f`; macOS/Linux use Homebrew or apt/dnf/yum/zypper/pacman when available;
  - `python install.py --list` is read-only and lists every prerequisite, resolved CLI path, and
    authentication follow-up; normal and `--dry-run` installs print the final checklist too;
  - `--only` limits both installation and the final list; `--skip-clis` retains its launcher-only
    behavior.
- Real Optiplex installation state verified on 2026-08-11: Python 3.13.5, Git
  2.55.0.windows.3, Node 22.14.0/npm 10.9.2, pipx, windows-curses, tzdata, Codex 0.147.0,
  Claude 2.1.227, Antigravity 1.1.12, Aider 0.86.2, Grok 1.0.0, and Qwen 0.21.9 are installed.
- Verification completed:
  - `python -m unittest test_install`: **130 passed**;
  - `python -m unittest test_roundtable`: **462 passed, 4 skipped**;
  - real provider checks: Codex, Aider, Grok, and Qwen ready; Claude authenticated but weekly
    quota-limited until 2026-08-12 09:00 America/Chicago; the latest desktop Roundtable log showed
    Antigravity passing, while an SSH-only logon cannot access/complete its interactive browser
    authentication context.
- Git for Windows was installed directly from the official Git-for-Windows release after WinGet's
  source database failed. App Installer was re-registered and Microsoft's `source.msix` installed;
  WinGet searches work again, though `winget source update winget` may still print `Cancelled` while
  returning success and using the installed source data.
- Treat the apparent mojibake seen through one PowerShell-over-SSH log display as a transport/code
  page presentation issue: later UTF-8 test output rendered the same separators correctly.

### Resume from this machine

1. `cd C:\Users\zen22\roundtable` and run `git status --short`, `git diff --check`, and review the
   complete diff before staging anything.
2. Re-run tests on a Windows Python environment (the Optiplex currently has the verified Python
   setup): `py -3 -m unittest test_install test_roundtable`.
3. Run `py -3 install.py --list` to inventory a target without modifying it; use `--dry-run` before
   a real installation on a new machine.
4. Do not print or copy values from `~/.roundtable/keys.env`; only key names/status belong in logs
   and handoffs.
