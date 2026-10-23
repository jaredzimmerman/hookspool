"""``script:<name>`` destination (contract C3; ARCHITECTURE §6.6).

Runs ``argv`` (no shell) with the batch document on stdin, in its own session so a timeout or
the worker deadline can kill the whole process group. A service package's processor destination
has ``processor = "<instance>"`` instead of ``argv``; the argv is then built at spawn as
``[sys.executable, "-I", "-m", "hookspool.processor", "<instance>"]``, so the interpreter is always
the dispatcher's own and the working directory is never on ``sys.path`` (ARCHITECTURE §14.3.3).
Environment (ARCHITECTURE §10): minimal. ``PATH``, ``HOME`` and ``LANG`` from the dispatcher, then
``env_file`` (re-read every attempt), then the ``env`` table, then ``HOOKSPOOL_BATCH_ID`` and
``HOOKSPOOL_DELIVERY_ID`` (and ``HOOKSPOOL_STATE_DIR`` when ``state_dir`` is configured; the
directory is created). Nothing else of the dispatcher's environment reaches a script, so its
secrets do not.

Outcomes: exit 0 is ``ok``; any other exit is ``batch`` (the script ran and failed on this
batch: counted); cannot start, timeout and the worker deadline are ``transport``; an unreadable
or undecodable ``env_file`` or an unwritable ``state_dir`` is ``config``. Stdout is capped at 1 MiB: past it the attempt
fails (``batch``) rather than truncating, because dropping the tail would drop emits while
marking the batch delivered.

Stdout lines of the form ``{"enqueue": {...}}`` are returned as ``Outcome.emits``. The
dispatcher validates them and commits them in the same fenced transaction as the delivered
mark (nothing is enqueued unless the batch is marked); a script that fails or times out
emits nothing. Other stdout lines are ignored. Script output is never logged.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import ClassVar

from .. import config as _config
from ..store import EMIT_KEYS
from .base import DeliveryContext, Destination, Outcome

STDOUT_CAP = 1024 * 1024
STDERR_TAIL = 2048
READER_GRACE_S = 2.0
_REQUIRED_EMIT = ("source", "key", "payload")
ENV_ALLOW = ("PATH", "HOME", "LANG")
PROCESSOR_MODULE = "hookspool.processor"
_ENQUEUE_LINE = re.compile(r"""^\{\s*["']enqueue["']""")
_ENQUEUE_LINE_B = re.compile(rb"""^\{\s*["']enqueue["']""")


class _Pump(threading.Thread):
    """Drain one pipe: keep up to ``cap`` bytes (or a rolling tail), discard the rest."""

    def __init__(self, stream, cap: int, tail: bool):
        super().__init__(daemon=True, name="hookspool-script-pipe")
        self.stream, self.cap, self.tail = stream, cap, tail
        self.buf = bytearray()
        self.total = 0

    def run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    return
                self.total += len(chunk)
                if self.tail:
                    self.buf += chunk
                    del self.buf[:-self.cap]
                elif len(self.buf) <= self.cap:
                    self.buf += chunk[: self.cap + 1 - len(self.buf)]
        except (OSError, ValueError):
            return


KILL_WAIT_S = 2.0
KILLED_PIPE_WAIT_S = 0.5


def _kill_group(p: subprocess.Popen) -> None:
    """SIGKILL the script's process group; if the group cannot be signalled (it is gone, or a
    member changed its credentials), SIGKILL the leader itself."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
        return
    except OSError:
        pass
    try:
        p.kill()
    except OSError:
        pass


def _reap(p: subprocess.Popen) -> None:
    """Wait for a killed script, bounded: a process that does not die (uninterruptible sleep)
    must not hold the worker past its deadline. It is reaped by a later ``poll``."""
    try:
        p.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def parse_emits(stdout: bytes) -> list[dict]:
    """The ``{"enqueue": {...}}`` objects in a script's stdout. Raises ``ValueError`` with a
    safe message for a malformed one: a line that opens as an enqueue (``{"enqueue"``, in any
    spacing or quoting) but is not valid JSON, is not valid UTF-8, or has a bad shape. Other
    lines are ignored."""
    emits: list[dict] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith(b"{"):
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            if _ENQUEUE_LINE_B.match(line):
                raise ValueError("an enqueue line is not valid UTF-8") from None
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            if _ENQUEUE_LINE.match(text):
                raise ValueError("an enqueue line is not valid JSON") from None
            continue
        if not isinstance(obj, dict) or "enqueue" not in obj:
            continue
        e = obj["enqueue"]
        if not isinstance(e, dict):
            raise ValueError("enqueue must be an object")
        unknown = sorted(set(e) - set(EMIT_KEYS))
        if unknown:
            raise ValueError(f"enqueue has unknown keys {unknown}")
        for k in _REQUIRED_EMIT:
            if k not in e:
                raise ValueError(f"enqueue is missing {k!r}")
        emits.append(e)
    return emits


class ScriptDestination(Destination):
    type_name = "script"
    idempotency: ClassVar[str] = "per-batch"
    default_timeout_s = 300.0

    def __init__(self, name, spec, config):
        super().__init__(name, spec, config)
        a = self.spec.get("argv")
        p = self.spec.get("processor")
        self.processor = str(p) if isinstance(p, str) and p else None
        self.argv = [str(x) for x in a] if isinstance(a, list) else []
        e = self.spec.get("env")
        self.env = {str(k): str(v) for k, v in e.items()} if isinstance(e, dict) else {}
        self.cwd = os.path.expanduser(self.spec["cwd"]) if self.spec.get("cwd") else None
        self.env_file = os.path.expanduser(self.spec["env_file"]) if self.spec.get("env_file") else None
        self.state_dir = os.path.expanduser(self.spec["state_dir"]) if self.spec.get("state_dir") else None

    def command(self) -> list[str]:
        """The argv to spawn: ``argv``, or the processor runtime under the dispatcher's own
        interpreter (``-I``: no working directory, user site or PYTHON* variables on the path)."""
        if self.processor:
            return [sys.executable, "-I", "-m", PROCESSOR_MODULE, self.processor]
        return list(self.argv)

    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome:
        argv = self.command()
        if not argv:
            return Outcome.config("script has no argv", detail=f"destination {self.name!r} has no argv")
        env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        if self.env_file:
            try:
                env.update(_config.read_env_file(self.env_file))
            except (OSError, ValueError) as e:
                return Outcome.config("env_file unreadable", detail=f"env_file unreadable: {e.__class__.__name__}")
        env.update(self.env)
        if self.state_dir:
            try:
                os.makedirs(self.state_dir, exist_ok=True)
            except OSError as e:
                return Outcome.config("state_dir not writable",
                                      detail=f"state_dir not writable: {e.__class__.__name__}")
            env["HOOKSPOOL_STATE_DIR"] = self.state_dir
        env["HOOKSPOOL_BATCH_ID"] = str(ctx.batch_id)
        env["HOOKSPOOL_DELIVERY_ID"] = delivery_id

        budget = ctx.timeout(self.timeout_s)
        why = "deadline" if budget < self.timeout_s else "timeout"
        if budget <= 0:
            return Outcome.transport(why, detail=why)
        try:
            p = subprocess.Popen(argv, cwd=self.cwd, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, ValueError) as e:
            return Outcome.transport(f"cannot start ({e.__class__.__name__})",
                                     detail=f"cannot start: {e.__class__.__name__}")
        out, err = _Pump(p.stdout, STDOUT_CAP, False), _Pump(p.stderr, STDERR_TAIL, True)
        out.start()
        err.start()

        def feed() -> None:
            try:
                p.stdin.write(body)
            except (OSError, ValueError):       # the script closed stdin early; its exit code decides
                pass
            finally:
                try:
                    p.stdin.close()
                except (OSError, ValueError):
                    pass
        threading.Thread(target=feed, daemon=True, name="hookspool-script-stdin").start()

        try:
            p.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            _kill_group(p)
            _reap(p)
            # Nothing is read from a killed script; a descendant that left the group may hold the
            # pipes, so the pumps get one short, shared wait (the worker is already at its bound).
            end = time.monotonic() + KILLED_PIPE_WAIT_S
            for t in (out, err):
                t.join(max(0.0, end - time.monotonic()))
            return Outcome.transport(why if why == "deadline" else f"timeout after {self.timeout_s:g}s",
                                     detail=why)
        # The script exited. A descendant that kept the pipes open must not hold the worker.
        end = time.monotonic() + min(READER_GRACE_S, max(0.0, ctx.remaining()))
        for t in (out, err):
            t.join(max(0.0, end - time.monotonic()))
        if out.is_alive() or err.is_alive():
            _kill_group(p)
            end = time.monotonic() + KILLED_PIPE_WAIT_S
            for t in (out, err):
                t.join(max(0.0, end - time.monotonic()))
        rc = p.returncode
        tail = bytes(err.buf).decode("utf-8", "replace")[-300:]
        if rc != 0:
            if rc < 0:
                return Outcome.batch(f"killed by signal {-rc}", status=rc, detail=f"killed by signal {-rc}: {tail}")
            return Outcome.batch(f"exit {rc}", status=rc, detail=f"exit {rc}: {tail}")
        if out.total > STDOUT_CAP:
            return Outcome.batch("stdout over 1 MiB", status=0,
                                 detail=f"stdout was {out.total} bytes, over the {STDOUT_CAP}-byte cap")
        try:
            emits = parse_emits(bytes(out.buf))
        except ValueError as e:
            return Outcome.batch("bad emit", status=0, detail=f"bad emit: {e}")
        return Outcome.ok(f"exit 0 out_bytes={out.total}", status=0, detail=f"exit 0, {len(emits)} emitted",
                          emits=emits)

    def check(self) -> list[str]:
        problems: list[str] = []
        argv = self.command()
        if not argv:
            return [f"{self.name}: argv is required"]
        exe = argv[0]
        if self.processor:
            if not (exe and os.path.isfile(exe) and os.access(exe, os.X_OK)):
                problems.append(f"{self.name}: the dispatcher's interpreter {exe!r} is not an executable file")
        elif os.sep in exe:
            if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
                problems.append(f"{self.name}: argv[0] {exe} is not an executable file")
        else:
            import shutil
            if shutil.which(exe) is None:
                problems.append(f"{self.name}: argv[0] {exe!r} is not on PATH")
        if self.cwd and not os.path.isdir(self.cwd):
            problems.append(f"{self.name}: cwd {self.cwd} is not a directory")
        if self.env_file:
            try:
                _config.read_env_file(self.env_file)
            except (OSError, ValueError) as e:
                problems.append(f"{self.name}: env_file {self.env_file} is unreadable ({e.__class__.__name__})")
        if self.state_dir:
            d = self.state_dir
            if os.path.isdir(d):
                if not os.access(d, os.W_OK):
                    problems.append(f"{self.name}: state_dir {d} is not writable")
            elif os.path.exists(d):
                problems.append(f"{self.name}: state_dir {d} is not a directory")
            else:
                parent = os.path.dirname(d.rstrip(os.sep)) or os.sep
                if not (os.path.isdir(parent) and os.access(parent, os.W_OK)):
                    problems.append(f"{self.name}: state_dir {d} does not exist and cannot be created")
        return problems
