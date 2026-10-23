"""The shared source test kit (ARCHITECTURE §14.1, §18).

Every source plugin is exercised the same way through the real intake pipeline
(``hookspool.intake.Intake``) with a fake clock, a temp spool and secret files holding
test values only. A plugin's test module describes the source once as a ``Kit`` and runs
the matrix::

    import pytest, sourcekit
    from kitplugins import signed

    KIT = sourcekit.Kit(name="kitsigned", module=signed, secrets={"secret": "test-..."},
                        sample=sourcekit.request("POST", "/hooks/kitsigned", {...}, body),
                        sign=my_sign)

    @pytest.mark.parametrize("case", sourcekit.CASES)
    def test_kit(case, tmp_path, clock):
        sourcekit.run(case, KIT, tmp_path, clock)

The matrix: signed, unsigned, tampered, stale and future timestamps (timestamped
schemes), oversized, chunked, missing length, duplicate, handshake with a good and a bad
signature, constant-ness of an unauthenticated handshake, plugin exception → quarantine,
spool unwritable → 503, low disk → 503, secrets never stored, and the sentinel-in-logs
check (the conftest fixture checks messages; ``run`` also renders every record through
the JSON formatter). A case that does not apply to a plugin is skipped.

Captures (``tests/captures/<source>/*.http``) run through ``run_capture``: the file's
``# expect:`` line is the result ``hookspool source test`` must report.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tomllib
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from hookspool import config as hs_config
from hookspool import intake as hs_intake
from hookspool import logs as hs_logs
from hookspool.plugin import RawRequest, parse_capture
from hookspool.store import SpoolError, Store

SENTINEL = "PAYLOAD-SENTINEL-7f3a"   # same marker as conftest: no log line may carry it
PLUGIN_FUNCS = ("verify", "challenge", "ignore", "payload", "key", "dedup_id", "event_type", "attrs", "action")
CAPTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOBBINS = os.path.join(ROOT, "hookspool", "bobbins")
TEMPLATE_MODULE = "hookspool.sources.template"
# The instance settings a template bobbin needs for its sample instance. The loader does not yet map
# [settings] into [source.template] (14.3.2a), so bobbin_source_cfg carries them across by hand.
SAMPLE_SETTINGS = {"trello": {"callback_url": "https://hooks.example.test/hooks/trello"}}


def request(method: str, target: str, headers: Mapping[str, str] | list[tuple[str, str]] | None = None,
            body: bytes = b"", meta: Mapping[str, str] | None = None) -> RawRequest:
    hs = list(headers.items()) if isinstance(headers, Mapping) else list(headers or [])
    return RawRequest(method, target, hs, bytes(body), dict(meta or {}))


@dataclass
class Kit:
    """One source plugin as the kit sees it.

    ``sample`` is an unsigned event request whose body carries ``SENTINEL``. ``sign(raw,
    secrets, now)`` returns the request with the sender's signature headers added for time
    ``now``. ``handshake`` is an unsigned handshake that ``challenge()`` answers after
    verification; ``unauth_handshake`` is one a ``challenge_unauthenticated`` source answers
    before verification. ``timestamped`` marks schemes with a time window of
    ``tolerance_s``; ``body_signed=False`` marks a static token, which authenticates the sender but not
    the body (the tampered-body case does not apply). ``break_stage`` is the plugin function the quarantine case makes raise.
    """
    name: str
    module: Any
    secrets: dict[str, str]
    sample: RawRequest
    sign: Callable[[RawRequest, Mapping[str, str], float], RawRequest]
    source_cfg: dict = field(default_factory=dict)
    handshake: RawRequest | None = None
    unauth_handshake: RawRequest | None = None
    timestamped: bool = False
    tolerance_s: float = 300.0
    break_stage: str = "key"
    body_signed: bool = True        # False for a static token (gmail, gcal): the body is not covered


# ---- environment ---------------------------------------------------------------------------------------
class _Spy:
    """Wraps a plugin module: records the plugin functions called, in order; ``broken``
    makes one of them raise."""

    def __init__(self, module: Any):
        self.module = module
        self.calls: list[str] = []
        self.broken: str | None = None

    def build(self) -> types.ModuleType:
        m = types.ModuleType(getattr(self.module, "__name__", "plugin"))
        for k in dir(self.module):
            if k.startswith("__"):
                continue
            val = getattr(self.module, k)
            if k in PLUGIN_FUNCS and callable(val):
                val = self._wrap(k, val)
            setattr(m, k, val)
        return m

    def _wrap(self, name: str, fn: Callable) -> Callable:
        def w(*a, **kw):
            self.calls.append(name)
            if self.broken == name:
                raise RuntimeError("kit: broken plugin " + SENTINEL)
            return fn(*a, **kw)
        w.__name__ = name
        return w


class _StatVfs:
    def __init__(self):
        self.free = 10 ** 12

    def __call__(self, path: str):
        return types.SimpleNamespace(f_bavail=self.free, f_frsize=1)


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def rendered(self) -> str:
        j, t = hs_logs.JsonFormatter(), hs_logs.TextFormatter()
        return "\n".join(j.format(r) + "\n" + t.format(r) for r in self.records)


class Env:
    """An intake for one kit: temp spool, secret files (mode 0600), fake clock, injected
    ``statvfs`` and a spy around the plugin."""

    def __init__(self, kit: Kit, tmp_path, clock, *, cfg_over: dict | None = None):
        self.kit = kit
        self.clock = clock
        self.tmp = str(tmp_path)
        os.makedirs(self.tmp, exist_ok=True)
        self.secret_paths: dict[str, str] = {}
        refs = {}
        for k, val in kit.secrets.items():
            p = os.path.join(self.tmp, f"secret-{kit.name}-{k}")
            with open(p, "w") as f:
                f.write(val)
            os.chmod(p, 0o600)
            self.secret_paths[k] = p
            refs[k] = {"file": p}
        src = dict(kit.source_cfg)
        src["secrets"] = refs
        cfg = hs_config.merge(hs_config.DEFAULTS, {
            "spool": {"home": os.path.join(self.tmp, "home")}, "alerts": {"dry": True},
            "limits": {"min_free_bytes": 1 << 20}, "sources": {kit.name: src}})
        self.cfg = hs_config.merge(cfg, cfg_over or {})
        self.spy = _Spy(kit.module)
        self.plugin = self.spy.build()
        self.db = os.path.join(self.tmp, "spool.db")
        self.store = Store(self.db, self.cfg, now=clock)
        self.store.ensure_schema()
        self.statvfs = _StatVfs()
        self.intake = hs_intake.Intake(self.cfg, self.store, now=clock, loader=self._load, statvfs=self.statvfs)
        self.logs = _Records()
        logging.getLogger("hookspool").addHandler(self.logs)

    def _load(self, name: str, module: str | None):
        if name != self.kit.name:
            raise ImportError(name)
        return self.plugin

    def close(self) -> None:
        logging.getLogger("hookspool").removeHandler(self.logs)
        self.store.close()

    # -- requests
    def signed(self, raw: RawRequest | None = None, at: float | None = None) -> RawRequest:
        raw = self.kit.sample if raw is None else raw
        return self.kit.sign(raw, self.kit.secrets, self.clock() if at is None else at)

    def send(self, raw: RawRequest, *, length: bool = True, peer: str = "127.0.0.1"):
        self.spy.calls.clear()
        hs = [(k, v) for k, v in raw.headers if k.lower() != "content-length"]
        if length:
            hs.append(("Content-Length", str(len(raw.body))))
        return self.intake.handle(raw.method, raw.target, hs, io.BytesIO(raw.body), peer)

    # -- spool
    def batches(self) -> list[sqlite3.Row]:
        return self.store._read("SELECT * FROM batches ORDER BY id")

    def events(self) -> list[sqlite3.Row]:
        return self.store._read("SELECT * FROM events ORDER BY id")

    def assert_clean_logs(self) -> None:
        text = self.logs.rendered()
        assert SENTINEL not in text, "a log line carries a payload or secret sentinel"
        for val in self.kit.secrets.values():
            assert val not in text, "a log line carries a secret value"

    def assert_no_secret_in_db(self) -> None:
        self.store.close()
        for suffix in ("", "-wal"):
            p = self.db + suffix
            if os.path.exists(p):
                with open(p, "rb") as f:
                    data = f.read()
                for val in self.kit.secrets.values():
                    assert val.encode("utf-8") not in data, "a secret value was written to the spool"
        self.store = Store(self.db, self.cfg, now=self.clock)
        self.intake.store = self.store


def _status(resp) -> str:
    return json.loads(resp.body)["status"]


def _tamper(body: bytes) -> bytes:
    if not body:
        return b" "
    i = len(body) // 2
    return body[:i] + bytes([body[i] ^ 0x01]) + body[i + 1:]


# ---- the matrix ----------------------------------------------------------------------------------------------
def case_signed(env: Env) -> None:
    r = env.send(env.signed())
    assert r.status == 200 and _status(r) == "queued", (r.status, r.body)
    calls = list(env.spy.calls)
    if getattr(env.kit.module, "challenge_unauthenticated", False) and calls[:1] == ["challenge"]:
        calls = calls[1:]                                       # the secret-free handshake check runs first
    assert calls[0] == "verify", env.spy.calls
    ev = env.events()
    assert len(ev) == 1 and bytes(ev[0]["raw"] or b"") in (b"", env.kit.sample.body)
    stored = json.loads(ev[0]["headers"] or "{}")
    lowered = {k.lower() for k in stored}
    for h in tuple(getattr(env.kit.module, "auth_headers", ())) + ("authorization", "cookie"):
        assert h.lower() not in lowered, f"auth header {h} was stored"


def case_unsigned(env: Env) -> None:
    r = env.send(env.kit.sample)
    assert (r.status, r.body, r.headers) == (401, b"", {}), (r.status, r.body, r.headers)
    assert [c for c in env.spy.calls if c != "challenge"] == ["verify"], env.spy.calls
    assert env.events() == []


def case_tampered(env: Env) -> None:
    if not env.kit.body_signed:
        pytest.skip("static token: the body is not covered by the credential")
    signed = env.signed()
    r = env.send(signed.with_body(_tamper(signed.body)))
    assert (r.status, r.body) == (401, b""), r.status
    assert env.events() == []


def case_stale(env: Env) -> None:
    if not env.kit.timestamped:
        pytest.skip("scheme has no timestamp")
    r = env.send(env.signed(at=env.clock() - env.kit.tolerance_s - 1))
    assert (r.status, r.body) == (401, b""), r.status
    r = env.send(env.signed(at=env.clock() - env.kit.tolerance_s))
    assert r.status == 200, "a signature exactly at the tolerance edge must verify"


def case_future(env: Env) -> None:
    if not env.kit.timestamped:
        pytest.skip("scheme has no timestamp")
    r = env.send(env.signed(at=env.clock() + env.kit.tolerance_s + 1))
    assert (r.status, r.body) == (401, b""), "a future-dated request must be rejected (two-sided window)"
    assert env.events() == []


def case_oversized(env: Env) -> None:
    cap = env.intake._snap.caps[env.kit.name]
    big = env.kit.sample.with_body(env.kit.sample.body + b" " * (cap + 1 - len(env.kit.sample.body)))
    r = env.send(env.signed(big))
    assert (r.status, r.body) == (413, b""), r.status
    assert env.spy.calls == [], f"plugin code ran before the body cap: {env.spy.calls}"


def case_chunked(env: Env) -> None:
    r = env.send(env.signed().set("Transfer-Encoding", "chunked"), length=False)
    assert (r.status, r.body) == (411, b""), r.status
    assert env.spy.calls == []


def case_no_length(env: Env) -> None:
    raw = env.signed()
    if raw.method not in ("POST", "PUT", "PATCH"):
        pytest.skip("sample has no body method")
    r = env.send(raw, length=False)
    assert r.status == 411 and env.spy.calls == []


def case_duplicate(env: Env) -> None:
    if not callable(getattr(env.kit.module, "dedup_id", None)):
        pytest.skip("plugin defines no dedup_id")
    raw = env.signed()
    assert _status(env.send(raw)) == "queued"
    r = env.send(raw)
    assert r.status == 200 and _status(r) == "duplicate", r.body
    assert len(env.events()) == 1


def case_handshake(env: Env) -> None:
    if env.kit.handshake is None:
        pytest.skip("source has no verified handshake")
    r = env.send(env.signed(env.kit.handshake))
    assert 200 <= r.status < 300, r.status
    assert env.spy.calls.index("verify") < env.spy.calls.index("challenge"), env.spy.calls
    assert env.spy.calls[-1] == "challenge", "a handshake must stop at challenge()"
    assert env.batches() == [], "a handshake must never enqueue"


def case_handshake_bad_signature(env: Env) -> None:
    if env.kit.handshake is None:
        pytest.skip("source has no verified handshake")
    signed = env.signed(env.kit.handshake)
    r = env.send(signed.with_body(_tamper(signed.body)))
    assert (r.status, r.body) == (401, b""), "a handshake with a bad signature must be 401 with no body"
    assert "challenge" not in env.spy.calls, "challenge() ran for an unverified request"
    r = env.send(env.kit.handshake)
    assert (r.status, r.body) == (401, b"")
    assert "challenge" not in env.spy.calls


def case_unauth_handshake_constant(env: Env) -> None:
    if env.kit.unauth_handshake is None:
        pytest.skip("source has no unauthenticated handshake")
    assert getattr(env.kit.module, "challenge_unauthenticated", False) is True
    hs = env.kit.unauth_handshake
    first = env.send(hs)
    assert 200 <= first.status < 300, first.status
    assert "verify" not in env.spy.calls, "an unauthenticated handshake is answered before verify"
    variants = [hs.set("X-Kit-Probe", SENTINEL), hs.set("User-Agent", "other"),
                request(hs.method, hs.target + ("&" if "?" in hs.target else "?") + "x=" + SENTINEL, hs.headers)]
    for p in env.secret_paths.values():                       # rotate every secret
        with open(p, "w") as f:
            f.write("rotated-test-secret")
    for raw in variants:
        r = env.send(raw)
        assert r.key() == first.key(), "an unauthenticated handshake answer must be constant"
        assert SENTINEL.encode() not in r.body
    # challenge() itself must not read secrets
    seen: list[str] = []

    class Recording(Mapping):
        def __getitem__(self, k):
            seen.append(k)
            raise KeyError(k)

        def __iter__(self):
            seen.append("*")
            return iter(())

        def __len__(self):
            seen.append("*")
            return 0
    from hookspool.plugin import Request
    env.kit.module.challenge(Request(hs.method, hs.target, hs.headers, hs.body, now=env.clock()), Recording())
    assert seen == [], "an unauthenticated challenge read secrets"
    assert env.batches() == []


def case_plugin_exception(env: Env) -> None:
    stage = env.kit.break_stage
    env.spy.broken = stage
    raw = env.signed().set("Authorization", "Bearer " + SENTINEL).set("Cookie", "c=" + SENTINEL)
    r = env.send(raw)
    assert r.status == 202 and _status(r) == "quarantined", (r.status, r.body)
    b = env.batches()
    assert len(b) == 1 and b[0]["state"] == "dead" and b[0]["destination"] == "quarantine", dict(b[0])
    assert b[0]["last_class"] == "quarantine"
    assert b[0]["last_error"] == f"plugin error: RuntimeError in {stage}", b[0]["last_error"]
    ev = env.events()
    assert bytes(ev[0]["raw"]) == raw.body
    stored = {k.lower() for k in json.loads(ev[0]["headers"])}
    for h in tuple(env.kit.module.auth_headers) + ("Authorization", "Cookie"):
        assert h.lower() not in stored, f"{h} was stored with a quarantined request"
    iss = env.store._read("SELECT fingerprint FROM issues WHERE state='open'")
    assert [i["fingerprint"] for i in iss] == [f"config:plugin:{env.kit.name}"]


def case_spool_unwritable(env: Env) -> None:
    orig = env.store.enqueue

    def fail(*a, **k):
        raise SpoolError("database or disk is full")
    env.store.enqueue = fail
    try:
        r = env.send(env.signed())
    finally:
        env.store.enqueue = orig
    assert (r.status, r.body, r.headers.get("Retry-After")) == (503, b"", "60"), (r.status, r.headers)


def case_low_disk(env: Env) -> None:
    env.statvfs.free = 1
    env.clock.advance(10)                                        # past the 5 s statvfs cache
    r = env.send(env.signed())
    assert (r.status, r.headers.get("Retry-After")) == (503, "60"), r.status
    assert env.events() == []


def case_secrets_not_stored(env: Env) -> None:
    env.send(env.signed())
    env.spy.broken = env.kit.break_stage
    env.send(env.signed().set("X-Kit-Other", "1"))
    env.assert_no_secret_in_db()


CASES = [case_signed, case_unsigned, case_tampered, case_stale, case_future, case_oversized, case_chunked,
         case_no_length, case_duplicate, case_handshake, case_handshake_bad_signature,
         case_unauth_handshake_constant, case_plugin_exception, case_spool_unwritable, case_low_disk,
         case_secrets_not_stored]
CASE_IDS = [c.__name__[5:] for c in CASES]


def run(case: Callable[[Env], None], kit: Kit, tmp_path, clock) -> None:
    """Run one matrix case against a fresh environment, then the log checks."""
    env = Env(kit, tmp_path, clock)
    try:
        case(env)
        env.assert_clean_logs()
    finally:
        env.close()


# ---- captures --------------------------------------------------------------------------------------------------
def capture_dir(source: str) -> str:
    """A bobbin's captures live in its own directory (14.3.1); other sources keep tests/captures/<name>."""
    own = os.path.join(BOBBINS, source, "captures")
    return own if os.path.isdir(own) else os.path.join(CAPTURES, source)


