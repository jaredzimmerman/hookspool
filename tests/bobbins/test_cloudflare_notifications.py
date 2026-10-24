"""The ``cloudflare.notifications`` template bobbin (ARCHITECTURE 14.3): its manifest loads with no errors and
names its provider, its captures produce the results they declare, a wrong secret is refused, a sample instance
expands through the real config path, and the setup documents show a configuration that expands and name every
secret. No payload or secret value reaches the intake's output."""
from __future__ import annotations

import dataclasses
import io
import json
import os
import re
import tomllib

import pytest

from hookspool import bobbin_loader as PK
from hookspool import config as C
from hookspool import intake as I
from hookspool.plugin import RawRequest, parse_capture
from hookspool.store import Store
from hookspool.testing.sourcekit import SENTINEL

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "cloudflare", "notifications")
TEST_SECRET = "test-template-secret-PAYLOAD-SENTINEL-7f3a"          # the value in captures/kit.toml; it contains SENTINEL, so the leak checks cover the secret too
INSTANCE = "cloudflare-alerts"
FENCE = re.compile(r"```toml\n(.*?)```", re.S)


def manifest() -> dict:
    with open(os.path.join(DIR, "bobbin.toml"), "rb") as f:
        return tomllib.load(f)


def doc(name: str) -> str:
    with open(os.path.join(DIR, name), encoding="utf-8") as f:
        return f.read()


def write_secret(path, value: str = TEST_SECRET) -> str:
    path.write_text(value)
    os.chmod(path, 0o600)
    return str(path)


def expanded(instances: dict) -> dict:
    return PK.expand(C.merge(C.DEFAULTS, {"bobbins": instances, "alerts": {"argv": ["true"]}}))


def spec(secret: str) -> dict:
    return {"bobbin": "cloudflare.notifications", "secrets": {"secret": {"file": secret}},
            "outputs": {"*": f"hermes:{INSTANCE}"}}


def captures() -> list[str]:
    d = os.path.join(DIR, "captures")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".http"))


def test_the_manifest_is_a_template_with_the_cloudflare_rules():
    pkgs, errs = PK.discover(C.merge(C.DEFAULTS, {}))
    assert errs == [], errs
    pkg = pkgs["cloudflare.notifications"]
    assert pkg.errors == [] and pkg.builtin and pkg.processor is None
    assert "template" in pkg.source and "module" not in pkg.source
    assert set(pkg.secrets) == {"secret"} and set(pkg.outputs) == {"*"}
    m = manifest()
    assert m["provider"]["vendor"] == "Cloudflare"
    assert m["provider"]["webhook_docs"].startswith("https://developers.cloudflare.com/")
    assert m["source"]["span"] == "burst"
    assert "sender_retries" not in m["source"] and "expect_every_s" not in m["source"]
    tmpl = m["source"]["template"]
    assert (tmpl["scheme"], tmpl["header"], tmpl["key"], tmpl["dedup"]) == (
        "shared-secret-header", "cf-webhook-auth", "name", "body_sha256")


def test_the_captures_declare_the_outcomes_the_bobbin_promises():
    expects = {os.path.basename(c): parse_capture(open(c, "rb").read()).meta["expect"] for c in captures()}
    assert expects == {"event.http": "queued", "wrong_secret.http": "unverified"}


@pytest.mark.parametrize("cap", captures(), ids=lambda c: os.path.basename(c))
def test_each_capture_runs_through_the_intake(cap, tmp_path):
    cfg = expanded({INSTANCE: spec(write_secret(tmp_path / "secret"))})
    raw = parse_capture(open(cap, "rb").read())
    out = I.source_test(cfg, INSTANCE, raw)
    assert out["result"] == raw.meta["expect"], out
    assert SENTINEL not in json.dumps(out, default=str)


def test_a_wrong_secret_is_refused_with_401(tmp_path):
    cfg = expanded({INSTANCE: spec(write_secret(tmp_path / "wrong", TEST_SECRET + "-wrong"))})
    raw = parse_capture(open(os.path.join(DIR, "captures", "event.http"), "rb").read())
    out = I.source_test(cfg, INSTANCE, raw)
    assert out["result"] == "unverified" and out["status"] == 401, out


def test_a_missing_secret_is_an_expansion_error(tmp_path):
    s = spec(write_secret(tmp_path / "secret"))
    del s["secrets"]
    with pytest.raises(PK.BobbinError) as ei:
        expanded({INSTANCE: s})
    assert any(f"bobbins.{INSTANCE}.secrets.secret: required by bobbin" in e for e in ei.value.errors)


def test_an_instance_expands_to_a_template_source_with_the_mapped_action(tmp_path):
    secret = write_secret(tmp_path / "secret")
    src = expanded({INSTANCE: spec(secret)})["sources"][INSTANCE]
    assert src["module"] == "hookspool.sources.template"
    assert src["action"] == f"hermes:{INSTANCE}"
    assert src["secrets"] == {"secret": {"file": secret}}


@pytest.mark.parametrize("name", ["README.md", "SETUP.md", "AGENTS.md"])
def test_the_documents_open_with_the_manifest_frontmatter(name):
    m = manifest()
    head = doc(name).split("---", 2)[1]
    assert f"bobbin: {m['name']}" in head
    assert f"provider: {m['provider']['vendor']} {m['provider']['product']}" in head
    assert f"webhook_docs: {m['provider']['webhook_docs']}" in head


