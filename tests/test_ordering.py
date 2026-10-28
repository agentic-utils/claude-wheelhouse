"""The lists' order (ordering.py, T114): sorts, the person's order inside and across groups."""

import pytest

from claude_wheelhouse import ordering

T0 = "2026-10-08T10:00:{:02d}+00:00"


def item(n, ref, status, kind="task", title="", sid="s1", pos=None, group=None, at=None):
    return {"id": n, "session_id": sid, "ref": ref, "kind": kind, "status": status, "title": title or ref,
            "updated_at": T0.format(at if at is not None else n), "dismissed": None, "pos": pos, "pos_group": group}


def order(spec, items, names=None):
    keyed = {f"{it['session_id']}|{it['ref']}": ordering.item_key(spec, it, names=names) for it in items}
    ranks = ordering.arrange(spec, keyed)
    return [k.split("|")[1] for k in sorted(keyed, key=ranks.get)], {k.split("|")[1]: v[1] for k, v in keyed.items()}


@pytest.mark.parametrize("spec, column, then, desc", [
    ("status*", "status", "status▲", "Status: * then ▲"),
    ("status▲", "status", "status▼", "then ▼"),
    ("status▼", "status", "status*", "then * again"),
    ("status*", "title", "title▲", "another column starts at ▲"),
    ("title▲", "title", "title▼", "then ▼"),
    ("title▼", "title", "status*", "then off, back to Status *"),
    ("", "title", "title▲", "ordering off: a column imposes its sort"),
    ("", "status", "status*", "ordering off: Status imposes *"),
])
def test_header_cycles(spec, column, then, desc):
    assert ordering.next_item_sort(spec, column) == then, desc


@pytest.mark.parametrize("spec, column, then, desc", [
    ("dot*", "dot", "dot*", "the dot's * stays on: only another column takes over"),
    ("", "dot", "dot*", "ordering off: the dot puts its * back"),
    ("ctx▲", "dot", "dot*", "the dot takes over from another column"),
    ("dot*", "session", "session▲", "the name starts at ▲"),
    ("session▲", "session", "session▼", "then ▼"),
    ("session▼", "session", "dot*", "then back to the dot's *"),
    ("dot*", "ctx", "ctx▼", "a size starts at ▼, the most at the top"),
    ("ctx▼", "ctx", "ctx▲", "then ▲"),
    ("ctx▲", "ctx", "dot*", "then back to the dot's *"),
    ("session▲", "drafts", "drafts▼", "a count starts afresh at ▼"),
    ("decisions▼", "decisions", "decisions▲", "a count's second click is ▲"),
    ("", "questions", "questions▼", "ordering off: a count imposes its sort, most first"),
])
def test_session_header_cycles(spec, column, then, desc):
    assert ordering.next_session_sort(spec, column) == then, desc
    assert then in ordering.SPECS["sessions"], f"{desc}: kept, it parses on the next start"


def test_session_sort_defaults():
    assert ordering.SESSIONS_DEFAULT == "dot*", "the session list starts sorted, its * showing (Q102)"
    assert {"", "dot*"} <= ordering.SPECS["sessions"], "a sort stored by older code still parses"