def bobbin_manifest(name: str) -> str:
    return os.path.join(BOBBINS, name, "bobbin.toml")


def bobbin_source_cfg(name: str) -> dict:
    """The [sources.<name>] table a template bobbin expands to for its sample instance, built from the
    manifest without running expansion. Secrets are a placeholder reference; callers drop them."""
    with open(bobbin_manifest(name), "rb") as f:
        m = tomllib.load(f)
    src = m["source"]
    cfg = {"module": TEMPLATE_MODULE, **src["template"], **SAMPLE_SETTINGS.get(name, {})}
    for k in ("span", "sender_retries"):
        if k in src:
            cfg[k] = src[k]
    cfg["action"] = f"hermes:{name}"
    cfg["secrets"] = {"secret": {"file": f"/etc/hookspool/secrets/{name}"}}
    return cfg


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"not a TOML value: {v!r}")


def source_table_text(name: str) -> str:
    """``[sources.<name>]`` for a bobbin's sample instance, without the secret (the caller adds it)."""
    cfg = bobbin_source_cfg(name)
    body = "\n".join(f"{k} = {toml_value(v)}" for k, v in cfg.items() if k != "secrets")
    return f"[sources.{name}]\n{body}\n"


def captures(source: str) -> list[str]:
    d = capture_dir(source)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".http")) if os.path.isdir(d) else []


def run_capture(path: str, kit: Kit, tmp_path, clock) -> dict:
    """``source_test`` on a capture; asserts the ``# expect:`` result. Returns the report."""
    with open(path, "rb") as f:
        raw = parse_capture(f.read())
    env = Env(kit, tmp_path, clock)
    try:
        out = hs_intake.source_test(env.cfg, kit.name, raw, loader=env._load)
        assert out["result"] == raw.meta.get("expect", "queued"), out
        assert SENTINEL not in json.dumps(out, default=str)
        assert env.batches() == [], "source test must never enqueue"
        env.assert_clean_logs()
        return out
    finally:
        env.close()
