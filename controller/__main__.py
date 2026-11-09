"""Entry point: python -m controller --url http://127.0.0.1:8700 [--state-dir state]

The state file is state/<scenario>.sqlite. If it exists and is ahead of the simulator's clock, the
simulator was restarted (a new world), so the old file is moved aside and we start fresh. Otherwise we
resume from it: that is the crash-recovery path.
"""
import argparse
import os
import time

from .client import SimClient
from .loop import STEP_MINUTES, Controller
from .store import Store


def same_world(s: Store, client: SimClient, sim_minute: int) -> bool:
    """The state file belongs to this simulator run if its clock isn't ahead and every approval we
    recorded still exists on the simulator."""
    last = s.get_meta("last_sim_minute")
    if last is None or last > sim_minute:
        return False
    ours = {r[0] for r in s.db.execute("SELECT approval_id FROM proposals WHERE approval_id IS NOT NULL")}
    return ours <= {a["approval_id"] for a in client.get("/approvals")}


def open_store(state_dir: str, scenario: str, sim_minute: int, client: SimClient) -> Store:
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, f"{scenario}.sqlite")
    if os.path.exists(path):
        s = Store(path)
        if same_world(s, client, sim_minute):
            print(f"resuming from {path} (simulator at minute {sim_minute})", flush=True)
            return s
        s.db.close()
        aside = f"{path}.{int(time.time())}.old"
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.replace(path + suffix, aside + suffix)
        print(f"state file is from another simulator run: moved it to {aside}", flush=True)
    s = Store(path)
    s.set_meta("scenario", scenario)
    s.set_meta("last_sim_minute", sim_minute)
    return s


def main():
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("--url", required=True)
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--step-minutes", type=int, default=STEP_MINUTES)
    ap.add_argument("--max-steps", type=int, default=None, help="stop after N advances (testing only)")
    a = ap.parse_args()
    client = SimClient(a.url)
    for _ in range(60):  # the simulator may still be generating history when we start
        try:
            client.get("/health")
            break
        except Exception:
            time.sleep(2)
    st = client.get("/sim/state")
    store = open_store(a.state_dir, st["scenario"], st["sim_minute"], client)
    Controller(client, store, a.step_minutes).run(a.max_steps)


if __name__ == "__main__":
    main()
