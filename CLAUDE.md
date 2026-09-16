# Governance for this project

This repo has one designated primary-context session: the one that has been doing the sustained
work on roundtable.py across many sessions (the `--chat` mode, the 6-agent expansion, the `--self`
data-loss incident and its fix, the installer work, etc. — see project memory / HANDOFF.md for the
detailed history). If you are a *different* Claude Code instance working in this directory (e.g.
reached over SSH), you don't have that accumulated context, so the rules below apply to you.

## Before doing anything, read

- `git log` and `git status` — check for uncommitted work or recent changes you don't have context for.
- `PENDING_REVIEW.md` in this directory — the sign-off queue described below. If it has unresolved
  entries, don't add conflicting work on top without checking it first.

## What needs sign-off before you act (not just before you commit)

For anything in this list: **ask your own user directly for explicit approval before running the
command / making the edit / pushing — the same way you'd already pause for any risky action — then
append a PENDING_REVIEW.md entry (format below) recording what was proposed and what they decided.
Do not treat writing the entry itself as permission, and do not proceed on a timer or in the
absence of a reply — the user's explicit yes is what unblocks you, nothing else.**

If there's a real chance your user has stepped away (this is a "needs a decision before I can
continue" situation, not routine progress), use your PushNotification tool so they see the request
even if they're not watching this session right now.

- Any `--self` run (agents editing roundtable's own source) — this is the exact mechanism that
  destroyed an uncommitted session once already; see project memory.
- `git push --force`, `git reset --hard`, rewriting published history, or deleting branches.
- Deleting or substantially restructuring existing public functions/classes/CLI flags (i.e.
  anything a user's saved command or script could depend on).
- Adding a new runtime dependency, or anything that would break roundtable's dependency-free design.
- Any system-level change on this machine outside the repo (sudoers, systemd units, firewall,
  installing services) — not just repo changes.
- Anything you are not confident is reversible.

## What does NOT need sign-off

Ordinary bug fixes, test additions, refactors that keep existing behavior, and documentation
updates — as long as the full test suite (`python3 -m unittest test_roundtable test_install`)
passes before you commit. Normal commits to your own in-progress work are fine; the sign-off gate
is for the specific risk categories above, not a blanket "ask before every change."

## PENDING_REVIEW.md entry format

This file is a durable record for the primary session to catch up on later — not the approval
mechanism itself (your user's real-time answer is). Append (don't overwrite) an entry once the
user has actually responded:

```
## [2026-08-05 21:40 UTC] <short title>
Proposed by: <your session identifier, e.g. "ssh-instance">
What: <the specific command/edit you asked to run>
Why: <one or two sentences>
User decision: APPROVED | REJECTED <reason>
```
