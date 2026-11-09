"""Caller-ID (DID) health: detect numbers that stopped getting picked up, stop using them, spread load.

Per DID we keep one of three states in wf_state["did"][did]:
  healthy    used at an equal share with the other healthy DIDs
  probing    used at PROBE_SHARE until it has PROBE_MIN_DIALS dials without being flagged
  unhealthy  not used; re-probed after a rest: 48h, doubling each time it is flagged again, and at least
             7 days if its pickup was under half the others' (spam-labelled: re-testing costs pickup)

Detection compares a DID's pickup rate with the pooled rate of the other DIDs over the same hours.
Because every dial picks its DID at random, those calls share the same leads and hours, so a lower rate
is the number's fault. We flag when the ratio is below RATIO and the one-sided p-value is below ALPHA,
over a 6h and a 24h window (the short one catches sudden flags, the long one slow ones).

Target allocation: equal split across healthy DIDs (minus probe shares). Spreading volume across
numbers is standard practice because carriers label high-volume numbers as spam.
"""
from __future__ import annotations

from .stats import p_greater, rate

PROBE_SHARE = 0.05
PROBE_MIN_DIALS = 300
MIN_DIALS = 300
RATIO = 0.7
ALPHA = 1e-4  # ~5,000 tests per run (hourly x DIDs x 2 windows x workflows): keep false flags rare
REST_HOURS = 48         # rest for a DID that looked tired (pickup ratio 0.5-0.7: often just overuse)
FLAGGED_RATIO = 0.5
FLAGGED_REST_HOURS = 168  # below half the others' pickup it is spam-labelled; re-testing costs pickup
WINDOWS_H = (6, 24)
MIN_CHANGE_TV = 0.03


def _ok(x):
    return x or [0, 0, 0, 0]


class CallerIds:
    def __init__(self, store, log):
        self.s = store
        self.log = log

    def init_state(self, wf: str, meta: dict, cfg: dict, now: int) -> dict:
        used = self.s.arm_totals(wf, "caller_id", "*", since_hb=now // 60 - 24 * 7)
        cur = cfg["allocations"]["default"]["caller_id"]
        out = {}
        for d in meta["options"]["caller_id"]:
            if d in cur and _ok(used.get(d))[0] >= PROBE_MIN_DIALS:
                out[d] = {"state": "healthy", "since": now}
            else:
                out[d] = {"state": "probing", "since": now}
        return out

    def update(self, wf: str, st: dict, now: int) -> list[str]:
        """Re-assess every DID. Returns notes for anything that changed state."""
        dids = st["did"]
        notes = []
        hb = now // 60
        for h in WINDOWS_H:
            t = self.s.arm_totals(wf, "caller_id", "*", since_hb=hb - h)
            for d, info in dids.items():
                if info["state"] == "unhealthy":
                    continue
                dd, dc = _ok(t.get(d))[:2]
                od = sum(_ok(t.get(o))[0] for o in dids if o != d)
                oc = sum(_ok(t.get(o))[1] for o in dids if o != d)
                if dd < MIN_DIALS or od < MIN_DIALS:
                    continue
                r, ro = rate(dc, dd), rate(oc, od)
                p = p_greater(oc, od, dc, dd)
                if ro > 0 and r / ro < RATIO and p < ALPHA:
                    times = info.get("times_flagged", 0) + 1  # each repeat flag doubles the rest (backoff)
                    rest = max(REST_HOURS * 2 ** (times - 1), FLAGGED_REST_HOURS if r / ro < FLAGGED_RATIO else 0)
                    info.update(state="unhealthy", since=now, rest_h=rest, times_flagged=times)
                    ev = {"did": d, "window_h": h, "pickup": round(r, 4), "others_pickup": round(ro, 4),
                          "ratio": round(r / ro, 3), "p": p, "dials": dd, "others_dials": od}
                    self.s.decide(now, wf, "did_unhealthy", f"caller ID {d} pickup {r:.3f} vs {ro:.3f} on the "
                                  f"other numbers over {h}h — stop using it", ev)
                    notes.append(f"stop using caller ID {d}: pickup {r:.3f} vs {ro:.3f} on other numbers "
                                 f"(last {h}h, {dd} dials, p={p:.1e})")
        for d, info in dids.items():
            if info["state"] == "probing":
                got = _ok(self.s.arm_totals(wf, "caller_id", "*", since_hb=info["since"] // 60).get(d))[0]
                if got >= PROBE_MIN_DIALS:
                    info.update(state="healthy", since=now)
                    self.s.decide(now, wf, "did_healthy", f"caller ID {d} passed probing ({got} dials)", {"did": d})
                    notes.append(f"caller ID {d} passed its probe ({got} dials, pickup in line with others)")
            elif info["state"] == "unhealthy" and now - info["since"] >= info.get("rest_h", REST_HOURS) * 60:
                info.update(state="probing", since=now)
                self.s.decide(now, wf, "did_reprobe", f"caller ID {d} rested {REST_HOURS}h, probing again", {"did": d})
                notes.append(f"re-test caller ID {d} at {PROBE_SHARE:.0%} after a {info.get('rest_h', REST_HOURS)}h rest")
        return notes

    @staticmethod
    def target(st: dict) -> dict | None:
        healthy = [d for d, i in st["did"].items() if i["state"] == "healthy"]
        probing = [d for d, i in st["did"].items() if i["state"] == "probing"]
        if not healthy and not probing:
            return None  # every number looks bad: don't guess, keep what we have and say so
        if not healthy:
            return {d: 1 / len(probing) for d in probing}
        rest = 1 - PROBE_SHARE * len(probing)
        out = {d: rest / len(healthy) for d in healthy}
        out.update({d: PROBE_SHARE for d in probing})
        return out

    @staticmethod
    def urgent(st: dict, cur: dict) -> bool:
        """True when the live allocation still sends traffic to a DID we have flagged."""
        return any(cur.get(d, 0) > 0 for d, i in st["did"].items() if i["state"] == "unhealthy")
