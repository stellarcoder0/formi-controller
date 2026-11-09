"""Event ingestion: chunk -> hourly counters in SQLite.

Each chunk is ingested in one transaction that also records the chunk number in `chunks_done`. A crash
before COMMIT means the chunk is ingested again from scratch on restart; a crash after means it is
skipped. Either way every event is counted exactly once.

What we count, per (workflow, hour bucket):
  dim=welcome_message|voice|caller_id|prompt_version, arm=<variant>, seg='*'       (all calls)
  dim=welcome_message|voice, seg='<feature>=<value>'                               (per segment)
  dim=welcome_message|voice, seg='pv=<prompt>'                                     (per prompt, for decay)
  dim='all', arm='all', seg='*' or '<feature>=<value>'                             (totals)
Each counter holds dials, connected, engaged, hallucination-flagged.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from .faults import fault

FEATURES = ("lang", "city_tier", "age_band", "value_band")
TRACKED_DIMS = ("welcome_message", "voice", "caller_id", "prompt_version")
SEGMENTED_DIMS = ("welcome_message", "voice")
D0 = dt.date(2027, 1, 4)
LEAD_MEMORY_DAYS = 5  # longest cadence we expect is 3-4 days; keep a margin


def ts_to_minute(ts: str, _cache={}) -> int:
    """'2027-01-05T09:07:00+05:30' -> minutes since D0 00:00 IST (negative during history)."""
    day = _cache.get(ts[:10])
    if day is None:
        day = _cache[ts[:10]] = (dt.date.fromisoformat(ts[:10]) - D0).days
    return day * 1440 + int(ts[11:13]) * 60 + int(ts[14:16])


class Ingestor:
    def __init__(self, store):
        self.store = store
        self.leads: dict[str, tuple] = {}  # lead_id -> (day, features tuple)

    def load_leads(self, min_day: int):
        rows = self.store.db.execute("SELECT lead_id, day, feat FROM leads WHERE day>=?", (min_day,))
        self.leads = {lid: (day, tuple(feat.split("|"))) for lid, day, feat in rows}

    def prune_leads(self, today: int):
        cutoff = today - LEAD_MEMORY_DAYS
        self.leads = {k: v for k, v in self.leads.items() if v[0] >= cutoff}
        self.store.db.execute("DELETE FROM leads WHERE day<?", (cutoff,))

    def ingest_chunk(self, n: int, kind: str, text: str) -> int:
        """Ingest one chunk atomically. Returns the number of events processed (0 if already done)."""
        db = self.store.db
        if db.execute("SELECT 1 FROM chunks_done WHERE chunk=?", (n,)).fetchone():
            return 0
        acc = defaultdict(lambda: [0, 0, 0, 0])
        new_leads, campaigns, changes = [], [], []
        last_minute = None
        count = 0
        for line in text.splitlines():
            if not line:
                continue
            ev = json.loads(line)
            count += 1
            t = ev["type"]
            if t == "call":
                self._count_call(ev, acc)
            elif t == "lead":
                m = ts_to_minute(ev["ts"])
                f = ev["features"]
                feat = tuple(f[k] for k in FEATURES)
                self.leads[ev["lead_id"]] = (m // 1440, feat)
                new_leads.append((ev["lead_id"], ev["workflow_id"], m // 1440, "|".join(feat)))
            elif t == "campaign_created":
                campaigns.append((ev["campaign_id"], ev["workflow_id"], ts_to_minute(ev["ts"]), ev["leads"],
                                  ev["config_version_at_creation"]))
            elif t == "config_changed":
                changes.append((ev["workflow_id"], ev["version"], ts_to_minute(ev["ts"]), ev["actor"],
                                json.dumps(ev["changed_dims"]), ev.get("proposal_id"), ev.get("approval_id")))
            last_minute = ev.get("ts")
        with self.store.tx():
            db.executemany(
                "INSERT INTO stats VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(wf,dim,seg,hb,arm) DO UPDATE SET "
                "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng, flag=flag+excluded.flag",
                [(wf, hb, dim, arm, seg, *v) for (wf, hb, dim, arm, seg), v in acc.items()])
            db.executemany("INSERT OR IGNORE INTO leads VALUES(?,?,?,?)", new_leads)
            db.executemany("INSERT OR IGNORE INTO campaigns VALUES(?,?,?,?,?)", campaigns)
            db.executemany("INSERT OR IGNORE INTO config_changes VALUES(?,?,?,?,?,?,?)", changes)
            fault("mid_ingest_tx")  # stats written, chunk not yet marked done
            db.execute("INSERT INTO chunks_done VALUES(?,?,?,?)",
                       (n, kind, count, ts_to_minute(last_minute) if last_minute else None))
        return count

    def _count_call(self, ev: dict, acc):
        wf = ev["workflow_id"]
        hb = ts_to_minute(ev["ts"]) // 60
        conn = 1 if ev["connected"] else 0
        eng = 1 if ev["engaged"] else 0
        flag = 1 if conn and ev["qa"] and ev["qa"]["hallucination_flag"] else 0
        v = ev["variant"]

        def add(dim, arm, seg):
            a = acc[(wf, hb, dim, arm, seg)]
            a[0] += 1
            a[1] += conn
            a[2] += eng
            a[3] += flag

        add("all", "all", "*")
        for dim in TRACKED_DIMS:
            add(dim, v[dim], "*")
        pv_seg = "pv=" + v["prompt_version"]
        for dim in SEGMENTED_DIMS:
            add(dim, v[dim], pv_seg)
        lead = self.leads.get(ev["lead_id"])
        if lead:
            for name, val in zip(FEATURES, lead[1]):
                seg = f"{name}={val}"
                add("all", "all", seg)
                for dim in SEGMENTED_DIMS:
                    add(dim, v[dim], seg)
