# Roundtable

A dependency-free terminal table where the Codex, Claude Code, Antigravity, Aider, Grok Build, and
Qwen Code CLIs solve a problem together as equally as possible — six agents spanning five labs
(OpenAI, Anthropic, Google, xAI, Alibaba) plus a model-agnostic sixth pinned to Mistral's Codestral
by default, so no two agents are running the same underlying model. Each agent is nudged toward one
of six complementary lanes — sandboxed execution and testing, architecture and reasoning, breadth
and stress-testing the others' work, fast narrowly-scoped diffs, skeptical verification, or
integrating the group's approaches into one plan — so six parallel attempts produce complementary
contributions instead of six competing full solutions. Which agent gets which lane rotates by
objective, so no agent is permanently typecast into the same role run after run. Agents also call
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

## Run it

All six CLIs must already be installed and authenticated (`codex`, `claude`, `agy`, `aider`, `grok`,
and `qwen`). Aider is model-agnostic — it defaults to `mistral/codestral-latest` here specifically
so it doesn't just duplicate one of the five lab-native agents; point `--aider-model` at a different
provider if you'd rather it run as something else.

```bash
cd /path/to/project
roundtable "Fix the flaky checkout test and verify the fix"
```

Or launch it without an objective to get an interactive prompt:

```bash
roundtable
```

