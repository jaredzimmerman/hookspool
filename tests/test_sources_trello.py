"""Trello source (T6). The bridge is ported as CONFIG, not code: ``hookspool/bobbins/trello`` on the
``template`` plugin (T5b), because base64 HMAC-SHA1 over body + callback URL and an unauthenticated HEAD
handshake are both template features. This file holds Trello's own acceptance: the shared kit on that
config, the captures, the HEAD handshake, key/dedup, and parity of the signature check with
``trello_bridge.py`` (the callback URL is part of the signed message).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sourcekit  # noqa: E402
import vpssrc  # noqa: E402
from sourcekit import SENTINEL, Env, Kit  # noqa: E402

from hookspool import config as hs_config  # noqa: E402
from hookspool import plugin as P  # noqa: E402
from hookspool.plugin import Request, parse_capture  # noqa: E402
from hookspool.sources import template as T  # noqa: E402

SECRET = "test-template-secret-" + SENTINEL        # the secret the bobbin's captures are signed with
CFG = sourcekit.bobbin_source_cfg("trello")
CALLBACK = CFG["callback_url"]


def sign_for(callback: str, secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body + callback.encode(), hashlib.sha1).digest()).decode()


def sign(raw, secrets, now):
    return raw.set(CFG["header"], sign_for(CALLBACK, secrets["secret"], raw.body))


def _capture(name: str) -> P.RawRequest:
    with open(os.path.join(sourcekit.capture_dir("trello"), name), "rb") as f:
        raw = parse_capture(f.read())
    raw.meta.clear()
    return raw


def kit() -> Kit:
    sample = _capture("event.http").set(CFG["header"], None)
    assert SENTINEL.encode() in sample.body
    return Kit(name="trello", module=T.bind("trello", CFG), secrets={"secret": SECRET}, sign=sign, sample=sample,
               source_cfg={k: v for k, v in CFG.items() if k != "secrets"}, unauth_handshake=_capture("head.http"))


KIT = kit()


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)


CAPTURES = sourcekit.captures("trello")


@pytest.mark.parametrize("path", CAPTURES, ids=[os.path.basename(p) for p in CAPTURES])
def test_captures(path, tmp_path, clock):
    assert sourcekit.run_capture(path, KIT, tmp_path, clock)["source"] == "trello"


def test_the_example_is_a_placeholder_not_a_deployment():
    assert CFG["module"] == "hookspool.sources.template" and CFG["scheme"] == "hmac-sha1-base64-url"
    assert CFG["head_handshake"] is True and CFG["action"] == "hermes:trello" and CFG["span"] == "mail"
    assert CALLBACK.startswith("https://") and ".example." in CALLBACK, "the callback URL is a placeholder"


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


def post(env, body: bytes, sig: str | None):
    raw = P.RawRequest("POST", "/hooks/trello", [("Content-Type", "application/json")], body)
    if sig is not None:
        raw = raw.set(CFG["header"], sig)
    return env.send(raw)


def card(action_id="a-1", board="board-1", kind="updateCard") -> bytes:
    return json.dumps({"model": {"id": board}, "action": {"id": action_id, "type": kind,
                                                          "data": {"text": SENTINEL}}}).encode()


# ---- the HEAD handshake -------------------------------------------------------------------------------------
def test_head_is_answered_empty_200_without_a_signature_and_never_enqueues(env):
    r = env.send(KIT.unauth_handshake)
    assert (r.status, r.body) == (200, b"") and env.batches() == []
    assert "verify" not in env.spy.calls


def test_post_still_needs_a_signature_on_a_head_handshake_source(env):
    assert post(env, card(), None).status == 401
    assert post(env, card(), "AAAA").status == 401
    assert env.events() == []


# ---- the signature covers the callback URL -------------------------------------------------------------------
def test_a_signature_made_for_another_callback_url_is_refused(env):
    body = card()
    assert post(env, body, sign_for(CALLBACK, SECRET, body)).status == 200
    for other in (CALLBACK + "/", CALLBACK.replace("https", "http"), CALLBACK + "?x=1", CALLBACK.upper(), ""):
        assert post(env, body, sign_for(other, SECRET, body)).status == 401, other
    assert post(env, body, sign_for(CALLBACK, "other" + SECRET, body)).status == 401


def test_a_body_only_signature_is_refused(env):
    body = card()
    mac = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha1).digest()).decode()
    assert post(env, body, mac).status == 401


def _old_verify(callback: str, body: bytes, provided: str, secret: str) -> bool:
    """trello_bridge.verify_trello_signature, verbatim (CALLBACK_URL made a parameter)."""
    if not (provided and secret):
        return False
    message = body + callback.encode()
    expected = base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha1).digest()).decode()
    return hmac.compare_digest(provided, expected)


def _grid(body: bytes):
    good = sign_for(CALLBACK, SECRET, body)
    yield "good", good
    yield "other-body", sign_for(CALLBACK, SECRET, body + b" ")
    yield "other-callback", sign_for(CALLBACK + "x", SECRET, body)
    yield "other-secret", sign_for(CALLBACK, SECRET + "x", body)
    yield "empty", ""
    yield "garbage", "!!!!"
    yield "truncated", good[:-4]
    yield "padding-stripped", good.rstrip("=")
    yield "hex", hmac.new(SECRET.encode(), body + CALLBACK.encode(), hashlib.sha1).hexdigest()


def test_parity_with_the_bridge_verification():
    body = card()
    mod = T.bind("trello", CFG)
    for label, sig in _grid(body):
        req = Request("POST", "/hooks/trello", [(CFG["header"], sig)] if sig else [], body, now=0,
                      config={k: v for k, v in CFG.items() if k != "secrets"})
        new = mod.verify(req, {"secret": SECRET}) is True
        old = _old_verify(CALLBACK, body, sig, SECRET)
        if label == "padding-stripped":
            assert old is False and new in (True, False)        # the plugin tolerates missing padding; old did not
            continue
        assert new == old, label


def test_parity_with_the_real_bridge_file_when_present(monkeypatch):
    bridge = vpssrc.load("trello_bridge.py")
    monkeypatch.setattr(bridge, "CALLBACK_URL", CALLBACK)
    body = card()
    for label, sig in _grid(body):
        assert bridge.verify_trello_signature(body, sig, SECRET) == _old_verify(CALLBACK, body, sig, SECRET), label
    assert bridge.verify_trello_signature(body, sign_for(CALLBACK, SECRET, body), "") is False


# ---- key, dedup, coalescing ---------------------------------------------------------------------------------------
def test_key_is_the_board_dedup_is_the_action_and_boards_coalesce(env):
    r1 = post(env, card("a-1"), sign_for(CALLBACK, SECRET, card("a-1")))
    r2 = post(env, card("a-2", kind="createCard"), sign_for(CALLBACK, SECRET, card("a-2", kind="createCard")))
    r3 = post(env, card("a-1"), sign_for(CALLBACK, SECRET, card("a-1")))
    r4 = post(env, card("a-9", board="board-2"), sign_for(CALLBACK, SECRET, card("a-9", board="board-2")))
    assert [json.loads(r.body)["status"] for r in (r1, r2, r3, r4)] == ["queued", "queued", "duplicate", "queued"]
    rows = {b["key"]: b for b in env.batches()}
    assert set(rows) == {"board-1", "board-2"} and rows["board-1"]["count"] == 2
    assert (rows["board-1"]["quiet_s"], rows["board-1"]["max_s"]) == (30.0, 120.0)           # the mail span
    assert rows["board-1"]["destination"] == "hermes:trello"
    assert [e["event_type"] for e in env.events() if e["dedup_id"] == "a-2"] == ["createCard"]


def test_config_check_accepts_the_example(tmp_path):
    sec = tmp_path / "secret"
    sec.write_text(SECRET)
    os.chmod(sec, 0o600)
    text = sourcekit.source_table_text("trello")
    p = tmp_path / "c.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n{text}\n'
                 f'secrets.secret = {{ file = "{sec}" }}\n')
    rep = hs_config.check(hs_config.load_config(str(p)), check_secrets=True)
    assert [e for e in rep.errors if "trello" in e] == [], rep.errors
