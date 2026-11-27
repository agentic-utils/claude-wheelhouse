"""Build a synthetic wheelhouse under a scratch HOME: many sessions, thousands of items,
a few long transcripts, one session compacting. Usage: seed.py <scratch_home> <sleep_pid>"""
import json
import random
import sys
import uuid
from pathlib import Path

from claude_wheelhouse import liveness, transcript
from claude_wheelhouse.store import Store

home, pid = Path(sys.argv[1]), int(sys.argv[2])
SESSIONS, ITEMS, LONG = 40, int(sys.argv[3]) if len(sys.argv) > 3 else 4000, 3
rng = random.Random(327)
db = home / "state/wheelhouse.db"
db.parent.mkdir(parents=True, exist_ok=True)
store = Store(db)
start, boot = liveness.start_time(pid), liveness.boot_id()
words = "the quick brown fox jumps over a lazy dog while compaction runs and items land in the inbox".split()


def text(n):
    return " ".join(rng.choice(words) for _ in range(n))


def write_transcript(sid, cwd, turns):
    folder = home / ".claude/projects" / transcript.project_folder(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{sid}.jsonl", "w") as f:
        for i in range(turns):
            ts = f"2026-11-25T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.000Z"
            f.write(json.dumps({"type": "user", "uuid": str(uuid.uuid4()), "timestamp": ts, "sessionId": sid,
                                "message": {"role": "user", "content": text(30)}}) + "\n")
            f.write(json.dumps({"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": ts, "sessionId": sid,
                                "message": {"role": "assistant", "content": [
                                    {"type": "text", "text": "## Heading\n\n" + text(120) + "\n\n- a\n- b\n\n```py\nx = 1\n```"},
                                    {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "ls -la " + text(5)}}]}}) + "\n")
            f.write(json.dumps({"type": "user", "uuid": str(uuid.uuid4()), "timestamp": ts, "sessionId": sid,
                                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                                          "content": text(400)}]}}) + "\n")


sids = []
for n in range(SESSIONS):
    cwd = f"/home/synthetic/projects/repo{n % 7}"
    sid = store.create_session(cwd, name=f"session {n} {text(3)}", ticket=f"#{100 + n}", brief=text(20))
    sids.append(sid)
    if n < 30:
        store.register(sid, pid, start, boot)
    store.set_activity(sid, text(8))
    store.set_synopsis(sid, text(25))
    write_transcript(sid, cwd, 1500 if n < LONG else 40)

kinds = ["task", "question", "task", "task", "agent"]
for i in range(ITEMS):
    sid = rng.choice(sids)
    kind = rng.choice(kinds)
    store.post_item(sid, kind, f"{kind} tq{i:04d} {text(8)}", f"bq{i:04d} " + text(60))

from claude_wheelhouse import whatsnew
store.see_features([f.id for f in whatsnew.FEATURES])
compacting = sids[0]
cid = store.start_compaction(compacting, "person", state="compacting")
print(json.dumps({"db": str(db), "compacting": compacting, "cycle": cid, "sids": sids[:5]}))
