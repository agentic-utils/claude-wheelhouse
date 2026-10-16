"""The lists' order (ordering.py, T114): sorts, the person's order inside and across groups."""

import pytest

from claude_wheelhouse import ordering

T0 = "2026-10-08T10:00:{:02d}+00:00"


def item(n, ref, status, kind="task", title="", sid="s1", pos=None, group=None, at=None):
    return {"id": n, "session_id": sid, "ref": ref, "kind": kind, "status": status, "title": title or ref,
            "updated_at": T0.format(at if at is not None else n), "dismissed": None, "pos": pos, "pos_group": group}


def order(spec, items, names=None):
    keyed = {f"{it['session_id']}|{it['ref']}": ordering.item_key(spec, it, names=names) for it in items}
    return [k.split("|")[1] for k in sorted(keyed, key=lambda k: keyed[k][0])], {k.split("|")[1]: v[1] for k, v in keyed.items()}


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


def test_drag_within_then_status_changes():
    """Q103: B dragged above A keeps the sort; once B's status matches C's, B moves there."""
    a, b = item(1, "T1", "running", at=1), item(2, "T2", "running", at=2)
    c = item(3, "T3", "todo", at=3)
    got, tokens = order("status*", [a, b, c])
    assert got == ["T2", "T1", "T3"]
    group, new = ordering.drop(got, tokens, "T1", before="T2")
    assert group == tokens["T1"] and new == ["T1", "T2"]
    for i, ref in enumerate(new):
        ({"T1": a, "T2": b}[ref]).update(pos=i, pos_group=group)
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
