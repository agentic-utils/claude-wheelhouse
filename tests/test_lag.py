"""What keeps the loop free for keys and clicks (#327): the inbox rebuilt in linear time,
Detail rendered once a burst of arrows rests and off the loop, a stale render never
landing, an idle tick that paints nothing, and the animation's frame rates."""
import asyncio

import pytest
from textual import events
from textual.widgets import DataTable

from claude_wheelhouse import features, tui
from claude_wheelhouse.tui import WheelhouseApp, fill
from conftest import SHIPPED_DETAIL_INLINE


@pytest.fixture
def anyio_backend():
    return "asyncio"


def blocks_of(name: str, n: int = 30) -> list[tuple[str, str]]:
    return [("claude", f"{name}: turn {i} of a long conversation") for i in range(n)]


def counting_ordered_rows(monkeypatch) -> list[int]:
    """DataTable.ordered_rows, its rebuilds (a cache miss) counted."""
    built, real = [0], DataTable.ordered_rows.fget

    def ordered_rows(self):
        built[0] += (self.row_count, self._update_count) not in self._ordered_row_cache
        return real(self)
    monkeypatch.setattr(DataTable, "ordered_rows", property(ordered_rows))
    return built


@pytest.mark.anyio
@pytest.mark.parametrize("items, desc", [
    (20, "a small inbox"),
    (200, "ten times the rows"),
    (800, "forty times the rows"),
])
async def test_an_inbox_rebuild_reads_its_row_order_a_fixed_number_of_times(store, sid, monkeypatch, items, desc):
    for i in range(items):
        store.post_item(sid, "task", f"task {i}")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        rows = [(k, tuple(table.get_row(k))) for k in table.keys()]
        built = counting_ordered_rows(monkeypatch)
        assert fill(table, rows, rebuild=True), desc
        assert table.row_count == len(rows), desc
        assert built[0] <= 2, f"{desc}: {built[0]} rebuilds of the row order for {len(rows)} rows (#327)"


async def settle(pilot, seconds: float = 0.4) -> None:
    await asyncio.sleep(seconds)
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("arrows, desc", [
    (1, "one arrow"),
    (4, "a burst of four"),
])
async def test_detail_paints_a_conversation_once_after_a_burst_of_arrows(store, tmp_path, monkeypatch, arrows, desc):
    monkeypatch.setattr(tui, "DETAIL_INLINE", SHIPPED_DETAIL_INLINE)
    sids = [store.create_session(str(tmp_path), name=f"s{i}") for i in range(6)]
    tui._block_strips.cache_clear()
    app = WheelhouseApp(store)
    monkeypatch.setattr(app.client, "conversation", lambda sid: blocks_of(sid))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=0)
        await settle(pilot)
        updates = []
        real = app.detail.update
        monkeypatch.setattr(app.detail, "update", lambda content, top=False: (updates.append(content), real(content, top)))
        for _ in range(arrows):   # a burst, as the terminal's driver posts keys, faster than the loop handles them
            app.post_message(events.Key("down", None))
        await settle(pilot)
        sid_now = app.viewing
        assert sid_now in sids, desc
        assert len(updates) == 1, f"{desc}: Detail painted {len(updates)} times"
        assert app.detail.blocks == blocks_of(sid_now) or app.detail.blocks[0][1].startswith(sid_now), desc


@pytest.mark.anyio
@pytest.mark.parametrize("first, second, desc", [
    (30, 30, "two conversations rendered on threads: the second's lands"),
    (30, 2, "one on a thread, then a short one at once: the thread's never lands over it"),
])
async def test_a_stale_render_never_overwrites_a_newer_one(store, sid, monkeypatch, first, second, desc):
    monkeypatch.setattr(tui, "DETAIL_INLINE", SHIPPED_DETAIL_INLINE)
    tui._block_strips.cache_clear()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        old, new = blocks_of("old", first), blocks_of("new", second)
        app.show_detail(old, True, False)
        app.show_detail(new, True, False)
        await settle(pilot)
        assert app.detail.blocks == new, desc
        app.detail_shown(app.detail_gen - 1, old, True, False)   # a result overtaken, arriving late
        assert app.detail.blocks == new, desc


@pytest.mark.anyio
@pytest.mark.parametrize("moved, desc", [
    (False, "the daemon's version where it was: nothing read, compared or painted"),
    (True, "the version moved: the inbox read and painted again"),
])
async def test_the_idle_tick_paints_nothing_when_the_version_has_not_moved(store, sid, monkeypatch, moved, desc):
    for i in range(5):
        store.post_item(sid, "task", f"task {i}")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.client.version = 1   # as RemoteClient stamps it
        await pilot.pause()
        app.paint_items()
        await pilot.pause()
        read = []
        real = app.ranked_keyed
        monkeypatch.setattr(app, "ranked_keyed", lambda *a, **kw: (read.append(1), real(*a, **kw))[1])
        filled = []
        monkeypatch.setattr(tui, "fill", lambda *a, **kw: (filled.append(1), fill(*a, **kw))[1])
        app.client.version += moved
        app.paint_items()
        assert (bool(read), bool(filled)) == (moved, moved), desc


@pytest.mark.anyio
@pytest.mark.parametrize("asked, app_fps, overlay_fps, desc", [
    (10, 5, 4, "the default 10: eyes and shimmer at 5, the overlay at 4"),
    (3, 3, 3, "a lower rate asked: both at it"),
    (0, None, 0, "animation off: no timer"),
])
async def test_frame_rates(store, sid, monkeypatch, asked, app_fps, overlay_fps, desc):
    monkeypatch.setattr(features, "animation_fps", lambda: float(asked))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        got = None if app.animation is None else round(1 / app.animation._interval, 2)
        assert got == app_fps, desc
        assert min(tui.DOCK_FPS, asked) == overlay_fps, desc
