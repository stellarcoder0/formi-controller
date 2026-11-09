"""A/B tests on the customer-facing dimensions (welcome_message, voice), at workflow level.

Per dimension, a small state machine stored in wf_state["dims"][dim]:

  idle ──► exploring ──► promoted ──(decay)──► cooldown ──► idle (re-explore without the decayed arm)
             │                │
             └► done          └► hold (supervisor edited this dim; we back off for 24h) ──► idle
             (no arm beat the champion in MAX_EXPLORE_DAYS, or only the champion is left)

Design (see SUBMISSION.md §5 for the reasoning):
  * Factorial: welcome and voice are randomised independently per call by the simulator, so one
    exploration tests both dimensions at once; each dimension's arms are compared marginally.
  * Exploration split: champion keeps 50%, challengers share 50% equally.
  * Evidence window: only calls since the exploration allocation was actually live (all arms running
    concurrently), so time-of-day / prompt / population shifts hit every arm equally.
  * Elimination: a challenger significantly worse than the champion is dropped (alpha 0.05, Bonferroni).
  * Promotion: a challenger significantly better (alpha 0.01, Bonferroni), with >= 2% relative lift and
    no significant pickup drop beyond the guardrail, becomes 100%.
  * Decay: once promoted, the winner's last-24h rate is compared with its first 24h after promotion
    (same prompt). A significant drop that leaves it below the old champion triggers a rollback.
"""
from __future__ import annotations

from .stats import p_greater, rate

DIMS = ("welcome_message", "voice")
EXPLORE_CHAMPION_SHARE = 0.5
MIN_CONN_ELIMINATE = 200
MIN_CONN_PROMOTE = 300
ALPHA_ELIMINATE = 0.05
ALPHA_PROMOTE = 0.01
MIN_LIFT = 0.02
MAX_EXPLORE_DAYS = 4
DECAY_MIN_HOURS = 24
DECAY_ALPHA = 0.01
COOLDOWN_HOURS = 24
HOLD_HOURS = 24


def explore_dist(champion: str, arms: list[str]) -> dict:
    others = [a for a in arms if a != champion]
    if not others:
        return {champion: 1.0}
    share = (1 - EXPLORE_CHAMPION_SHARE) / len(others)
    return {champion: EXPLORE_CHAMPION_SHARE, **{a: share for a in others}}


def champion_of(dist: dict) -> str:
    return max(sorted(dist), key=lambda k: dist[k])


def dim_state(st: dict, dim: str) -> dict:
    return st.setdefault("dims", {}).setdefault(dim, {"phase": "idle", "excluded": []})


def put_on_hold(st: dict, dim: str, now: int, version: int):
    d = dim_state(st, dim)
    d.update(phase="hold", hold_until=now + HOLD_HOURS * 60, hold_version=version)


def observe(st: dict, cfg: dict, now: int):
    """Record when an allocation we asked for is actually live (approval latency means it isn't immediate)."""
    for dim in DIMS:
        d = dim_state(st, dim)
        cur = cfg["allocations"]["default"][dim]
        if d["phase"] == "exploring" and d.get("live_at") is None and len(cur) > 1 \
                and set(cur) <= set(d["arms"]):
            d["live_at"] = now
        if d["phase"] == "promoted" and d.get("promo_live_at") is None and set(cur) == {d["winner"]}:
            d["promo_live_at"] = now


