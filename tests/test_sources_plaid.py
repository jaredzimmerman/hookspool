"""Plaid source (contract C4): the shared kit, plus the JWT checks, the key-fetch guard
(ARCHITECTURE section 10) and the 600 s dedup window (built into the plugin)."""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request

import plaidkit
import pytest
import sourcekit
from sourcekit import Env, request

from hookspool.sources import plaid

KIT = plaidkit.kit(plaid)
REAL_FETCH = plaid.fetch_key
BODY = plaidkit.BODY


class Fetcher:
    """Stub for ``plaid.fetch_key``: counts calls, serves the test key's JWK for known kids."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.known = {plaidkit.KID: plaidkit.jwk_of(plaidkit.private_key())}
        self.error: Exception | None = None

    def __call__(self, env, client_id, secret, kid):
        self.calls.append((env, client_id, secret, kid))
        if self.error is not None:
            raise self.error
        return self.known.get(kid)


@pytest.fixture(autouse=True)
def fetcher(monkeypatch):
    plaid.reset()
    f = Fetcher()
    monkeypatch.setattr(plaid, "fetch_key", f)
    yield f
    plaid.reset()


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


def post(env, token, body=BODY):
    raw = request("POST", "/hooks/plaid", {"Content-Type": "application/json", "Plaid-Verification": token}, body)
    return env.send(raw)


def status(resp):
    return resp.status, (json.loads(resp.body)["status"] if resp.body else "")


# ---- the shared matrix and captures ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)


CAPS = sourcekit.captures("plaid")


def test_captures_exist():
    assert {os.path.basename(p) for p in CAPS} >= {"sync_updates.http", "tampered.http", "stale.http"}


@pytest.mark.parametrize("path", CAPS, ids=[os.path.basename(p) for p in CAPS])
def test_captures(path, tmp_path, clock):
    out = sourcekit.run_capture(path, KIT, tmp_path, clock)
    assert out["source"] == "plaid"


# ---- verification --------------------------------------------------------------------------------------------
def test_signed_event_is_classified(env, clock):
    r = post(env, plaidkit.make_jwt(BODY, clock()))
    assert status(r) == (200, "queued")
    (b,) = env.batches()
    assert b["key"] == "item-abc123"
    (ev,) = env.events()
    assert ev["event_type"] == "TRANSACTIONS.SYNC_UPDATES_AVAILABLE"
    assert ev["dedup_id"].startswith(__import__("hashlib").sha256(BODY).hexdigest() + ":")


def test_known_key_is_fetched_once_with_credentials(env, clock, fetcher):
    for i in range(3):
        clock.advance(1)
        assert post(env, plaidkit.make_jwt(BODY, clock()), BODY + b" " * (i + 1)).status == 401   # body != signed
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    assert fetcher.calls == [(plaidkit.ENV, plaidkit.CLIENT_ID, plaidkit.SECRET, plaidkit.KID)]


def test_body_hash_must_match(env, clock):
    tok = plaidkit.make_jwt(BODY, clock())
    assert post(env, tok, BODY + b" ").status == 401
    assert env.events() == []


def test_body_hash_claim_required(env, clock):
    tok = plaidkit.make_jwt(BODY, clock(), claims={"request_body_sha256": None})
    assert post(env, tok).status == 401


def test_signature_from_a_foreign_key_is_refused(env, clock):
    tok = plaidkit.make_jwt(BODY, clock(), key=plaidkit.private_key("another-test-key"))
    assert post(env, tok).status == 401


def test_iat_edges_are_two_sided(env, clock):
    assert post(env, plaidkit.make_jwt(BODY, clock() - 300)).status == 200
    clock.advance(5)
    assert post(env, plaidkit.make_jwt(BODY, clock() - 301), BODY + b" ").status == 401
    assert post(env, plaidkit.make_jwt(BODY, clock() + 301), BODY + b" ").status == 401


def test_missing_iat_is_refused(env, clock):
    import jwt
    c = {"request_body_sha256": __import__("hashlib").sha256(BODY).hexdigest()}
    tok = jwt.encode(c, plaidkit.private_key(), algorithm="ES256", headers={"kid": plaidkit.KID})
    assert post(env, tok).status == 401


@pytest.mark.parametrize("make", [
    lambda c: "",
    lambda c: "not-a-jwt",
    lambda c: "a.b.c",
    lambda c: plaidkit.make_jwt(BODY, c, alg="HS256", key="x" * 40),
    lambda c: __import__("jwt").encode({"iat": int(c)}, None, algorithm="none", headers={"kid": plaidkit.KID}),
    lambda c: __import__("jwt").encode({"iat": int(c)}, plaidkit.private_key(), algorithm="ES256"),   # no kid
    lambda c: plaidkit.make_jwt(BODY, c, kid="k" * 300),
    lambda c: "x" * 20000,
], ids=["empty", "garbage", "abc", "hs256", "alg-none", "no-kid", "long-kid", "huge"])
def test_malformed_tokens_never_reach_the_network(env, clock, fetcher, make):
    r = post(env, make(clock()))
    assert (r.status, r.body) == (401, b"")
    assert fetcher.calls == []
    assert env.events() == []


# ---- the key-fetch guard -------------------------------------------------------------------------------------
def test_random_kids_cause_at_most_one_fetch_per_minute(env, clock, fetcher):
    other = plaidkit.private_key("attacker")
    for i in range(20):
        r = post(env, plaidkit.make_jwt(BODY, clock(), key=other, kid=f"rand-{i}"))
        assert r.status == 401
    assert len(fetcher.calls) == 1
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock(), key=other, kid="rand-x")).status == 401
    assert len(fetcher.calls) == 2


def test_unknown_kid_is_negative_cached_for_ten_minutes(env, clock, fetcher):
    other = plaidkit.private_key("attacker")
    def tok():
        return plaidkit.make_jwt(BODY, clock(), key=other, kid="ghost")
    assert post(env, tok()).status == 401
    assert len(fetcher.calls) == 1
    clock.advance(120)                                   # cooldown over, negative cache still holds
    assert post(env, tok()).status == 401
    assert len(fetcher.calls) == 1
    clock.advance(500)                                   # 620 s after the miss
    assert post(env, tok()).status == 401
    assert len(fetcher.calls) == 2


def test_rotation_new_key_is_picked_up_after_the_cooldown(env, clock, fetcher):
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    new = plaidkit.private_key("rotated")
    fetcher.known["kid-2"] = plaidkit.jwk_of(new, "kid-2")
    tok = plaidkit.make_jwt(BODY, clock(), key=new, kid="kid-2")
    assert post(env, tok).status == 401                  # inside the 60 s cooldown: Plaid will retry
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock(), key=new, kid="kid-2")).status == 200
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200       # the old key is still cached
    assert len(fetcher.calls) == 2


def test_key_cache_expires_after_a_day(env, clock, fetcher):
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    clock.advance(24 * 3600 + 1)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    assert len(fetcher.calls) == 2


def test_known_key_survives_a_fetch_slot_starved_by_random_kids(env, clock, fetcher):
    """An unauthenticated client sending one random-kid request a minute must not stop the
    real key being served after its TTL (the stale key is kept while a refresh is blocked)."""
    other = plaidkit.private_key("attacker")
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    clock.advance(24 * 3600 + 1)
    served = 0
    for i in range(120):                                  # two hours, attacker first every minute
        clock.advance(60)
        assert post(env, plaidkit.make_jwt(BODY, clock(), key=other, kid=f"rand-{i}")).status == 401
        served += post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    assert served == 120


def test_known_key_survives_a_failing_refresh_and_is_dropped_when_plaid_says_expired(env, clock, fetcher):
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    clock.advance(24 * 3600 + 1)
    fetcher.error = OSError("network down")
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200      # stale key kept
    fetcher.error = None
    fetcher.known[plaidkit.KID] = plaidkit.jwk_of(plaidkit.private_key(), expired_at=1_700_000_000)
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401      # a successful fetch said expired
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401      # and it stays dropped (negative cache)


@pytest.mark.parametrize("refresh", ["failing", "blocked"])
def test_key_plaid_marked_expired_stays_dropped_after_the_negative_cache_lapses(env, clock, fetcher, refresh):
    """Once a successful fetch said "expired", the old key is gone: when the 10 min negative
    cache lapses and the next refresh fails or is blocked by the cooldown, it is NOT served again."""
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    clock.advance(24 * 3600 + 1)
    fetcher.known[plaidkit.KID] = plaidkit.jwk_of(plaidkit.private_key(), expired_at=1_700_000_000)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401      # Plaid said expired
    clock.advance(plaid.NEGATIVE_TTL_S + 1)
    if refresh == "failing":
        fetcher.error = OSError("network down")
    else:                                                                 # a random kid takes the slot first
        other = plaidkit.private_key("attacker")
        assert post(env, plaidkit.make_jwt(BODY, clock(), key=other, kid="rand-x")).status == 401
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401


class _Resp:
    """A 200 answer for the stubbed opener (the shape ``net.Http`` reads)."""
    status = 200

    def __init__(self, data):
        self.fp = io.BytesIO(json.dumps(data).encode())

    def read(self, n=-1):
        return self.fp.read(n)

    def close(self):
        pass


def _real_fetch(monkeypatch, answers):
    """Run the real ``fetch_key`` (through ``net.Http``) against a stubbed opener: each answer is a JWK dict
    (200) or an ``(http status, error_code)`` refusal."""
    calls = []

    def open_(self, req, data=None, timeout=None):
        calls.append(json.loads(req.data))
        a = answers.pop(0)
        if isinstance(a, tuple):
            body = json.dumps({"error_code": a[1], "error_message": "x " + plaidkit.SECRET}).encode()
            raise urllib.error.HTTPError(req.full_url, a[0], "refused", {}, io.BytesIO(body))
        return _Resp({"key": a})
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_)
    monkeypatch.setattr(plaid, "fetch_key", REAL_FETCH)
    return calls


def test_the_real_fetch_posts_the_credentials_to_the_env_host(monkeypatch):
    seen = []

    def open_(self, req, data=None, timeout=None):
        seen.append((req.full_url, req.get_method(), json.loads(req.data)))
        return _Resp({"key": plaidkit.jwk_of(plaidkit.private_key())})
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_)
    jwk = REAL_FETCH("sandbox", "cid", "sec", "kid-1")
    assert jwk["kid"] == plaidkit.KID
    assert seen == [("https://sandbox.plaid.com/webhook_verification_key/get", "POST",
                     {"client_id": "cid", "secret": "sec", "key_id": "kid-1"})]


def test_a_400_invalid_api_keys_keeps_the_known_key_and_is_not_negative_cached(monkeypatch, env, clock):
    jwk = plaidkit.jwk_of(plaidkit.private_key())
    calls = _real_fetch(monkeypatch, [jwk, (400, "INVALID_API_KEYS"), (400, "INVALID_API_KEYS"), jwk])
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    clock.advance(24 * 3600 + 1)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200      # credential problem: stale key kept
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200      # refetched (not negative-cached), kept
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200      # credentials fixed: fresh key
    assert len(calls) == 4
    text = env.logs.rendered()
    assert "plaid sandbox key fetch failed: Permanent http=400 error_code=INVALID_API_KEYS" in text
    assert plaidkit.SECRET not in text and plaidkit.CLIENT_ID not in text


@pytest.mark.parametrize("answer", [(400, "INVALID_WEBHOOK_VERIFICATION_KEY_ID"), (404, "")])
def test_plaid_saying_unknown_key_drops_it_and_negative_caches(monkeypatch, env, clock, answer):
    jwk = plaidkit.jwk_of(plaidkit.private_key())
    calls = _real_fetch(monkeypatch, [jwk, answer])
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    clock.advance(24 * 3600 + 1)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401      # negative-cached: no fetch
    assert len(calls) == 2


def test_error_code_is_sanitised_before_logging(monkeypatch, env, clock):
    _real_fetch(monkeypatch, [(400, "bad code\n" + plaidkit.SECRET)])
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401
    text = env.logs.rendered()
    assert "http=400 error_code=-" in text and plaidkit.SECRET not in text


def test_key_fetch_failures_leave_one_safe_log_line(env, clock, fetcher):
    fetcher.error = OSError("network down " + plaidkit.SECRET)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401
    text = env.logs.rendered()
    assert "plaid sandbox key fetch failed: OSError" in text
    assert plaidkit.SECRET not in text and plaidkit.CLIENT_ID not in text


def test_a_400_from_plaid_is_logged_and_still_401(monkeypatch, env, clock):
    def refuse(self, req, data=None, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"error_code":"INVALID_API_KEYS"}'))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(plaid, "fetch_key", REAL_FETCH)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401
    text = env.logs.rendered()
    assert "plaid sandbox" in text and "http=400" in text and plaidkit.SECRET not in text


def test_expired_key_is_not_used(env, clock, fetcher):
    fetcher.known[plaidkit.KID] = plaidkit.jwk_of(plaidkit.private_key(), expired_at=1_700_000_000)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401
    assert env.events() == []


def test_network_failure_is_401_and_not_negative_cached(env, clock, fetcher):
    fetcher.error = OSError("network down")
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 401
    fetcher.error = None
    clock.advance(61)
    assert post(env, plaidkit.make_jwt(BODY, clock())).status == 200
    assert len(fetcher.calls) == 2


def test_unreadable_secret_answers_503_not_401(env, clock, fetcher):
    os.unlink(env.secret_paths["secret"])
    r = post(env, plaidkit.make_jwt(BODY, clock()))
    assert (r.status, r.headers.get("Retry-After")) == (503, "60")
    assert fetcher.calls == []
    # but a structurally invalid header is still a plain 401: no secret is read for it
    assert post(env, "garbage").status == 401


def test_a_bad_env_is_refused_when_the_config_loads(tmp_path, clock):
    """``bind`` runs when the source is loaded (and in ``check``), so a bad ``env`` never reaches a webhook."""
    kit = plaidkit.kit(plaid)
    kit.source_cfg = {"env": "staging"}
    with pytest.raises(plaid.PlaidConfigError) as e:
        Env(kit, tmp_path, clock)
    assert e.value.errors == ["env: must be one of production, development, sandbox"]


def test_a_bad_env_that_reaches_verify_answers_503():
    """Defence in depth: a request that carries a bad ``env`` is a configuration fault (retry), not a 401."""
    from hookspool.plugin import Request
    req = Request("POST", "/hooks/plaid", [("Plaid-Verification", plaidkit.make_jwt(BODY, 1_000_000.0))], BODY,
                  now=1_000_000.0, config={"env": "staging"})
    with pytest.raises(ValueError):
        plaid.verify(req, {"client_id": "c", "secret": "s"})


def test_two_instances_in_one_env_share_the_one_fetch_a_minute(tmp_path, clock, fetcher):
    """The reason the key cache is shared: a second Plaid instance in the same ``env`` cannot be used to
    get round the one-fetch-per-minute guard, and it reuses the keys the first one fetched."""
    first = Env(KIT, tmp_path / "a", clock)
    second = Env(KIT, tmp_path / "b", clock)
    try:
        assert post(first, plaidkit.make_jwt(BODY, clock(), kid="unknown-1")).status == 401
        assert len(fetcher.calls) == 1
        assert post(second, plaidkit.make_jwt(BODY, clock(), kid="unknown-2")).status == 401
        assert len(fetcher.calls) == 1                       # the guard is shared: no second fetch inside a minute
        assert post(second, plaidkit.make_jwt(BODY, clock(), kid="unknown-1")).status == 401
        assert len(fetcher.calls) == 1                       # and the negative cache is shared too
        clock.advance(61)
        assert post(second, plaidkit.make_jwt(BODY, clock())).status == 200      # the known key, fetched now
        assert len(fetcher.calls) == 2
        assert post(first, plaidkit.make_jwt(BODY, clock(), kid=plaidkit.KID), BODY + b" ").status == 401
        assert post(first, plaidkit.make_jwt(BODY, clock())).status == 200       # cached by the other instance
        assert len(fetcher.calls) == 2
    finally:
        first.close()
        second.close()


def test_default_fetch_rejects_a_bad_env():
    with pytest.raises(ValueError):
        REAL_FETCH("evil.example", "c", "s", "k")


# ---- classification and the dedup window ---------------------------------------------------------------------
@pytest.mark.parametrize("body,expect_key,expect_type", [
    ({"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "i-9"}, "i-9", "ITEM.ERROR"),
    ({"webhook_type": "LINK", "webhook_code": "SESSION_FINISHED", "link_token": "link-sandbox-1"},
     "link-sandbox-1", "LINK.SESSION_FINISHED"),
    ({"webhook_type": "ASSETS", "webhook_code": "PRODUCT_READY"}, "ASSETS", "ASSETS.PRODUCT_READY"),
    ({"hello": "world"}, "plaid", ""),
])
def test_key_and_event_type(body, expect_key, expect_type):
    from hookspool.plugin import Request
    raw = json.dumps(body).encode()
    req = Request("POST", "/hooks/plaid", [], raw)
    p = req.json()
    assert plaid.key(p, req) == expect_key
    assert plaid.event_type(p, req) == expect_type


def test_identical_body_is_a_duplicate_inside_600s_and_new_after(env, clock):
    assert status(post(env, plaidkit.make_jwt(BODY, clock()))) == (200, "queued")
    clock.advance(30)
    assert status(post(env, plaidkit.make_jwt(BODY, clock()))) == (200, "duplicate")
    clock.advance(601)                                   # a later 600 s bucket
    assert status(post(env, plaidkit.make_jwt(BODY, clock()))) == (200, "queued")
    assert len(env.events()) == 2


def test_default_config_has_no_dedup_window_and_still_delivers_later_identical_bodies(env, clock):
    """C4: the 600 s window must not depend on a config line. Plaid sends byte-identical
    SYNC_UPDATES_AVAILABLE bodies for every notification on an item."""
    from hookspool import config as hs_config
    assert "dedup_window_s" not in env.cfg["sources"]["plaid"]
    assert hs_config.dedup_window_s(env.cfg, "plaid") == 86400          # the core default is a day
    first = status(post(env, plaidkit.make_jwt(BODY, clock())))
    assert first == (200, "queued")
    for _ in range(3):                                                  # four hours of notifications
        clock.advance(3600)
        assert status(post(env, plaidkit.make_jwt(BODY, clock()))) == (200, "queued")
    assert len(env.events()) == 4


def test_dedup_id_is_the_body_hash_and_the_600s_bucket():
    from hookspool.plugin import Request
    a = Request("POST", "/hooks/plaid", [], BODY, now=1_000_000.0)
    same = Request("POST", "/hooks/plaid", [], BODY, now=1_000_000.0 + 1)
    later = Request("POST", "/hooks/plaid", [], BODY, now=1_000_000.0 + 600)
    assert plaid.dedup_id(None, a).split(":")[0] == __import__("hashlib").sha256(BODY).hexdigest()
    assert plaid.dedup_id(None, a) == plaid.dedup_id(None, same)
    assert plaid.dedup_id(None, a) != plaid.dedup_id(None, later)


def test_plugin_declares_what_check_reads():
    assert plaid.auth_headers == ("Plaid-Verification",)
    assert plaid.sender_retries is True and plaid.span == "sync" and plaid.config_keys == ("env",)


def test_builtin_loads_and_passes_config_check(tmp_path):
    """The default loader finds hookspool.sources.plaid and `hookspool check` accepts its table."""
    from hookspool import config as hs_config
    refs = {}
    for k in ("client_id", "secret"):
        p = tmp_path / ("secret-" + k)
        p.write_text("test-value")
        p.chmod(0o600)
        refs[k] = {"file": str(p)}
    src = {"secrets": refs}
    src.update(env="sandbox")
    cfg = hs_config.merge(hs_config.DEFAULTS, {"spool": {"home": str(tmp_path / "home")}, "sources": {"plaid": src}})
    rep = hs_config.check(cfg)
    assert [e for e in rep.errors if "sources." in e] == []
