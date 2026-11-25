# UI responsiveness under CPU load: review (#385)

Reviewed at `main` 1e72e3f, 25 Nov 2026. Line numbers are that commit's. Everything measured comes from the bench in `.plan/385-bench/` (section 1 says how to rerun it). Estimates are marked as estimates.

## Verdict

The app is quick at the one thing earlier rounds tuned (a key echoes in 9 ms median on a quiet core) and slow at almost everything else, with or without load. Three causes account for nearly all of it. First, about 35 actions send their command to the daemon from the UI thread and then refetch the whole inbox (2.6 MB of JSON here) before anything is painted, so the "accepted" signal arrives after the round trip: Alt+P takes 773 ms median on a quiet core and 2.5 to 4 s once the core is shared. Second, the app is never idle: with 30 busy sessions it spends 13 to 20% of a core and writes 70 KB a second to the terminal just animating, and opening a dialog costs about a third of a second of pure UI-thread work, so every competitor for the CPU multiplies costs that are already too high. Third, the app runs at the same priority as the sessions it launches, which are the load: with the competing process at nice 10, every number under load returned to its quiet-core value. None of this needs a new framework, a new Python or a Textual upgrade. It needs the acknowledgement painted before the work, the work off the UI thread, less work at rest, and the launched sessions run below the UI.

## Top 5 fixes

| # | Fix | Gain | Size | Risk |
|---|---|---|---|---|
| 1 | Paint the acknowledgement first, send the command on a worker (one helper, about 35 call sites) | Measured cost today: pin 773/1049 ms (p50/p95) quiet, 1395/1846 with one busy loop, 2716/3970 on a shared core. Estimate after: one frame, about what a click's highlight takes now (47/148 ms quiet) | M to L | Medium: key ordering (`land`, `settle`), undo on failure |
| 2 | Run launched sessions (and the daemon) below the UI: `os.nice` before `launch.py:446` execs claude | Measured: with the load at nice 10, dialog 425 to 331 ms, click 48 to 42, pin 1395 to 776, burst p95 559 to 221: quiet-core values | S | Low. Sessions' own builds yield to anything un-niced: Doug's call |
| 3 | Animation that yields: rest for a second after any input, and drop to 1 fps when the loop runs late | Measured (animation off, upper bound): app at rest 13-20% to 4% of a core, terminal output 70 to 3.7 KB/s; under load burst p95 559 to 74 ms, click-to-Detail p95 396 to 222 | S | Low |
| 4 | Stop refetching the whole inbox after every command and every change: send what changed | Measured cost today: `GET /inbox` 107-153 ms and 2.57 MB per fetch on a quiet core, on the UI thread after each command. Estimate after: under 10 ms | M | Medium: protocol change, both ends |
| 5 | Dialogs: show the box first and shade a frame later (or keep the shaded lines from the last open), reuse the dialog widgets | Measured cost today: New session 325/584 ms quiet, 425/716 one loop, 1070/3144 shared core; the shade is 39% of the UI thread's time in that phase, layout 28%. Estimate after: about 120 ms quiet | M | Medium: `Dialog.render_lines` leans on Textual internals |

The rest of the list is in section 5.

## 1. Measured baseline

### Method