@pytest.mark.parametrize("spec, rows, want, desc", [
    ("status*", [item(1, "T1", "running"), item(2, "Q1", "open", "question"), item(3, "T2", "blocked")],
     ["Q1", "T2", "T1"], "*: today's inbox order"),
    ("status▲", [item(1, "T1", "open", "question"), item(2, "T2", "blocked"), item(3, "T3", "waiting"),
                 item(4, "T4", "todo"), item(5, "T5", "running")],
     ["T2", "T3", "T1", "T5", "T4"], "▲: blocked, waiting, open, running, todo (Q104)"),
    ("status▼", [item(1, "T1", "open", "question"), item(2, "T2", "blocked"), item(3, "T4", "todo")],
     ["T4", "T1", "T2"], "▼: the reverse"),
    ("status▲", [item(1, "T1", "running"), item(2, "T2", "running")], ["T2", "T1"], "▲: ties newest first"),
    ("status▲", [item(1, "T1", "done"), item(2, "T2", "done")], ["T1", "T2"],
     "▲: the settled as * has them, the latest to settle lowest"),
    ("status▲", [item(1, "T1", "running"), item(2, "M1", "new", "doc"), item(3, "Q1", "open", "question")],
     ["Q1", "M1", "T1"], "▲: a status not in the table between open and running"),
    ("ref▲", [item(1, "T10", "running"), item(2, "T2", "running"), item(3, "Q1", "open", "question")],
     ["Q1", "T2", "T10"], "ref: natural order"),
    ("title▼", [item(1, "T1", "running", title="apple"), item(2, "T2", "running", title="Banana")],
     ["T2", "T1"], "title ▼, casefolded"),
    ("title▲", [item(1, "T1", "running", title="b"), item(2, "T2", "done", title="a")],
     ["T1", "T2"], "a sort keeps the bands: settled after active"),
    ("status*", [item(1, "T1", "running", pos=0, group="status*|0|3"), item(2, "T2", "running", pos=1, group="status*|0|3"),
                 item(3, "T3", "running")],
     ["T3", "T1", "T2"], "the placed in their own order, a newcomer straight before the first of its status (T121)"),
    ("status*", [item(1, "T1", "running", pos=1, group="status*|0|3"), item(2, "T2", "running", pos=0, group="status*|0|4")],
     ["T2", "T1"], "a place from another group counts for nothing: in by status, before T1"),
    ("", [item(1, "T1", "running", pos=1, group="manual"), item(2, "T2", "done", pos=0, group="manual"),
          item(3, "Q1", "open", "question")],
     ["Q1", "T2", "T1"], "ordering off: the person's order, settled or not; a newcomer first"),
])
def test_item_order(spec, rows, want, desc):
    assert order(spec, rows)[0] == want, desc


NAMES = {"s1": "zeta", "s2": "alpha", "s3": "s10", "s4": "s2"}


@pytest.mark.parametrize("spec, rows, want, desc", [
    ("status*", [item(1, "T1", "running", sid="s1"), item(2, "T2", "running", sid="s2"), item(3, "T3", "running", sid="s1")],
     ["T2", "T3", "T1"], "*: by session name within a status, then newest first (#236)"),
    ("status*", [item(1, "Q1", "open", "question", sid="s1"), item(2, "T1", "running", sid="s2")],
     ["Q1", "T1"], "*: the status first, the name second"),
    ("status*", [item(1, "T1", "running", sid="s3"), item(2, "T2", "running", sid="s4")],
     ["T2", "T1"], "*: names in natural order, s2 before s10"),
    ("status*", [item(1, "T1", "done", sid="s1"), item(2, "T2", "done", sid="s2")],
     ["T2", "T1"], "*: the settled by name too, then the latest to settle lowest"),
    ("status▲", [item(1, "T1", "running", sid="s2"), item(2, "T2", "running", sid="s1")],
     ["T2", "T1"], "▲: no name tie-break, newest first"),
])
def test_item_order_by_session_name(spec, rows, want, desc):
    assert order(spec, rows, NAMES)[0] == want, desc


@pytest.mark.parametrize("before, after, moved, group, written, desc", [
    ("A", None, "B", "g1", ["B", "A"], "within its group: the group's new order"),
    (None, "D", "C", "g2", ["D", "C"], "the last of its group"),
    ("C", None, "A", "g1", ["B", "A"], "the foot of its group, by the next group's head: still within"),
    (None, "E", "B", None, ["A", "C", "D", "E", "B", "F"], "into another group: every row, ordering off"),
    ("A", None, "A", "", [], "where it was: nothing"),
])
def test_drop(before, after, moved, group, written, desc):
    keys = list("ABCDEF")
    tokens = dict(zip(keys, ["g1", "g1", "g2", "g2", "g3", "g3"]))
    got = ordering.drop(keys, tokens, moved, before=before, after=after) if before != moved else ("", [])
    assert got == (group, written), desc


def test_drop_goes_by_the_neighbours_shown():
    """C dropped before B: as shown (A, B, C) its neighbours there are A and B, so within A's
    and its group; by the whole order (B first) it would be B alone: across."""
    order, tokens = ["B", "A", "C"], {"A": "g1", "B": "g2", "C": "g1"}
    assert ordering.drop(order, tokens, "C", before="B")[0] is None, "by the whole order: across"
    assert ordering.drop(order, tokens, "C", before="B", shown=["A", "B", "C"])[0] == "g1", "as shown: within"


