"""The shared source test kit, for the test suite (ARCHITECTURE §14.1, §18).

The kit itself is ``hookspool.testing.sourcekit`` (the bobbin conformance kit runs it too); this
module re-exports it and turns a case that does not apply (``Skip``) into a pytest skip. Test
modules keep using it as before::

    import sourcekit

    @pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
    def test_kit(case, tmp_path, clock):
        sourcekit.run(case, KIT, tmp_path, clock)

Captures (``tests/captures/<source>/*.http``, or ``hookspool/bobbins/<vendor>/<product>/captures/*.http`` for a built-in
bobbin) run through ``run_capture``: the file's
``# expect:`` line is the result ``hookspool source test`` must report.
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

from hookspool import bobbin_loader as hs_bobbins
from hookspool import config as hs_config
from hookspool.testing import sourcekit as _kit
from hookspool.testing.sourcekit import (  # noqa: F401 - re-exported for the test modules
    CASE_IDS,
    CASES,
    PLUGIN_FUNCS,
    SENTINEL,
    Env,
    FakeClock,
    Kit,
    Skip,
    request,
)

CAPTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOBBINS = os.path.join(ROOT, "hookspool", "bobbins")
TEMPLATE_MODULE = "hookspool.sources.template"
# The settings a template bobbin's sample instance is given: the table an operator writes under
# [bobbins.<name>]. bobbin_source_cfg expands them through the loader (14.3.2a), like production does.
SAMPLE_SETTINGS = {"atlassian.trello": {"callback_url": "https://hooks.example.test/hooks/trello"}}
# The source (instance) name each built-in bobbin's source tests use: the name its source had before bobbins were
# namespaced (``[sources.gcal]`` still loads ``hookspool.sources.gcal``), and the docs' instance name otherwise.
BOBBIN_OF = {"gmail": "google.gmail", "gcal": "google.calendar", "drive": "google.drive", "github": "github.repo",
             "trello": "atlassian.trello", "kaneo": "kaneo.projects", "zoom": "zoom.meetings", "plaid": "plaid.items",
             "webhook": "hookspool.webhook"}
SOURCE_OF = {v: k for k, v in BOBBIN_OF.items()}


def bobbin_name(name: str) -> str:
    """``google.gmail`` for ``gmail`` (a source name) or for ``google.gmail`` itself."""
    return name if "." in name else BOBBIN_OF.get(name, name)


def bobbin_dir(name: str) -> str:
    return os.path.join(BOBBINS, *bobbin_name(name).split("."))


def run(case, kit: Kit, tmp_path, clock) -> None:
    """Run one matrix case against a fresh environment, then the log checks."""
    try:
        _kit.run(case, kit, tmp_path, clock)
    except Skip as e:
        pytest.skip(str(e))


# ---- captures --------------------------------------------------------------------------------------------------
def capture_dir(source: str) -> str:
    """Where a source's captures live: its bobbin's ``captures/`` when it is a built-in bobbin (ARCHITECTURE 14.3.1),
    else ``tests/captures/<source>``."""
    own = os.path.join(bobbin_dir(source), "captures")
    return own if os.path.isdir(own) else os.path.join(CAPTURES, source)


def bobbin_manifest(name: str) -> str:
    return os.path.join(bobbin_dir(name), "bobbin.toml")


def bobbin_source_cfg(name: str) -> dict:
    """The [sources.<name>] table a template bobbin expands to for its sample instance, produced by the
    loader's own expansion (14.3.3). Secrets are a placeholder reference; callers drop them."""
    bobbin = bobbin_name(name)
    inst = SOURCE_OF.get(bobbin, bobbin.partition(".")[2])
    spec = {"bobbin": bobbin, "secrets": {"secret": {"file": f"/etc/hookspool/secrets/{inst}"}},
            "outputs": {"*": f"hermes:{inst}"}, "settings": dict(SAMPLE_SETTINGS.get(bobbin, {}))}
    cfg = hs_bobbins.expand(hs_config.merge(hs_config.DEFAULTS, {"bobbins": {inst: spec}}))
    return cfg["sources"][inst]


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
    return f"[sources.{SOURCE_OF.get(bobbin_name(name), name)}]\n{body}\n"


def captures(source: str) -> list[str]:
    d = capture_dir(source)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".http")) if os.path.isdir(d) else []


def run_capture(path: str, kit: Kit, tmp_path, clock) -> dict:
    """``source_test`` on a capture; asserts the ``# expect:`` result. Returns the report."""
    return _kit.run_capture(path, kit, tmp_path, clock)
