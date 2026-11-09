"""The control loop: ingest -> reconcile governance -> decide per workflow -> advance the clock.

Every step is safe to repeat. If the process dies anywhere, restarting runs the same step again:
already-ingested chunks are skipped, in-flight proposals are reconciled against the simulator by id,
and the per-workflow state is re-read from SQLite.
"""
from __future__ import annotations

import copy
import json
import resource
import sys
import time

from .callerid import MIN_CHANGE_TV, CallerIds
from .client import ApiError
from .experiments import DIMS, Experiments, observe, put_on_hold
from .governance import Governance
from .incidents import Incidents
from .ingest import Ingestor
from .stats import normalise, rate, step_towards, tv_distance

STEP_MINUTES = 60
SUPERVISOR_HOLD_H = 24


def log(msg: str):
    print(msg, flush=True)


class Controller:
    def __init__(self, client, store, step_minutes: int = STEP_MINUTES):
        self.c = client
        self.s = store
        self.step_minutes = step_minutes
        self.ing = Ingestor(store)
        self.gov = Governance(client, store, log)
        self.exp = Experiments(store, log)
        self.dids = CallerIds(store, log)
        self.inc = Incidents(client, store, log)
        self.meta = {w["workflow_id"]: w for w in client.get("/workflows")}

    # ------------------------------------------------------------------ main loop
    def run(self, max_steps: int | None = None):
        state = self.c.get("/sim/state")
        self.ing.load_leads(state["sim_minute"] // 1440 - 5)
        log(f"controller up: {state['scenario']} at {state['ts']}, {len(self.meta)} workflows, "
            f"{len(self.ing.leads)} leads in memory")
        steps = 0
        while True:
            t0 = time.time()
            now = state["sim_minute"]
            events = self.ingest()
            t1 = time.time()
            self.gov.reconcile(now)
            actions = 0
            for wf in sorted(self.meta):
                actions += self.step_workflow(wf, now)
            t2 = time.time()
            if now % 1440 == 0:
                self.ing.prune_leads(now // 1440)
            self.record_metrics(now, t0, events, t1 - t0, t2 - t1, actions)
            if state["finished"]:
                log(f"scenario finished at {state['ts']}")
                return
            if max_steps is not None and steps >= max_steps:
                return
            try:
                state = self.c.post("/sim/advance", {"minutes": self.step_minutes})
            except ApiError as e:
                if e.code != "finished":
                    raise
                state = self.c.get("/sim/state")
            self.s.set_meta("last_sim_minute", state["sim_minute"])
            steps += 1

    def ingest(self) -> int:
        done = {r[0] for r in self.s.db.execute("SELECT chunk FROM chunks_done")}
        todo = []
        for kind in ("history", "events"):
            todo += [ch for ch in self.c.get("/events/chunks", kind=kind) if ch["chunk"] not in done]
        n = 0
        for ch in sorted(todo, key=lambda x: x["chunk"]):
            n += self.ing.ingest_chunk(ch["chunk"], ch["kind"], self.c.chunk_text(ch["chunk"]))
        return n

    def record_metrics(self, now, t0, events, ingest_s, decide_s, actions):
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
        credits = self.c.get("/sim/state")["credits_remaining"]
        self.s.db.execute("INSERT OR REPLACE INTO loop_metrics VALUES(?,?,?,?,?,?,?,?)",
                          (now, time.time() - t0, events, ingest_s, decide_s, credits, rss_mb, actions))
        if now % 1440 == 0 or events > 100_000:
            log(f"[D{now // 1440} {now % 1440 // 60:02d}:00] events={events} ingest={ingest_s:.1f}s "
                f"decide={decide_s:.1f}s credits={credits} peak_rss={rss_mb:.0f}MB")

    # ------------------------------------------------------------------ per workflow
    def step_workflow(self, wf: str, now: int) -> int:
        meta = self.meta[wf]
        cur = self.c.get(f"/workflows/{wf}/config")
        cfg, version = cur["config"], cur["version"]
        st = self.s.get_state(wf)
        if "did" not in st:
            st["did"] = self.dids.init_state(wf, meta, cfg, now)
        self.process_config_changes(wf, st, now)
        observe(st, cfg, now)
        actions = 0

        # 1. hallucination incident (highest priority)
        did_notes = self.dids.update(wf, st, now)
        bad = self.inc.check(wf, cfg, st, now)
        if bad and not self.gov.live(wf, ["incident"]):
            actions += self.fix_incident(wf, meta, cfg, version, st, bad, now)
            cur = self.c.get(f"/workflows/{wf}/config")  # may have changed if we applied something
            cfg, version = cur["config"], cur["version"]

        # 2. decay rollback (safety)
        changes, notes, ev = self.exp.check_decay(wf, cfg, st, now)
        if changes and not self.gov.live(wf, ["rollback"]):
            new = self.with_changes(cfg, changes)
            actions += self.propose(wf, meta, cfg, version, new, "rollback", notes, ev, st, now)

        # 3. caller-ID health / spread
        actions += self.caller_ids(wf, meta, cfg, version, st, did_notes, now)

        # 4. daily batched proposal (experiments + non-urgent caller-ID changes)
        if self.can_plan(wf, meta, st, now):
            changes, notes, ev = self.exp.plan(wf, meta, cfg, st, now)
            target = self.dids.target(st)
            if target and not self.auto_allowed(meta, "caller_id") and not self.did_on_hold(st, now) \
                    and tv_distance(cfg["allocations"]["default"]["caller_id"], target) > MIN_CHANGE_TV:
                changes["caller_id"] = target
                notes.append(f"spread caller IDs: {fmt(target)}")
            new = self.with_changes(cfg, changes)
            if self.differs(cfg, new):
                actions += self.propose(wf, meta, cfg, version, new, "experiment", notes, ev, st, now)
        self.s.put_state(wf, st)
        return actions

    def process_config_changes(self, wf, st, now):
        seen = st.get("seen_version", 0)
        rows = self.s.db.execute("SELECT version, minute, actor, dims FROM config_changes WHERE wf=? AND version>? "
                                 "ORDER BY version", (wf, seen)).fetchall()
        for version, minute, actor, dims in rows:
            dims = json.loads(dims)
            if actor == "supervisor" and minute >= 0:
                held = [d for d in dims if d in DIMS]
                for d in held:
                    put_on_hold(st, d, now, version)
                if "caller_id" in dims:
                    st["did_hold_until"] = now + SUPERVISOR_HOLD_H * 60
                self.s.decide(now, wf, "supervisor_edit",
                              f"supervisor changed {dims} (v{version}); we will not touch {held or 'nothing'}"
                              f"{' and caller_id' if 'caller_id' in dims else ''} for {SUPERVISOR_HOLD_H}h",
                              {"version": version, "dims": dims, "edited_at_minute": minute})
                log(f"{wf}: supervisor edit v{version} {dims}")
            st["seen_version"] = version

    # ------------------------------------------------------------------ incident
    def fix_incident(self, wf, meta, cfg, version, st, bad, now) -> int:
        self.s.put_state(wf, st)
        if self.gov.requests_last_24h(wf, now) >= meta["governance"]["max_requests_per_24h"]:
            self.s.decide(now, wf, "quota_blocked", "incident fix waiting: approval quota for 24h used up", {})
            return 0
        fix = self.inc.choose_fix(wf, meta, st, bad, now)
        if not fix:
            self.s.decide(now, wf, "no_fix", f"no validated fix available for {bad} yet; will retry", {})
            return 0
        pv, val, how = fix
        new = copy.deepcopy(cfg)
        new["prompt_version"] = pv
        ev = {"incident": {k: v for k, v in st["incident"].items() if k != "failed_fixes"}, "validation": val,
              "selection": how}
        notes = [f"switch prompt {bad} -> {pv} because hallucination flags are elevated on {bad}; "
                 f"{pv} validated at {val['pass_rate']:.3f}"]
        return self.propose(wf, meta, cfg, version, new, "incident", notes, ev, st, now,
                            evidence={"validation_run_id": val["validation_run_id"]})

    # ------------------------------------------------------------------ caller IDs
    def caller_ids(self, wf, meta, cfg, version, st, notes, now) -> int:
        target = self.dids.target(st)
        cur = cfg["allocations"]["default"]["caller_id"]
        if not target or tv_distance(cur, target) <= MIN_CHANGE_TV:
            return 0
        urgent = self.dids.urgent(st, cur)
        if self.did_on_hold(st, now) and not urgent:
            return 0
        if self.auto_allowed(meta, "caller_id"):
            if self.gov.live(wf) and not urgent:
                return 0  # don't make a pending approval stale for a non-urgent tweak
            pol = meta["governance"]["auto_apply"]
            step = step_towards(cur, target, pol.get("max_shift", 0) * 0.98)
            new = self.with_changes(cfg, {"caller_id": step})
            if not self.differs(cfg, new):
                return 0
            reason = "; ".join(notes) or f"move caller IDs towards {fmt(target)}"
            if step != normalise(target):
                reason += f" (step limited by policy max_shift {pol.get('max_shift')}; full target {fmt(target)})"
            self.s.put_state(wf, st)
            v = self.gov.auto_apply(wf, "did_health" if urgent else "did_spread", version, new, now,
                                    summary=f"caller IDs {fmt(cur)} -> {fmt(step)}", rationale=reason,
                                    evidence={"did_state": st["did"], "pickup_24h": self.did_pickup(wf, now)})
            return 1 if v else 0
        if urgent and not self.gov.live(wf, ["did_health"]):
            new = self.with_changes(cfg, {"caller_id": target})
            return self.propose(wf, meta, cfg, version, new, "did_health", notes or ["stop using a flagged caller ID"],
                                {"did_state": st["did"], "pickup_24h": self.did_pickup(wf, now)}, st, now)
        return 0

    def did_pickup(self, wf, now):
        t = self.s.arm_totals(wf, "caller_id", "*", since_hb=now // 60 - 24)
        return {d: {"dials": v[0], "pickup": round(rate(v[1], v[0]), 4)} for d, v in t.items()}

    # ------------------------------------------------------------------ packaging a proposal
    def propose(self, wf, meta, cfg, version, new, kind, notes, ev, st, now, evidence=None) -> int:
        safety = kind in ("incident", "rollback", "did_health")
        limit = meta["governance"]["max_requests_per_24h"] - (0 if safety else 1)
        if self.gov.requests_last_24h(wf, now) >= limit:
            self.s.decide(now, wf, "quota_blocked", f"{kind} proposal held back: {limit} requests in 24h used", {})
            return 0
        self.s.put_state(wf, st)
        diff = {d: {"from": a, "to": b} for d, a, b in self.diff_dims(cfg, new)}
        dry = self.dry_run(wf, cfg, new, now)
        affected = self.affected_leads(wf, meta, cfg, new, now)
        impact = {"incident": 1.0, "did_health": 0.8}.get(kind)
        if impact is None:
            impact = min(1.0, max(0.1, abs(dry.get("expected_relative_change", 0)) * 5))
            if kind == "rollback":
                impact = max(impact, 0.6)
        summary = f"{wf}: " + "; ".join(notes)
        rationale = ("Population-level evidence from this workflow's own calls (randomised arms, same hours). "
                     "Winners are chosen by a two-proportion z-test with Bonferroni correction, never by an LLM. "
                     + json.dumps(ev, default=str)[:3000])
        pid = self.gov.request_approval(wf, kind, version, new, now, summary=summary[:500], diff=diff,
                                        affected_leads=affected, dry_run=dry, impact_score=impact,
                                        rationale=rationale, evidence=evidence)
        return 1 if pid else 0

    @staticmethod
    def diff_dims(cfg, new):
        out = []
        for d in ("welcome_message", "voice", "caller_id"):
            a, b = cfg["allocations"]["default"][d], new["allocations"]["default"][d]
            if tv_distance(a, b) > 1e-6:
                out.append((d, a, b))
        if cfg["prompt_version"] != new["prompt_version"]:
            out.append(("prompt_version", cfg["prompt_version"], new["prompt_version"]))
        return out

    def dry_run(self, wf, cfg, new, now) -> dict:
        """Replay the proposed allocation against what each arm actually did on this workflow (last 7 days)."""
        out = {"method": "expected rate = sum over arms of (proposed weight x observed rate of that arm, last 7 "
                         "days); arms with <100 connected calls are assumed equal to the current mix",
               "dims": {}}
        total = 1.0
        since = now // 60 - 24 * 7
        for d, a, b in self.diff_dims(cfg, new):
            if d == "prompt_version":
                t = self.s.arm_totals(wf, "prompt_version", "*", since_hb=since)
                out["dims"][d] = {p: {"connected": v[1], "flag_rate": round(rate(v[3], v[1]), 4),
                                      "engagement": round(rate(v[2], v[1]), 4)} for p, v in t.items()}
                continue
            t = self.s.arm_totals(wf, d, "*", since_hb=since)
            metric = 1 if d == "caller_id" else 2  # pickup for caller IDs, engagement otherwise
            denom = 0 if d == "caller_id" else 1
            r = {k: rate(v[metric], v[denom]) for k, v in t.items() if v[denom] >= 100}
            base = sum(w * r.get(k, 0) for k, w in normalise(a).items() if k in r)
            wsum = sum(w for k, w in normalise(a).items() if k in r) or 1
            base /= wsum
            exp_new = sum(w * r.get(k, base) for k, w in normalise(b).items())
            rel = exp_new / base - 1 if base else 0.0
            if d != "caller_id":
                total *= 1 + rel
            out["dims"][d] = {"kpi": "pickup_rate" if d == "caller_id" else "engagement_rate",
                              "current_expected": round(base, 4), "proposed_expected": round(exp_new, 4),
                              "relative_change": round(rel, 4),
                              "arms": {k: round(v, 4) for k, v in r.items()},
                              "arms_without_data": [k for k in b if k not in r]}
        out["expected_relative_change"] = round(total - 1, 4)
        return out

    def affected_leads(self, wf, meta, cfg, new, now) -> int:
        """Leads that will be dialled while this config is live: leads still inside their cadence, times the
        share of traffic whose treatment changes. For next_campaign_only workflows only the next batch counts."""
        days = len(meta["cadence"])
        live = self.s.db.execute("SELECT COALESCE(SUM(leads),0) FROM campaigns WHERE wf=? AND minute>?",
                                 (wf, now - days * 1440)).fetchone()[0]
        shares = [1.0 if d == "prompt_version" else tv_distance(a, b) for d, a, b in self.diff_dims(cfg, new)]
        share = max(shares or [0])
        agent_only = all(d in ("welcome_message", "voice", "caller_id") for d, _, _ in self.diff_dims(cfg, new))
        if meta["mid_flight"] == "next_campaign_only" and agent_only:
            live = meta["daily_batch"]["size"]
        return int(round(live * share))

    # ------------------------------------------------------------------ helpers
    def can_plan(self, wf, meta, st, now) -> bool:
        lo, hi = meta["governance"]["approver_hours"]
        hour = now % 1440 // 60
        if not (lo + 1 <= hour < hi - 2):  # not in the approver's first hour, and leave time to decide+apply
            return False
        if st.get("incident", {}).get("open"):
            return False
        if self.gov.live(wf):
            return False
        return self.gov.applied_today(wf, now, "experiment") == 0 and self.gov.sent_today(wf, now, "experiment") < 2

    @staticmethod
    def auto_allowed(meta, dim) -> bool:
        pol = meta["governance"]["auto_apply"]
        return bool(pol.get("enabled")) and dim in pol.get("allowed_dims", [])

    @staticmethod
    def did_on_hold(st, now) -> bool:
        return now < st.get("did_hold_until", -1)

    @staticmethod
    def with_changes(cfg, changes):
        new = copy.deepcopy(cfg)
        for d, dist in changes.items():
            new["allocations"]["default"][d] = {k: round(v, 6) for k, v in dist.items() if v > 0}
        return new

    def differs(self, cfg, new) -> bool:
        return bool(self.diff_dims(cfg, new))


def fmt(d: dict) -> str:
    return ", ".join(f"{k} {v:.0%}" for k, v in sorted(d.items()))
