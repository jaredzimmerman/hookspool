"""Structured logs (ARCHITECTURE §11.3).

stdlib ``logging`` to stderr. ``HOOKSPOOL_LOG=json`` emits one JSON object per line with
the fixed fields below; ``text`` (the default) is the human form of the same fields.
Only these fields are ever written: an ``extra`` key that is not one of them is dropped,
so a caller cannot leak a body or a header by passing it along. There is no level that
prints bodies.

    log.info("queued", extra=fields(source="github", batch=12, bytes=345))
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time

from . import config as _config

FIELDS = ("source", "route", "batch", "event", "destination", "attempt", "status", "bytes",
          "duration_ms", "reason", "tee")
_RESERVED = {"msg", "message", "args", "name", "levelname", "levelno", "pathname", "filename", "module",
             "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
             "relativeCreated", "thread", "threadName", "processName", "process", "taskName"}


def fields(**kw) -> dict:
    """``extra=`` for a log call: the known fields only, ``None`` values dropped."""
    return {k: v for k, v in kw.items() if k in FIELDS and v is not None}


def _component(record: logging.LogRecord) -> str:
    name = record.name
    return name.split(".", 1)[1] if name.startswith("hookspool.") else name


def _base(record: logging.LogRecord) -> dict:
    out = {"ts": round(record.created, 3), "level": record.levelname.lower(),
           "component": getattr(record, "component", None) or _component(record),
           "msg": record.getMessage()}
    for f in FIELDS:
        v = record.__dict__.get(f)
        if v is not None:
            out[f] = v
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = _base(record)
        if record.exc_info and record.exc_info[0] is not None:
            out["error"] = record.exc_info[0].__name__   # the class only: messages can carry data
        return json.dumps(out, separators=(",", ":"), default=str, ensure_ascii=False)


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _one_line(v) -> str:
    """A field value as one line: a value derived from a request (a forwarded address, a
    plugin's ignore reason) cannot start a forged log line."""
    return _CTRL.sub("?", str(v))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        b = _base(record)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(b.pop("ts")))
        head = f"{ts} {b.pop('level'):<7} {b.pop('component')}: {_one_line(b.pop('msg'))}"
        tail = " ".join(f"{k}={_one_line(v)}" for k, v in b.items())
        if record.exc_info and record.exc_info[0] is not None:
            tail += f" error={record.exc_info[0].__name__}"
        return head + (" " + tail if tail else "")


def formatter() -> logging.Formatter:
    return JsonFormatter() if _config.log_format() == "json" else TextFormatter()


def setup(level: int = logging.INFO) -> logging.Logger:
    """Configure the ``hookspool`` logger once for a service process."""
    root = logging.getLogger("hookspool")
    for h in list(root.handlers):
        if getattr(h, "_hookspool", False):
            root.removeHandler(h)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(formatter())
    h._hookspool = True  # type: ignore[attr-defined]
    root.addHandler(h)
    root.setLevel(level)
    return root
