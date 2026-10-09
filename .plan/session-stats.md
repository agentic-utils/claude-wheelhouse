# Session stats pane (design)

## Intent

Show, at a glance, how heavy a session is and whether its prompt cache is still warm, so
the person can decide when to answer, compact or leave a session alone. The item list in
the Inbox's middle column (the "questions section") shrinks to the top half of its
column, and a stats panel fills the bottom half.

## What's shown

For the session in context: the highlighted session row, or the session of the
highlighted item.

- **Header.** Session name, model and context window.
- **Context.** After a blank line, current context size as a gauge and a number against the window, coloured
  green under 150k, yellow under 300k, amber under 600k, red above, whatever the window
  (#51: Doug's thresholds, shared with the session list's context bar; they replaced
  claude-dashboard's window-relative `ctx_grade` and its flashing red).
- **Cache.** The time-to-live (TTL) in use (5m or 1h), warm or cold, and when it goes
  cold, as a clock time and a countdown. Once cold: "cold since 10:12 BST".
- **Compactions.** Count, time of the last one, and its before and after sizes.
- **Histograms.** Small versions of claude-dashboard's "context assembly" and "output
  tokens" charts for this session, with the shimmer. They span the last 2 hours, split
  into as many buckets as the charts have columns. Context assembly stacks tokens read
  from cache (green), new input (blue) and cache misses (red); output is yellow.

With no session in context, the panel shows totals across the running sessions.

## Layout

On a 200-column terminal the middle column is 55 wide (53 inside the border). Since #43
the pane follows claude-dashboard's look: a panel with the dashboard's rounded border
(its dimmest colour, the title in its cyan accent) holds the three gauges, each with a
blank line under it, then the cache and compaction rows. The gauges are the dashboard's:
a solid bar on a dotted track and the percentage bold in the bar's colour, one bar width
for all three so they line up. Below the panel, two charts share one hour axis. Sketch at
53 columns and 26 rows, from `stats.render` (colours stripped):

```
╭─ holly · Opus 5.5 · 1M window ────────────────────╮
│context ███░░░░░░░░░░░░░░  18% 183k/1M             │
│                                                   │
│session ████░░░░░░░░░░░░░  23% resets Fri 01:18 BST│
│                                                   │
│weekly  █████████████░░░░  74% resets Tue 15:01 BST│
│                                                   │
│cache   1h · warm · cold at 00:45 BST (in 50m)     │
│compact 1× · last 23:05 BST · 201k → 52k           │
╰───────────────────────────────────────────────────╯
▸ context assembly  ▆ cache  ▆ new  ▆ miss
          ▇                █▆▂    █   █ █▅       ▇
531k  ▇   █ █  ▄ ▂      ▃  ███  ▅ █  ▄█ ██▄▅     █ ▄▄
      █▇ ▂█ █▄▇█▅█▁  ▇ ██▃ ███  █ █  ██ ████ ▇ ▇▄█ ██
265k  ██▁██▇███████▁▃█████▇███ ▁█▆█▅▄██▃██████████▁██
      ████████████████████████▅██████████████████████
    0 └──────────────────────────────────────────────

▸ output  ▆ output tokens
                                      █
 18k   ▄  ▅                ▅ ▂  ▅     █          ▇
       █  █  ▃    ▁      ▁ █ █  █ ▂  ▂█  ▆█▆▃  ▂ █
  9k  ██  █▃▇█▇▃▆▄█▅ ▃▁▇▇█▅███ ▁█ █▄ ██ ▆████ ▃█▇█ ▆▁
      ███▇████████████████████▅█████▆████████████████
    0 └──────────────────────────────────────────────
        22:00                  23:00
```

- **Chart height.** Each chart is 40% of the height the single chart had before #43 (the
  pane less six text rows and the chart's own three), and no taller than fits under the
  panel. A short pane drops the output chart first, then the context assembly chart
  (below two rows of bars each). A blank line separates the two charts when there's room.
- **Notes** ("reading …", a failed read) wrap inside the panel instead of being cut off at
  its border.

In Textual, `#items-pane` is a `Vertical` holding the items table (`height: 1fr`) and the
`SessionStats` widget (`height: 1fr`), which draws what `stats.layout` and `stats.render`
return.

## Data sources

All of it comes from the transcript files Claude Code already writes. I checked the field
names against a live transcript (`~/.claude/projects/-home-doug/<id>.jsonl`, 8 Oct 2026).

- **Main thread:** `~/.claude/projects/<project>/<session id>.jsonl`, the file
  `transcript.locate` already finds.
- **Subagents:** `~/.claude/projects/<project>/<session id>/subagents/agent-*.jsonl`, one
  file per subagent or fork. Their records carry `isSidechain: true`. They aren't in the
  main file, so `tail_records`' sidechain filter never sees them.
- **Usage records.** `type: "assistant"` records with `message.usage`.
  - **Repeated lines.** One API response is written as several lines that share
    `message.id` and `requestId`, each with the same `usage`. Count each `message.id`
    once, as the dashboard does.
  - **Fields used:** `input_tokens`, `cache_creation_input_tokens`,
    `cache_read_input_tokens`, `output_tokens`, and
    `cache_creation.ephemeral_5m_input_tokens` and `cache_creation.ephemeral_1h_input_tokens`.
  - **Model:** `message.model` (for example `claude-opus-5-5`).
- **Context size.** From the latest main-thread response: `input_tokens +
  cache_creation_input_tokens + cache_read_input_tokens`. In the live sample that was
  2 + 861 + 81,966 = 82,829.
- **Window.** The model id never says whether the 1M context is on: `[1m]` is stripped
  from the logged id. Use claude-dashboard's rule: Opus and Sonnet count as 1M, Haiku as
  200k, and any session seen above 200k is 1M.
- **Cache TTL.** Taken from the newest main-thread response that wrote cache: 1h if
  `ephemeral_1h_input_tokens > 0`, 5m if `ephemeral_5m_input_tokens > 0`.
  - **Why read it each time:** claude-dashboard's rule that "5m is subagent work and 1h is
    main thread" no longer holds. In the live sample a fork also wrote 1h. Claude Code also
    drops to 5m when an account goes into overage. Reading it response by response follows
    both.
- **Cache expiry.** The time of the latest main-thread response plus the TTL: a cache read
  refreshes the entry's TTL as well. Use the earliest record for that `message.id`. That
  is close to the request start, late only by the time to first token, which is a few
  seconds.
  - **What it ignores:** forks read the parent's cached prefix (the sample fork read 78k
    from cache), so their activity keeps part of it warm. The panel shows the main
    thread's own clock, which errs on the cold side.
