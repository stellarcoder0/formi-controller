"""Governance client: every config change we make goes through here.

Two channels:
  approval  POST /approvals -> human decides -> POST /workflows/{id}/apply
  auto      PUT /workflows/{id}/config, only when the workflow's policy permits the change

Proposal lifecycle (column `proposals.status`):

  sending ──► pending ──► approved ──► applying ──► applied
     │           │            │            │
     │           ├► rejected  ├► superseded (a safety fix is in flight; we let this one lapse)
     │           └► expired   └► lapsed / stale (config changed underneath us -> re-plan from fresh data)
     └► not_sent (crashed before the simulator saw it, or it refused the request)

`sending` and `applying` are written *before* the HTTP call, so after a crash we know exactly which
calls might have landed and reconcile them against the simulator (by proposal_id / approval_id)
instead of guessing. proposal_ids are unique, so a retried POST can never create a second request.
"""
from __future__ import annotations

import json

from .client import ApiError
from .faults import fault

LIVE = ("sending", "pending", "approved", "applying")
SAFETY_KINDS = ("incident", "rollback", "did_health")


class Governance:
    def __init__(self, client, store, log):
        self.c = client
        self.s = store
        self.log = log

    # ------------------------------------------------------------------ queries
    def live(self, wf: str, kinds=None) -> list[dict]:
        rows = self.s.db.execute(
            "SELECT proposal_id, kind, channel, status, approval_id, created_minute FROM proposals "
            f"WHERE wf=? AND status IN ({','.join('?' * len(LIVE))})", (wf, *LIVE)).fetchall()
        out = [dict(zip(("proposal_id", "kind", "channel", "status", "approval_id", "created_minute"), r)) for r in rows]
        return [p for p in out if kinds is None or p["kind"] in kinds]

    def requests_last_24h(self, wf: str, now: int) -> int:
        return self.s.db.execute(
            "SELECT COUNT(*) FROM proposals WHERE wf=? AND channel='approval' AND status!='not_sent' "
            "AND created_minute>?", (wf, now - 1440)).fetchone()[0]

    def sent_today(self, wf: str, now: int, kind: str) -> int:
        day0 = (now // 1440) * 1440
        return self.s.db.execute(
            "SELECT COUNT(*) FROM proposals WHERE wf=? AND kind=? AND status!='not_sent' AND created_minute>=?",
            (wf, kind, day0)).fetchone()[0]

    def applied_today(self, wf: str, now: int, kind: str) -> int:
        day0 = (now // 1440) * 1440
        return self.s.db.execute(
            "SELECT COUNT(*) FROM proposals WHERE wf=? AND kind=? AND status='applied' AND created_minute>=?",
            (wf, kind, day0)).fetchone()[0]

    # ------------------------------------------------------------------ actions
    def _new_id(self, wf: str, kind: str, now: int) -> str:
        n = self.s.db.execute("SELECT COUNT(*) FROM proposals WHERE wf=?", (wf,)).fetchone()[0]
        return f"{wf}:{kind}:m{now}:{n + 1}"

    def _record(self, pid, wf, kind, channel, base_version, cfg, request, now):
        with self.s.tx():
            self.s.db.execute(
                "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, wf, kind, channel, base_version, json.dumps(cfg), json.dumps(request, default=str),
                 "sending", None, now, now, None, None))

    def _set(self, pid, now, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        self.s.db.execute(f"UPDATE proposals SET {sets}, updated_minute=? WHERE proposal_id=?",
                          (*cols.values(), now, pid))

    def request_approval(self, wf: str, kind: str, base_version: int, cfg: dict, now: int, *, summary: str,
                         diff: dict, affected_leads: int, dry_run: dict, impact_score: float, rationale: str,
                         evidence: dict | None = None, ttl_hours: float | None = None) -> str | None:
        pid = self._new_id(wf, kind, now)
        body = {"proposal_id": pid, "workflow_id": wf, "base_version": base_version, "proposed_config": cfg,
                "change_summary": summary, "diff": diff, "affected_leads": int(affected_leads),
                "dry_run": dry_run, "impact_score": round(float(min(max(impact_score, 0.0), 1.0)), 3),
                "rationale": rationale}
        if evidence:
            body["evidence"] = evidence
        if ttl_hours:
            body["expires_in_hours"] = ttl_hours
        self._record(pid, wf, kind, "approval", base_version, cfg, {k: v for k, v in body.items()
                                                                   if k != "proposed_config"}, now)
        try:
            r = self.c.post("/approvals", body)
            fault("after_approval_post")
        except ApiError as e:
            if e.code == "duplicate_proposal_id":  # a retry of a POST that already landed
                return self._reconcile_sending(pid, wf, now)
            with self.s.tx():
                self._set(pid, now, status="not_sent", reasons=json.dumps(e.body))
                self.s.decide(now, wf, "proposal_refused", f"simulator refused {kind} proposal: {e.code}",
                              e.body, pid)
            self.log(f"{wf}: proposal {pid} refused: {e.status} {e.body}")
            return None
        with self.s.tx():
            self._set(pid, now, status="pending", approval_id=r["approval_id"])
            self.s.decide(now, wf, "proposal_sent", summary,
                          {"approval_id": r["approval_id"], "kind": kind, "impact_score": body["impact_score"],
                           "affected_leads": body["affected_leads"], "diff": diff, "dry_run": dry_run,
                           "rationale": rationale, "evidence": evidence}, pid)
        self.log(f"{wf}: requested approval {r['approval_id']} ({kind}): {summary}")
        return pid

    def auto_apply(self, wf: str, kind: str, base_version: int, cfg: dict, now: int, *, summary: str,
                   rationale: str, evidence: dict | None = None) -> int | None:
        pid = self._new_id(wf, kind, now)
        body = {"proposal_id": pid, "base_version": base_version, "proposed_config": cfg, "rationale": rationale,
                "evidence": evidence or {}}
        self._record(pid, wf, kind, "auto", base_version, cfg, {"summary": summary, "rationale": rationale,
                                                                "evidence": evidence}, now)
        try:
            r = self.c.put(f"/workflows/{wf}/config", body)
            fault("after_auto_apply")
        except ApiError as e:
            if e.code == "duplicate_proposal_id":
                self._reconcile_sending(pid, wf, now)
                return None
            with self.s.tx():
                self._set(pid, now, status="not_sent", reasons=json.dumps(e.body))
                self.s.decide(now, wf, "auto_apply_refused", f"{summary} — refused: {e.code}", e.body, pid)
            self.log(f"{wf}: auto-apply {pid} refused: {e.status} {e.body}")
            return None
        with self.s.tx():
            self._set(pid, now, status="applied", applied_version=r["version"])
            self.s.decide(now, wf, "auto_applied", summary,
                          {"version": r["version"], "changed_dims": r["changed_dims"], "rationale": rationale,
                           "evidence": evidence, "policy": "governance.auto_apply"}, pid)
        self.log(f"{wf}: auto-applied v{r['version']} ({kind}): {summary}")
        return r["version"]

    # ------------------------------------------------------------------ reconciliation (every step + after restart)
    def reconcile(self, now: int):
        rows = self.s.db.execute(
            f"SELECT proposal_id, wf, kind, channel, status, approval_id FROM proposals "
            f"WHERE status IN ({','.join('?' * len(LIVE))}) ORDER BY created_minute", LIVE).fetchall()
        for pid, wf, kind, channel, status, aid in rows:
            if status == "sending":
                self._reconcile_sending(pid, wf, now)
                continue
            if channel != "approval":
                continue
            try:
                a = self.c.get(f"/approvals/{aid}")
            except ApiError as e:
                self.log(f"{wf}: cannot read approval {aid}: {e}")
                continue
            st = a["status"]
            if st in ("rejected", "expired", "lapsed"):
                with self.s.tx():
                    self._set(pid, now, status=st, reasons=json.dumps(a.get("reasons") or []))
                    self.s.decide(now, wf, f"approval_{st}", f"{aid} {st}", {"reasons": a.get("reasons")}, pid)
                self.log(f"{wf}: approval {aid} {st} {a.get('reasons') or ''}")
            elif st == "applied":  # we applied it but crashed before recording
                ver = self._version_for(wf, approval_id=aid)
                with self.s.tx():
                    self._set(pid, now, status="applied", applied_version=ver)
                    self.s.decide(now, wf, "applied", f"{aid} applied as v{ver} (recovered after restart)", {}, pid)
            elif st == "approved":
                if status == "pending":
                    with self.s.tx():
                        self._set(pid, now, status="approved")
                        self.s.decide(now, wf, "approval_approved", f"{aid} approved by the client approver",
                                      {"decided_at": a.get("decided_at"), "apply_by": a.get("apply_by")}, pid)
                self._apply(pid, wf, kind, aid, now)

    def _apply(self, pid, wf, kind, aid, now):
        if kind not in SAFETY_KINDS and self.live(wf, SAFETY_KINDS):
            # A safety fix is waiting for approval. Applying this would make it stale, so this one yields.
            with self.s.tx():
                self._set(pid, now, status="superseded")
                self.s.decide(now, wf, "superseded", f"{aid} not applied: a safety change is in flight", {}, pid)
            return
        with self.s.tx():
            self._set(pid, now, status="applying")
        try:
            r = self.c.post(f"/workflows/{wf}/apply", {"approval_id": aid})
            fault("after_apply")
        except ApiError as e:
            status = {"approval_lapsed": "lapsed", "stale_base_version": "stale"}.get(e.code)
            if status is None and e.code == "not_approved" and (e.body.get("detail") or {}).get("status") == "applied":
                ver = self._version_for(wf, approval_id=aid)
                with self.s.tx():
                    self._set(pid, now, status="applied", applied_version=ver)
                return
            with self.s.tx():
                self._set(pid, now, status=status or "failed", reasons=json.dumps(e.body))
                self.s.decide(now, wf, f"apply_{status or 'failed'}", f"apply of {aid} failed: {e.code}", e.body, pid)
            self.log(f"{wf}: apply {aid} failed: {e.status} {e.body}")
            return
        with self.s.tx():
            self._set(pid, now, status="applied", applied_version=r["version"])
            self.s.decide(now, wf, "applied", f"{aid} applied as v{r['version']}",
                          {"changed_dims": r["changed_dims"]}, pid)
        self.log(f"{wf}: applied {aid} -> v{r['version']} dims={r['changed_dims']}")

    def _version_for(self, wf, approval_id=None, proposal_id=None):
        for v in self.c.get(f"/workflows/{wf}/config/history"):
            if (approval_id and v.get("approval_id") == approval_id) or \
                    (proposal_id and v.get("proposal_id") == proposal_id):
                return v["version"]
        return None

    def _reconcile_sending(self, pid, wf, now):
        """We wrote the intent but don't know whether the simulator saw the call. Ask it."""
        ch = self.s.db.execute("SELECT channel FROM proposals WHERE proposal_id=?", (pid,)).fetchone()[0]
        if ch == "auto":
            ver = self._version_for(wf, proposal_id=pid)
            with self.s.tx():
                if ver:
                    self._set(pid, now, status="applied", applied_version=ver)
                    self.s.decide(now, wf, "auto_applied", f"auto-apply {pid} landed as v{ver} (recovered)", {}, pid)
                else:
                    self._set(pid, now, status="not_sent")
            return None
        match = [a for a in self.c.get("/approvals", workflow_id=wf) if a["proposal_id"] == pid]
        with self.s.tx():
            if match:
                self._set(pid, now, status="pending", approval_id=match[0]["approval_id"])
                self.s.decide(now, wf, "proposal_sent", f"{pid} found as {match[0]['approval_id']} (recovered)",
                              {}, pid)
            else:
                self._set(pid, now, status="not_sent")
        return pid if match else None