In a real terminal (not `--plain` or piped), startup shows a quick numbered toggle screen for the
opt-in options below before anything else — press the number shown next to an option to flip it,
then Enter (or Esc/`q`) to continue (skipped on a `--self` restart, which carries forward the
choices already made before the source changed, so it can relaunch unattended). Any matching CLI
flag you already passed sets that toggle's
starting state; leaving a toggle untouched keeps whatever the flag specified (so a specific
`--elevated codex` survives even if you don't touch that option).

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
--reassign-idle        In parallel phases, an agent that finishes while others are still working
                       gets one extra prompt to pick up different unclaimed work or help a
                       still-running agent, instead of sitting idle for the round
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

The fullscreen view provides evenly spaced live Codex, Claude, Antigravity, Aider, Grok, and Qwen
panes, independent working/waiting states, and agent-specific activity tickers next to agent names
(a pulsing circle for Codex, an asterisk pulse for Claude, moving braille dots for Antigravity, a
rotating quadrant for Aider, a dashing line for Grok, and a spinning arc for Qwen). Per-agent usage
sparklines show response time, output size, and activity, while a live work feed inside each active
agent's own pane keeps reported file reads, searches, edits, commands, tests, and other CLI progress
attributed to the agent that emitted them; once the agent finishes, its pane returns to the completed
response. Read/execute/write counters provide an at-a-glance summary; because CLI output formats
differ, they are progress indicators rather than audit totals. A live elapsed-time readout, a wrapped
multiline task composer, and a task-outcome box summarizing completed, failed, and incomplete work,
plus a live code monitor showing files changed during the session and a console panel round out the
diagnostics. Expand an individual agent pane to inspect its full response.

While agents are working, press `i` (or click/tap the "ADD PROMPT [i]" control next to the status
line) to interrupt and open the same follow-up box used between rounds, without stopping the run.
Anything you send there is queued rather than applied immediately; it lands in the transcript, and
takes effect, at the start of the very next phase — proposal, review round, or synthesis — whichever
comes next. An active agent acknowledges the queued prompt for you on the status line ("[Codex]
Acknowledged queued task: …"), rotating through whichever agents are currently working so the
acknowledgment isn't always attributed to the same one. After an answer, a follow-up text box lets
you keep the same roundtable conversation going. Complete transcripts are written to `.roundtable/`.

The console opens on **key events** — phase changes, completed turns, and errors — instead of a
firehose of every raw line each CLI prints, so the signal-dense view is the default. Press `c` to
cycle it through **all activity** (adds raw per-line ticks), **prompts** (just what was sent to each
agent), and **errors only**; each line is tagged with a small glyph (`▶` phase, `✓` turn, `✗` error,
`➤` prompt, `·` tick) that also carries color, so kind is legible even without color. A running count
by kind sits under the console title. Press `0` or click the console to expand it full-screen for the
complete filtered history, same as the agent and answer panels. Mouse wheel or two-finger scroll works
on the console the same as any other panel, whether it's expanded or still in the compact dashboard
view, and the title shows `↑N` while scrolled back from the latest entry.

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
work. They still get a full turn in the next phase (typically the following review round) to check
and refine what was done, so nothing is lost, just not redone from scratch.

`--reassign-idle` covers the ordinary case, without anyone declaring the whole thing done: an agent
that just finishes faster than the other two. Rather than sit idle for the rest of the round, it gets
one extra prompt — informed by what's already been claimed via `DIBS:` — to either pick up a different
useful part of the objective or prepare something that helps whichever agents are still working. The
result lands as its own turn (`proposal · extra`) alongside the normal one. At most one extra attempt
per agent per phase, and it's cut short if it's still running once the round would otherwise be over,
so it can add value without ever making the phase wait longer than its slowest primary agent.

A CLI failure on a real, load-bearing turn (proposal, review, or the final synthesis relay) is
retried once after a short pause before it's treated as fatal — real-world failures on a long run are
often a transient rate limit or network timeout partway through a long chain of tool calls, not a
broken prompt, and a short retry recovers most of them. A deliberate stop (Ctrl+C, or
`--task-status-check` cutting an agent off) is never retried, since that agent wasn't trying to finish
in the first place. Provider usage/session limits have a separate recovery path: Roundtable keeps the
agent's turn pending and, when the provider's own message names a reset time (e.g. "resets 5:30pm
(America/Chicago)"), sleeps until just past that moment instead of polling — there's no point checking
early when the limit is known not to have cleared yet. Without a recognizable reset time it falls back
to checking availability every 30 seconds with the lightweight preflight prompt. Either way, the
console only shows which method is being used, announced once, and a final line once the agent
responds — the repeated 30-second rechecks stay silent instead of flooding the console, and the
original task is resent once the agent responds. The wait continues only while Roundtable is running
and remains cancellable with Ctrl+C (or by `--task-status-check`).

Final synthesis uses six sequential model calls by default: the chosen synthesizer drafts, then the
other five agents refine in turn. For a faster, lower-cost run, `--synthesis-passes 1` returns the
first draft directly; values up to `5` keep that many refinements. The selected `--synthesizer` is
always the drafter, and the default of six preserves the full roundtable review.

Any resend — after a transient-failure retry or a usage-limit wait — appends a note telling the
agent to check current progress (`git status`/`git diff`, re-reading relevant files) before doing
anything else, since time has passed and its own earlier partial work, or another agent's in a
shared `--self` workspace, may already cover part of the task.

Any panel — Codex, Claude, Antigravity, Aider, Grok, Qwen, the task outcome, or the console — can be
expanded to full-screen for its complete, un-truncated content: press `1`-`6`/`f`/`0`, or click/tap
the panel.
The same key, a click on the expanded panel, or `Esc`/`q` collapses it back to the dashboard. Keyboard
shortcuts are only live while agents are working (not while typing a follow-up, so digits still type
normally there); clicking a panel works in both.

Every run also writes `.roundtable/roundtable-<stamp>.log` — the same activity the console panel
shows, plus the **full prompt sent to each agent** (not just a truncated summary), so you can
`tail -f` a live run or grep a completed one for detail `--mock` never produces: real exit codes,
empty responses, auth failures, timeouts. It shares a filename stem with that session's `.json`/`.md`
transcript and is created in both fullscreen and `--plain` modes.

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
short explanation instead of real output). Aider is also always run with `--no-git`, so it never
auto-creates a git repo or auto-commits in your workspace on your behalf, regardless of the
`--elevated` setting below. Qwen is deliberately never run with its own `--sandbox` flag: verified
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

## Smoke test

Mock mode checks the orchestration and transcript path without making model calls:

```bash
roundtable --mock --plain -r 1 "Design a rate limiter"
```
