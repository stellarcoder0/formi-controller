"""Fault injection for the crash-recovery test (scripts/crash_test.py). Inert unless CONTROLLER_FAULT is set.

CONTROLLER_FAULT="after_approval_post:2" makes the process die (os._exit, like SIGKILL: no cleanup,
no flush) the 2nd time execution reaches fault("after_approval_post"). Points are placed at the
moments where a crash is most dangerous: after the simulator accepted a request but before we
recorded the result.
"""
import os

_spec = os.environ.get("CONTROLLER_FAULT", "")
_point, _, _nth = _spec.partition(":")
_count = 0


def fault(point: str):
    global _count
    if not _point or point != _point:
        return
    _count += 1
    if _count == int(_nth or 1):
        print(f"FAULT INJECTED at {point} (hit {_count}): exiting without cleanup", flush=True)
        os._exit(137)
