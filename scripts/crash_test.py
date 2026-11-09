"""Crash-recovery evidence: SIGKILL the controller several times mid-run, restart it, check the run.

    python scripts/crash_test.py --scenario scenarios/public_02_incidents.yaml --name crash_public_02

What it does
  1. starts the simulator and the controller (./run.sh) exactly as a grader would;
  2. kills the controller with SIGKILL at a few sim times (chosen to land during ingestion, while
     approvals are pending, and mid-incident), restarting it each time with the same state dir;
  3. lets it finish, grades the run, and checks invariants from the simulator's log and our store:
       - every chunk ingested exactly once (our call counts == the simulator's call events)
       - no approval applied twice, no duplicate proposal ids, nothing left half-sent
       - every approval the simulator knows about is in our store (no lost approvals)
Only this test script reads runs/ — the controller never does.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def wait_minute(url: str, minute: int, proc) -> bool:
    while proc.poll() is None:
        try:
            if httpx.get(f"{url}/sim/state", timeout=30).json()["sim_minute"] >= minute:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_02_incidents.yaml")
    ap.add_argument("--name", default="crash_public_02")
    ap.add_argument("--port", type=int, default=8709)
    ap.add_argument("--kill-at", default="6 11:40,7 13:20,8 16:10",
                    help="sim times 'D HH:MM' at which to SIGKILL the controller")
    ap.add_argument("--faults", default="mid_ingest_tx:5,after_approval_post:2,after_apply:3,after_auto_apply:2",
                    help="injected crashes (controller/faults.py), one run each, before the timed kills")
    a = ap.parse_args()
    os.chdir(ROOT)
    url = f"http://127.0.0.1:{a.port}"
    run_dir, base_dir, state_dir = f"runs/mine_{a.name}", f"runs/base_{a.name}", f"state/{a.name}"
    Path("logs").mkdir(exist_ok=True)
    Path("reports").mkdir(exist_ok=True)
    subprocess.run(["rm", "-rf", state_dir], check=True)
    if not Path(base_dir).exists():
        subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", a.scenario, "--run-dir", base_dir],
                       check=True)
    sim = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", a.scenario, "--run-dir", run_dir,
                            "--port", str(a.port), "--force"], stdout=open(f"logs/sim_{a.name}.log", "w"),
                           stderr=subprocess.STDOUT)
    log = open(f"logs/controller_{a.name}.log", "w")
    out = []
    try:
        while True:
            try:
                httpx.get(f"{url}/health", timeout=5)
                break
            except httpx.HTTPError:
                time.sleep(1)
        env = {**os.environ, "STATE_DIR": state_dir}
        kills = []
        for spec in a.kill_at.split(","):
            d, hm = spec.split()
            h, m = hm.split(":")
            kills.append(int(d) * 1440 + int(h) * 60 + int(m))
        for f in [x for x in a.faults.split(",") if x]:
            proc = subprocess.Popen(["./run.sh", url], stdout=log, stderr=subprocess.STDOUT,
                                    env={**env, "CONTROLLER_FAULT": f})
            rc = proc.wait()
            st = httpx.get(f"{url}/sim/state").json()
            msg = f"injected crash '{f}' -> exit {rc} at sim {st['ts']}"
            print(msg, flush=True)
            log.write(f"\n===== {msg} =====\n")
            out.append(f"- {msg}")
        for k in kills:
            proc = subprocess.Popen(["./run.sh", url], stdout=log, stderr=subprocess.STDOUT, env=env)
            if not wait_minute(url, k, proc):
                break
            time.sleep(0.3)  # land somewhere inside the next step (ingest/decide), not on a boundary
            st = httpx.get(f"{url}/sim/state").json()
            db = sqlite3.connect(f"{state_dir}/{st['scenario']}.sqlite")
            inflight = db.execute("SELECT proposal_id, status FROM proposals WHERE status IN "
                                  "('sending','pending','approved','applying')").fetchall()
            db.close()
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            msg = f"SIGKILL at sim {st['ts']} (minute {st['sim_minute']}); proposals in flight: {inflight}"
            print(msg, flush=True)
            log.write(f"\n===== {msg} =====\n")
            log.flush()
            out.append(f"- {msg}")
        proc = subprocess.Popen(["./run.sh", url], stdout=log, stderr=subprocess.STDOUT, env=env)
        rc = proc.wait()
        out.append(f"- final run exited with code {rc}")
    finally:
        sim.send_signal(signal.SIGTERM)
        sim.wait()
    subprocess.run([sys.executable, "-m", "sim", "grade", "--run", run_dir, "--baseline", base_dir, "--out",
                    f"reports/{a.name}_grade.md"], check=True, stdout=subprocess.DEVNULL)

    # ---- invariants
    simlog = [json.loads(l) for l in open(f"{run_dir}/sim_log.jsonl")]
    applied = Counter(r["approval_id"] for r in simlog if r["kind"] == "approval_applied")
    requested = [r for r in simlog if r["kind"] == "approval_requested"]
    sim_calls = 0
    for p in sorted(Path(run_dir, "events").glob("*")):
        sim_calls += sum(1 for line in open(p) if '"type":"call"' in line)
    st_name = next(Path(state_dir).glob("*.sqlite"))
    db = sqlite3.connect(st_name)
    our_calls = db.execute("SELECT SUM(dials) FROM stats WHERE dim='all' AND seg='*' AND hb>=0").fetchone()[0]
    ours = {r[0] for r in db.execute("SELECT approval_id FROM proposals WHERE approval_id IS NOT NULL")}
    recovered = db.execute("SELECT COUNT(*) FROM decisions WHERE summary LIKE '%recovered%'").fetchone()[0]
    left = db.execute("SELECT COUNT(*) FROM proposals WHERE status IN ('sending','applying')").fetchone()[0]
    finished = json.loads(open(f"{run_dir}/_truth/aggregates.json").read())["sim_minute"]
    checks = {
        "scenario ran to the end": finished == json.loads(open(f"{run_dir}/_truth/aggregates.json").read())
        ["sim_minute"] and any(r["kind"] == "sim_finished" for r in simlog),
        f"calls counted once (ours {our_calls}, simulator {sim_calls})": our_calls == sim_calls,
        "no approval applied twice": all(v == 1 for v in applied.values()),
        f"no lost approvals ({len(requested)} requested, all in our store)": {r["approval_id"] for r in requested} <= ours,
        "no proposal left half-sent / half-applied": left == 0,
        "no duplicate_proposal_id errors": "duplicate_proposal_id" not in open(f"logs/controller_{a.name}.log").read(),
        f"recovery paths exercised ({recovered} 'recovered' decisions in our store)": recovered > 0,
    }
    lines = [f"# Crash-recovery test — {a.scenario}", "", "Command: `python scripts/crash_test.py "
             f"--scenario {a.scenario} --name {a.name}`", "", "## Kills", "", *out, "", "## Invariants", ""]
    lines += [f"- [{'x' if ok else ' '}] {name}" for name, ok in checks.items()]
    g = open(f"reports/{a.name}_grade.md").read().split("## Governance")[0]
    lines += ["", "## Grade of the killed-and-restarted run", "", g.split("\n", 2)[2]]
    Path(f"reports/crash_recovery.md").write_text("\n".join(lines))
    print("\n".join(lines))
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
