"""``ctx.pace`` (the ``Pacer`` class in the net module) and the XML-declaration charset of ``Http.get_text`` (ARCHITECTURE 14.4)."""
from __future__ import annotations

import pytest

from hookspool import net
from hookspool import processor as P


class Clock:
    """A fake monotonic clock whose sleep advances it."""

    def __init__(self, t=100.0):
        self.t = t
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_the_first_wait_returns_at_once_and_later_ones_space_requests_by_the_interval():
    c = Clock()
    p = net.Pacer(5, clock=c.now, sleep=c.sleep)
    starts = []
    for _ in range(4):
        p.wait()
        starts.append(c.t)
    assert c.slept == pytest.approx([0.2, 0.2, 0.2]) and starts == pytest.approx([100.0, 100.2, 100.4, 100.6])


def test_time_spent_on_the_request_counts_towards_the_interval():
    c = Clock()
    p = net.Pacer(10, clock=c.now, sleep=c.sleep)
    p.wait()
    c.t += 0.04                                    # the request took 40 ms
    assert p.wait() == pytest.approx(0.06) and c.slept == pytest.approx([0.06])
    c.t += 5.0                                     # a slow one: no wait, and no credit for the idle time
    assert p.wait() == 0.0
    assert p.wait() == pytest.approx(0.1)


def test_the_rate_never_exceeds_the_limit_over_a_window():
    c = Clock()
    p = net.Pacer(10, clock=c.now, sleep=c.sleep)
    starts = []
    for _ in range(50):
        p.wait()
        starts.append(c.t)
        c.t += 0.01
    assert all(b - a >= 0.1 - 1e-9 for a, b in zip(starts, starts[1:]))
    assert sum(1 for s in starts if s < starts[0] + 0.999) <= 10


@pytest.mark.parametrize("bad", [0, -1, 0.05, float("nan"), True, "5", None])
def test_a_bad_rate_is_permanent(bad):
    with pytest.raises(net.Permanent):
        net.Pacer(bad)


def test_make_context_extras_builds_a_pacer_with_the_given_sleep():
    slept = []
    x = net.make_context_extras({}, lambda n: "", sleep=slept.append)
    p = x["pace"](1000)
    p.wait()
    p.wait()
    assert isinstance(p, net.Pacer) and len(slept) <= 1


def test_the_context_has_pace_with_and_without_an_extras_factory():
    slept = []
    ctx = P.Context(instance="t", extras_factory=lambda st, r: net.make_context_extras(st, r, sleep=slept.append))
    p = ctx.pace(1000)
    p.wait()
    p.wait()
    assert isinstance(p, net.Pacer)
    bare = P.Context(instance="t", extras_factory=lambda st, r: {"http": None})      # a custom factory without pace
    assert isinstance(bare.pace(10), net.Pacer)
    assert isinstance(P.Context(instance="t").pace(10), net.Pacer)


# ---- get_text charset -------------------------------------------------------------------------------------
def test_body_charset_prefers_the_header_then_the_xml_declaration_then_utf8():
    decl = b'<?xml version="1.0" encoding="ISO-8859-1" ?><a/>'
    assert net._body_charset("application/atom+xml", decl) == "iso8859-1"
    assert net._body_charset("application/atom+xml; charset=utf-8", decl) == "utf-8"
    assert net._body_charset("text/plain", b"hello") == "utf-8"
    assert net._body_charset("", b'<?xml version="1.0" encoding="no-such-codec"?>') == "utf-8"
    assert net._body_charset("", b"  <?xml encoding='UTF-16'?>") == "utf-16"
    assert net._body_charset("", b"<a>" + b"x" * 400 + b'<?xml encoding="latin-1"?>') == "utf-8"
