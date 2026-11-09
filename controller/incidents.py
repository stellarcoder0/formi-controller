"""Hallucination incidents: detect -> pick a fix -> validate -> propose with evidence -> verify after apply.

Detection is on the population of connected calls under the *current* prompt in the last 6 hours:
  at least MIN_CONN connected calls, flag rate above max(2 x baseline, baseline + 1pp), and a one-sided
  binomial test against the baseline with p < 1e-4. The baseline is the workflow's own flag rate in the
  history before D0 (the detector has false positives, so the baseline is not zero).

Fix selection, cheapest first ("the best compute is no compute"):
  1. Roll back to the most recent prompt this workflow ran with a normal flag rate on real traffic.
     We still need one validation run (approvers require it), ~5 credits.
  2. Only if there is no known-good prompt: ask the LLM for candidates (3 credits) and validate them.
     The LLM proposes; the validation pass rate (a deterministic threshold) decides.

Verification: once the fix is live, the flag rate under the new prompt is tested the same way. The
incident is closed when MIN_CONN calls show a normal rate, and re-opened if they don't.
"""
from __future__ import annotations

from .client import ApiError
from .stats import p_rate_above, rate

WINDOW_H = 6
MIN_CONN = 300
P_DETECT = 1e-4
VALIDATION_N = 500
CREDIT_RESERVE = 10  # never spend the last few credits on anything but a validation for a live incident
DEFAULT_BASELINE = 0.015


