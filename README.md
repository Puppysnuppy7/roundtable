# Roundtable

A dependency-free terminal table where the Codex, Claude Code, Antigravity, Aider, Grok Build, and
Qwen Code CLIs solve a problem together as equally as possible — six agents spanning five labs
(OpenAI, Anthropic, Google, xAI, Alibaba) plus a model-agnostic sixth pinned to Mistral's Codestral
by default, so no two agents are running the same underlying model. Each agent is nudged toward one
of six complementary lanes — sandboxed execution and testing, architecture and reasoning, breadth
and stress-testing the others' work, fast narrowly-scoped diffs, skeptical verification, or
integrating the group's approaches into one plan — so six parallel attempts produce complementary
contributions instead of six competing full solutions. Which agent gets which lane rotates by
objective, so no agent is permanently typecast into the same role run after run. Each working prompt
also makes the agent self-aware of the roster: it is told its own display name and CLI, and the
names and CLIs of the other five members, derived from the same `AGENT_NAMES` /
`AGENT_EXECUTABLES` maps that drive preflight and `--list-agents` — so agents do not invent extra
peers or rediscover membership by grepping source. The same identity context follows an agent into
reassignment, final synthesis/refinement, and the dead-code check instead of disappearing when its
role changes. Agents also call
dibs: each is asked to open its turn with `DIBS: <what I'm taking>`, and the next round's prompts
list what the others already claimed, so the round-by-round split of the task stays visible and
agents pick up something new instead of redoing each other's ground. Every agent's prompt also
points at a shared, append-only `AGENT_PROMPTS.md` scratch board in the workspace, so an agent can
leave a note, question, or candidate solution for the others to read on a later turn — treated as
untrusted peer input, never as authoritative as the user's objective. All six develop proposals,
then review the shared transcript in each configured round, coordinating in parallel, in a strict
relay, or a mix of both (`--collab`). The final answer is itself a relay: one agent drafts it, and
the rest refine it in turn, so the result is shaped by all of them instead of authored by whichever
model wrote it first — by default that first drafter rotates by objective so it isn't always the
same model (`--synthesizer`).

## Install

```bash
python3 install.py
```

Links `roundtable.py` onto `PATH` as the `roundtable` command (prefers `~/.local/bin` if it's
already on `PATH`, else the first other writable directory under your home, else creates
`~/.local/bin`; override with `--bin-dir`). On Windows it writes a `roundtable.cmd` launcher so the
command works with `PATHEXT`; elsewhere it uses a symlink with a copy fallback. An unrelated
existing command is left untouched unless you pass `--force`; a previous install created by this
script (symlink, matching `.cmd` shim, or copy of `roundtable.py`) is refreshed in place without
`--force`. It then installs whichever of the six agent CLIs it has a
verified command for: `npm install -g @openai/codex` (Codex), `npm install -g
@anthropic-ai/claude-code` (Claude), `pipx install aider-chat` (Aider), `npm install -g
@xai-official/grok` (Grok), and `npm install -g @qwen-code/qwen-code` (Qwen) — each skipped if
already on `PATH`, and skipped with an explanation if the required package manager (`npm`/`pipx`)
isn't. Antigravity (`agy`) has no package-manager install command this script can verify (its
official installer is a `curl | bash` / `irm | iex` script, not a registry package), so it only
reports whether the CLI is already present rather than guessing an install command; install it
yourself per the vendor's own instructions. `--skip-clis` links only the `roundtable` command;
`--only Codex Aider ...` restricts CLI installation to specific agents; `--dry-run` prints what
would happen without changing anything. Exit status is non-zero if linking fails or any attempted
CLI auto-install fails (or cannot run because its package manager is missing); missing agy is
informational and does not fail the install. All still need authenticating after install — see below.
You can also run `roundtable --install` once the command is already on `PATH` (or
`python3 roundtable.py --install` from the repo). Extra installer flags after `--install`
(`--dry-run`, `--skip-clis`, `--only …`, `--bin-dir`, `--force`) are forwarded to
`install.py`. When `roundtable.py` is launched as a script with `--install`, that path
runs *before* importing `curses`, so stock Windows Python (no stdlib curses) can still
install the launcher the same way `python3 install.py` does.

