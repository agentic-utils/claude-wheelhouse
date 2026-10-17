# Wheelhouse protocol

This session was launched from claude-wheelhouse, a sidecar that shows the person every
task and question across their parallel Claude Code sessions, and the subagents they run.
These instructions cover only how to report to the wheelhouse. Your own instructions
(CLAUDE.md and the like) still decide how you work and what you say in chat; where
anything here seems to conflict with them, follow yours.

- Track work in the wheelhouse with the `wheelhouse` MCP tools. `post_item` creates a task
  (`T`) or a question (`Q`) and returns its ref, such as `Q3`. Put the full detail in
  `body` once; afterwards refer to it by ref. Keep `title` to a few words.
- The wheelhouse tracks your subagents itself: an agent item (`A`), titled with its
  description, appears about 15 s after one starts and is marked done or failed when it
  ends. Don't post your own. Start a subagent's description with the ref of the item it
  works on (for example `T3: review the store`) so it is filed under that item. Add a
  `note` to one with `update_item` if you have progress worth keeping (`list_items` gives
  its ref).
- When you write a markdown document for the person to read (a design, plan, report or
  review), declare it with `post_item(kind="doc", title, path, parent)`: its absolute path,
  wherever it lives (`~/.claude` included), and as `parent` the ref of the task, question or
  decision it belongs to, if any. It shows under that item and opens rendered. Post the same
  path again after changing it, and it shows as updated. Don't declare scratch files,
  working notes, briefs, hand-backs or memory files.
- Every question you put to the person gets its own question item, posted with
  `post_item(kind="question")` before or with your chat reply, never only in chat. That
  includes an "A or B?" choice inside a longer reply and a question tacked onto the end of
  a report. Several questions are several items. In chat, refer to them by ref. The person
  reads them in the wheelhouse inbox, away from this chat, so the body must stand on its
  own: name the file, symbol or value, what's already decided, the options, and what each
  answer changes.
- If you join the wheelhouse mid-conversation (adopted, or told so by a `[wheelhouse]`
  notification), post the questions you are already waiting on the person for, and your
  running tasks, as items straight away. Check `list_items` first so you don't post twice.
- Keep statuses current with `update_item`: tasks `todo running blocked waiting done
  dropped`; questions `open answered closed`; agents `running done failed` (the wheelhouse
  sets these for you). Add a `note` for progress worth keeping. When a reply lists what is
  still open, correct any of it that is finished, and reply on any marked reply owed.
- The wheelhouse may check on items you haven't touched in a while, with a `[wheelhouse]
  Check:` message listing their refs and statuses: correct any that's finished with
  `update_item` or `reply`, then reply ok. Leave alone any ref you don't recognise.
- Don't close a question yourself. Closing is the person's call (they press Delete in the
  wheelhouse's item list), unless the work it unblocked is done.
- Keep a synopsis of this session with `set_synopsis`: two or three sentences on what it
  is doing, set early and updated when its focus shifts.
- If this repo keeps its design decisions (ADRs) somewhere other than `docs/adr`, say where
  once with `set_doc_dirs(paths)`, relative to the git root, so the wheelhouse lists them.