class Incidents:
    def __init__(self, client, store, log):
        self.c = client
        self.s = store
        self.log = log

    def baseline(self, wf: str, st: dict) -> float:
        if "flag_baseline" not in st:
            t = self.s.arm_totals(wf, "all", "*", until_hb=0).get("all")
            st["flag_baseline"] = rate(t[3], t[1]) if t and t[1] >= 1000 else DEFAULT_BASELINE
        return st["flag_baseline"]

    def is_bad(self, wf, pv, st, since_hb) -> tuple[bool, dict]:
        t = self.s.arm_totals(wf, "prompt_version", "*", since_hb=since_hb).get(pv, [0, 0, 0, 0])
        b = self.baseline(wf, st)
        conn, flag = t[1], t[3]
        p = p_rate_above(flag, conn, b)
        bad = conn >= MIN_CONN and rate(flag, conn) > max(2 * b, b + 0.01) and p < P_DETECT
        return bad, {"prompt": pv, "connected": conn, "flagged": flag, "flag_rate": round(rate(flag, conn), 4),
                     "baseline": round(b, 4), "p": p, "window_from_hb": since_hb}

    def check(self, wf: str, cfg: dict, st: dict, now: int) -> str | None:
        """Updates the incident state. Returns the prompt we need a fix for, or None."""
        inc = st.setdefault("incident", {"open": False})
        pv = cfg["prompt_version"]
        if inc["open"] and pv != inc["bad_prompt"]:
            # a fix is live (ours or the supervisor's): verify it on fresh traffic only
            since = inc.setdefault("fix_live_at", now) // 60
            bad, ev = self.is_bad(wf, pv, st, since)
            if ev["connected"] >= MIN_CONN and not bad:
                inc.update(open=False, closed_at=now)
                self.s.decide(now, wf, "incident_resolved",
                              f"hallucination flag rate back to {ev['flag_rate']:.3f} on {pv} "
                              f"(baseline {ev['baseline']:.3f}); incident closed", ev)
                self.log(f"{wf}: incident resolved on {pv}")
            elif bad:
                inc.update(bad_prompt=pv, fix_live_at=None)
                self.s.decide(now, wf, "incident_reopened", f"{pv} also shows elevated hallucination flags", ev)
            return None
        if not inc["open"]:
            bad, ev = self.is_bad(wf, pv, st, now // 60 - WINDOW_H)
            if not bad:
                return None
            inc.update(open=True, bad_prompt=pv, opened_at=now, fix_live_at=None)
            self.s.decide(now, wf, "incident_opened",
                          f"hallucination flags {ev['flag_rate']:.3f} on {pv} vs baseline {ev['baseline']:.3f} "
                          f"over {ev['connected']} connected calls (p={ev['p']:.1e})", ev)
            self.log(f"{wf}: INCIDENT hallucination on {pv}: {ev}")
        return inc["bad_prompt"] if pv == inc["bad_prompt"] else None

    # ------------------------------------------------------------------ choosing and validating a fix
    def known_good(self, wf: str, st: dict, bad: str) -> list[str]:
        """Prompts this workflow actually ran with a normal flag rate, most recently used first."""
        b = self.baseline(wf, st)
        rows = self.s.db.execute(
            "SELECT arm, SUM(conn), SUM(flag), MAX(hb) FROM stats WHERE wf=? AND dim='prompt_version' AND seg='*' "
            "GROUP BY arm", (wf,)).fetchall()
        good = [(last, arm) for arm, conn, flag, last in rows
                if arm != bad and conn >= 1000 and rate(flag, conn) <= max(1.5 * b, b + 0.005)]
        return [arm for _, arm in sorted(good, reverse=True)]

    def credits(self) -> float:
        return float(self.c.get("/sim/state")["credits_remaining"])

    def validate(self, wf: str, pv: str, now: int, min_pass: float) -> dict | None:
        row = self.s.db.execute("SELECT run_id, pass_rate, n FROM validations WHERE wf=? AND prompt=? "
                                "ORDER BY minute DESC LIMIT 1", (wf, pv)).fetchone()
        if row and row[1] >= min_pass:
            return {"validation_run_id": row[0], "pass_rate": row[1], "n": row[2], "reused": True}
        cost = -(-VALIDATION_N // 100)
        if self.credits() < cost:
            self.s.decide(now, wf, "credits_exhausted", f"cannot validate {pv}: not enough credits", {})
            return None
        try:
            r = self.c.post("/validation/runs", {"workflow_id": wf, "prompt_version": pv, "n": VALIDATION_N})
        except ApiError as e:
            self.s.decide(now, wf, "validation_failed", f"validation call for {pv} failed: {e.code}", e.body)
            return None
        with self.s.tx():
            self.s.db.execute("INSERT OR REPLACE INTO validations VALUES(?,?,?,?,?,?)",
                              (r["validation_run_id"], wf, pv, r["n"], r["pass_rate"], now))
            self.s.decide(now, wf, "validation_run", f"{pv}: pass rate {r['pass_rate']:.3f} on {r['n']} "
                          f"synthetic conversations (need {min_pass})", r)
        return {"validation_run_id": r["validation_run_id"], "pass_rate": r["pass_rate"], "n": r["n"]}

    def choose_fix(self, wf: str, meta: dict, st: dict, bad: str, now: int) -> tuple[str, dict, str] | None:
        """Returns (prompt, validation evidence, how it was chosen) or None."""
        min_pass = meta["governance"].get("min_validation_pass_rate", 0.97)
        tried = set(st["incident"].setdefault("failed_fixes", []))
        for pv in self.known_good(wf, st, bad):
            if pv in tried:
                continue
            v = self.validate(wf, pv, now, min_pass)
            if v and v["pass_rate"] >= min_pass:
                return pv, v, "rollback to the last prompt with a normal flag rate on real traffic"
            st["incident"]["failed_fixes"].append(pv)
        if self.credits() < 3 + CREDIT_RESERVE:
            self.s.decide(now, wf, "credits_low", "no known-good prompt and too few credits to ask the LLM", {})
            return None
        try:
            sug = self.c.post("/llm/suggest-fixes", {
                "workflow_id": wf, "problem": f"hallucination flag rate is elevated on prompt {bad}; "
                                             f"need a safer prompt that keeps the conversation natural"})
        except ApiError as e:
            self.s.decide(now, wf, "llm_failed", f"suggest-fixes failed: {e.code}", e.body)
            return None
        self.s.decide(now, wf, "llm_suggestions", f"LLM suggested {[x['prompt_version'] for x in sug['suggestions']]}",
                      sug)
        results = []
        for cand in sug["suggestions"]:
            pv = cand["prompt_version"]
            if pv in tried or pv == bad:
                continue
            v = self.validate(wf, pv, now, min_pass)
            if v:
                results.append((v["pass_rate"], pv, v))
        passing = sorted([r for r in results if r[0] >= min_pass], reverse=True)
        if not passing:
            return None
        _, pv, v = passing[0]
        return pv, v, "LLM candidate with the highest validation pass rate (no known-good prompt available)"
