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


def test_session_sort_toggles():
    assert [ordering.next_session_sort(s) for s in ("", "dot*")] == ["dot*", ""]
    assert ordering.SESSIONS_DEFAULT == "dot*", "the session list starts sorted, its * showing (Q102)"


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
     ["T3", "T1", "T2"], "placed rows after the group's newcomer, in their own order"),
    ("status*", [item(1, "T1", "running", pos=1, group="status*|0|3"), item(2, "T2", "running", pos=0, group="status*|0|4")],
     ["T2", "T1"], "a place from another group counts for nothing"),
    ("", [item(1, "T1", "running", pos=1, group="manual"), item(2, "T2", "done", pos=0, group="manual"),
          item(3, "Q1", "open", "question")],
     ["Q1", "T2", "T1"], "ordering off: the person's order, settled or not; a newcomer first"),
])
def test_item_order(spec, rows, want, desc):
    assert order(spec, rows)[0] == want, desc


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
    a.update(status="todo", updated_at=T0.format(9))   # a status change is a write: the newest
    assert order("status*", [a, b, c])[0] == ["T2", "T1", "T3"], "its key changed: the top of its new group"


@pytest.mark.parametrize("parked, running, last, want, desc", [
    ((0, 0, 0), (False, True, False), ("1", "2", "3"), ["b", "c", "a"], "dot*: running first, then the latest"),
    ((0, 1, 0), (False, False, False), ("1", "9", "3"), ["c", "a", "b"], "dot*: parked still at the foot"),
])
def test_session_sort(parked, running, last, want, desc):
    rows = [{"id": sid, "parked": p, "created_at": T0.format(0), "last_at": T0.format(int(at)), "pos": None,
             "pos_group": None} for sid, p, at in zip("abc", parked, last)]
    keyed = {s["id"]: ordering.session_key("dot*", s, r, i)[0] for i, (s, r) in enumerate(zip(rows, running))}
    assert sorted(keyed, key=keyed.get) == want, desc


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


def test_doug_example_drop_anywhere_in_the_band():
    """T121 (Q108 B): A and B in one status, C and D in another, E and F in a third, all
    active. B dragged between E and F keeps Status * on and stays there; once B's status
    changes it goes in by status."""
    rows = {r: item(n, r, s) for n, (r, s) in enumerate(
        [("A", "blocked"), ("B", "blocked"), ("C", "running"), ("D", "running"), ("E", "todo"), ("F", "todo")], 1)}

    def shown():
        raw = {r: ordering.item_key("status*", it) for r, it in rows.items()}
        ranks = ordering.arrange("status*", raw)
        return sorted(rows, key=ranks.get), {r: token for r, (_, token) in raw.items()}

    order, tokens = shown()
    assert order == ["B", "A", "D", "C", "F", "E"]
    group, new = ordering.drop(order, tokens, "B", after="F")
    assert group == "status*|0" and new == ["A", "D", "C", "F", "B", "E"], "within the band: * stays"
    for i, r in enumerate(new):
        rows[r].update(pos=i, pos_group=tokens[r])   # each placed with its own tier
    assert shown()[0] == ["A", "D", "C", "F", "B", "E"], "B stays where it was dropped"
    rows["B"].update(status="running", updated_at=T0.format(30))
    assert shown()[0] == ["A", "B", "D", "C", "F", "E"], "its status changed: in by status, at the head of it"
    rows["G"] = item(9, "G", "open", "question", at=40)
    assert shown()[0] == ["G", "A", "B", "D", "C", "F", "E"], "a newcomer goes in by status too: an open question first"


def test_a_drop_into_another_band_is_across():
    rows = [item(1, "T1", "running"), item(2, "T2", "running"), item(3, "T3", "done")]
    got, tokens = order("status*", rows)
    assert ordering.drop(got, tokens, "T2", after="T3")[0] is None