The installer is platform-aware, not x86_64-only: it detects the OS and CPU architecture and
prints them up front, uses a native command shim on Windows, symlinks on POSIX and falls back to a
plain file copy where symlinks are unavailable, and flags a CLI install it's about to
attempt when that package has no verified prebuilt binary for the detected architecture (checked
against each npm package's own `optionalDependencies`) instead of letting an opaque failure happen
partway through. Concretely: Codex and Claude Code both publish `linux-arm64`/`darwin-arm64`
binaries, so they install cleanly on 64-bit Arm (e.g. a 64-bit Raspberry Pi OS); neither publishes a
32-bit Arm build, so this warns up front on arm32 (installs may still be attempted and then fail).
Qwen Code depends on a native module with no `linux-arm64` prebuild, so an aarch64 Linux install is
flagged as unverified and may need to compile from source. Aider is pure Python and installs the
same way everywhere. On Windows, the script still installs the `roundtable.cmd` launcher and reports
CLI status, but warns that actually running roundtable's GUI needs the third-party `windows-curses`
package, which this installer does not manage. The installer deliberately does not `import
roundtable` (it keeps a small local agent-name manifest, checked by tests against
`AGENT_EXECUTABLES`) so it can still start on a Windows Python that lacks `curses`.

## Authenticate the agents

After installing the CLIs, run the guided setup:

```bash
roundtable --auth-setup
```

It prompts with hidden input for the three API-key paths Roundtable uses by default: Aider's
`MISTRAL_API_KEY`, Grok's optional `XAI_API_KEY`, and Qwen's `OPENAI_API_KEY`. Press Enter to skip
any provider you do not have a key for. Values are stored in `~/.roundtable/keys.env`, locked to
the current user where the platform supports it, and are never printed. Existing environment
variables take priority over stored values.

Login-based agents still use their vendor commands: `codex login`, `claude`, and `agy`; Grok can
use `grok login --device-code` instead of an API key. For direct or scripted administration, use
`roundtable --set-key NAME` (hidden prompt), `roundtable --list-keys` (names only), and
`roundtable --clear-key NAME`. Key commands intentionally require an interactive terminal so a
secret cannot accidentally land in shell history, command arguments, or piped logs.

## Run it

All six CLIs must already be installed and authenticated (`codex`, `claude`, `agy`, `aider`, `grok`,
and `qwen`) — `roundtable --list-agents` reports which are currently found on `PATH`. Aider is
model-agnostic — it defaults to `mistral/codestral-latest` here specifically so it doesn't just
duplicate one of the five lab-native agents; point `--aider-model` at a different provider if you'd
rather it run as something else.

```bash
cd /path/to/project
roundtable "Fix the flaky checkout test and verify the fix"
```

Or launch it without an objective to get an interactive prompt:

```bash
roundtable
```

In a real terminal (not `--plain` or piped), startup shows a quick options screen for the
opt-in flags below before anything else — ↑/↓ (or j/k) moves the highlight, Space/Tab or the
row's number toggles it, and the header shows how many flags are currently on. Dangerous
options (elevated permissions) draw in bold/red when enabled. Enter (or Esc/`q`, or the Continue
button) proceeds with the current checks (skipped on a `--self` restart, which carries forward the
choices already made before the source changed, so it can relaunch unattended). Any matching CLI
flag you already passed sets that toggle's starting state; leaving a toggle untouched keeps
whatever the flag specified (so a specific `--elevated codex` survives even if you don't touch
that option). The interactive objective prompt that follows uses the same panel chrome as the
dashboard, shows a live character/line count, and documents the multiline editing shortcuts.

Useful options:

```text
-r 0-5                 Number of back-and-forth review rounds (default: 1)
-C PATH                Workspace all agents can inspect and edit
--self                 Point the workspace at roundtable's own source instead, so the agents can
                       improve roundtable itself (-C still overrides). Adds a standing note asking
                       agents to read the existing code, stay dependency-free, and run the test
                       suite before finishing.
--codex-model MODEL    Override the configured Codex model
--claude-model MODEL   Override the configured Claude model
--antigravity-model MODEL
                       Override the configured Antigravity model
--aider-model MODEL    Model for Aider, in LiteLLM naming (default: mistral/codestral-latest, kept
                       distinct from the five lab-native agents below)
--grok-model MODEL     Override the configured Grok model
--qwen-model MODEL     Override the configured Qwen model
--reasoning-effort LEVEL
                       auto (default), low, medium, or high. Auto uses low effort for connectivity
                       checks, each CLI's default for working turns, and medium for final synthesis.
                       Explicit levels apply to all turns on Codex, Claude, Antigravity, Aider, and
                       Grok; Qwen has no equivalent option. Auto leaves Aider at its model default
                       because reasoning-effort support varies by provider/model.
--collab MODE          parallel (default), sequential (strict relay through every agent), or mixed
                       (parallel proposal, then rounds alternate relay/parallel)
--synthesizer WHO      codex, claude, antigravity, aider, grok, qwen, or rotate — who drafts the
                       final answer first, before the others refine it in turn (default: rotate by
                       objective)
--synthesis-passes 1-6 Number of sequential final-answer calls: one draft plus up to five
                       refinements (default: 6; use 1 for the lowest latency and model usage)
--balance-load         Give an agent running notably slower than the others a narrower-scoped
                       prompt in later parallel phases, instead of the same full task
--task-status-check    In parallel phases, stop agents still working once one marks the objective
                       fully done, instead of letting them redo the same finished work
--reassign-idle        In parallel phases, the first agent that finishes while ≥2 others are still
                       on their primary turn gets one extra prompt to pick up unclaimed work or help
                       a still-running agent; later finishers stay idle (one concurrent bonus max)
--dead-code-check      Before the final answer is drafted, have one agent search this session's
                       code changes for now-unused functions/branches and remove any it finds.
                       Forced off in --chat mode, which never edits files
--chat                 Plain-text discussion mode: agents discuss/answer the objective as a
                       question instead of editing code. Every turn runs read-only (the same
                       no_edit mode the final-answer relay always uses), role hints and the final
                       answer format are reframed for prose instead of a task-outcome summary, and
                       the Code Monitor panel is replaced with a plain "chat mode" label
--preflight-timeout S  Set the positive timeout in seconds for each startup connectivity check
                       (default: 90, or 25 with --no-extended-preflight)
--skip-preflight       Skip startup connectivity checks
--extended-preflight   Use a 90s preflight timeout (default: on) instead of the tighter 25s;
                       real agents with slow but healthy startup (e.g. sandbox/container setup)
                       have been observed exceeding 25s with nothing actually wrong. Pass
                       --no-extended-preflight for the tighter timeout; either way, ignored if
                       --preflight-timeout is set explicitly
--debug                Enable verbose diagnostic logging of sub-process commands, PIDs, exit codes, and tracebacks
--elevated AGENT       Run codex, claude, antigravity, aider, grok, qwen, or all with that CLI's
                       own permission-bypass flag instead of the sandboxed default (repeatable).
                       Dangerous — see Safety model.
--plain                Stream a non-fullscreen version (also used in pipes)
--output-dir PATH      Where Markdown, JSON, and log files are saved
--resume SESSION.json  Resume a saved session; an objective argument becomes the follow-up
--touch / --no-touch   Override automatic touchscreen detection
--list-agents          Print which of the six known AI CLIs (codex, claude, agy, aider, grok,
                       qwen) are actually installed on this machine, then exit -- no objective,
                       TTY, or preflight required
```

Resume a prior conversation from its JSON transcript. The original objective, turns, workspace,
round count, and save filename are retained; `-C`, `-r`, and `--output-dir` can override those defaults.

```bash
roundtable --resume .roundtable/roundtable-20260718-120000-000000.json \
  "Now add regression tests for the fix"
```

Without follow-up text, fullscreen mode opens the follow-up editor. Plain or piped mode requires the
follow-up as an argument or on standard input.

Before the real task starts, all six CLIs get a quick "reply OK" preflight check (90s timeout each
by default, run concurrently). This exists so a hung or unauthenticated CLI fails fast with a named
reason instead of leaving every panel stuck on "waiting for task" with no explanation. Override the
per-agent timeout with `--preflight-timeout`, or skip the check entirely with `--skip-preflight` if
you already know the CLIs are reachable. A provider session/usage-limit response is treated as
temporary rather than a failed preflight: the run starts, the other agents can work, and the limited
agent waits until it becomes available again.

Each agent's panel shows a "used N% of usage limit" gauge (yellow at 80%, red at 95%+) whenever a
figure is actually known: hitting the limit above pins it at 100% until the agent answers again, and
a CLI's own self-reported percentage (when one prints it) is picked up opportunistically in between.
No CLI is guessed at or estimated — an agent with no such signal simply shows no gauge at all.

Some agents are just slow to answer even a trivial check without anything being wrong — sandboxed
agents in particular can spend most of that time on their own startup overhead (e.g. Antigravity's
sandbox, or Aider/Qwen against certain providers) rather than the model call itself. `--extended-preflight`
is on by default for exactly this reason; pass `--no-extended-preflight` for the tighter 25s if you'd
rather fail fast. Explicit `--preflight-timeout` takes precedence over either.

Launching all six agent subprocesses in the same instant can itself cause a real CPU/memory
contention spike on modest hardware, pushing every agent's response past its timeout — including ones
that are individually fast. To avoid that, agent subprocesses are staggered by a fraction of a second
each rather than all spawned at once; they still all run concurrently overall, and each agent's own
timeout clock only starts once its own call actually begins, so nobody's effective budget shrinks.
The coordinator remains responsive during that launch window: it processes early results immediately,
and with `--task-status-check` it can cancel agents that are still waiting to launch once another
agent has already completed and verified the objective. Load-balancing timings likewise measure only
an agent's actual turn, not its intentional stagger delay. Checkpoint resumes preserve that verified
completion state, so restarting a run does not restore review rounds that were already made redundant.

The fullscreen view provides evenly spaced live Codex, Claude, Antigravity, Aider, Grok, and Qwen
panes, independent working/waiting states, and agent-specific activity tickers next to agent names
(a pulsing circle for Codex, an asterisk pulse for Claude, moving braille dots for Antigravity, a
rotating quadrant for Aider, a dashing line for Grok, and a spinning arc for Qwen). On tall enough
terminals the six panes lay out as a 2×3 grid (roughly double the panel width of a single six-wide
row); shorter terminals keep one row of six so the outcome/monitor band still fits. A one-line roster
under the status line shows each agent's icon and ● working / ↻ retrying / ⏳ rate-limited /
✓ done / ✗ failed / ○ waiting mark at a glance. While a phase is running, the status line also
reports how many agents are currently working and how many have finished that phase (and, when
applicable, how many are mid-retry or waiting on a provider limit, and how many were dropped after a
hard failure). Failures are not counted as done; stalled agents stay inside the working count and
are called out separately (`N retrying` / `N limited`) so a silent backoff is not mistaken for
progress. The count is retained as a sequential relay hands work from one agent to the next and
resets when the operation moves to a new phase. Coordinator signals for a hard phase drop or a
TASK STATUS: complete declaration also appear in the default console filter (key events), not only
in the all-activity firehose.
Each agent pane's subtitle shows that agent's latest `DIBS:` ownership claim from the transcript
(replacing the static lab label while a claim is active), so you can see who owns what without
expanding panels or scanning the shared board. Per-agent usage sparklines show response time,
output size, and activity, while a live work feed
inside each active agent's own pane keeps reported file reads, searches, edits, commands, tests, and
other CLI progress attributed to the agent that emitted them; once the agent finishes, its pane
returns to the completed response. Read/execute/write counters provide an at-a-glance summary;
because CLI output formats differ, they are progress indicators rather than audit totals. The header
always shows turn count and session elapsed time (battery and touch mode append when present rather
than replacing them). A wrapped multiline task composer and a task-outcome box summarizing completed,
failed, and incomplete work, plus a live code monitor showing files changed during the session and a
console panel round out the diagnostics. Expand an individual agent pane to inspect its full
response. Mouse-wheel or two-finger scrolling over any panel (including compact agent panes) reveals
earlier content and marks the offset as `↑N` in that panel's title.
If new agent work or visible console events arrive while a panel is scrolled back, its title also
shows `+N new`; returning to the live tail with `End`, ↓, or a downward wheel gesture clears it.

Once the first task phase completes, phase status lines also show a coarse completion estimate.
It is derived only from wall time observed in the current run and the remaining scheduled work:
a parallel phase counts as one wall-time unit, a sequential six-agent relay as six, and each final
synthesis pass as one. No estimate is shown before there is real timing evidence, and bonus work,
retries, or unusually different later phases can move it; treat it as an operational ETA, not a
deadline. Time spent waiting for a provider usage limit to reset is excluded from later latency
samples, so one blocked agent does not turn the rest of the run's estimates into hour-scale guesses.
Every active phase also begins with `Step N/total`, where a step is one proposal, review, optional
dead-code sweep, or final-answer pass. Unlike a speculative percentage, this is an exact pipeline
position; its total contracts when verified completion skips planned reviews or synthesis passes.
The same step and estimate text appears in plain-mode phase output.

While agents are working, press `i` (or click/tap the "ADD PROMPT [i]" control next to the status
line) to interrupt and open the same follow-up box used between rounds, without stopping the run.
Anything you send there is queued rather than applied immediately; it lands in the transcript, and
takes effect, at the start of the very next phase — proposal, review round, or synthesis — whichever
comes next. An active agent acknowledges the queued prompt for you on the status line ("[Codex]
Acknowledged queued task: …"), rotating through whichever agents are currently working so the
acknowledgment isn't always attributed to the same one. After an answer, a follow-up text box lets
you keep the same roundtable conversation going. Complete transcripts are written to `.roundtable/`.

Every run also opens with a one-line `Config:` summary (collab mode, reasoning effort, synthesis
passes, which optional checks — `--balance-load`, `--task-status-check`, `--reassign-idle`,
`--dead-code-check` — are enabled, and which agents run `--elevated`), so the flags actually
governing this run's behavior are visible up front instead of requiring a trip into the private
`.log` file. `--mock` runs are flagged with a leading `⚠ MOCK` marker so simulated output is never
mistaken for a real CLI response. It appears at the top of the console panel in the GUI and as the
first printed line in plain mode.

Agents can also coordinate through the append-only `AGENT_PROMPTS.md` board in the workspace.
It remains available through every phase, follow-up, and internal `--self` checkpoint restart in
one logical run. On a terminal exit—successful, cancelled, or failed—Roundtable copies the complete
board into the private activity log, but leaves the workspace file as-is. The reset instead happens
when the next fresh run starts (a brand-new objective, or a plain `--resume` of a run that already
exited) — not a `--self` restart continuing the same run, which keeps the board intact. Resetting at
the start rather than only at a clean exit means even a hard kill can't leave stale claims, old test
counts, or obsolete requests to steer an unrelated later run.
Active board entries are included directly in each agent prompt. To keep an accidentally large
board from crowding out the objective and transcript, this prompt excerpt is capped at 12,000
characters and favors the newest content; the complete board remains available in the workspace
and activity log.

The console opens on **key events** — phase changes, completed turns, and errors — instead of a
firehose of every raw line each CLI prints, so the signal-dense view is the default. Press `c` to
cycle it through **all activity** (adds raw per-line ticks), **prompts** (just what was sent to each
agent), and **errors only**; each line is tagged with a small glyph (`▶` phase, `✓` turn, `✗` error,
`➤` prompt, `·` tick) that also carries color, so kind is legible even without color. A running count
by kind sits under the console title. Press `0` or click the console to expand it full-screen for the
complete filtered history, same as the agent and answer panels. Mouse wheel or two-finger scroll works
on the console the same as any other panel, whether it's expanded or still in the compact dashboard
view, and the title shows `↑N` while scrolled back from the latest entry. Pressing `?` or `h`
(or, in touch mode, tapping the `? HELP` button in the header) opens an interactive Help modal
overlay detailing all keyboard shortcuts and controls. On a terminal too short to list every
shortcut, the modal shows as many as fit and a trailing `+N more — resize taller to see all`
line rather than truncating silently. In any expanded panel,
the arrow keys scroll one line; Page Up/Page Down move a screen at a time; and Home/End jump to the
oldest/latest content, so long output remains navigable without a mouse.
For keyboard-only navigation, `Tab` and `Shift-Tab` move a visible focus highlight through the six
agent panels, Task Outcome, Code Monitor, and Console; press `Enter` to expand or collapse the
selected panel. The direct `1`–`6`, `f` (outcome), `m` (code monitor), and `0` (console) shortcuts
remain available.

In a parallel phase, agents finish independently but the transcript only advances once every agent
in the round is done — so a lone slow agent can leave the screen looking stuck. The console and log
report each agent's completion the moment it happens, naming who's still outstanding (e.g. `[Codex]
finished this phase (12.4s) — waiting on Claude`), so a genuinely slow round is distinguishable from
a hang.

`--task-status-check` addresses the related case where one agent finishes the whole objective while
the others are still independently working on the same thing. With it on, each agent is asked to end
its turn with `TASK STATUS: complete` once the objective is fully done and verified. The first agent
to say so stops the other agents still running that phase — `[Claude] stopped early — Codex already
completed the task; will review it next phase instead` — rather than letting them duplicate finished
work. They still get one verification review phase to check and refine what was done; any further
configured review rounds are skipped, and final synthesis is shortened (draft + one refine) so the
run does not keep spending full multi-agent phases after the objective is already done.

`--reassign-idle` covers the ordinary case, without anyone declaring the whole thing done: the first
agent that finishes while at least two others are still on their primary turn gets one extra prompt —
informed by what's already been claimed via `DIBS:` — to either pick up a different useful part of
the objective or prepare something that helps whichever agents are still working. The result lands
as its own turn (`proposal · extra`) alongside the normal one. Later finishers stay idle rather than
starting concurrent bonus turns (which would burn tokens and risk colliding workspace edits), and a
bonus is not started when only one primary remains (it would almost always be cancelled before
finishing). The in-flight bonus is cut short if it's still running once the round would otherwise be
over, so it can add value without ever making the phase wait longer than its slowest primary agent.
Under `--reasoning-effort auto`, that opportunistic bonus turn is hinted at low effort.

`--dead-code-check` runs once, after review rounds finish and before the synthesis relay drafts the
final answer. Unlike synthesis itself (prose only, no file edits), this turn keeps edit rights: one
agent is asked to search this session's code changes for functions, branches, or variables that were
added or left behind but no longer have any caller, confirm via a real search for call sites (not
just the definition) before touching anything, remove what's genuinely unused, and run the project's
test suite to confirm nothing broke. A failure here is non-fatal — synthesis proceeds without it, the
same way a failed refinement pass does.

In a `--self` session (agents editing roundtable's own source), every proposal, review, and
dead-code-check turn is followed by an independent verification: Roundtable itself — not the agent,
and not another agent's opinion — runs `python3 -m unittest test_roundtable` against the live
workspace and reports the real pass/fail result (`independent verification: PASS — Ran 333 tests in
45.0s · OK`, or the equivalent `FAIL` line). An agent's own claim of "233 tests passed" is never
taken at face value; ground truth is checked after every single turn that could have changed the
code. Concurrent post-turn checks in a parallel phase are serialized, and a workspace whose
`roundtable.py` / `test_roundtable.py` / `README.md` content matches the last verification reuses
that result instead of spawning another full suite (so six agents finishing over unchanged source
pay for one suite, not six). Changing any of those files invalidates the cache for that workspace;
timeouts and launch failures are not cached. This still adds real wall-clock time to a `--self` run
when source actually changes, but catches an overclaiming or hallucinating agent immediately rather
than after the fact. It costs nothing outside `--self` (there is no known test command for an
arbitrary workspace) and nothing during `--mock` (a MockAgent's simulated output has no real code
change behind it to check).

A CLI failure on a real, load-bearing proposal, review, or initial final-draft turn is retried once
after a short pause before it's treated as fatal — real-world failures on a long run are often a
transient rate limit or network timeout partway through a long chain of tool calls, not a broken
prompt, and a short retry recovers most of them. Once final synthesis has a valid draft, a failed
optional refiner is skipped without a second full-timeout attempt; later refiners continue from the
last good draft, so one unavailable provider cannot discard the completed result. A deliberate stop (Ctrl+C, or
`--task-status-check` cutting an agent off) is never retried, since that agent wasn't trying to finish
in the first place. Provider usage/session limits have a separate recovery path: Roundtable keeps the
agent's turn pending and, when the provider's own message names a reset time (e.g. "resets 5:30pm
(America/Chicago)"), sleeps until just past that moment instead of polling — there's no point checking
early when the limit is known not to have cleared yet. Without a recognizable reset time or
timezone it falls back to checking availability every 30 seconds with the lightweight preflight
prompt. Either way, the console only shows which method is being used, announced once, and a final
line once the agent
responds — the repeated 30-second rechecks stay silent instead of flooding the console, and the
original task is resent once the agent responds. The wait continues only while Roundtable is running
and remains cancellable with Ctrl+C (or by `--task-status-check`).

Retries and provider-limit waits use a yellow `↻` event in the GUI console and remain visible in
its default **key events** filter. A transient retry reports its exact backoff and attempt count
(for example, `retrying once in 3s (attempt 2/2)`); usage-limit events report whether Roundtable is
waiting for a provider-supplied reset time or polling, followed by confirmation when service
returns. The same stall is mirrored on the agent pane (`↻ retrying` / `⏳ rate limited` with
elapsed time), the roster mark, and optional status counts (`N retrying` / `N limited`) so the
operator does not need the console open to notice a blocked turn. These events are operational
warnings, not fatal errors; exhausted retries still surface as failures.

Final synthesis uses six sequential model calls by default: the chosen synthesizer drafts, then the
other five agents refine in turn. For a faster, lower-cost run, `--synthesis-passes 1` returns the
first draft directly; values up to `5` keep that many refinements. The selected `--synthesizer` is
normally the drafter, and the default of six preserves the full roundtable review. With
`--task-status-check`, once an agent has marked the objective complete (and at most one verification
review has run), synthesis is automatically capped at draft + one refine so the final answer does
not spend five more full CLI turns on polish; under `--synthesizer rotate`, the agent that declared
completion drafts first. An explicit `--synthesizer` name still drafts when set. Every stored
proposal, review, and extra contribution ends with `Signed: <agent>`. The final answer ends with
`Signed by:` listing the successful synthesis participants in relay order; failed or skipped
refiners are not credited.

Prompt preparation avoids repeatedly rendering and scanning the same transcript: parallel agents
share one phase-stable prompt context, and the synthesis relay reuses one transcript rendering
across all passes. Sequential collaboration still rebuilds it after every turn because each agent
must see the immediately preceding contribution.

Any resend — after a transient-failure retry or a usage-limit wait — appends a note telling the
agent to check current progress (`git status`/`git diff`, re-reading relevant files) before doing
anything else, since time has passed and its own earlier partial work, or another agent's in a
shared `--self` workspace, may already cover part of the task.

Any panel — Codex, Claude, Antigravity, Aider, Grok, Qwen, the task outcome, the code monitor, or the
console — can be expanded to full-screen for its complete, un-truncated content: press
`1`-`6`/`f`/`m`/`0`, or click/tap the panel. The code monitor title also shows compact `+N ~M −D`
change counts when files have been added, modified, or deleted.
The same key, a click on the expanded panel, or `Esc`/`q` collapses it back to the dashboard. Keyboard
shortcuts are only live while agents are working (not while typing a follow-up, so digits still type
normally there); clicking a panel works in both. Prompt and follow-up text boxes support multiline
editing with Up/Down arrow line navigation, Tab insertion, multiline pasting, and standard readline
shortcuts (`Ctrl+A` / `Ctrl+E` home/end, `Ctrl+U` clear-before-cursor, `Ctrl+K` clear-after-cursor,
`Ctrl+W` delete-word-backward).
While agents are working, press `i` to add a prompt without cancelling them. Dashboard and expanded
panel footers adapt their help to the terminal width, keeping cancel, expand/collapse, and scrolling
controls visible instead of truncating the control list with an ellipsis.
At the 72-column minimum, agent states use compact labels and panel activity is clipped to its own
column so neighboring panels and their borders remain distinct. When a usage-limit percentage is
known, the compact state keeps a short `· N%` suffix whenever the column still has room for it.
Panel work counters (Reads / Execs / Writes) match tool-style phrases only, not bare prose verbs.

Every run also writes `.roundtable/roundtable-<stamp>.log`. It records the full prompt and every
subprocess output line without the dashboard's truncation or sampling, plus reproducibility
configuration, source-file hash and timestamp, Git HEAD and complete short worktree status,
executable paths, models, reasoning settings, process IDs/groups, sanitized command arguments,
start/exit timing, output and answer sizes, retry classification, cancellation signals, phase
transitions, artifact paths, and full exception tracebacks. Environment variables, credentials,
and authentication tokens are deliberately excluded; prompt arguments in command records are
replaced by their length and SHA-256 fingerprint because the complete prompt already has its own
record. You can `tail -f` a live run or grep a completed one for real exit codes, empty responses,
auth failures, and timeouts. The log shares a filename stem with the session's `.json`/`.md`
transcript and is created in both fullscreen and `--plain` modes. Both paths are printed on exit —
after a normal finish, a `--plain` failure, and a `Ctrl-C` cancel (whenever a transcript exists to
report) — so you never have to guess where a run's evidence went.

Text-box controls:

```text
Enter      Start / send
Ctrl+N     Insert a new line
Arrow keys Move the cursor
Esc        Exit or finish the session
Ctrl+C     Cancel active work
```

Ctrl+C cancels cleanly at any point in the process, not just once agents are working — including the
startup options screen, the objective prompt, and the preflight connectivity check — printing
`Cancelled.` and exiting instead of a raw traceback.

The UI automatically adjusts when you resize your terminal window, maintaining proper layout and
preventing display corruption. Expanded panels remain open; if a compact layout hides the focused
Console panel, keyboard focus moves to the nearest visible panel instead of leaving Enter attached
to an invisible target.

## Touchscreen and convertible use

Roundtable automatically detects Linux touch digitizers, including the Atmel maXTouch panel in the
Lenovo Yoga 2 11. Touch mode adds large **START**, **SEND**, **NEW LINE**, **CLEAR**, **FINISH**, and
**STOP** targets. Swipe or two-finger-scroll an agent pane to review older output. The header shows
touch mode and the current battery percentage.

Touch events are delivered through the terminal emulator's mouse protocol. Use `--touch` if the
terminal does not expose the digitizer automatically, or `--no-touch` when working over SSH.

## Safety model

By default, Codex runs with `workspace-write`; Claude runs with `acceptEdits`; Antigravity runs
sandboxed in `accept-edits` mode; Aider auto-accepts edits but is kept from suggesting or running
shell commands; Grok runs with its own `acceptEdits` permission mode plus its `workspace` sandbox
profile; Qwen runs with its `auto-edit` approval mode. All six receive the same working directory
and may edit it, so use a version-controlled project and review the resulting diff. None of these
defaults ask the agent to confirm file edits, but they do stop it short of running arbitrary shell
commands unsandboxed or unconfirmed — in headless mode that can surface as an agent silently
declining a step it needed (for example Antigravity soft-denying a `Bash` tool call and returning a
short explanation instead of real output). On editing turns, Aider discovers an existing git
repository so its model receives a repository map, but Roundtable disables Aider's automatic
clean-tree and dirty-tree commits, automatic `.gitignore` edits, and Playwright installation.
Read-only Aider turns (preflight and synthesis) use `--no-git`: their complete context is already in
the prompt, and this prevents Aider from auto-adding mentioned source files to what can otherwise
become a very large request. A non-repository workspace also retains `--no-git` so unattended
operation cannot initialize one. Qwen is deliberately never run with its own `--sandbox` flag:
verified
against the real CLI, that flag launches a container-backed sandbox and hangs indefinitely rather
than failing cleanly when no container runtime is reachable, so its approval mode is the only gate
by default.

`--elevated AGENT` (repeatable; `codex`, `claude`, `antigravity`, `aider`, `grok`, `qwen`, or `all`)
swaps that agent's sandboxing for its CLI's own permission-bypass flag
(`--dangerously-bypass-approvals-and-sandbox` for Codex, `--dangerously-skip-permissions` for Claude
and Antigravity, `--suggest-shell-commands` for Aider, `bypassPermissions` for Grok, `yolo` for
Qwen), so it can run shell commands freely instead of hitting that wall. This is off by default and
does what its name says: an elevated agent can run any command in your workspace without asking.
Only use it in a project you trust the agents in, and still review the diff.

Proposal and review phases run concurrently. This lowers latency and lets agents own different parts of
a solution, but simultaneous edits to the same file can conflict at the filesystem level. Give agents a
task with separable parts when possible, use version control, and inspect the final diff.

`--self` is the same "edit a real workspace" model, just pointed at roundtable's own source — commit
or otherwise back up roundtable.py, test_roundtable.py, and README.md first (put the directory under
version control if it isn't already) so a self-edit run is always reversible, and review the diff
before trusting it. Editing the file on disk doesn't affect the roundtable process already running —
Python has it loaded in memory. After a phase or final synthesis changes `roundtable.py`, a `--self`
run saves its transcript with an atomic file replacement, then replaces the running process with the
updated program. Completed phases and consensus are skipped on the resumed process, so work continues
from the saved progress rather than starting over or synthesizing the same answer twice.

That restart is normally immediate and silent the moment `roundtable.py`'s content changes. The one
exception: if the change lands during the proposal phase and at least one review round is scheduled,
the restart is deferred by exactly one phase so the agents get a say in timing instead of being
restarted out from under them with no warning. Review round 1's prompt tells every agent the source
changed and asks each to end its turn with `RESTART: now` or `RESTART: later`, plus a short reason.
Majority decides (a tie favors restarting, since running stale source is the riskier failure mode);
if the vote is `later`, one more review round runs with no further vote requested, and the restart
happens for real right after — regardless of that round's own outcome — so the deferral is bounded to
a single grace phase, not an open-ended negotiation. When the vote resolves, the dashboard status
line and run log record a one-line tally (`RESTART vote: now=N later=M → …` plus who voted which way)
so operators do not have to re-read every agent turn to see the outcome. Any agent whose vote didn't
match the outcome still has its stated reason on the record in the transcript. With zero review rounds configured, or
outside a `--self` session, the restart stays immediate as before — there's no next phase to defer
into, and no vote to hold in the first place.

Every `--self` run is also told about a throwaway copy of the source kept at
`<output-dir>/self-test-sandbox`, refreshed at the start of the run and again on every restart. An
agent can copy its edited `roundtable.py` there and run it directly with `--mock --plain
--skip-preflight --synthesis-passes 1 -r 0` to smoke-test a change without running inside the live
shared workspace other agents may be concurrently editing, waiting for interactive follow-up input,
or interfering with this run's own in-memory process. If the sandbox directory cannot be created,
startup fails with a clear error; individual file copy failures warn on stderr and leave the rest of
the refresh intact. In the TUI, `--self` shows a `⚡ self` header badge, strips the standing note from
the objective line, and surfaces the sandbox path on the status line so operators can see where the
smoke-test copy lives without reading the full agent prompt.

## Smoke test

Mock mode checks the orchestration and transcript path without making model calls:

```bash
roundtable --mock --plain -r 1 "Design a rate limiter"
```
