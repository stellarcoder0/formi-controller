"""Answer questions from the controller's own store (never the simulator's logs).

  python -m controller.audit --db state/public_02_incidents.sqlite why ks_cancel --at "D3 14:00"
  python -m controller.audit --db ... timeline ks_cancel
  python -m controller.audit --db ... status            # one screen per workflow (supervisor view)
  python -m controller.audit --db ... health            # the loop's own health
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from .stats import rate


def when(minute: int) -> str:
    if minute is None:
        return "?"
    d, m = divmod(minute, 1440)
    return f"D{d} {m // 60:02d}:{m % 60:02d}"


def parse_when(s: str) -> int:
    day, hm = s.strip().split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * 1440 + int(h) * 60 + int(m)


def kpis(db, wf, since_hb, until_hb):
    r = db.execute("SELECT SUM(dials), SUM(conn), SUM(eng), SUM(flag) FROM stats WHERE wf=? AND dim='all' "
                   "AND seg='*' AND hb>=? AND hb<?", (wf, since_hb, until_hb)).fetchone()
    d, c, e, f = (x or 0 for x in r)
    return f"dials={d} pickup={rate(c, d):.3f} engagement={rate(e, c):.3f} halluc_flag_rate={rate(f, c):.4f}"


def why(db, wf, at):
    row = db.execute("SELECT version, minute, actor, dims, proposal_id, approval_id FROM config_changes "
                     "WHERE wf=? AND minute<=? ORDER BY version DESC LIMIT 1", (wf, at)).fetchone()
    if not row:
        print(f"{wf} ran its business default config at {when(at)} (no change recorded before then).")
        return
    version, minute, actor, dims, pid, aid = row
    print(f"== {wf} at {when(at)}: config v{version}, applied {when(minute)} by {actor}, changed {json.loads(dims)}")
    if actor == "supervisor":
        print("   A client supervisor edited the config directly (this bypasses governance by design).")
        for m, kind, summary in db.execute("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute>=? "
                                           "AND minute<=? ORDER BY id", (wf, minute, minute + 1440)):
            print(f"   {when(m)} {kind}: {summary}")
    else:
        p = db.execute("SELECT kind, channel, request, status, approval_id, created_minute FROM proposals "
                       "WHERE proposal_id=?", (pid,)).fetchone()
        if p:
            kind, channel, req, status, aid2, created = p
            req = json.loads(req)
            print(f"   proposal {pid} ({kind}) created {when(created)} via {channel}")
            if channel == "approval":
                print(f"   approved by the client approver on {aid2}; status now {status}")
            else:
                print("   auto-applied under the workflow's governance.auto_apply policy (no human in the loop)")
            print(f"   summary  : {req.get('change_summary') or req.get('summary')}")
            if req.get("diff"):
                print(f"   diff     : {json.dumps(req['diff'])}")
            if req.get("dry_run"):
                dr = req["dry_run"]
                print(f"   dry run  : {json.dumps(dr.get('dims'))[:400]}"
                      f"{'' if dr.get('prompt_note') else ' expected relative change ' + str(dr.get('expected_relative_change'))}")
            if "impact_score" in req:
                print(f"   impact   : {req['impact_score']}  affected leads: {req.get('affected_leads')}")
            if req.get("evidence"):
                print(f"   evidence : {json.dumps(req['evidence'])}")
            print("   decision trail:")
            for m, k, s in db.execute("SELECT minute, kind, summary FROM decisions WHERE proposal_id=? ORDER BY id",
                                      (pid,)):
                print(f"     {when(m)} {k}: {s}")
            print(f"   rationale: {(req.get('rationale') or '')[:600]}")
    hb = minute // 60
    print("   what happened next:")
    print(f"     24h before : {kpis(db, wf, hb - 24, hb)}")
    print(f"     24h after  : {kpis(db, wf, hb, hb + 24)}")
    for m, kind, summary in db.execute("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute>? AND "
                                       "minute<=? AND kind NOT IN ('proposal_sent') ORDER BY id LIMIT 12",
                                       (wf, minute, minute + 2 * 1440)):
        print(f"     {when(m)} {kind}: {summary}")
    nxt = db.execute("SELECT version, minute, actor, dims FROM config_changes WHERE wf=? AND version>? "
                     "ORDER BY version LIMIT 1", (wf, version)).fetchone()
    if nxt:
        print(f"   next change: v{nxt[0]} at {when(nxt[1])} by {nxt[2]} {json.loads(nxt[3])}")


def timeline(db, wf):
    for m, kind, summary in db.execute("SELECT minute, kind, summary FROM decisions WHERE wf=? ORDER BY id", (wf,)):
        print(f"{when(m):>10}  {kind:<20} {summary}")


def status(db):
    last = db.execute("SELECT MAX(minute) FROM loop_metrics").fetchone()[0] or 0
    for (wf,) in db.execute("SELECT DISTINCT wf FROM wf_state ORDER BY wf").fetchall():
        st = json.loads(db.execute("SELECT state FROM wf_state WHERE wf=?", (wf,)).fetchone()[0])
        print(f"== {wf}  (as of {when(last)})")
        hb = last // 60
        print(f"   today so far : {kpis(db, wf, hb - hb % 24, hb + 1)}")
        for dim, d in st.get("dims", {}).items():
            extra = {"exploring": f"arms {d.get('arms')} vs champion {d.get('champion')}",
                     "promoted": f"winner {d.get('winner')} (was {d.get('prev_champion')})",
                     "hold": "supervisor edit, hands off", "cooldown": f"rolled back {d.get('decayed')}"}
            print(f"   test {dim:<15}: {d['phase']} {extra.get(d['phase'], '')}")
        print(f"   caller IDs   : " + ", ".join(f"{k}={v['state']}" for k, v in st.get("did", {}).items()))
        inc = st.get("incident", {})
        print(f"   incident     : {'OPEN on ' + inc['bad_prompt'] if inc.get('open') else 'none open'}")
        for pid, kind, status_, aid in db.execute(
                "SELECT proposal_id, kind, status, approval_id FROM proposals WHERE wf=? AND status IN "
                "('pending','approved','sending','applying')", (wf,)):
            print(f"   WAITING ON YOU: {aid} ({kind}) {status_}")
        for m, kind, summary in db.execute("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute>=? "
                                           "AND kind IN ('applied','auto_applied','supervisor_edit','incident_opened',"
                                           "'incident_resolved','decay_detected') ORDER BY id",
                                           (wf, last - last % 1440)):
            print(f"   changed today: {when(m)} {kind}: {summary[:140]}")


def health(db):
    rows = db.execute("SELECT minute, wall, events, ingest_s, decide_s, credits, rss_mb, actions FROM loop_metrics "
                      "ORDER BY minute").fetchall()
    if not rows:
        print("no metrics yet")
        return
    days = {}
    for m, wall, ev, ing, dec, cr, rss, act in rows:
        d = days.setdefault(m // 1440, [0, 0, 0.0, 0.0, 0.0, 0, 0])
        d[0] += ev
        d[1] += act
        d[2] += wall
        d[3] += ing
        d[4] = max(d[4], rss)
        d[5] = cr
    print("day  events     actions  wall_s  ingest_s  ev/s(ingest)  peak_rss_MB  credits_left")
    for day, (ev, act, wall, ing, rss, cr, _) in sorted(days.items()):
        print(f"D{day:<3} {ev:<10} {act:<8} {wall:<7.1f} {ing:<9.1f} {ev / ing if ing else 0:<13.0f} {rss:<12.0f} {cr}")


def main():
    ap = argparse.ArgumentParser(prog="controller.audit")
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("why")
    w.add_argument("workflow")
    w.add_argument("--at", required=True, help='e.g. "D3 14:00"')
    t = sub.add_parser("timeline")
    t.add_argument("workflow")
    sub.add_parser("status")
    sub.add_parser("health")
    a = ap.parse_args()
    db = sqlite3.connect(a.db)
    if a.cmd == "why":
        why(db, a.workflow, parse_when(a.at))
    elif a.cmd == "timeline":
        timeline(db, a.workflow)
    elif a.cmd == "status":
        status(db)
    else:
        health(db)


if __name__ == "__main__":
    main()
