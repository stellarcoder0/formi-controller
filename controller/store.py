"""SQLite store: the controller's memory and its audit trail.

One file per scenario run. Every table is written inside a transaction together with the work it
describes, so a crash leaves the store either before or after a step, never half-way.

Tables
  meta            key/value (last sim minute seen, ...)
  chunks_done     which event chunks have been ingested (ingestion is idempotent per chunk)
  stats           hourly counters per workflow x dimension x arm x segment
  leads           lead_id -> features (only needed while the lead's campaign is live)
  campaigns       campaign_created events
  config_changes  every config_changed event the simulator emitted (supervisor, ours, ...)
  proposals       every change we tried to make, its full request and its lifecycle
  decisions       append-only audit log: detections, test results, proposals, outcomes
  wf_state        per-workflow learning state (experiments, caller-ID health, incidents) as JSON
  validations     validation runs we paid for (reused instead of paying twice)
  loop_metrics    the loop's own health per step
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chunks_done(chunk INTEGER PRIMARY KEY, kind TEXT, events INTEGER, minute INTEGER);
CREATE TABLE IF NOT EXISTS stats(
  wf TEXT, hb INTEGER, dim TEXT, arm TEXT, seg TEXT,
  dials INTEGER, conn INTEGER, eng INTEGER, flag INTEGER,
  PRIMARY KEY(wf, dim, seg, hb, arm)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS leads(lead_id TEXT PRIMARY KEY, wf TEXT, day INTEGER, feat TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS campaigns(campaign_id TEXT PRIMARY KEY, wf TEXT, minute INTEGER, leads INTEGER,
  config_version INTEGER);
CREATE TABLE IF NOT EXISTS config_changes(wf TEXT, version INTEGER, minute INTEGER, actor TEXT, dims TEXT,
  proposal_id TEXT, approval_id TEXT, PRIMARY KEY(wf, version));
CREATE TABLE IF NOT EXISTS proposals(proposal_id TEXT PRIMARY KEY, wf TEXT, kind TEXT, channel TEXT,
  base_version INTEGER, config TEXT, request TEXT, status TEXT, approval_id TEXT, created_minute INTEGER,
  updated_minute INTEGER, applied_version INTEGER, reasons TEXT);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals(wf, status);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, wf TEXT, kind TEXT,
  summary TEXT, detail TEXT, proposal_id TEXT);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions(wf, minute);
CREATE TABLE IF NOT EXISTS wf_state(wf TEXT PRIMARY KEY, state TEXT);
CREATE TABLE IF NOT EXISTS validations(run_id TEXT PRIMARY KEY, wf TEXT, prompt TEXT, n INTEGER,
  pass_rate REAL, minute INTEGER);
CREATE TABLE IF NOT EXISTS loop_metrics(minute INTEGER PRIMARY KEY, wall REAL, events INTEGER, ingest_s REAL,
  decide_s REAL, credits REAL, rss_mb REAL, actions INTEGER);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)  # we manage transactions explicitly
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._in_tx = False

    @contextmanager
    def tx(self):
        """One atomic unit of work. Nested calls join the outer transaction."""
        if self._in_tx:
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self._in_tx = False

    # ---- meta
    def get_meta(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    # ---- per-workflow state
    def get_state(self, wf: str) -> dict:
        r = self.db.execute("SELECT state FROM wf_state WHERE wf=?", (wf,)).fetchone()
        return json.loads(r[0]) if r else {}

    def put_state(self, wf: str, state: dict):
        self.db.execute("INSERT OR REPLACE INTO wf_state VALUES(?,?)", (wf, json.dumps(state, sort_keys=True)))

    # ---- audit
    def decide(self, minute: int, wf: str, kind: str, summary: str, detail=None, proposal_id=None):
        self.db.execute("INSERT INTO decisions(minute, wf, kind, summary, detail, proposal_id) VALUES(?,?,?,?,?,?)",
                        (minute, wf, kind, summary, json.dumps(detail or {}, default=str), proposal_id))

    # ---- stats queries
    def arm_totals(self, wf: str, dim: str, seg: str = "*", since_hb: int = -10**9, until_hb: int = 10**9) -> dict:
        """{arm: [dials, conn, eng, flag]} summed over hour buckets since_hb <= hb < until_hb."""
        rows = self.db.execute(
            "SELECT arm, SUM(dials), SUM(conn), SUM(eng), SUM(flag) FROM stats "
            "WHERE wf=? AND dim=? AND seg=? AND hb>=? AND hb<? GROUP BY arm",
            (wf, dim, seg, since_hb, until_hb)).fetchall()
        return {r[0]: [r[1], r[2], r[3], r[4]] for r in rows}

    def hourly_totals(self, wf: str, seg: str = "*", since_hb: int = -10**9, until_hb: int = 10**9) -> dict:
        """{hour bucket: [dials, conn, eng, flag]} for all calls of a workflow."""
        rows = self.db.execute(
            "SELECT hb, dials, conn, eng, flag FROM stats WHERE wf=? AND dim='all' AND seg=? AND hb>=? AND hb<?",
            (wf, seg, since_hb, until_hb)).fetchall()
        return {r[0]: list(r[1:]) for r in rows}