`ptybench.py` runs the real app (`python -m claude_wheelhouse tui`, Textual's Linux driver with its input and writer threads) in a 220x60 pseudo-terminal it owns, against a scratch daemon and state directory. It writes the bytes a terminal would send (keys, SGR mouse reports) and times from that write to the arrival of the output that shows the app took them. It answers the synchronized-output query as Windows Terminal does. It leaves out the terminal emulator's own latency (Windows Terminal adds a median of about 67 ms per the source in section 4) and the keyboard.

| Metric | Input | Timed to |
|---|---|---|
| key | a letter not used before, one every 150 ms | that letter in the output |
| burst key | 36 letters at 12 a second | each letter's echo |
| hover | pointer report on an inbox row | that row drawn again |
| click | press and release on an inbox row | that row drawn again (the highlight) |
| click detail | the same click | the item's body text in Detail |
| arrow, arrow detail | Down in the inbox | the row the cursor lands on drawn; the body in Detail |
| dialog, close | Alt+N; Esc | the New session title; the next frame over 1500 bytes |
| pin | Alt+P on an item (a daemon command) | that row drawn again |

Data: 40 sessions (30 live, each with an animated eye), 4000 items, three 1500-turn transcripts (`seed.py`). The store is static: no session is posting items during the run.

Load, as Doug asked: the app, its daemon and one busy loop are all pinned to core 13 (`taskset -c 13`), each burst at most 55 s. The bench records what ran on the core in each phase: the app, the daemon, the loop, and "others" (the rest of the core's busy time). That last figure matters: the machine was at a load average near 20 throughout, and other sessions' processes landed on core 13 for whole runs. I have grouped runs by what was on the core, not by what I asked for.

To rerun:

```
cd .plan/385-bench
./setup.sh                 # seeds ~/.claude/scratchpad/385-ui-perf-review/home, daemon on port 7461 (takes about 4 min)
./pin-daemon.sh 13         # daemon restarted on core 13
./run.sh idle --core 13    # results in the scratchpad as idle.json
./run.sh load --core 13 --load 1
./run.sh prof --core 13 --load 1 --prof /path/out.prof.json     # with the UI-thread profiler
./teardown.sh
```

### Results (p50 / p95, ms)

Thirty events per metric (dialog 15, pin 20, burst 36), one run per column, so a p95 is close to the second-worst event. Columns A, B and E ran with the profiler attached (same overhead in each). "Missed" means nothing drawn within 4 s.

| Metric | A: quiet core | B: one busy loop | C: one busy loop, second run | D: core shared with other sessions' processes, no loop | E: one busy loop at nice 10 |
|---|---|---|---|---|---|
| Run | prof-idle | prof-load | load1-c | idle-c | exp-nice10-load |
| Others on the core | 1-5% | 0-9% | 0-1% (hover: 14%) | 20-61% | 0-2% |
| key | 8.8 / 31 | 17 / 39 | 33 / 147 | 74 / 1328 | 9.5 / 39 |
| burst key | 7.4 / 27 | 20 / 559 | 38 / 313 | 92 / 383 | 12.5 / 221 |
| hover | 28 / 62 | 32 / 173 | 439 / 791 | 473 / 920 | 18 / 98 |
| click (highlight) | 47 / 148 | 48 / 197 | 78 / 1012 | 369 / 1493 | 42 / 105 |
| click detail | 89 / 335 | 120 / 396 | 213 / 1315 | 1067 / 2008 | 61 / 162 |
| arrow | 16 / 93 | 93 / 156 | 20 / 152 | 393 / 1326 | not run |
| arrow detail | 71 / 232 | 93 / 156 | 95 / 247 | 807 / 1849 | not run |
| dialog open | 325 / 584 | 425 / 716 | 933 / 1303 | 1070 / 3144 | 331 / 498 |
| dialog close | 72 / 315 | 73 / 418 | 109 / 550 | 153 / 810 | 44 / 144 |
| pin | 773 / 1049 | 1395 / 1846 | 2478 / 3934 | 2716 / 3970 (8 of 20 missed) | 776 / 1313 |

Worst run seen (load1-pin13-b: the busy loop and other sessions' processes together): key 88 / 662, burst key 849 / 1307, hover 452 / 1510, click 626 / 2691, click detail 2321 / 3723, dialog 3576 / 3825, pin 2433 with 7 of 11 missed.

Reading it:

- Column D is the nearest to Doug's complaint: no synthetic load at all, just a core shared with the rest of a busy machine. Hover and click take half a second, a dialog a second, a pin three.
- One busy loop alone (B) hurts less than D. The kernel gives a task that mostly sleeps a fair turn against one competitor. Several competitors, each taking a slice before the UI runs again, is what produces the seconds.
- B and C are the same condition and differ by up to 2x. The machine was busy on every other core too (shared caches, hyperthread siblings). Treat single figures as plus or minus a factor of two, and the pattern across columns as the finding.
- Quiet-core numbers are themselves poor for anything but typing: 325 ms for a dialog and 773 ms for a pin are over the 100 ms "instant" limit with nothing else running.

At rest (nothing touched, quiet core): the app used 13-20% of the core and the daemon 3-4%; the app made 18-25 writes and 70 KB of output a second.

Invalid metric, kept out of the table: `sweep_settle` (a pointer swept down the inbox) reads about 600 ms in every condition because it catches the row tooltip, which appears half a second after the pointer rests. The sweep phase is still used for its CPU cost.

## 2. Where the UI thread's time goes

Method: `profapp.py` samples the UI thread's stack every 4 ms inside the real app and reads that thread's CPU clock. The sampler needs the GIL, so it sees long stretches better than short ones and the "share" figures are of samples where the thread was busy. Phase boundaries are good to about a second. Shares are from the one-busy-loop run unless noted.

| Phase | UI thread CPU (quiet / loaded) | Where (share of busy samples) |
|---|---|---|
| At rest (quiet run) | process 13-20% of a core | Compositor repaint from the screen's update timer 73%, of which `DataTable.render_lines` 41%: the session rows' eyes and dots (`SessionList._render_line`, tui.py:1468, lines 1482-1497, 16%). The 1 s tick `refresh_data` (tui.py:5968) 17.5%, of which the module panes' tick hooks (`each_pane`, tui.py:7770 line 7779) 12.5% |
| Pin (Alt+P) | 8.8 s / 9.0 s over 20 pins, but 19.3 s of busy wall time under load | `toggle_now` (tui.py:8920) 58%, all of it blocked in `client.request` (client.py:333, line 341) reading the daemon's answer. `refresh_data` then `client.refresh` (client.py:405) then `catch_up` (client.py:431) then `fetch` (client.py:494) 47%: the forced `GET /sessions` and `GET /inbox`. `paint_items` (tui.py:6366) 11% |
| Dialog open and close | 13.2 s / 10.8 s over 15 cycles | `Dialog.render_lines` (tui.py:3359) 39%, of which `lines_beneath` (tui.py:3375, line 3392) 32%: the screen beneath rendered again for the shade. `Screen._refresh_layout` 28%. Stylesheet `apply` 8%. `new_session` (tui.py:9741) 7% |
| Click on a row | 6.0 s / 4.3 s over 30 clicks | Compositor `_get_renders` 44%. `Screen._refresh_layout` 28%: a row click triggers a layout pass. `settle` / `highlighted` / `select_row` (tui.py:8591, 8583, 8618) 14%, with `paint_detail` (tui.py:7223) 7%. Synchronous `GET` for the thread (`client.get`, client.py:354) 5%. `each_pane` 10% |
| Pointer sweep (600 moves) | 5.5 s / 3.4 s over 15 s | `DataTable.render_lines` 35% (the hovered row drawn again per move), `_render_line` (tui.py:1468) 10%. About 5 ms of UI thread per move, each move handled in turn |
| Typing burst | 0.6 s / 0.8 s over 6-7 s | The tick's `refresh_data` 26% (`each_pane` 13-14%), `TextArea.render_lines` 11%. Typing itself is cheap; the tick is what a keystroke waits behind |

Two measurements outside the profile:

- `GET /inbox` for 4000 items: 107-153 ms and 2,571,137 bytes of JSON per fetch on a quiet core (seven fetches, daemon and client both on core 13). `GET /sessions`: 2-5 ms, 84 KB.
- UI thread CPU against busy wall time in the pin phase under load: 9.0 s of CPU in 19.3 s busy. The other 10 s the thread sat in a socket read waiting for a daemon that was itself queued for the core.

What I did not find: the app's own worker threads holding the GIL against the UI thread. In every phase the other threads were asleep (the stream's socket read, condition waits). That may differ with live sessions, see "not measured" below.

## 3. Interactions without immediate "accepted" feedback

Shared mechanism, checked in the code:

- Every `client.<command>()` is a synchronous POST on the caller's thread (`request`, client.py:333-342; `command`, client.py:359-390), with a 10 s read timeout and a 0.5 s sleep and retry on a lost answer (client.py:379).
- `command` sets `forced` (client.py:384-386), so the next read (`refresh_data`, tui.py:5968, called straight after most commands) fetches `/sessions` and `/inbox` on the UI thread (`catch_up`, client.py:431-456). One press is three round trips before a frame is painted.
- Busy buttons (#107): `press()` (tui.py:7943) is called only after the daemon answers ok (for example `host_command`, tui.py:9646-9650), so a button greys out after the round trip, not on the press.
- The press flash: a key runs `button._start_active_affect()` and then dispatches `Pressed` inline (tui.py:9807-9808). The handler blocks before a frame is painted, and the flash lasts 0.2 s, so on a slow round trip it is never seen (inferred from the code, not observed).
- A dialog's confirm runs its callback before the frame that removes the dialog (Textual's `dismiss`), so the dialog looks stuck for the length of the round trip (inferred).
- Busy is drawn as plain `disabled` (tui.py:5195-5198): no way to tell "under way" from "doesn't apply". A click on a busy button says nothing; only the key path says "still under way" (tui.py:9799-9802).

Acknowledged only after a blocking daemon call on the UI thread:

| Interaction | Handler | Blocking call |
|---|---|---|
| Submit answer (Ctrl+Enter, Alt+S) | tui.py:9325-9343 | `submit` :9340; the box is cleared only after (`submitted`) |
| Send, Send all, Mode | tui.py:9490-9526 | `send_queue` :9492, `send_all` :9497, `toggle_mode` :9507 |
| Wake and send | tui.py:9478-9487 | `wake_and_send` :9482 |
| Recall (Ctrl+R) | tui.py:9359-9385 | `unqueue` :9378 |
| Reopen | tui.py:9394-9406, 9186-9203 | `close` per row :8982, `delete_family` :9195 |
| Allow, Always, Deny | tui.py:9528-9560 | `answer_permission` :9083 |
| Relaunch, Unpark | tui.py:8483-8532, 9992 | `relaunch` :8485, `stop_host` / `compact_then_relaunch` :8530, `unpark` :9992 |
| Park, Forget (ask, act, cancel, force) | tui.py:10111-10135 | `ask` :10113, `act_on_dead` :10121, `cancel_request` :10128, `force` :10133 |
| First press of Relaunch, Forget, Compact | tui.py:7723-7745 | `steps_table` GET :7730, then `choose_steps` :7740 |
| Interrupt, Compact, Handover | tui.py:9562-9588, 9646-9650 | `host_command` :9648 |
| Compact now, Leave it, Keep waiting, Abort | tui.py:9652-9701 | `close` :9661, `notes_wait` :9674, `host_command` :9697 |
| Model | tui.py:9714-9718 | `set_model` :9716 |
| Effort slider | tui.py:7805-7815 | `set_effort` :7809, once per step |
| Rename | tui.py:10010-10013 | `rename` :10012 |
| Fork (open, confirm) | tui.py:10026-10047 | `fork_plan` GET :10035 before the dialog shows, `fork` :10043 |
| New session, Launch | tui.py:9748-9752 | `new_session` :9751 |
| Adopt | tui.py:9759-9766, 3988-4000 | `adoptable` GET :9762, `running_pid` GET :3994, `adopt` :9765 |
| Pin (Alt+P, pin column) | tui.py:8915-8932 | `set_pinned` :8927 (measured above) |
| Drag reorder, on drop | tui.py:6844-6848 | `reorder_items` / `reorder_sessions` |
| Sort header click | tui.py:6798-6812 | `set_setting` :6773 before the repaint |
| Splitter release, pane swap, column weights, doc layout, reset layout | tui.py:9720-9724, 7590, 7675, 7427, 7694-7701 | `set_setting` each |
| Select an inbox row whose thread or doc isn't cached | tui.py:8619-8631, paint_detail :7222-7240 | `thread` / `agent_conversation` :6904-6905, `document` :7236, `read_doc` :7230. The cache is dropped after every command (client.py:438-440) |
| Open a doc full screen | tui.py:2637, 2761-2765 | `thread`, `read_doc`, `document` |
| Save doc (Ctrl+S) | tui.py:7123-7155 | `write_doc` :7134 |
| Open the doc editor | tui.py:6913-6921 | `grant_doc` :6917 |
| Run command | tui.py:9594-9629 | `thread` :9606 and `shell_dir` :9610 before the dialog, `run_command` :9622 |
| MCP servers | tui.py:9945-9956, 3854-3868 | `mcp("check")` :9955 before the dialog, `mcp` :3860, :3867 |
| Session info | tui.py:10015-10024 | `session_info` GET :10018 |
| Session steps | tui.py:7714-7721 | `steps_table` :7721, `set_step` :7718 |
| Tour next, end, invite | tui.py:8210-8217, 8246, 8376-8379, 9288, 9299 | `tour_at`, `see_features`, `answer_offer` |
| Module send | tui.py:7075-7081 | `submit` :7079 |

Acknowledged only when a worker or the 1 s tick lands: Undo (tui.py:9014-9058, nothing until `undo_landed` :9060); Relaunch all (tui.py:9976-9977, no `press`, rows change at the next tick); Shell (tui.py:9631-9644, no change in the app at all); MCP sign-in (tui.py:3854-3860); older conversation page (tui.py:7327-7343, no "loading" mark).

No acknowledgement: keys with no button on screen (Ctrl+Enter, Alt+S, Ctrl+S, Ctrl+R, Alt+P) have nothing to flash; a click on a busy (disabled) button.

Already right, and the pattern to copy: Delete (`delete_off_thread`, tui.py:8790-8838: rows go at once, the command on a worker), Mark finished, Show finished, search, copy, dialogs that need no fetch, the PRs pane's merge / close / ready, the conversation's "reading..." note (tui.py:7447-7461).

Hover: the app's only `:hover` rules are the session pane's buttons (tui.py:5189) and the splitters. Lists shade the hovered row. Textual's `DataTable._on_mouse_move` redraws the hovered row on every pointer report, even within one row; `_kept_line` (tui.py:1347) softens it. Pointer reports are not coalesced (section 4).

Spot-checked against the code by me: `toggle_now`, `action_submit`, `host_command`, `action_press`, `command` / `catch_up`. The rest of the table is a subagent's reading, not re-verified line by line.

## 4. Community techniques

| Technique | Source | Fits here? |
|---|---|---|
| Respond within 0.1 s to feel instant; 1 s keeps flow; busy indicator from 1 s | https://www.nngroup.com/articles/response-times-3-important-limits/ | Yes. It is the target for fix 1: the press shows within a frame whatever the work takes |
| Feedback within 400 ms (Doherty) | https://lawsofux.com/doherty-threshold/ | Yes, as the outer bound for a result |
| "20ms feels fine, 50ms feels laggy, and 150ms feels unbearable"; under load terminals degrade in the tail, not the median | https://danluu.com/term-latency/ | Yes. Budget 50 ms key-to-paint and judge p95 under load. Typing meets it on a quiet core only |
| Windows Terminal adds a median 66.7 ms keypress latency | https://chadaustin.me/2024/02/windows-terminal-latency/ | Context: a floor outside the app. It makes every ms inside it count more |
| Run CPU work in processes, not threads: threads share the GIL | https://docs.python.org/3/library/concurrent.futures.html ; https://github.com/python/cpython/issues/52194 | Partly. No GIL contention showed in this bench, but a 2.6 MB `json.loads` on the fetch thread holds the GIL for its whole length (inferred). Fix 4 removes the need |
| `sys.setswitchinterval` below the 5 ms default | https://docs.python.org/3/library/sys.html | Not now: no evidence of a GIL convoy in the profile. Hook left in `profapp.py` (`EXP=switch1ms`) to test with live sessions |
| Process priority: each nice step is a factor of 1.25 in CPU share; SCHED_BATCH / SCHED_IDLE for background work | https://man7.org/linux/man-pages/man7/sched.7.html | Yes, measured (column E). Checked here: `ulimit -e` is 0, so the UI cannot raise itself, only lower the others; autogroup is absent on this WSL kernel, so nice works across sessions |
| cgroup `cpu.weight` | https://docs.kernel.org/admin-guide/cgroup-v2.html | Maybe later. Nice does the job without needing systemd in WSL |
| EEVDF time-slice hints via `sched_setattr` | https://docs.kernel.org/scheduler/sched-eevdf.html | No: no Python wrapper, small gain over nice |
| Textual's compositor updates only the changed region; keep refresh regions small; Line API | https://textual.textualize.io/blog/2024/12/12/algorithms-for-high-performance-terminal-apps/ ; https://textual.textualize.io/guide/widgets/ | Already used (eyes redraw one cell, #338). The gap is `_refresh_layout` on a row click and the dialog's shade |
| `TEXTUAL_SLOW_THRESHOLD` logs slow handlers; `TEXTUAL_FPS`; `TEXTUAL_ANIMATIONS` | https://textual.textualize.io/api/constants/ | Yes as a free diagnostic. The app's own animation is separate from Textual's and is the one that costs |
| Coalesce pointer-move events: Textual replaces only Resize, Update, Layout and scroll messages; every MouseMove is handled in turn | https://raw.githubusercontent.com/Textualize/textual/main/src/textual/message_pump.py (`can_replace`) | Yes, small. Tried (`EXP=coalesce`): UI thread CPU in the sweep fell from 5.5 s to 3.9 s on a quiet core (one run); no latency figure, the sweep metric being invalid |
| Render at a fixed frame rate and draw once per batch of events | https://ratatui.rs/tutorials/counter-async-app/full-async-events/ ; Bubble Tea's renderer https://raw.githubusercontent.com/charmbracelet/bubbletea/v1.3.4/standard_renderer.go | Textual already paints on a timer. The lesson that applies is fix 3: frames are for input first |
| Synchronized output (mode 2026) | https://gist.github.com/christianparpart/d8a62cc1ab659194337d73e399004036 | Automatic in Textual when the terminal answers the query. Worth one check that Doug's Windows Terminal does |
| `textual-speedups` (Rust geometry classes) | https://github.com/willmcgugan/textual-speedups | Maybe: a cheap experiment, its author says the gain "is yet to be determined" |
| TextArea key path profiled with pyinstrument | https://textual.textualize.io/blog/2023/09/18/things-i-learned-while-building-textuals-textarea/ | Method only. Typing is already the fast path here |
| DataTable slows with size | https://github.com/Textualize/textual/issues/5951 | Known; `_kept_line` and `fill` already work round it |
| uvloop | https://github.com/Textualize/textual/issues/2051 ("I don't think UVLoop benefits us much") | No |
| Free-threaded Python 3.13/3.14 | https://docs.python.org/3/howto/free-threading-python.html | No: single-thread overhead, and no statement of support from Textual or Rich found |
| `gc.freeze` | https://docs.python.org/3/library/gc.html | Already done (tui.py:5554) |

Not verified at a source: Windows Terminal's version for mode 2026 support; any WSL2-specific scheduling caveat. If the competing load is on the Windows side, nothing in the priority rows helps.

## 5. Ranked fixes

Gain is measured where a bench column or experiment is named, otherwise an estimate.

| # | Fix | Expected gain | Size | Risk |
|---|---|---|---|---|
| 1 | Acknowledge, then work. One helper used by every command: `press()` or the optimistic row change first, the POST and `fetch_now` on a worker, `land` when it answers, the press undone with a toast on failure. `delete_off_thread` (tui.py:8790) is the template. Start with Submit, Send, Pin, Relaunch, Park, Interrupt, Compact, permissions | Estimate: every row of section 3 acknowledges in one frame. Measured cost removed: pin 773 ms quiet to 2.7 s shared | M to L | Medium |
| 2 | Launched sessions and the daemon below the UI: `os.nice(n)` before the exec at `launch.py:446`, the same for hosts and the daemon, as a setting | Measured (column E against B) | S | Low; a visible trade-off |
| 3 | Animation that yields: rest for 1 s after any key, click or pointer move (today only while the answer box has focus, tui.py:5930), and fall to 1 fps while a timer fires more than 20 ms late | Measured upper bound (animation off): rest CPU 13-20% to 4%; quiet click 47 to 33 ms, click detail 89 to 54; loaded burst p95 559 to 74, click detail p95 396 to 222 (one run each) | S | Low |
| 4 | Inbox deltas: a command's answer carries the rows it changed, and the stream's fetch asks for changes since a version, not all 4000 items | Measured cost removed: 107-153 ms per fetch. Also ends the cache drop after every command (client.py:438-440) that makes the next row click fetch its thread again | M | Medium |
| 5 | Dialog open: first frame without the shade, shade on the next idle frame or from kept lines; keep one instance of each plain dialog mounted and hidden | Estimate: 325 to about 120 ms quiet, from the shade's 39% and part of the layout's 28% | M | Medium (Textual internals; pinned in pyproject) |
| 6 | No layout pass on a row click: find which `refresh(layout=True)` or size change `select_row` / `paint_detail` causes and avoid it when the boxes keep their size | Estimate: up to 28% of a click's UI time, about 25 ms of 100 | S to M | Low |
| 7 | The tick waits for input: skip `refresh_data` within 300 ms of a key or click, and time each module pane's `tick` (`each_pane` is 10-14% of UI time in most phases) | Estimate: removes the 100 ms-class stall a keystroke can land behind; burst p95 under load is where it shows | S | Low |
| 8 | Fetch a thread or doc on a worker with a "reading..." note, as conversations do | Estimate: removes a synchronous GET (5% of click time here, far more on a shared core) | S | Low |
| 9 | Coalesce pointer moves: drop a MouseMove when a newer one is already queued | Measured: sweep CPU 5.5 s to 3.9 s quiet, 3.4 to 3.0 loaded (one run); latency gain unmeasured | S | Low to medium (drags need the last move, which they keep) |
| 10 | A busy look distinct from disabled (dimmed caption plus a moving mark), the same on a click as on a key, and a "still under way" tooltip | Perceived only | S | Low |
| 11 | Keep it won: add `ptybench.py` phases to `tests/test_lag.py` with budgets, and log the loop's lag p95 in the app | No direct gain; stops round five | S | Low |

Not worth doing now: uvloop, free-threaded Python, a Textual upgrade, GC tuning, moving rendering to subprocesses (no GIL contention found; revisit after fix 4 if live sessions show it).

## 6. Earlier rounds: what held and what didn't

- #268 (PR #269, #270, #275), paint on change, index, shade drawn where changed: held for the tick. The shade is still the largest single cost of opening a dialog (39%), because on open every line beneath is new.
- #327 (PR #338), "work off the UI thread": true for Delete, Undo, the dwell's seen mark, conversation fetch and big renders. About 35 command paths still block (section 3). `quietly`'s docstring says "off the UI thread" but only its refetch is. The 5 fps cap still leaves 13-20% of a core spent at rest with 30 busy sessions.
- #366 (PR #373), focus moves: not re-measured here.
- All three rounds measured with Textual's headless pilot. This bench goes through the real driver in a pty, which is why its quiet-core key figure (9 ms) is far below the earlier 124 ms (that was mostly pilot overhead, as PR #269 noted).

## 7. Not measured

- Live sessions. The store was static, so the stream-driven refetch of the inbox, the module panes reading transcripts, and any GIL contention they cause never ran. This is the most likely gap between these numbers and Doug's worst moments. A churn generator (a process posting items) would close it.
- Windows Terminal and ConPTY: what 70 KB a second of output costs on the Windows side.
- Conversation rows (a 1500-turn transcript in Detail), the doc editor, `--local` mode, `--connect` to another machine.
- Controlled multi-competitor load. Column D is other sessions' processes, uncontrolled. Doug's one-loop method is clean but mild; three loops on the one core would reproduce D on demand.

## Files

- `.plan/385-bench/ptybench.py`: the latency bench. `profapp.py`: the UI-thread profiler and the experiment switches (`EXP=coalesce`, `EXP=switch1ms`). `seed.py`, `layout.py`, `setup.sh`, `pin-daemon.sh`, `run.sh`, `teardown.sh`, `env.sh`: the scratch home and daemon.
- `.plan/385-bench/results/`: each run's JSON (the summary, every raw latency, what ran on the core per phase) and the two profiles.
