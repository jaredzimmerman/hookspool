"""The ``courtlistener.alerts`` bobbin (ARCHITECTURE 14.3): a fixed url-token template, the one secret that
authenticates a delivery, and the two captures the bobbin ships with. CourtListener does not sign its
deliveries, so the token in the callback URL is the whole check; the ``Idempotency-Key`` header is the
delivery id that recognises a redelivery."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.dirname(TESTS))

from hookspool import bobbin_loader as PK  # noqa: E402
from hookspool import config as C  # noqa: E402
from hookspool import intake as I  # noqa: E402
from hookspool import plugin as P  # noqa: E402
from hookspool.plugin import parse_capture  # noqa: E402
from hookspool.sources import template as T  # noqa: E402

import sourcekit  # noqa: E402
from sourcekit import Env, Kit  # noqa: E402

BOBBIN = os.path.join(os.path.dirname(os.path.dirname(HERE)), "hookspool", "bobbins", "courtlistener", "alerts")
CAPTURES = os.path.join(BOBBIN, "captures")
SECRET = "test-template-secret-PAYLOAD-SENTINEL-7f3a"


def capture(name: str):
    with open(os.path.join(CAPTURES, name), "rb") as f:
        return parse_capture(f.read())


def secret_file(tmp_path, value: str = SECRET) -> str:
    p = tmp_path / "secret"
    p.write_text(value)
    os.chmod(p, 0o600)
    return str(p)


def expand(tmp_path, secret: str = SECRET) -> dict:
    inst = {"bobbin": "courtlistener.alerts", "secrets": {"secret": {"file": secret_file(tmp_path, secret)}},
            "outputs": {"*": "hermes:alerts"}}
    return PK.expand(C.merge(C.DEFAULTS, {"bobbins": {"alerts": inst}, "alerts": {"argv": ["true"]}}))


def test_the_manifest_is_a_fixed_url_token_template():
    pkgs, errs = PK.discover(C.merge(C.DEFAULTS, {}))
    assert errs == []
    pkg = pkgs["courtlistener.alerts"]
    assert pkg.errors == [] and pkg.builtin and pkg.processor is None
    tpl = pkg.source["template"]
    assert tpl["scheme"] == "url-token" and tpl["param"] == "token"
    assert tpl["dedup"] == "header:Idempotency-Key" and tpl["key"] == "webhook.event_type"
    assert set(pkg.secrets) == {"secret"} and set(pkg.outputs) == {"*"} and pkg.settings == {}


def test_the_expanded_source_uses_the_token_and_the_idempotency_key(tmp_path):
    src = expand(tmp_path)["sources"]["alerts"]
    assert src["scheme"] == "url-token" and src["key"] == "webhook.event_type"
    assert src["dedup"] == "header:Idempotency-Key" and src["sender_retries"] is True


def test_the_accepted_capture_is_queued(tmp_path):
    got = I.source_test(expand(tmp_path), "alerts", capture("event.http"))
    assert got["result"] == "queued"


def test_a_wrong_token_is_refused_and_queues_nothing(tmp_path):
    got = I.source_test(expand(tmp_path, secret="test-secret-other"), "alerts", capture("event.http"))
    assert got["result"] == "unverified" and got["status"] == 401


def test_the_wrong_token_capture_is_refused(tmp_path):
    got = I.source_test(expand(tmp_path), "alerts", capture("wrong_token.http"))
    assert got["result"] == "unverified"


# ---- the intake, through the shared kit's environment: redelivery, a missing token, and what a refusal carries ----
KIT_CFG = sourcekit.bobbin_source_cfg("alerts")
KIT = Kit(name="alerts", module=T.bind("alerts", KIT_CFG), secrets={"secret": SECRET}, sample=capture("event.http"),
          sign=lambda raw, secrets, now: raw, source_cfg={k: v for k, v in KIT_CFG.items() if k != "secrets"})


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


def with_target(raw, target: str):
    return P.RawRequest(raw.method, target, list(raw.headers), raw.body)


def test_a_delivery_with_no_token_is_unverified_and_queues_nothing(env):
    r = env.send(with_target(KIT.sample, "/hooks/alerts"))
    assert r.status == 401 and env.events() == []


def test_a_redelivery_with_the_same_idempotency_key_is_a_duplicate(env):
    assert json.loads(env.send(KIT.sample).body)["status"] == "queued"
    r = env.send(KIT.sample)
    assert r.status == 200 and json.loads(r.body)["status"] == "duplicate"
    assert len(env.events()) == 1
    other = KIT.sample.set("Idempotency-Key", "00000000-0000-4000-8000-000000000002")
    assert json.loads(env.send(other).body)["status"] == "queued"


def test_an_unverified_result_carries_no_token_text(tmp_path, env):
    wrong = "test-secret-other"
    r = env.send(with_target(KIT.sample, f"/hooks/alerts?token={wrong}"))
    assert r.status == 401
    assert SECRET.encode() not in r.body and wrong.encode() not in r.body
    got = I.source_test(expand(tmp_path, secret=wrong), "alerts", capture("event.http"))
    assert got["result"] == "unverified"
    assert SECRET not in json.dumps(got) and wrong not in json.dumps(got)