- **Compactions.** `type: "system"` records with `subtype: "compact_boundary"`.
  `compactMetadata` has `trigger` (manual or auto), `preTokens` and `postTokens`, and the
  record's `timestamp` gives the time. `postTokens` counts only the messages kept: it
  leaves out the system prompt and tools, 40 to 60k under the real size across 25 of
  Doug's compactions. So the row's size after is the next main-thread response's context;
  before that response, the SDK host's estimate (below); with neither, `postTokens`
  labelled "msgs kept". A compaction before the span is never sized: the response after
  it went unread.
- **Context straight after a compaction (#54).** The transcript has nothing to size the
  context by until the next response. An SDK-hosted session's host records Claude Code's
  `get_context_usage()` (as /context reports it) after every turn, with the time
  (`sessions.context_tokens`, `context_max`, `context_at`), and the hub passes it to the
  module as each session's `context`. While a compaction is newer than the main thread's
  last response and the host's count is newer than the compaction, the gauge, the
  compaction row and the session list's bar show that count against `context_max`,
  marked `~`: it ran about 6% under the next response's (20,465 against 21,752). The
  next response makes it exact again. Tab sessions have no host and keep the transcript's
  size.
- **Histogram.** Every usage record in the span, from the main file plus the subagent
  files modified within it (checked by file `mtime`). Subagent tokens count, because they
  are this session's spend. They are left out of the context, cache and compaction rows,
  which describe the main thread.

## Refresh

- **Data is event-driven.** Each session gets a `UsageFollower` beside the existing
  `transcript.Follower`. It keeps a byte offset per file and reads only bytes appended
  since the last read, when `(size, mtime)` changes. That's checked on the existing
  1-second `refresh_data`. No new timer, and no rescan of a 6 MB transcript. One follower
  per session is shared by the pane and the session list's context bar (`stats.follower`,
  held weakly), so a transcript is read once for both.
  - **No full tail:** the 1 MB tail `Follower` holds can cover less than four hours of a
    busy session, so the histogram can't reuse it.
  - **First read:** the first time a session is shown, the follower reads from the start
    of the span: it seeks to the byte offset of the first record inside the window, found
    by a backwards scan that goes no further back than the main thread's latest response.
    Before that offset only compactions are picked out, by a byte search.
  - **Off the UI thread:** every read runs on a worker thread and swaps in a new snapshot
    when done; until a session's first read lands the pane says "reading …". Measured on
    a 159 MB transcript with 1,683 subagent files: the first read went from 3.3 s (on the
    UI thread) to 0.8 s (off it), and the steady per-second cost from 27 ms to 0.2 ms, since
    subagent files finished before the span aren't looked at again until the folder
    changes or 10 seconds pass (on the worker, so the re-listing costs the UI nothing, and a
    resumed subagent reaches the chart within 10 seconds).
  - **A failed read** (a transcript deleted between `stat` and `open`, say) is shown dimly
    in the pane; the worker never takes the app down with it.
  - **Skipped:** Claude Code's `<synthetic>` stand-in replies (errors, interrupts) and
    any response with no usage: no request was made, so they say nothing about the
    context or the cache.
