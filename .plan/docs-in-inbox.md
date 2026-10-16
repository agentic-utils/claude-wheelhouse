# Children in the inbox: agents and docs under their item

Sessions dispatch subagents for a task or question, and write markdown documents for the
person to read. Both now show in the inbox as children of the item that raised them,
indented under it, and a doc opens as rendered markdown.

The ask, in Doug's words: "The sessions often produce markdown docs; let's capture those in
the inbox as children of the elements that raise them (rendered similarly to how agents are
shown as children of questions or tasks) and if I click on one, render it properly. No need
for the textbox when viewing a document, I think."

## The parent link

`items.parent_ref` (added through `ADDED_COLUMNS`) holds the ref of an item in the same
session. Only a task, question or decision is ever a parent (`store.PARENTS`): one level of
nesting, no more. Rows written before the column existed have it NULL, and show as they
always did.

## Agents

An A item's parent comes first from the session's own word: the first ref of one of its
tasks, questions or decisions named anywhere in the subagent's description, else the ref
its prompt starts with (`store.agent_parent`): further into a prompt a ref may be one it is
told to leave alone ("unlike T2"). The protocol asks sessions to start a description with the item's
ref (`T3: review the store`). A session that posts T1 to T5 and then dispatches five agents
is the usual pattern, and only the refs can tell those apart.

With no ref named, it is a guess: the item the session last posted, updated or replied on at
or before the subagent started: the latest of each item's `created_at`, its `session_at` and
the session's own messages on it (replies, notes, status words, a host's replies), up to the
subagent's first transcript stamp. Stamps are to the second, so within the second it
started the first posted wins, not one posted just after the dispatch. The parent is set
once, as `track_agent` makes or matches the item, so later words on other items move
nothing. With neither, the agent sits in its session's group at the foot of its band, as
before.

"The session's running task" was passed over: an orchestrating session often has several
running at once, which is exactly where it would be ambiguous.

## Docs

A doc is an item of kind `doc`, ref prefix `M` (for markdown: D is taken), declared by the
session: `post_item(kind="doc", title, path, parent=<ref>)`. Doug, on detection: "The
sessions are too prolific in creating docs for us to rely on deterministic pattern
matching. Also I know that your sessions often actually create in ~/.claude and expect me to
read." So the wheelhouse watches no files: the session says which docs are meant for the
person, wherever they live (`~/.claude` included), and leaves out its scratch and working
notes. The protocol and the tool's docstring say so.

- The path must be absolute and the file must exist; anything else is refused, saying why.
  It is stored in the item's body.
- parent is optional: a task, question or decision of the same session. Without it the doc
  sits in the session's group, as an orphaned agent does.
- Posting the same path again from the same session updates that row: its title (and its
  parent, if given), and the status `updated` once the person has read it. It is never
  flagged `new` again. A doc not yet read stays `new`.
- Statuses: `new`, `updated`, `read`, all the person's. Being in sight for the dwell (as a
  decision is seen) makes it `read`. Delete dismisses it, like a settled task.
- `PROTOCOL_VERSION` 13: the new kind and the protocol line that asks for it.

## Rows and the collapse control

`tui.item_rows` places each child straight after its parent, in the parent's band, while the
parent shows; a child whose parent doesn't show (dismissed, or finished with F off) falls
back to its session's group in its own band. Children rows are styled as agent rows were
(`└ A3`, dimmed title, no session name). They sink and rise with the parent (Sink), as they
share its band.

A parent row with children shows a single `-` (expanded) or `+` (collapsed) on a grey
background in its line's last cell, drawn over the row by `ItemList._render_line` so it sits
at the right-hand end whatever the scroll. Clicking it toggles the children and does not
select or open the row (`_on_click` stops the event before DataTable's handler). Children
start expanded, and the state lasts for the app's run only. Hiding the children of a
selected child gives the selection to the parent.

## Delete on a parent

Doug: "a simple confirm dialog, then delete that item AND all its children. If any children
are open/unread, the confirm dialog should make that clear." Delete on an item with no
children behaves as before. On one with children not yet finished, a Confirm names how many
it takes, how many are still open (a question unanswered, a task or subagent not settled)
and how many unread (a decision unseen, a doc new or updated). Yes closes or dismisses the
parent and every child, whatever its status; No or Esc takes nothing. A parent that can't be
closed (a running task) still says so, as before.

Delete on marked rows asks the same, once for the whole selection, whenever any marked row
has children, and deletes each such row with its children.

Children deleted this way stay deleted (`Hub.delete_family`, one daemon route): each is
marked `items.dismissed = 'deleted'` (`store.DELETED`), finished ones included (one
already dismissed would otherwise come back when posted again), which `standing` counts as finished
whatever the status, and which a status change, a doc posted again or a subagent finishing
leaves in place, where an ordinary dismissal clears. The confirm says so. Ctrl+R on one
brings it back. A session still on older code clears the mark when it changes the item's
status, as its own update_item predates it.

## The doc viewer

Selecting a doc shows its ref, title, status and path, then the file rendered with Rich's
Markdown through the same `Blocks` renderer as a thread, in the item pane and full screen
(`DocView`, Enter), with no answer box. Doug tried Textual's `MarkdownViewer` first and
chose Rich (Q100): `MarkdownViewer` mounts a widget per block, which took about 7 s of CPU
to first show a 14 KB doc, and once mounted slowed every layout pass, even with another item
selected. Rich has no clickable links or contents list.

Reading goes through the hub, so it works through the daemon too: `Hub.document(sid, ref)`
reads only the path stored on the item, never one a client sends, opens only a regular file
(checked by stat and again on the opened descriptor, opened without blocking, so a FIFO or
device swapped in never holds the daemon), caps a read at 1 MB, and re-reads only when the
file's mtime or size changes. The daemon serves it at `/items/{sid}/{ref}/document`; the
remote client polls it while shown, as it does a subagent's conversation, and the daemon
answers "same" when its stamp hasn't moved. A file that has gone, or isn't a regular file,
shows a clear line saying so, over the last text read, if any.

Measured headless, CPU time on the app's loop, synthetic docs of headings, prose, lists,
tables and code blocks:

| Doc | First show, pane | First show, full screen | Refresh after |
|---|---|---|---|
| 14 KB | 0.7 s | 0.9 s | 0.05 s (0.08 s full screen) |
| 42 KB | 1.8 s | 2.0 s | 0.05 s (0.26 s full screen) |

Moving between tasks costs about 0.05 s, and back to a doc already shown about 0.1 s. A
change to the file, or to the pane's width, renders it again.

A doc over 2000 characters is rendered on a worker thread (`Transcript.render_off_loop`),
showing "rendering…" meanwhile; a render for a width or text since left is dropped, and the
block laid out again. The loop's worst stall while showing a doc fell from 743 ms to 334 ms
at 42 KB, and from 168 ms to 85 ms at 8 KB (a width change: 793 to 188 ms, 184 to 70 ms).
What is left is the render thread holding the interpreter lock.

## Mixed versions

Old code has no `doc` kind in `store.SETTLED`, so an app on old code with its hub in
process fails on every refresh once a session relaunched on new code posts a doc. The
fingerprint restart only covers a daemon. Nothing the MCP server can read says what code
the person's app runs, so the README asks for the app to be restarted straight after an
update.
