# Review and test cadence: evidence from PRs #80 to #109, and a lighter rule

Written 19 Oct 2026 from the 17 to 19 Oct session (1fed95e2), its 60 wheelhouse subagent transcripts, and the "by Claude" summary comments on each PR.

## Headline

- Round 1 earns its keep. It found a blocker or bug in 13 of the 15 PRs that had an agent review, and nearly all of the findings a person would actually have hit.
- Later agent rounds mostly reviewed their own fixes. Of about 25 findings in rounds 2 to 4, about 17 were introduced by the previous round's fix (my classification from the summaries). Three PRs (#100, #104, #105) took 4 rounds each, and together used 22 of the 34 full-suite runs.
- The suite ran at least 34 times in 3 days, about 12.7 hours of machine time. A typical run took 15 to 24 min, and the worst runs took 43 min (#108) and 86 min (#93, at load 30). Builders ran it after every fix round, because they were reused across rounds and each brief said "the full suite once".
- Both escapes to main were cross-PR or cross-feature interactions, and neither was a gap in per-PR depth. #102 (a crash) got past 2 review rounds on #82. #99 (a test hard-coding the API version) broke main for about an hour after #93 merged.

## Evidence

Columns: rounds are agent review rounds, plus `+s` where I read a fix diff myself. In "R1" findings, B is blocker, Bug is bug, M is material and N is nit. "Later" is the findings from rounds 2 onwards, with the fix-induced ones in brackets. "Hit" is my estimate of how many findings Doug or a colleague would have hit in normal use. "Suite" is the full-suite runs and their total minutes, from the transcripts (this may undercount runs whose results were only summarised). "Open to merge" is from `gh`.

| PR | What | Rounds | R1 (B/Bug/M/N) | Later (fix-induced) | Hit | Suite | Open to merge |
|---|---|---|---|---|---|---|---|
| #80 | legacy reply wait | 0+s | 0 | 0 | 0 | 0 | 0.4 h |
| #82 | column sort | 2 | 0/0/2/2 | 0 | 1, and missed #102 | 0 | 1.2 h |
| #84 | Tab order | 2+s | 0/1/3/1 | 1 (1) | 2 | 0 | 1.3 h |
| #85 | dialog keys | 2 | 1/0/0/3 | 0 | 1 | 0 | 0.9 h |
| #86 | modules stage 1 | 2+s | 0/3/3/3 | 2 (0, both cross-PR with the tour) | 5 | 0 | 4.0 h |
| #89 | make targets | 0+s | 0 | 0 | 0 | 1 / 10 min | 0.2 h |
| #90 | tutorial stage 1 | 1+s | 0/1/1/2 | 0 | 0 | 0 | 0.5 h |
| #91 | state dir (tfu session) | none here | - | - | - | - | 0.0 h |
| #92 | tour stage 2 | 1+s | 0/1/0/3 | 0 | 1 | 0 | 0.4 h |
| #93 | tour stage 3 | 2+s | 0/3/2/2 | 2 (1) | 4 | 2 / 112 min | 4.6 h |
| #94 | stats leaves core | 1+s | 1/0/3/3 | 0 | 2 | 1 / 27 min | 0.7 h |
| #95 | pane swap | 1+s | 0/3/1/3 | 0 | 3 | 0 | 0.9 h |
| #96 | block doc viewer | 1+s | 0/0/1/3 | 0 | 0 | 0 | 1.0 h |
| #97 | module routes | 2+s | 0/1/3/3 | 2 (2, threading) | 1 | 2 / 35 min | 1.0 h |
| #98 | tour stage 4 | 1+s | 0/2/1/0 | 0 | 2 | 2 / 34 min | 0.5 h |
| #99 | API test fix | 0+s | 0 | 0 | 0 | 0 | 0.2 h |
| #100 | design module | 4 | 6 (unclassified) | 4 (4, compact panes) | 3 | 7 / 120 min | 26.3 h (overnight, for Doug) |
| #103 | #102 crash fix | 0+s | 0 | 0 | 0 | 0 | 0.2 h |
| #104 | tour tweaks | 4 + verify | 4 (spec gaps) | 7 (6, placement) | 3 | 7 / 146 min | 9.7 h (for Doug) |
| #105 | doc editor | 4 | 0/2/0/0 | 8 (5, kept buffers) | 4 | 8 / 168 min | 9.1 h (for Doug) |
| #108 | busy buttons | 2 so far | open | open | - | 2 / 66 min | open |
| #109 | docs section | 2 so far | open | open | - | 2 / 45 min | open |

#81, #83, #87, #88, #101, #102, #106 and #107 are issues, not PRs. The repo has no CI workflow, so local suite runs are its only CI.

## Which rounds paid for themselves

- **Every round 1 that found something.** Examples: focus stolen mid-typing (#93), the tutorial offer lost to any click (#85), stats vanishing on upgrade (#94), Later never coming back (#92), and unsaved edits dropped (#105).
- **#86 round 2.** It caught the tour callout pointing at a widget #86 had retired: a cross-PR interaction, which is exactly what a batch review exists for.
- **#105 round 3.** The paint_title crash was real, and the builder had written off its 17 failures as flakes. It was introduced by round 2's fix, though.
- **Rounds that only reviewed the last fix:** #84 round 2, #97 round 2 (threading added in round 1's fix), #100 rounds 2 and 3 (compact panes), #104 rounds 3 and 4 and the final verification (callout placement), and #105 rounds 3 and 4. Most of their findings were edge cases (below 80x24, three-slot columns, experimental SDK mode). Nobody would have hit them unless the fix that caused them had shipped.
- **Self-read rounds** (me reading the fix diff, about 5 min) closed 12 PRs cleanly. Nothing they passed has come back.

## Where batching catches the same bugs

- **#102 (crash).** Two rounds on #82 and 94 targeted tests missed it, and no existing test covered sort-then-click, so a full suite would have missed it too. A batch-level smoke pass (sort each column, then click a session) would have caught it. Per-PR depth would not.
- **#99 (API version test).** One full suite on an integration branch holding #93 and #98 catches it before main.
- **Fingerprint API collisions (#97 and #98)** and **#86 against #84 and #92.** These are cross-PR interactions, and one review over the combined diff sees them for free.
- **Per-PR bugs from round 1.** These are unchanged. The proposal keeps round 1 on every PR.
- **What batching loses.** It loses round 2's re-check of round 1's fixes. The evidence says those fixes produced about 17 findings, mostly cosmetic or edge cases. The batch review sees every fix diff anyway.

## What an escape to main costs here

- **#102.** The crash shipped at 09:36Z on 17 Oct, Doug hit it about 37 hours later (issue opened 23:10Z on 18 Oct), and the fix (#103) merged 19 min after that. My inference: one TUI crash and a restart, with no data loss, because sessions run in their own tabs and the database is unaffected.
- **#99.** Main's suite was red for about an hour, with no user impact.
- **The real exposure is colleagues.** Colleagues pull main (the beta 1 decision on #17), so an escape can reach someone else's machine. The repo is still a hobby tool: no money and no data at risk.
- **The cost of the strict loop, for comparison.** #104 and #105 ran overnight for about 10 h and 3.5 h of builder time, with 15 suite runs between them. They still ended as questions for Doug (Q129, Q130). Machine load from the parallel suites (30 to 55) caused the timing flakes that then cost reruns.

## Recommendation

- **Scope it to this repo.** Put it in wheelhouse project memory and leave the global rule alone. Two Inc payment repos carry a different escape cost, and the global self-review lane already allows "Project memory can waive ...".
- **Per task.** Targeted tests only, and no full suite. Run one adversarial review round, fix what it finds, then one subagent reviews the fix diff (Q134: not the main thread, which fills its context and may run a lighter model). A second agent round runs only when the fix is a redesign (new state, threading, placement rules) rather than a patch.
- **Per batch.** Up to 5 PRs or about 2500 changed lines, or whatever is ready at the end of a working block. The PRs merge into a short-lived `batch/<date>` branch. On that branch, run:
  - one full suite;
  - one adversarial review of the combined diff, briefed to hunt cross-PR interactions;
  - a Pilot smoke run of the flows the batch touched.
- **Then merge to main** through one batch PR, using the self-merge lane.

### Current text this replaces

Project memory `project_claude_wheelhouse.md`, the "How to apply" paragraph:

> On 17 Oct (for that day) and again on 18 Oct 2026 ("Go ahead and self-merge everything"), Doug authorised self-merging after a clean adversarial review, with a summary signed "by Claude" posted on the PR and 10 minutes after the last commit. A PR that hits the oscillation cap (4 rounds) goes back to him as a Q instead.

The global rules, `~/.claude/CLAUDE.md` lines 140 and 142, stay as they are. They read "Merge only after a clean adversarial review round ... After fixes, review the updated diff again" and "Self-review lane ... run your own adversarial review to a clean round". This repo's memory overrides them, as line 142 allows. So does `/home/doug/projects/CLAUDE.md` line 11 ("Run the adversarial review loop unattended until a round comes back clean ..."), which applies here because the clone lives under `~/projects`. The Wheelhouse protocol files (`claude_wheelhouse/protocol*.md`) say nothing about review or test cadence, so they need no change. The full-suite habit came from the brief template ("Run the touched tests, then the full suite once"), which repeats on every fix round when a builder is reused. That template goes too.

### Paste-in text (project memory)

> **Review and test cadence (claude-wheelhouse only, overrides the global self-review lane).** Doug authorises self-merging under this cadence.
> - Per PR: run only the targeted tests (touched test files, plus tui and hub when the UI changes). Don't run the full suite in a builder or a fix round. Run one adversarial review round, fix blockers, bugs and material findings, then have one subagent review the fix diff, never the main thread. Run a second agent round only if a fix is a redesign (new state, threading or placement rules), not a patch. If that round still finds a blocker, stop and post a Q. Nits, and findings introduced by a fix that are cosmetic or edge-case, go to a GitHub issue, not another round.
> - Per batch: merge reviewed PRs into a short-lived `batch/<yyyy-mm-dd>[-n]` branch off main, up to 5 PRs or about 2500 changed lines. Close the batch with one full suite, one adversarial review of the combined diff (briefed to look for cross-PR interactions: shared constants such as fingerprint.API, focus and Tab order, tour selectors, and sort and pin shapes), and a Pilot smoke run of the flows the batch touched. Fix what they find on the batch branch, rerun only the affected tests, then self-merge the batch PR into main with a "by Claude" summary 10 minutes after the last commit. A batch branch is not a long-lived branch, so the promotion-PR rule doesn't apply.
> - Treat any full-suite failure as real until a traceback shows it's outside this batch's symbols. Never call it a flake from a rerun alone.

## What this adds, and when to go back

- **Risk added.**
  - Fix-induced bugs live longer, until the batch gate rather than the next round.
  - A failure at the batch gate has to be bisected across up to 5 PRs.
  - A 2500-line combined review is shallower per line than a per-PR round, which is why the cap exists.
  - Main moves less often, so colleagues get fixes later.
- **Go back to the strict rule** (per-PR clean round, full suite per PR) if any of these happen:
  - an escape to main that loses data or state, or breaks startup;
  - a second crash-class escape within 2 weeks;
  - a batch gate that fails on interactions twice in a row;
  - a colleague reports a regression.
- **Also return to it** for any PR that touches the store schema, migrations, the daemon's auth token or /docs path confinement.
