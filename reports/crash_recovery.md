# Crash-recovery test — scenarios/public_02_incidents.yaml

Command: `python scripts/crash_test.py --scenario scenarios/public_02_incidents.yaml --name crash_public_02`

## Kills

- injected crash 'mid_ingest_tx:5' -> exit 137 at sim 2027-01-04T11:00:00+05:30
- injected crash 'after_approval_post:2' -> exit 137 at sim 2027-01-04T11:00:00+05:30
- injected crash 'after_apply:3' -> exit 137 at sim 2027-01-04T16:00:00+05:30
- injected crash 'after_auto_apply:2' -> exit 137 at sim 2027-01-05T13:00:00+05:30
- SIGKILL at sim 2027-01-10T21:00:00+05:30 (minute 9900); proposals in flight: []
- SIGKILL at sim 2027-01-12T10:00:00+05:30 (minute 12120); proposals in flight: []
- SIGKILL at sim 2027-01-13T12:00:00+05:30 (minute 13680); proposals in flight: []
- final run exited with code 0

## Invariants

- [x] scenario ran to the end
- [x] calls counted once (ours 104000, simulator 104000)
- [x] no approval applied twice
- [x] no lost approvals (17 requested, all in our store)
- [x] no proposal left half-sent / half-applied
- [x] no duplicate_proposal_id errors
- [x] recovery paths exercised (3 'recovered' decisions in our store)

## Grade of the killed-and-restarted run

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.368 | +56.2% | 0.251 | +38.4% | 0.500 | +6.8% | 34% | 98 (348) |
| nw_kyc | 0.425 | +13.8% | 0.267 | +14.1% | 0.601 | -1.2% | 2% | 32 (29) |
| nw_renewal | 0.470 | +8.6% | 0.289 | +10.3% | 0.544 | -1.9% | 4% | 54 (54) |
| ALL | 0.414 | +25.3% | 0.267 | +21.8% | 0.536 | +0.6% | 21% | 184 (431) |

## Changes applied by the controller

21 applied — neutral: 4, beneficial: 13, harmful: 4

- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.009 → **neutral**
- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.018 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.032 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=-0.018 didΔ=+0.048 → **harmful**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.026 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.012 → **neutral**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.037 → **beneficial**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.018 → **neutral**
- ks_cancel v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.168 → **beneficial**
- ks_cancel v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.101 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.127 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_approved) dims=['voice', 'prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- ks_cancel v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.016 didΔ=+0.000 → **harmful**
- nw_kyc v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.041 didΔ=+0.000 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.191 → **beneficial**
- nw_renewal v9 (controller_approved) dims=['welcome_message'] engageΔ=+0.111 didΔ=+0.000 → **beneficial**
- nw_renewal v10 (controller_approved) dims=['voice'] engageΔ=-0.013 didΔ=+0.000 → **harmful**
- nw_kyc v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.091 → **beneficial**
- ks_cancel v11 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.030 → **harmful**

