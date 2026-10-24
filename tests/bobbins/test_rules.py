"""ARCHITECTURE 14.3.7 for the ``gmail`` and ``gcal`` bobbins: they call core and never re-implement it. The
walk lives in ``tests/_bobbinrules.py``; here it is checked against known-bad code first, so a pass means
something."""
import os
import textwrap

import pytest

import _bobbinrules as R

BOBBINS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "hookspool", "bobbins")


@pytest.mark.parametrize("bobbin", ["google.gmail", "google.calendar", "google.drive", "google.meet"])
def test_the_bobbin_calls_core_and_never_re_implements_it(bobbin):
    assert R.violations(os.path.join(BOBBINS, *bobbin.split("."))) == []


@pytest.mark.parametrize("code,expect", [
    ("import hmac", "imports hmac"),
    ("import urllib.request", "imports urllib.request"),
    ("from urllib.parse import quote", "imports from urllib.parse"),
    ("import subprocess", "imports subprocess"),
    ("from http import client", "imports from http"),
    ("import logging", "imports logging"),
    ("import importlib", "imports importlib"),
    ("import sqlite3, tempfile", "imports sqlite3"),
    ("open('x')", "calls open()"),
    ("print('x')", "calls print()"),
    ("eval('1')", "calls eval()"),
    ("__import__('os')", "calls __import__()"),
    ("import os\nos.system('x')", "calls os.system()"),
    ("import os\nos.getenv('X')", "calls os.getenv()"),
    ("import os\nx = os.environ['X']", "uses os.environ"),
    ("from os import system", "something other than os.path"),
    ("import sys\nsys.path.insert(0, 'x')", "uses sys.path"),
    ("import time\ntime.sleep(1)", "calls time.sleep()"),
    ("p.write_text('x')", "calls .write_text()"),
    ("p.unlink()", "calls .unlink()"),
    ("p.open()", "calls .open()"),
    ("import hookspool.bobbins.google.gmail.source", "a bobbin never imports a bobbin"),
    ("from hookspool.bobbins.google.gmail import source", "a bobbin never imports a bobbin"),
    ("from hookspool import bobbins", "imports hookspool.bobbins"),
    ("from . import source", "relative import"),
])
def test_the_walk_flags_what_the_contract_bans(code, expect):
    got = R.violations_in(textwrap.dedent(code))
    assert any(expect in g for g in got), got


def test_the_walk_allows_what_a_bobbin_may_use():
    ok = """
    import json
    import re
    from datetime import datetime
    from os.path import exists
    import os.path
    from zoneinfo import ZoneInfo
    from hookspool import net
    from hookspool.providers import google
    from hookspool.plugin import Request, verify as v

    def f(ctx):
        ctx.log("x")
        return exists("/") or os.path.join("a", "b")
    """
    assert R.violations_in(textwrap.dedent(ok)) == []
