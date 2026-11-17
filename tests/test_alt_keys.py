"""#374: Alt bindings in Windows Terminal 1.25, which honours the kitty keyboard protocol Textual asks for."""

import pytest
from textual import constants
from textual._xterm_parser import XTermParser
from textual.drivers import linux_driver
from textual.drivers.linux_driver import LinuxDriver

from claude_wheelhouse.tui import AltKeysDriver, WheelhouseApp


class Writer:
    """Textual's writer thread, keeping what the driver writes instead of sending it to a terminal."""

    def __init__(self, file):
        self.written = []

    def start(self):
        pass

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


class Thread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class App:
    async def _post_message(self, message):
        pass


@pytest.mark.anyio
@pytest.mark.parametrize("driver, disabled, wanted, desc", [
    (AltKeysDriver, False, ["\x1b[>1u"], "the wheelhouse's driver asks for flag 1 alone"),
    (AltKeysDriver, True, [], "TEXTUAL_DISABLE_KITTY_KEY still asks for nothing"),
    (LinuxDriver, False, ["\x1b[>25u"], "Textual's own asks for 25, the string the driver swaps"),
])
async def test_kitty_flags_asked_for(monkeypatch, driver, disabled, wanted, desc):
    monkeypatch.setattr(linux_driver.signal, "signal", lambda *args: None)
    monkeypatch.setattr(linux_driver, "WriterThread", Writer)
    monkeypatch.setattr(linux_driver, "Thread", Thread)
    monkeypatch.setattr(constants, "DISABLE_KITTY_KEY", disabled)
    d = driver(App(), size=(80, 24))
    d.start_application_mode()
    asked = [w for w in d._writer_thread.written if w.startswith("\x1b[>") and w.endswith("u")]
    assert asked == wanted, desc


def test_app_picks_the_driver():
    assert WheelhouseApp.get_driver_class(WheelhouseApp.__new__(WheelhouseApp)) is AltKeysDriver


@pytest.mark.parametrize("sequence, key, desc", [
    ("\x1b[115;3u", "alt+s", "Alt+S under flag 1"),
    ("\x1b[113;3u", "alt+q", "Alt+Q under flag 1"),
    ("\x1b[97;4u", "alt+shift+a", "Alt+Shift+A under flag 1"),
    ("\x1b[13;5u", "ctrl+enter", "Ctrl+Enter under flag 1"),
    ("\x1b[32;5u", "ctrl+space", "Ctrl+Space under flag 1"),
    ("\x1b[115;3;115u", "s", "Alt+S under flag 25: Textual drops the alt, the bug"),
])
def test_textual_reads_kitty_keys(sequence, key, desc):
    assert [e.key for e in XTermParser().feed(sequence)] == [key], desc