- **Hour ticks** sit on local hours. A label that wouldn't fit at its hour at the
  right-hand end is left out, never moved off its hour.
- **The countdown is genuinely clock-driven.** Time passes without any event, so the
  cache row is repainted on the existing 1-second tick. It's one line of text.
- **The shimmer is animation.** It runs on the existing 0.1-second `animate` tick, every
  other frame (5 fps, the dashboard's rate). It only recolours the cells, without
  recomputing the columns. If typing in the answer box lags while it runs, the shimmer
  pauses while the box has focus. Measure first, since the title bar already shimmers on
  the same tick.
- **The shimmer needs 24-bit colour.** Its cells fade by 3 to 5 levels a frame. At 256
  colours each cell instead jumps 40 to 95 levels between two to four colours, which reads
  as obvious and jerky (#50). Windows Terminal draws 24-bit colour but leaves COLORTERM
  unset in WSL, so Rich picks 256; the CLI sets `TEXTUAL_COLOR_SYSTEM=truecolor` when
  `WT_SESSION` is set and nothing else says. The dashboard never hit this: it writes
  24-bit escapes whatever the terminal claims.

## Reuse or port

Port, don't import. `claude_dashboard.py` is a 5,200-line single-file script that renders
ANSI strings straight to the terminal and keeps view state in module globals (`VIEW_WINDOW`,
`VIEW_BUCKET`). The wheelhouse draws with Rich `Text` inside Textual. What carries over,
adapted, into a new `claude_wheelhouse/stats.py` (about 150 lines):

- `build_column`: the stacked column with eight sub-levels per cell, pure, copied as is.
- The `CO` palette, `shade`, and the shimmer wave
  `1 + 0.18 * sin(0.20*i + 0.45*row - 0.11*anim)` with the vertical gradient
  `0.5 + 0.5*row/(height-1)`, emitted as Rich styles instead of escape codes.
- `model_max_window` and `window_for` (`ctx_grade` too, until #51 replaced it).
- The usage parsing from `collect`, cut down to the fields above.

Copying about 150 lines between two of Doug's repos is acceptable for now. A shared
package is only worth making if a third consumer appears.

## Decided

Doug's answers, 8 Oct 2026:

1. **What the chart shows: context assembly** (read from cache, new input, cache miss).
   "you won't get 200 columns in that tab. Probably only 50-80. But we only really need a
   couple of hours; we can provide a click-in later (not now) to open a full dashboard."
   So the span is fixed at 2 hours and the bucket width is the span divided by the columns
   the chart has: about 2 minutes a column at 60 wide. The click-through to the full
   dashboard is for later.
2. **The cold-resume cost line: yes.** "yes let's see what it looks like". Once the cache
   is cold the cache row is followed by "next turn re-pays ~83k (~166k effective)": the
   context size, and that times the write price (2x for a 1h write, 1.25x for 5m).
3. **With no session in context: B**, totals across the running sessions: summed context,
   how many caches are warm and the soonest to go cold, and one chart of all of them.
