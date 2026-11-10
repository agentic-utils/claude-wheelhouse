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
    ("FOO=1 sudo -E gh pr list", ["gh"], "past an env assignment, a wrapper and its flag"),
    ("nice -n 19 timeout 5 kubectl get pods", ["kubectl"], "past wrappers and their values"),
    ("cd infra; terraform plan | tee out", ["terraform"], "a later segment, and a pipe"),
    ("ls -l\nhelm list || aws s3 ls", ["kubectl", "aws"], "newline and ||: helm shares kubectl's check"),
    ("/usr/bin/az account list", ["az"], "a program given by path"),
    ("npm install", [], "npm only for publishing"),
    ("npm --tag next publish", ["npm"], "npm publish, past a flag"),
    ("echo gcloud", [], "a CLI's name as an argument"),
    ("claude mcp list", [], "claude is never checked"),
    ("echo 'unterminated", [], "a segment that won't parse is skipped"),
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
    (1, 'gcloud isn\'t signed in (gcloud auth print-access-token --quiet failed): suggest '
        '"gcloud auth login --update-adc" first, as its own command, and tell the person why.', "signed out"),
    (127, None, "not installed: skipped, not reported"),
    (None, "couldn't confirm gcloud is signed in", "a timeout: unknown"),
])
def test_notes(monkeypatch, code, note, desc):
    calls, killed = [], []

    def popen(argv, **kw):
        calls.append((argv, kw))
        return FakeProc(code)
    monkeypatch.setattr(authcheck.subprocess, "Popen", popen)
    monkeypatch.setattr(authcheck.os, "killpg", lambda pid, sig: killed.append(pid))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    lines = authcheck.notes("gcloud run deploy && bq ls")
    assert (lines[0].startswith(note) if note else lines == []), desc
    argv, kw = calls[0]
    assert len(calls) == 1 and argv == ["/bin/zsh", "-lc", "gcloud auth print-access-token --quiet"], desc
    assert (kw["stdin"], kw["stdout"], kw["stderr"], kw["start_new_session"]) == \
        (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL, True), desc
    assert killed == ([4242] if code is None else []), desc


def test_a_check_is_cached_for_a_minute(monkeypatch):
    calls, clock = [], [1000.0]
    monkeypatch.setattr(authcheck, "run_check", lambda cli: calls.append(cli) or authcheck.SIGNED_OUT)
    monkeypatch.setattr(authcheck.time, "monotonic", lambda: clock[0])
    authcheck.notes("gh pr list")
    clock[0] += 59
    assert authcheck.notes("gh pr view 1") and len(calls) == 1
    clock[0] += 2
    authcheck.notes("gh pr list")
    assert len(calls) == 2


def test_suggest_command_still_posts_and_adds_the_note(store, sid, monkeypatch):
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    monkeypatch.setattr(authcheck, "run_check", lambda cli: authcheck.SIGNED_OUT)
    result = mcp_server.suggest_command("gh pr view 1", "Check the PR")
    assert result.startswith("C1: shown\ngh isn't signed in") and 'suggest "gh auth login" first' in result
    assert store.item(sid, "C1")["body"] == "gh pr view 1"