def test_drag_within_then_status_changes():
    """Q103: B dragged above A keeps the sort; once B's status matches C's, B moves there."""
    a, b = item(1, "T1", "running", at=1), item(2, "T2", "running", at=2)
    c = item(3, "T3", "todo", at=3)
    got, tokens = order("status*", [a, b, c])
    assert got == ["T2", "T1", "T3"]
    group, new = ordering.drop(got, tokens, "T1", before="T2")
    assert group == "status*|0" and new == ["T1", "T2", "T3"], "the band, each to be placed with its own tier"
    for i, ref in enumerate(new):
        ({"T1": a, "T2": b, "T3": c}[ref]).update(pos=i, pos_group=tokens[ref])
    assert order("status*", [a, b, c])[0] == ["T1", "T2", "T3"], "within its group: the sort stays"
    a.update(status="todo", updated_at=T0.format(9), pos=None, pos_group=None)   # as the store's trigger lets it go
    assert order("status*", [a, b, c])[0] == ["T2", "T1", "T3"], "its key changed: in by status, at the head of it"


@pytest.mark.parametrize("parked, running, last, want, desc", [
    ((0, 0, 0), (False, True, False), ("1", "2", "3"), ["b", "c", "a"], "dot*: running first, then the latest"),
    ((0, 1, 0), (False, False, False), ("1", "9", "3"), ["c", "a", "b"], "dot*: parked still at the foot"),
])
def test_session_sort(parked, running, last, want, desc):
    rows = [{"id": sid, "parked": p, "created_at": T0.format(0), "last_at": T0.format(int(at)), "pos": None,
             "pos_group": None} for sid, p, at in zip("abc", parked, last)]
    keyed = {s["id"]: ordering.session_key("dot*", s, r, i)[0] for i, (s, r) in enumerate(zip(rows, running))}
    assert sorted(keyed, key=keyed.get) == want, desc


# rows a to d: (name, parked, running, context, open questions, unseen decisions, drafts, last)
SESSION_ROWS = (("s10", 0, False, 900, 0, 2, 1, "1"), ("S2", 0, True, 50, 3, 0, 0, "2"),
                ("alpha", 0, False, 400, 1, 0, 0, "3"), ("zed", 1, False, 9999, 9, 9, 9, "4"),
                ("new", 0, False, None, 0, 0, 0, "5"))


@pytest.mark.parametrize("spec, want, desc", [
    ("session▲", ["alpha", "new", "S2", "s10", "zed"], "name: casefolded, numbers by value, parked at the foot"),
    ("session▼", ["s10", "S2", "new", "alpha", "zed"], "name ▼: reversed, parked still at the foot"),
    ("ctx▲", ["S2", "alpha", "s10", "new", "zed"], "context, smallest first, unknown last"),
    ("ctx▼", ["s10", "alpha", "S2", "new", "zed"], "context, largest first, unknown still last"),
    ("questions▲", ["new", "s10", "alpha", "S2", "zed"], "open questions, ties as dot* (running, then latest)"),
    ("questions▼", ["S2", "alpha", "new", "s10", "zed"], "open questions, most first"),
    ("decisions▼", ["s10", "S2", "new", "alpha", "zed"], "unseen decisions, ties running first"),
    ("drafts▼", ["s10", "S2", "new", "alpha", "zed"], "drafts, ties running first"),
    ("drafts▲", ["S2", "new", "alpha", "s10", "zed"], "drafts ▲: ties running first, then the latest"),
])
def test_session_column_sorts(spec, want, desc):
    rows = [{"id": n, "parked": p, "created_at": T0.format(0), "last_at": T0.format(int(at)), "pos": None,
             "pos_group": None, "open_questions": q, "unseen_decisions": d, "drafts": dr}
            for n, p, _, _, q, d, dr, at in SESSION_ROWS]
    keyed = {s["id"]: ordering.session_key(spec, s, r[2], i, s["id"], r[3])
             for i, (s, r) in enumerate(zip(rows, SESSION_ROWS))}
    assert sorted(keyed, key=lambda k: keyed[k][0]) == want, desc
    assert all(t.startswith(spec + "|") for _, t in keyed.values()), f"{desc}: grouped under the sort"
    assert len({t for _, t in keyed.values()}) == len({str(k[0][1]) for k in keyed.values()}), \
        f"{desc}: a group's token reads the same whichever way its value sorts"


