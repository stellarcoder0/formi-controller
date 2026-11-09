"""Hallucination incidents: detect -> pick a fix -> validate -> propose with evidence -> verify after apply.

Detection is on the population of connected calls under the *current* prompt: at least MIN_CONN
connected calls, flag rate above max(2 x baseline, baseline + 1pp), and a one-sided binomial test against
the baseline. The baseline is the workflow's own flag rate in the history before D0 (the detector has
false positives, so it is not zero). Two regimes:
  * the prompt changed in the last 24h: 6h window, p < 1e-4 (a new prompt is the usual cause, be fast);
  * the prompt has run unchanged for >24h: 12h window, p < 1e-6. Nothing about the agent changed, so a
    spike is more likely noise or a detector blip; checking 3 workflows hourly for 10 days is ~700 tests,
    and on public_02 the 1e-4 rule fired once on an unchanged, healthy prompt.
When a supervisor ships a new prompt we also validate it straight away (5 credits): if it fails the
approver's own pass-rate bar we open the incident without waiting for hours of bad calls.

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
from .stats import p_greater, p_rate_above, rate

WINDOW_H = 6            # prompt changed in the last 24h: react fast
P_DETECT = 1e-4
EST_WINDOW_H = 12       # prompt has run >24h with no change: a spike needs more evidence (see below)
EST_P_DETECT = 1e-6
NEW_PROMPT_H = 24
MIN_CONN = 300
ENGAGE_DROP = 0.05      # a fix that costs >5% relative engagement (p<0.01) is itself a regression
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

    def is_bad(self, wf, pv, st, since_hb, p_detect=P_DETECT) -> tuple[bool, dict]:
        t = self.s.arm_totals(wf, "prompt_version", "*", since_hb=since_hb).get(pv, [0, 0, 0, 0])
        b = self.baseline(wf, st)
        conn, flag = t[1], t[3]
        p = p_rate_above(flag, conn, b)
        bad = conn >= MIN_CONN and rate(flag, conn) > max(2 * b, b + 0.01) and p < p_detect
        return bad, {"prompt": pv, "connected": conn, "flagged": flag, "flag_rate": round(rate(flag, conn), 4),
                     "baseline": round(b, 4), "p": p, "window_from_hb": since_hb}

    def check(self, wf: str, cfg: dict, st: dict, now: int) -> str | None:
        """Updates the incident state. Returns the prompt we need a fix for, or None."""
        inc = st.setdefault("incident", {"open": False})
        pv = cfg["prompt_version"]
        if st.get("prompt_seen") != pv:
            st["prompt_since"] = now if "prompt_seen" in st else -10**9  # first sight: it has been running
            st["prompt_seen"] = pv
        if inc["open"] and pv != inc["bad_prompt"]:
            # a fix is live (ours or the supervisor's): verify it on fresh traffic only
            if inc.get("fix_live_at") is None:
                inc["fix_live_at"] = now
            since = inc["fix_live_at"] // 60
            bad, ev = self.is_bad(wf, pv, st, since)
            regress = self.engagement_regression(wf, pv, inc, since)
            if regress:
                inc.update(bad_prompt=pv, fix_live_at=None, reason="engagement_regression")
                inc.setdefault("failed_fixes", []).append(pv)
                self.s.decide(now, wf, "fix_regressed", f"{pv} stopped the hallucinations but engagement fell "
                              f"{regress['before']:.3f} -> {regress['after']:.3f} (p={regress['p']:.4f}); "
                              f"looking for another fix", regress)
                return pv
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
            if now - st["prompt_since"] < NEW_PROMPT_H * 60:
                since = max(now // 60 - WINDOW_H, st["prompt_since"] // 60)
                bad, ev = self.is_bad(wf, pv, st, since, P_DETECT)
            else:
                bad, ev = self.is_bad(wf, pv, st, now // 60 - EST_WINDOW_H, EST_P_DETECT)
            if not bad:
                return None
            inc.update(open=True, bad_prompt=pv, opened_at=now, fix_live_at=None, reason="flag_rate",
                       engage_before=self.engagement(wf, now // 60 - 24, st["prompt_since"] // 60))
            self.s.decide(now, wf, "incident_opened",
                          f"hallucination flags {ev['flag_rate']:.3f} on {pv} vs baseline {ev['baseline']:.3f} "
                          f"over {ev['connected']} connected calls (p={ev['p']:.1e})", ev)
            self.log(f"{wf}: INCIDENT hallucination on {pv}: {ev}")
        return inc["bad_prompt"] if pv == inc["bad_prompt"] else None

    def engagement(self, wf, since_hb, until_hb) -> list:
        """[connected, engaged] for all calls of the workflow in [since_hb, until_hb)."""
        t = self.s.arm_totals(wf, "all", "*", since_hb=since_hb, until_hb=until_hb).get("all", [0, 0, 0, 0])
        return [t[1], t[2]]

    def engagement_regression(self, wf, pv, inc, since_hb) -> dict | None:
        """The fix's engagement vs the last good day before the incident (whole-workflow rates; other
        changes in between would confound this, which is why the bar is a 5% drop at p < 0.01)."""
        before = inc.get("engage_before") or [0, 0]
        after = self.engagement(wf, since_hb, 10**9)
        if before[0] < MIN_CONN or after[0] < MIN_CONN:
            return None
        rb, ra = rate(before[1], before[0]), rate(after[1], after[0])
        p = p_greater(before[1], before[0], after[1], after[0])
        if ra < rb * (1 - ENGAGE_DROP) and p < 0.01:
            return {"prompt": pv, "before": rb, "after": ra, "p": p, "n_before": before[0], "n_after": after[0]}
        return None

    def screen_new_prompt(self, wf: str, meta: dict, st: dict, pv: str, now: int):
        """A supervisor just shipped `pv`. One validation run tells us now whether it meets the bar."""
        min_pass = meta["governance"].get("min_validation_pass_rate", 0.97)
        if self.credits() < 5 + CREDIT_RESERVE:
            return
        v = self.validate(wf, pv, now, min_pass)
        inc = st.setdefault("incident", {"open": False})
        if v and v["pass_rate"] < min_pass and not inc["open"]:
            inc.update(open=True, bad_prompt=pv, opened_at=now, fix_live_at=None, reason="validation",
                       engage_before=self.engagement(wf, now // 60 - 24, now // 60))
            self.s.decide(now, wf, "incident_opened", f"supervisor's new prompt {pv} failed validation "
                          f"({v['pass_rate']:.3f} < {min_pass}); proposing a rollback before it reaches more calls", v)
            self.log(f"{wf}: INCIDENT {pv} failed validation {v['pass_rate']}")

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
        # Highest pass rate wins; ties keep the LLM's order (never the alphabet). Validation only measures
        # safety, so engagement is checked after the fix goes live (engagement_regression).
        passing = sorted([r for r in results if r[0] >= min_pass], key=lambda r: -r[0])
        if not passing:
            return None
        _, pv, v = passing[0]
        return pv, v, "LLM candidate with the highest validation pass rate (no known-good prompt available)"
