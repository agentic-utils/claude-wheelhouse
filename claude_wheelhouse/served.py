"""The modules' daemon side (.plan/modules.md, "The module boundary"): their routes and their
doc roots, served by the daemon under /m/<id>/ and /docs, and by a hub in process (--local)
the same way, so a pane's Context.call and show_doc answer alike through either. Never
imports Textual."""

import sys
import traceback
from pathlib import Path
from urllib.parse import parse_qsl

from . import api, docroots


def facts(hub) -> list[dict]:
    """The hub's sessions as a module's daemon side sees them (api.Context.sessions): id,
    name, whether it's running, its directory and the doc dirs it declared (set_doc_dirs)."""
    return [{"id": s["id"], "name": s["name"], "running": hub.running(s["id"]),
             "cwd": s["cwd"], "doc_dirs": docroots.declared(s["doc_dirs"])} for s in hub.sessions]


def match(pattern: str, path: str) -> dict[str, str] | None:
    """A route's path against one asked for, segment by segment, {name} taking any one:
    its params, or None if it doesn't match."""
    want, got = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(want) != len(got):
        return None
    params = {}
    for w, g in zip(want, got):
        if w.startswith("{") and w.endswith("}"):
            params[w[1:-1]] = g
        elif w != g:
            return None
    return params


class Served:
    """The running modules' routes and doc roots, asked of ctx once (routes) or on each read
    (doc roots, which follow the sessions). A module whose routes(ctx) fails serves none,
    said on stderr; one whose doc_roots(ctx) fails adds none."""

    def __init__(self, loaded: list[api.Loaded], ctx: api.Context):
        self.ctx = ctx
        self.modules = {i.module.id: i.module for i in loaded if i.module and not i.error and not i.disabled}
        self.routes: dict[str, list[api.Route]] = {}
        for mid, module in self.modules.items():
            if module.routes is not None:
                try:
                    self.routes[mid] = list(module.routes(ctx))
                except Exception as e:
                    said(f"module {mid}'s routes failed", e)

    def route(self, mid: str, method: str, path: str) -> tuple[api.Route, dict]:
        """The route a call reaches, and its params; KeyError (404) for none."""
        for route in self.routes.get(mid, ()):
            if route.method.upper() == method and (params := match(route.path, path)) is not None:
                return route, params
        raise KeyError(f"no route {method} /m/{mid}/{path}")

    def dispatch(self, mid: str, method: str, path: str, query: dict, body: dict | None):
        route, params = self.route(mid, method, path)
        return route.handler(api.Call(params, dict(query), body))

    def call(self, mid: str, path: str, body: dict | None = None):
        """As a pane's Context.call reaches the daemon's, in process: a GET without a body, a
        POST with one; what the route answers, else api.Refused with the status the daemon
        would have answered."""
        path, _, qs = path.partition("?")
        try:
            return self.dispatch(mid, "GET" if body is None else "POST", path, dict(parse_qsl(qs)), body)
        except api.Refused:
            raise
        except KeyError as e:
            raise api.Refused(404, e.args[0]) from None
        except Exception as e:
            said(f"module {mid}'s route failed", e)
            raise api.Refused(500, f"the daemon failed: {type(e).__name__}: {e}") from None

    def doc_roots(self) -> list[Path]:
        out = []
        for mid, module in self.modules.items():
            if module.doc_roots is not None:
                try:
                    out += [Path(p) for p in module.doc_roots(self.ctx)]
                except Exception as e:
                    said(f"module {mid}'s doc roots failed", e)
        return out

    def read(self, path: str, have: str | None = None) -> dict:
        """A doc under a doc root (docroots.read), else docroots.Outside."""
        return docroots.read(path, self.doc_roots(), have)


def said(what: str, e: Exception) -> None:
    print(f"wheelhouse: {what}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    traceback.print_exception(e)


def serve(hub) -> Served:
    """The modules installed and not switched off (api.DISABLED, as the store has it now),
    served for a hub: the daemon's, or the app's own with --local."""
    loaded = api.switch_off(api.load(), api.disabled(hub.store.setting(api.DISABLED)))
    ctx = api.Context(hub.store.path.parent, lambda: facts(hub), lambda: None, usage=hub.usage)
    return Served(loaded, ctx)