def test_sessions_off_keep_creation_order_until_placed():
    rows = [{"id": sid, "parked": p, "created_at": T0.format(0), "pos": pos, "pos_group": g}
            for sid, p, pos, g in (("a", 1, None, None), ("b", 0, None, None), ("c", 0, None, None))]
    keyed = {s["id"]: ordering.session_key("", s, False, i)[0] for i, s in enumerate(rows)}
    assert sorted(keyed, key=keyed.get) == ["b", "c", "a"]
    rows[0].update(pos=0, pos_group="manual")
    rows[2].update(pos=1, pos_group="manual")
    keyed = {s["id"]: ordering.session_key("", s, False, i)[0] for i, s in enumerate(rows)}
    assert sorted(keyed, key=keyed.get) == ["b", "a", "c"], "a newcomer first, the placed as the person put them"


@pytest.mark.parametrize("order_, settled, want, desc", [
    (["a", "b", "c"], {"c"}, {"c"}, "settled at the foot: they sink there"),
    (["a", "c", "b"], {"c"}, set(), "one put among the rest: nothing sinks"),
])
def test_sunk_keys(order_, settled, want, desc):
    assert ordering.sunk_keys(order_, settled) == want, desc


SIX = [("A", "blocked"), ("B", "blocked"), ("C", "running"), ("D", "running"), ("E", "todo"), ("F", "todo")]


@pytest.mark.parametrize("rows_, drag, steps, want, desc", [
    (SIX, ("B", "before", "E"), [], "ADCFBE", "Q103: B dropped between F and E stays there, * on"),
    (SIX, ("B", "before", "E"), [("B", "running")], "ABDCFE", "Q103: B set to running lands at the head of running"),
    (SIX, ("B", "before", "E"), [("B", "running"), ("B", "blocked")], "BADCFE",
     "back to blocked: at the head of blocked, not where it was dropped (review 1)"),
    (SIX, ("F", "before", "B"), [("N", "running")], "FBANDCE", "round 1: F dragged to the top, a new running N before D"),
    (SIX, ("B", "before", "E"), [("R", "todo")], "ADCRFBE", "a new task R before the first placed todo"),
    ([("q1", "open"), ("q2", "open"), ("E", "todo")], ("q1", "after", "E"), [("q3", "open")], ["q3", "q2", "E", "q1"],
     "round 2: q1 dragged to the foot, a new q3 before q2 at the top"),
])
def test_rows_go_in_by_status_around_the_placed(rows_, drag, steps, want, desc):
    """T121: a row put in by status (new, or let go by a status change) goes straight before
    the first placed row of its status, else after the last placed row of a more urgent
    one, else at the head of the band; the newest first among those going into one spot."""
    kinds = {"open": "question"}
    rows = {r: item(n, r, s, kinds.get(s, "task")) for n, (r, s) in enumerate(rows_, 1)}
    order_, tokens = shown(rows)
    moved, where, other = drag
    group, new = ordering.drop(order_, tokens, moved, **{where: other})
    assert group == "status*|0", f"{desc}: within the band"
    place(rows, new, tokens)
    for at, (r, status) in enumerate(steps, 30):
        if r in rows:
            set_status(rows, r, status, at)
        else:
            rows[r] = item(at, r, status, kinds.get(status, "task"), at=at)
    assert shown(rows)[0] == list(want), desc


def shown(rows):
    raw = {r: ordering.item_key("status*", it) for r, it in rows.items()}
    ranks = ordering.arrange("status*", raw)
    return sorted(rows, key=ranks.get), {r: token for r, (_, token) in raw.items()}


def place(rows, new, tokens):
    for i, r in enumerate(new):
        rows[r].update(pos=i, pos_group=tokens[r])   # each placed with its own tier


def set_status(rows, r, status, at):
    """A status change, as the store makes it: a write (the newest), and its placement let go."""
    rows[r].update(status=status, updated_at=T0.format(at), pos=None, pos_group=None)


def test_a_drop_into_another_band_is_across():
    rows = [item(1, "T1", "running"), item(2, "T2", "running"), item(3, "T3", "done")]
    got, tokens = order("status*", rows)
    assert ordering.drop(got, tokens, "T2", after="T3")[0] is None
