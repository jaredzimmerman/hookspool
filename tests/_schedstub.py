"""A stand-in for the optional scheduling hand-off module gmail_delta can load
(``GMAIL_DELTA_SCHED_MODULE``): subjects containing "[sched]" belong to a tracked meeting thread."""


def classify(msg, header, body_text):
    if "[sched]" in header(msg, "Subject"):
        return {"thread_id": "tracked-1", "kind": "reply", "text": body_text(msg)}
    return None


def load():
    return {"tracked-1": {"subject": "Tracked meeting", "others": ["other@elsewhere.example.test"]}}


def ref_tag(thread_id):
    return f"[ref-{thread_id}]"
