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

An A item's parent is the task, question or decision its session last posted, updated or
replied on at or before the subagent started (`store.agent_parent`): the latest of each
item's `created_at`, its `session_at` and the session's own messages on it (replies, notes,
status words), up to the subagent's first transcript stamp. It is set once, as
`track_agent` makes or matches the item, so later words on other items move nothing. With
none, the agent sits in its session's group at the foot of its band, as before.

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
closed (a running task) still says so, as before. A running subagent dismissed this way
comes back, settled, when it finishes, as any dismissed item does on a status change.

## The doc viewer

Selecting a doc shows its path and status, then the file rendered with Textual's
`MarkdownViewer` (Doug: "Try the Textual viewer and let's see how it performs"), in the item
pane and full screen (`DocView`, Enter), with no answer box. Rich's Markdown through the
existing `Blocks` path was the alternative: faster on long documents, as `MarkdownViewer`
mounts a widget per block, but with no links or contents.

Reading goes through the hub, so it works through the daemon too: `Hub.document(sid, ref)`
reads only the path stored on the item, never one a client sends, caps a read at 1 MB, and
re-reads only when the file's mtime or size changes. The daemon serves it at
`/items/{sid}/{ref}/document`; the remote client polls it while shown, as it does a
subagent's conversation, and the daemon answers "same" when its stamp hasn't moved. A file
that has gone shows a clear line saying so, over the last text read, if any.

The pane's viewer has no contents list; the full screen's has one. Links: a web link opens
in the browser, an anchor scrolls to its heading, and a link to a file is not followed
(MarkdownViewer would load it from the app's own disk, which under `--connect` is not the
doc's machine).

## How MarkdownViewer performs

Measured headless (CPU time on the app's loop, synthetic docs of headings, prose, lists,
tables and code blocks):

| Doc | Blocks | First show | Refresh while shown | Back to the same doc |
|---|---|---|---|---|
| 1 KB | 50 | 0.7 s | 0.06 s | 0.05 s |
| 14 KB | 500 | about 7 s | 0.06 s | 0.05 s |
| 42 KB | 1500 | over 10 s | about 2.5 s | 0.7 s |

Moving between tasks costs about 0.05 s. The cost is in mounting a widget per block, so it
comes again each time the file changes. A doc kept mounted costs nothing more until it is
big; past a thousand blocks or so every layout pass slows, even with another item selected
(the last doc stays mounted, hidden, so going back to it is quick): about 1.5 s a refresh
at 42 KB. If this is too slow in use, the
cheap change is Rich's Markdown through `Blocks` in the pane, keeping MarkdownViewer for
full screen only.
