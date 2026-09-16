# Decision log

See CLAUDE.md for what requires sign-off. Approval happens in real time with whichever human is
driving the session at the time — this file is the durable record of what was asked and decided,
so a later session can catch up without re-litigating it. Append new entries at the bottom; don't
edit or delete past ones.

(No entries yet.)

## [2026-08-06 23:42 UTC] install.py auto-installs windows-curses/tzdata on Windows

Proposed by: ssh-instance (Windows desktop, cross-machine test coordinator)
What: Added `ensure_windows_dependencies()` to install.py, run unconditionally on Windows (even
with `--skip-clis`, since it's not an agent CLI): `python -m pip install windows-curses tzdata`.
Why: Neither package is optional in practice on Windows -- `windows-curses` is needed just to
`import curses`/roundtable.py at all there (previously only a printed warning, never installed),
and `tzdata` is needed for the two reset-time tests that need a real IANA zone (Windows ships none)
to actually run instead of skip. This is install.py's first departure from "dependency-free
install, just report what's missing" for the six agent CLIs -- these two are dependencies of
roundtable.py/its test suite itself, not of an agent.
User decision: APPROVED -- asked directly whether to add both, windows-curses only, or neither;
chose to add both. Verified: 458/458 on Yoga and Pi, 455 passed + 3 legitimately-POSIX-only skips
(0 errors) on the Optiplex after running the updated installer for real.

## [2026-08-07 04:15 UTC] Repo primary location moved to the Pi

Proposed by: ssh-instance (Windows desktop, cross-machine test coordinator)
What: Gave the Raspberry Pi a proper `git clone` of the GitHub repo (replacing its earlier
ad-hoc, non-git test snapshot), set up git identity and `gh`-authenticated push access there.
Why: User (Liam) explicitly directed this -- asked to make the Pi the primary/canonical repo
location and have this Yoga machine become just a testing device going forward.
User decision: APPROVED -- explicit direction, confirmed via direct question about scope
("push files to the Pi so the Yoga can be a testing device" -> clarified as "make the Pi the
primary repo, Yoga becomes just a test box").
Note: this change has NOT been communicated to the live Yoga Claude session directly (no channel
exists for an SSH-guest instance to message a live interactive session) -- relayed via this entry,
a HANDOFF.md addendum, and a note appended to this session's own persistent memory file instead.
Liam may still want to update ~/roundtable/CLAUDE.md's own primary-session designation, which
doesn't yet reflect this change.

## [2026-08-17 13:52 UTC] Governance docs copied to the pi (closes the 08-07 gap noted above)

Proposed by: yoga session (working the home-lab-infra outstanding-items list, not a roundtable
code change).
What: Copied `CLAUDE.md` and this file from yoga's local, untracked copies to
`pi:/home/user/roundtable/`, which had neither. No content changes -- same untracked-by-design
convention as before, just present on the actual canonical box now.
Why: The 08-07 entry above flagged that the primary-session move to the Pi was never reflected in
CLAUDE.md, but the deeper problem was that CLAUDE.md and this log didn't exist on the Pi at all --
a fresh session working there had no visibility into the sign-off rules or decision history.
Wording still reads "the session that has been doing the sustained work" rather than naming a
machine, which was already Pi-compatible; the file just wasn't where it needed to be.
User decision: not explicitly asked -- this is a low-risk documentation-placement fix (copying
existing untracked files, no code/config/repo-state changes), done while working the home-lab
HANDOFF.md outstanding-items list at the user's direction ("work on the other tasks"). Flagging
here per the log's own purpose rather than treating silence as license for anything higher-stakes.
optiplex (test box) still lacks both files as of this entry -- lower priority since it's not the
canonical location, not done yet.
