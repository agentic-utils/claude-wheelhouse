# Docs section and doc threads (#106)

Docs are reference material. They get a section of their own in the inbox, where clearing the inbox can't reach them. Each doc also gets a discussion thread, so the person can read a doc and type questions or suggestions about it right beside it.

Status: rough draft for Doug's review. The mock-ups are what to react to.

## 1. The Docs band

Docs move out of the inbox's status bands into a band of their own at the foot of the list. The band's header row collapses like a session's children (- and +).

```
 Inbox                                                  ▲
 ● T3   Modules stage 7                  doug-f5   run
 ? Q125 Docs section: band or pane?      doug-f5   open
 ✓ T122 Fresh-clone smoke test           doug-f5   done
 - Docs (3)
 ≡ M2   Tour: features not yet shown     doug-f5   2 new
 ≡ M1   Tutorial overhaul plan           doug-f5
 ≡ M3   Docs section design              doug-f5
```

- The sort applies inside the band. The band itself always stays at the foot.
- The session filter narrows the band too.
- Ctrl+A ("every finished row") never marks a doc row.
- A multi-select Delete skips doc rows and says so in the notice ("2 closed, 1 doc kept").
- Delete on a single doc row asks first: "Close doc M2? The file stays on disk." The default is No.
- A doc with replies you haven't seen shows "n new" and stays in the band. It doesn't jump into the active band.
- A doc with a message from you that hasn't been answered yet counts as awaiting a reply, like a question. Delete warns about that as well.

## 2. Detail for a doc: three states

The layout setting is **split** (the default) or **tabbed**. You switch it with Ctrl+L while a doc is shown. The setting is kept with the other layout settings, and full screen (Enter) uses the same one.

### 2a. Split (default)

The doc takes the upper half of Detail. Its thread and the answer box take the lower half. A splitter between them can be dragged, and the share is kept as `layout.doc-thread`.

```
 Detail · M2 Tour: features not yet shown
┌──────────────────────────────────────────────────────┐
│ # Wheelhouse tour: features not yet demonstrated     │
│                                                      │
│ This is a survey of claude-wheelhouse at main ...    │
│                                                      │
│ ## Recommendation                                    │
│ **Add as tour steps or tips (must).** These are ...  │
├───────────────────────── thread ─────────────────────┤
│ doug   > "Add as SDK-only tips"                      │
│          Interrupt needs a demo turn long enough...  │
│ holly    Agreed: the story turn is about 20 s.       │
│ ┌──────────────────────────────────────────────────┐ │
│ │ Type a comment on this doc...                    │ │
│ └──────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
```

### 2b. Tabbed

One area, with two tabs across the top. Ctrl+PageUp and Ctrl+PageDown switch tabs (Textual's usual keys), and a click on a tab works too. The Thread tab shows its count of new replies.

```
 Detail · M2 Tour: features not yet shown
┌──────────────────────────────────────────────────────┐
│ [ Doc ]   Thread (2 new)                             │
├──────────────────────────────────────────────────────┤
│ # Wheelhouse tour: features not yet demonstrated     │
│                                                      │
│ This is a survey of claude-wheelhouse at main ...    │
│                                                      │
│ ## Recommendation                                    │
│ **Add as tour steps or tips (must).** These are ...  │
│                                                      │
│ ## Not yet in the tour                               │
│ | Feature | Trigger | What the step would show | ... │
│                                                      │
└──────────────────────────────────────────────────────┘
```

Typing on the Doc tab moves you to the Thread tab with the text already typed in, so a comment is never more than a keystroke away.

### 2c. Editing (click a block)

A click on a block of the doc opens the stage 7 editor (PR #105) in the lower half, in place of the thread and the answer box. In the tabbed layout, the editor becomes a third tab and is shown at once. Esc offers Discard or Keep editing (and Save, once stage 8 lands), then puts the thread back.

```
 Detail · M2 Tour: features not yet shown · edited
┌──────────────────────────────────────────────────────┐
│ # Wheelhouse tour: features not yet demonstrated     │
│                                                      │
│ This is a survey of claude-wheelhouse at main ...    │
│                                                      │
│ ## Recommendation                                    │
│ **Add as tour steps or tips (must).** These are ...  │
├───────────────────────── editing ────────────────────┤
│ ## Recommendation                                    │
│                                                      │
│ **Add as tour steps or tips (must).** These are▌     │
│ cheap, live, and each one unlocks things users ...   │
│                                                      │
│ Esc: done editing   Ctrl+Enter: re-render            │
└──────────────────────────────────────────────────────┘
```

## 3. The thread

- A comment goes to the session that posted the doc, as a message on the doc's ref. It is queued or sent at once, following the session's mode, exactly like an answer to a question.
- **Location.** If you clicked a block in the doc before typing, the comment quotes that block's first line (the `> "Add as SDK-only tips"` line in 2a). Otherwise the comment is general. Esc in an empty answer box drops the quote.
- **Revisions.** The session replies in the thread. If it revises the doc, it posts the same path again: the doc shows as updated, and the viewer reloads with your scroll position kept.
- **Edits pending.** If you have unsaved edits in the editor when the session revises the doc, your buffer is kept and a notice says the file changed underneath it. Stage 8's save then has to deal with that conflict.

## 4. What changes

- The store and the protocol: a doc item can carry messages (it already has a ref), and AWAITING counts docs. This may need a PROTOCOL_VERSION bump.
- The inbox ordering: a docs band (ordering.py bands), with Ctrl+A and the multi-select Delete taught to skip it.
- Detail: a doc container with split, tabbed and editing states, the Ctrl+L toggle, and the `layout.doc-thread` splitter.
- The MCP side: nothing new. Sessions already receive messages on a ref, and they re-post a doc to update it.
- The tour: the docs step (#101) says "comment on the story now". It lands after #104.
- Order of work: after PRs #104 and #105 merge, because both touch the doc views.

## Decided

1. Ctrl+L toggles a doc between the split and tabbed layouts.
2. In split, a doc with no comments yet shrinks its thread area to the answer box alone, so the doc gets the room. The area grows to its share on the first comment.
