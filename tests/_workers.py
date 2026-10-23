"""Helpers that run in child processes (multiprocessing spawn / subprocess)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def enqueue_many(db_path: str, worker: int, n: int, max_events: int, max_bytes: int, start_evt=None) -> int:
    from hookspool.config import DEFAULTS
    from hookspool.store import Store
    st = Store(path=db_path, config=DEFAULTS)
    if start_evt is not None:
        start_evt.wait(30)
    for i in range(n):
        st.enqueue("gmail", f"k{i % 5}", "hermes:test-route",
                   {"w": worker, "i": i, "note": "PAYLOAD-SENTINEL-7f3a", "pad": "x" * (i % 7) * 40},
                   event_type="push", dedup_id=f"w{worker}-{i}", quiet_s=30, max_s=120,
                   max_events=max_events, max_bytes=max_bytes)
    st.close()
    return n
