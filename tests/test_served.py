from pathlib import Path

import pytest

from claude_wheelhouse import api, served


def echo(call: api.Call):
    return {"params": call.params, "query": call.query, "body": call.body}


def refuse(call: api.Call):
    raise api.Refused(409, "not now")


def broken(call: api.Call):
    raise RuntimeError("bug")


def lookup(call: api.Call):
    return {}["missing"]


def parse(call: api.Call):
    return int("x")


def probe(roots=(), routes=None, id="probe"):
    """The test module: routes that echo, refuse and break, and roots as given."""
    def made(ctx):
        return routes if routes is not None else [
            api.Route("echo/{a}", echo), api.Route("echo/{a}", echo, "POST"), api.Route("refuse", refuse),
            api.Route("broken", broken), api.Route("lookup", lookup), api.Route("parse", parse)]
    return api.Module(id, id.title(), "1.0", api.WHEELHOUSE_API, routes=made,
                      doc_roots=lambda ctx: [Path(r) for r in roots])


CTX = api.Context(Path("/tmp"), lambda: [], lambda: None)


@pytest.mark.parametrize("pattern, path, params, desc", [
    ("decisions", "decisions", {}, "a plain path"),
    ("decisions/{n}", "decisions/0042", {"n": "0042"}, "a param"),
    ("/decisions/{n}/", "decisions/7", {"n": "7"}, "slashes at either end don't count"),
    ("decisions/{n}", "decisions", None, "too few segments"),
    ("decisions", "decisions/7", None, "too many"),
    ("decisions/{n}", "topics/7", None, "another word"),
])
def test_match(pattern, path, params, desc):
    assert served.match(pattern, path) == params, desc


@pytest.mark.parametrize("path, body, expect, desc", [
    ("echo/x?q=1", None, {"params": {"a": "x"}, "query": {"q": "1"}, "body": None}, "a GET, its param and query"),
    ("echo/y", {"k": 1}, {"params": {"a": "y"}, "query": {}, "body": {"k": 1}}, "a POST with its body"),
    ("refuse", None, (409, "not now"), "a route's own refusal, as it said it"),
    ("missing", None, (404, "no route GET /m/probe/missing"), "no such route: 404"),
    ("refuse", {}, (404, "no route POST /m/probe/refuse"), "a route by another method: 404"),
    ("broken", None, (500, "the module failed: RuntimeError: bug"), "a route that fails: 500, as the daemon says it"),
    ("lookup", None, (500, "the module failed: KeyError: 'missing'"), "its own KeyError: 500, not 404"),
    ("parse", None, (500, "the module failed: ValueError: invalid literal for int() with base 10: 'x'"),
     "its own ValueError: 500, not 400"),
])
def test_call_in_process(path, body, expect, desc):
    s = served.Served([api.Loaded("probe", probe())], CTX)
    if isinstance(expect, tuple):
        with pytest.raises(api.Refused) as e:
            s.call("probe", path, body)
        assert (e.value.status, e.value.error) == expect, desc
    else:
        assert s.call("probe", path, body) == expect, desc


@pytest.mark.parametrize("loaded, routes, desc", [
    ([api.Loaded("probe", probe())], {"probe"}, "a running module's routes"),
    ([api.Loaded("probe", probe(), disabled=True)], set(), "a module switched off serves none"),
    ([api.Loaded("probe", probe(), "written for API 1")], set(), "nor one that didn't start"),
    ([api.Loaded("probe", api.Module("probe", "P", "1", api.WHEELHOUSE_API))], set(), "one with none has none"),
    ([api.Loaded("probe", api.Module("probe", "P", "1", api.WHEELHOUSE_API, routes=lambda ctx: 1 / 0))], set(),
     "one whose routes fail serves none, and the rest run"),
])
def test_which_modules_serve(loaded, routes, desc):
    assert set(served.Served(loaded, CTX).routes) == routes, desc


def test_doc_roots_are_every_running_modules(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    loaded = [api.Loaded("one", probe([a], id="one")), api.Loaded("two", probe([b], id="two")),
              api.Loaded("off", probe([tmp_path], id="off"), disabled=True),
              api.Loaded("bad", api.Module("bad", "B", "1", api.WHEELHOUSE_API, doc_roots=lambda ctx: 1 / 0))]
    assert served.Served(loaded, CTX).doc_roots() == [a, b]


@pytest.mark.parametrize("raw, names, desc", [
    (None, set(), "none kept"),
    ('["design"]', {"design"}, "one"),
    ('["design", 3]', {"design"}, "a name that isn't a string is skipped"),
    ("not json", set(), "junk is none"),
    ('{"design": 1}', set(), "not a list is none"),
])
def test_disabled(raw, names, desc):
    assert api.disabled(raw) == names, desc


def test_switch_off_by_entry_point_or_id():
    loaded = [api.Loaded("ep", probe(id="pid")), api.Loaded("other", probe(id="other")),
              api.Loaded("broken", None, "failed to load")]
    off = api.switch_off(loaded, {"pid", "broken"})
    assert [(i.name, i.disabled) for i in off] == [("ep", True), ("other", False), ("broken", True)]
    assert api.panes(off, "left.middle") == [], "neither a pane nor a card for a module switched off"