class Experiments:
    def __init__(self, store, log):
        self.s = store
        self.log = log

    # ------------------------------------------------------------------ daily planning (regular proposals)
    def plan(self, wf: str, meta: dict, cfg: dict, st: dict, now: int) -> tuple[dict, list[str], dict]:
        """Returns ({dim: new default distribution}, human notes, evidence) for one batched proposal."""
        changes, notes, evidence = {}, [], {}
        for dim in DIMS:
            opts = meta["options"][dim]
            d = dim_state(st, dim)
            cur = cfg["allocations"]["default"][dim]
            if d["phase"] in ("hold", "cooldown") and now >= d.get(d["phase"] + "_until", 0):
                d.update(phase="idle", live_at=None)
            if d["phase"] == "cooldown" and d.get("decayed") in cur:
                changes[dim] = {d["prev_champion"]: 1.0}  # the rollback was not applied yet: ask again
                notes.append(f"roll back {dim} {d['decayed']} -> {d['prev_champion']} (decay, re-requested)")
                continue
            if d["phase"] == "idle":
                arms = [o for o in opts if o not in d["excluded"]]
                champ = champion_of(cur)
                if champ not in arms:
                    arms.append(champ)
                if len(arms) < 2:
                    d["phase"] = "done"
                    continue
                d.update(phase="exploring", champion=champ, arms=arms, started_at=now, live_at=None)
                self.s.decide(now, wf, "experiment_started",
                              f"{dim}: explore {arms} with {champ} as champion", {"dim": dim, "arms": arms})
                changes[dim] = explore_dist(champ, arms)
                notes.append(f"start test on {dim}: {champ} keeps {EXPLORE_CHAMPION_SHARE:.0%}, "
                             f"{', '.join(a for a in arms if a != champ)} share the rest")
            elif d["phase"] == "exploring":
                res = self.evaluate(wf, meta, dim, d, now)
                evidence[dim] = res
                if res["winner"]:
                    w = res["winner"]
                    d.update(phase="promoted", winner=w, prev_champion=d["champion"], promoted_at=now,
                             promo_live_at=None, explore_until=now)
                    changes[dim] = {w: 1.0}
                    notes.append(f"promote {dim} {w} (engagement {res['arms'][w]['rate']:.3f} vs champion "
                                 f"{d['champion']} {res['arms'][d['champion']]['rate']:.3f}, p={res['p'][w]:.4f})")
                    self.s.decide(now, wf, "winner_found", f"{dim}: {w} beats {d['champion']}", res)
                    continue
                for a in res["eliminated"]:
                    d["arms"].remove(a)
                    d["excluded"].append(a)
                    notes.append(f"drop {dim} {a} (significantly worse than {d['champion']}, p={res['p_worse'][a]:.4f})")
                    self.s.decide(now, wf, "arm_eliminated", f"{dim}: {a} worse than {d['champion']}", res)
                age_days = (now - (d.get("live_at") or now)) / 1440
                if len(d["arms"]) < 2 or age_days >= MAX_EXPLORE_DAYS:
                    d["phase"] = "done"
                    changes[dim] = {d["champion"]: 1.0}
                    notes.append(f"end {dim} test: no challenger beat {d['champion']}")
                    self.s.decide(now, wf, "experiment_ended", f"{dim}: no winner, keep {d['champion']}", res)
                else:
                    changes[dim] = explore_dist(d["champion"], d["arms"])
            elif d["phase"] == "promoted":
                changes[dim] = {d["winner"]: 1.0}
        return changes, notes, evidence

    def evaluate(self, wf: str, meta: dict, dim: str, d: dict, now: int) -> dict:
        since = (d.get("live_at") or now) // 60
        t = self.s.arm_totals(wf, dim, "*", since_hb=since)
        champ = d["champion"]
        arms = d["arms"]
        k = max(len(arms) - 1, 1)
        get = lambda a: t.get(a, [0, 0, 0, 0])
        cd, cc, ce, _ = get(champ)
        out = {"window_from_minute": since * 60, "arms": {}, "p": {}, "p_worse": {}, "eliminated": [], "winner": None,
               "alpha_promote": ALPHA_PROMOTE / k, "alpha_eliminate": ALPHA_ELIMINATE / k}
        guard = meta["objective"].get("guardrail", {})
        max_pick_drop = guard.get("pickup_rate_max_drop", 0.02)
        best = None
        for a in arms:
            dd, c, e, _ = get(a)
            out["arms"][a] = {"dials": dd, "connected": c, "engaged": e, "rate": rate(e, c), "pickup": rate(c, dd)}
            if a == champ:
                continue
            out["p"][a] = p_better = p_greater(e, c, ce, cc)
            out["p_worse"][a] = p_worse = p_greater(ce, cc, e, c)
            if c >= MIN_CONN_ELIMINATE and cc >= MIN_CONN_ELIMINATE and p_worse < ALPHA_ELIMINATE / k:
                out["eliminated"].append(a)
                continue
            pickup_ok = not (rate(c, dd) < rate(cc, cd) - max_pick_drop and p_greater(cc, cd, c, dd) < 0.05)
            if c >= MIN_CONN_PROMOTE and cc >= MIN_CONN_PROMOTE and p_better < ALPHA_PROMOTE / k \
                    and rate(e, c) >= rate(ce, cc) * (1 + MIN_LIFT) and pickup_ok:
                if best is None or rate(e, c) > out["arms"][best]["rate"]:
                    best = a
        out["winner"] = best
        return out

    # ------------------------------------------------------------------ decay (checked every step)
    def check_decay(self, wf: str, cfg: dict, st: dict, now: int) -> tuple[dict, list[str], dict]:
        """Compare the winner's last 24h with its first 24h after promotion (same prompt).

        We deliberately do NOT compare with the rate measured during the experiment: the winner was picked
        *because* that number was high, so it is biased upwards (winner's curse) and a later "drop" would
        often be regression to the mean, not decay. The first day after promotion is an unbiased baseline.
        """
        changes, notes, evidence = {}, [], {}
        pv_seg = "pv=" + cfg["prompt_version"]
        for dim in DIMS:
            d = dim_state(st, dim)
            if d["phase"] != "promoted" or not d.get("promo_live_at"):
                continue
            live_hb = d["promo_live_at"] // 60
            if now // 60 - live_hb < 24 + DECAY_MIN_HOURS:
                continue
            w, champ = d["winner"], d["prev_champion"]
            base = self.s.arm_totals(wf, dim, pv_seg, since_hb=live_hb, until_hb=live_hb + 24).get(w, [0, 0, 0, 0])
            recent = self.s.arm_totals(wf, dim, pv_seg, since_hb=now // 60 - 24).get(w, [0, 0, 0, 0])
            during = self.s.arm_totals(wf, dim, "*", since_hb=(d.get("live_at") or 0) // 60, until_hb=live_hb)
            cd = during.get(champ, [0, 0, 0, 0])
            if base[1] < MIN_CONN_ELIMINATE or recent[1] < MIN_CONN_ELIMINATE or cd[1] < MIN_CONN_ELIMINATE:
                continue  # not enough comparable data under the current prompt
            p = p_greater(base[2], base[1], recent[2], recent[1])
            r_recent, r_base, r_champ = rate(recent[2], recent[1]), rate(base[2], base[1]), rate(cd[2], cd[1])
            ev = {"winner": w, "rate_first_24h_after_promotion": r_base, "rate_last_24h": r_recent,
                  "old_champion_rate_during_test": r_champ, "p_drop": p, "prompt": cfg["prompt_version"]}
            if p < DECAY_ALPHA and r_recent < r_champ:
                d.update(phase="cooldown", cooldown_until=now + COOLDOWN_HOURS * 60, decayed=w)
                d["excluded"].append(w)
                changes[dim] = {champ: 1.0}
                notes.append(f"roll back {dim} {w} -> {champ}: engagement fell from {r_base:.3f} (first day after "
                             f"promotion) to {r_recent:.3f} (last 24h, p={p:.4f}), below {champ}'s {r_champ:.3f}")
                evidence[dim] = ev
                self.s.decide(now, wf, "decay_detected", f"{dim}: {w} decayed, rolling back to {champ}", ev)
        return changes, notes, evidence