@pytest.mark.parametrize("name", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_documents_expands(name, tmp_path):
    secret = write_secret(tmp_path / "secret")
    text = doc(name)
    (block,) = [b for b in FENCE.findall(text) if "[bobbins." in b]
    block = block.replace("/etc/hookspool/secrets/cloudflare-alerts", secret)
    parsed = tomllib.loads(block)
    assert set(parsed["bobbins"]) == {INSTANCE}
    out = PK.expand(C.merge(C.DEFAULTS, parsed))
    assert INSTANCE in out["sources"]


@pytest.mark.parametrize("name", ["README.md", "SETUP.md", "AGENTS.md"])
def test_no_document_leaves_a_placeholder_or_a_drafting_fragment(name):
    text = doc(name)
    assert "TODO" not in text and "TBD" not in text


def test_the_bobbin_has_no_python_and_calls_nothing_outside_core():
    assert not [f for f in os.listdir(DIR) if f.endswith(".py")]


# ---- negative cases the README promises: each one is checked through the same path as a real delivery -------------
EVENT = parse_capture(open(os.path.join(DIR, "captures", "event.http"), "rb").read())


def variant(*, drop: str | None = None, body: bytes | None = None) -> RawRequest:
    """The captured alert with one header removed and/or another body, Content-Length kept honest."""
    payload = EVENT.body if body is None else body
    headers = [(k, v) for k, v in EVENT.headers
               if k.lower() not in ("content-length", (drop or "").lower())]
    headers.append(("Content-Length", str(len(payload))))
    return dataclasses.replace(EVENT, target=f"/hooks/{INSTANCE}", headers=headers, body=payload)


def live(tmp_path):
    """A real intake over a temporary spool, with the expanded instance: the only path that enqueues."""
    cfg = expanded({INSTANCE: spec(write_secret(tmp_path / "secret"))})
    cfg = C.merge(cfg, {"spool": {"home": str(tmp_path / "home")}, "alerts": {"dry": True},
                        "limits": {"min_free_bytes": 1 << 20}})
    store = Store(str(tmp_path / "spool.db"), cfg, now=lambda: 1_800_000_000.0)
    store.ensure_schema()
    return I.Intake(cfg, store, now=lambda: 1_800_000_000.0)


def post(intake: I.Intake, raw: RawRequest):
    headers = list(raw.headers)
    return intake.handle(raw.method, raw.target, headers, io.BytesIO(raw.body), "127.0.0.1")


def test_an_alert_without_the_secret_header_is_refused_with_401(tmp_path):
    cfg = expanded({INSTANCE: spec(write_secret(tmp_path / "secret"))})
    out = I.source_test(cfg, INSTANCE, variant(drop="cf-webhook-auth"))
    assert out["result"] == "unverified" and out["status"] == 401, out
    assert SENTINEL not in json.dumps(out, default=str)


@pytest.mark.parametrize("body", [
    b'{"text":"Made-up test alert PAYLOAD-SENTINEL-7f3a"}',     # Cloudflare's Save and Test body: text only, no name
    b"not json PAYLOAD-SENTINEL-7f3a",                          # a body that is not JSON at all
], ids=["save-and-test-without-name", "non-json"])
def test_a_body_that_cannot_be_keyed_is_quarantined_with_202(body, tmp_path):
    cfg = expanded({INSTANCE: spec(write_secret(tmp_path / "secret"))})
    out = I.source_test(cfg, INSTANCE, variant(body=body))
    assert out["result"] == "quarantined" and out["status"] == 202, out
    assert SENTINEL not in json.dumps(out, default=str)


def test_a_redelivered_identical_body_is_a_duplicate_and_a_new_alert_is_queued(tmp_path):
    intake = live(tmp_path)
    first = post(intake, variant())
    assert (first.status, json.loads(first.body)["status"]) == (200, "queued")
    again = post(intake, variant())
    assert (again.status, json.loads(again.body)["status"]) == (200, "duplicate")
    later = variant(body=EVENT.body.replace(b'"ts":1800000000', b'"ts":1800000060'))
    assert json.loads(post(intake, later).body)["status"] == "queued"


def test_setup_checks_the_proxy_only_after_the_reload():
    text = doc("SETUP.md")
    reload_at = text.index("`hookspool check`, then `hookspool reload`")
    assert text.index("It must return 401") > reload_at
    assert "after step 2 below" in text[: text.index("## Apply, then create")]


def test_agents_has_a_retries_step_matching_setup_and_consistent_numbering():
    text = doc("AGENTS.md")
    steps = [int(n) for n in re.findall(r"^(\d+)\. \*\*", text, re.M)]
    assert steps == list(range(1, len(steps) + 1))
    assert f"Repeat steps 1 to {len(steps)}" in text
    assert "retry behaviour is known" in text and "ask the\n   operator for a sweep" in text
    assert "## Retries" in doc("SETUP.md")


def test_agents_policy_step_precedes_the_queued_alert_check():
    text = doc("AGENTS.md")
    policy = text.index("adds the destination to a notification policy")
    queued = text.index("an alert is queued")
    retries = text.index("retry behaviour is known")
    assert retries < policy < queued
    assert "before a notification policy uses it (step 6)" in text
    assert "(step 5)" in text and "6. **Outward, human-performed:** the person adds" in text
    assert "7. **Goal:** an alert is queued" in text
