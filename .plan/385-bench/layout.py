"""#385: where things are on a 220x60 screen, for ptybench.py to aim at. Headless (run_test).
Usage: layout.py <daemon url> <token file>  -> JSON on stdout."""
import asyncio
import json
import sys
from pathlib import Path

from textual.widgets import Button

from claude_wheelhouse import tui
from claude_wheelhouse.client import RemoteClient

URL, TOKEN = sys.argv[1], Path(sys.argv[2]).read_text().strip()


async def main():
    client = RemoteClient(URL, TOKEN)
    app = tui.WheelhouseApp(client=client)
    async with app.run_test(size=(220, 60)) as pilot:
        for _ in range(50):
            await asyncio.sleep(0.2)
            if client.sessions_loaded() and app.session_list.row_count:
                break
        await asyncio.sleep(2)
        dialogs = []
        for _ in range(3):
            if isinstance(app.screen, tui.Dialog):
                dialogs.append(type(app.screen).__name__)
                await pilot.press("escape")
                await asyncio.sleep(0.3)
        app.items_table.focus()
        await asyncio.sleep(1)

        def reg(w):
            r = w.region
            return [r.x, r.y, r.width, r.height]
        out = {"dialogs_at_start": dialogs, "answer": reg(app.query_one("#answer")), "items": reg(app.items_table),
               "sessions": reg(app.session_list),
               "buttons": [{"id": b.id, "label": str(b.label), "region": reg(b), "disabled": b.disabled}
                           for b in app.screen.query(Button) if b.display and b.region.width]}
        print(json.dumps(out))


asyncio.run(main())
