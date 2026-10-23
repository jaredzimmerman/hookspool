"""hookspool: durable, coalescing intake for inbound webhooks.

    from hookspool.store import Store
    st = Store()
    st.enqueue("gmail", "account-1", "script:gmail_delta", payload, dedup_id=message_id)

``enqueue()`` commits to the SQLite spool before it returns; the dispatcher delivers
the coalesced batch later and retries until it succeeds.
"""
from .store import EnqueueResult, InvalidEvent, LeaseLost, SchemaError, SpoolError, Store

__all__ = ["EnqueueResult", "InvalidEvent", "LeaseLost", "SchemaError", "SpoolError", "Store"]
__version__ = "0.2.0.dev0"
