"""#361: a suggested command's CLIs are checked for sign-in, never with a real CLI here."""

import subprocess

import pytest

from claude_wheelhouse import authcheck


@pytest.fixture(autouse=True)
def fresh_cache():
    authcheck._cache.clear()


@pytest.mark.parametrize("command, expected, desc", [
    ("gcloud run services list", ["gcloud"], "a plain gcloud command"),
    ("bq ls && gsutil ls gs://x", ["gcloud"], "bq and gsutil share gcloud's check, once"),
    ("FOO=1 sudo -u root gh pr list", ["gh"], "past an env assignment and a wrapper's valued flag"),
    ("timeout -s KILL 5 gcloud compute ssh vm", ["gcloud"], "past a wrapper's flag and values"),
    ("curl -H \"Bearer $(gcloud auth print-access-token)\" x", ["gcloud"], "inside $(...)"),
    ("bash -c 'kubectl get pods'", ["kubectl"], "inside bash -c"),
    ("bash -c 'gh pr list'", ["gh"], "inside bash -c, after the opening quote"),
    ("sudo -E gh pr list", ["gh"], "a wrapper's flag that takes no value"),
    ("cd infra/terraform && make plan", [], "a directory named like a CLI"),
    ("ls supabase/migrations", [], "a path through a CLI's name"),
    ("cat charts/helm/values.yaml", [], "a CLI's name mid-path"),
    ('git commit -m "fix gh auth note"', [], "a CLI's name in a message"),
    ("npm run build && git tag publish", [], "publish in another program's words"),
    ("gcloud auth login --update-adc", [], "the login itself: not run before itself"),
    ("gcloud auth login --update-adc && gh pr list", ["gh"], "the login, and another CLI"),
    ("ls -l\nhelm list || aws s3 ls", ["aws", "kubectl"], "helm shares kubectl's check; table order"),
    ("/usr/bin/gh pr list", ["gh"], "a program given by path"),
    ("ghost run", [], "a longer word"),
    ("my-gh x && /x/gh.sh", [], "a hyphenated word and a file name"),
    ("npm test", [], "npm only for publishing"),
    ("npm --tag next publish", ["npm"], "npm publish, past a valued flag"),
    ("claude mcp list", [], "claude is never checked"),
])
def test_clis(command, expected, desc):
    assert [c.names[0] for c in authcheck.clis(command)] == expected, desc


class FakeProc:
    def __init__(self, code):
        self.code, self.pid = code, 4242

    def wait(self, timeout=None):
        if self.code is None and timeout is not None:
            raise subprocess.TimeoutExpired("check", timeout)
        return self.code


@pytest.mark.parametrize("code, note, desc", [
    (0, None, "signed in: no note"),
    (1, "gcloud's sign-in check failed (gcloud auth print-access-token --quiet): it may need signing in, or be "
        'offline. Suggest "gcloud auth login --update-adc" as its own command, and ask the person to run it before '
        "C4.", "signed out"),
    (127, None, "not installed: skipped, not reported"),
    (None, "couldn't confirm gcloud is signed in (its check timed out): if C4 stops at a login prompt, suggest "
        '"gcloud auth login --update-adc".', "a timeout: unknown, with what to do"),
])
def test_notes(monkeypatch, code, note, desc):
    calls, killed = [], []

    def popen(argv, **kw):
        calls.append((argv, kw))
        return FakeProc(code)
    monkeypatch.setattr(authcheck.subprocess, "Popen", popen)
    monkeypatch.setattr(authcheck.os, "killpg", lambda pid, sig: killed.append(pid))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    lines = authcheck.notes("gcloud run deploy && bq ls", "C4")
    assert lines == ([note] if note else []), desc
    argv, kw = calls[0]
    assert len(calls) == 1 and argv == ["/bin/zsh", "-lc", "gcloud auth print-access-token --quiet"], desc
    assert (kw["stdin"], kw["stdout"], kw["stderr"], kw["start_new_session"]) == \
        (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL, True), desc
    assert killed == ([4242] if code is None else []), desc


@pytest.mark.parametrize("result, checks, desc", [
    (authcheck.SIGNED_IN, [1, 1, 2], "a pass is trusted for a minute, then checked again"),
    (authcheck.SIGNED_OUT, [1, 2, 3], "a failure is checked again every time"),
    (authcheck.UNKNOWN, [1, 2, 3], "so is a timeout"),
])
def test_only_a_pass_is_cached(monkeypatch, result, checks, desc):
    calls, clock = [], [1000.0]
    monkeypatch.setattr(authcheck, "run_check", lambda cli: calls.append(cli) or result)
    monkeypatch.setattr(authcheck.time, "monotonic", lambda: clock[0])
    seen = []
    for step in (0, 59, 2):
        clock[0] += step
        authcheck.notes("gh pr list", "C1")
        seen.append(len(calls))
    assert seen == checks, desc


def test_suggest_command_still_posts_and_adds_the_note(store, sid, monkeypatch):
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    monkeypatch.setattr(authcheck, "run_check", lambda cli: authcheck.SIGNED_OUT)
    result = mcp_server.suggest_command("gh pr view 1", "Check the PR")
    assert result.startswith("C1: shown\ngh's sign-in check failed") and result.endswith("run it before C1."), result
    assert store.item(sid, "C1")["body"] == "gh pr view 1"


def test_a_kubectl_failure_names_the_kube_context(monkeypatch):
    monkeypatch.setattr(authcheck, "run_check", lambda cli: authcheck.SIGNED_OUT)
    [line] = authcheck.notes("helm list", "C2")
    assert "or be offline, or the kube context is missing or unreachable." in line and "(assuming a GKE cluster)" in line
