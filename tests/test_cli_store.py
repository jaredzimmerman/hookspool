"""The store-level CLI commands (ARCHITECTURE §16), run in-process through main(argv)."""
import io
import json
import os

import pytest

from conftest import SENTINEL
from hookspool import __main__ as cli
from hookspool.store import Store, acquire_lock


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOOKSPOOL_HOME", str(h))
    monkeypatch.delenv("HOOKSPOOL_CONFIG", raising=False)
    monkeypatch.delenv("HOOKSPOOL_DB", raising=False)
    (h / "config.toml").write_text('[coalesce]\nmax_events = 3\n')
    return h


def run(capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin.encode())))
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_round_trip(home, capsys, monkeypatch):
    assert run(capsys, "status")[0] == 2                                  # no spool yet
    assert not os.path.exists(home / "spool.db")                          # a status probe never creates it
    rc, out, _ = run(capsys, "migrate")
    assert rc == 0 and "applied [1, 2, 3]" in out
    assert run(capsys, "migrate")[1].strip() == "schema is current"
    payload = json.dumps({"n": 1, "note": SENTINEL})
    for i in range(4):
        rc, out, err = run(capsys, "--json", "enqueue", "--source", "sweep", "--key", "k", "--destination",
                           "hermes:x", "--dedup-id", f"d{i}", stdin=payload, monkeypatch=monkeypatch)
        assert rc == 0, err
    r = json.loads(run(capsys, "--json", "enqueue", "--source", "sweep", "--key", "k", "--destination", "hermes:x",
                       "--dedup-id", "d0", stdin=payload, monkeypatch=monkeypatch)[1])
    assert r["status"] == "duplicate"
    rc, out, _ = run(capsys, "list", "--state", "sealed")
    assert rc == 0 and SENTINEL not in out and "hermes:x" in out
    rc, out, _ = run(capsys, "--json", "show", "1", "--events")
    b = json.loads(out)
    assert b["count"] == 3 and len(b["events"]) == 3 and "payload" not in b["events"][0]
    assert SENTINEL not in out
    rc, out, _ = run(capsys, "split", "1", "--max-events", "2")
    assert rc == 0 and "split into [3, 4]" in out
    rc, out, _ = run(capsys, "exclude", "3", "1", "--yes")
    assert rc == 0 and "excluded 1" in out
    assert run(capsys, "drop", "4", "--yes")[0] == 0
    assert run(capsys, "drop", "4", "--yes")[0] == 1
    assert run(capsys, "replay", "3")[0] == 1                             # not dead
    assert run(capsys, "replay", "--dead")[0] == 1                        # nothing to do
    assert run(capsys, "show", "999")[0] == 1
    rc, out, _ = run(capsys, "pause", "hermes:x")
    assert rc == 0 and "paused" in out
    st = json.loads(run(capsys, "--json", "status")[1])
    assert st["destinations"]["hermes:x"]["state"] == "paused" and st["batches"]["dropped"] == 2
    assert run(capsys, "resume", "hermes:x")[0] == 0
    assert run(capsys, "issues")[0] == 0
    rc, out, _ = run(capsys, "--json", "prune")
    assert rc == 0 and json.loads(out)["delivered"] == 0
    rc, out, _ = run(capsys, "export")
    lines = [json.loads(x) for x in out.splitlines()]
    assert [x["id"] for x in lines] == [1, 2, 3, 4]


def test_cli_check_and_lock(home, capsys):
    assert run(capsys, "check")[0] == 0
    (home / "config.toml").write_text('[limits]\nmax_pending_batches = 5\n')
    rc, _, err = run(capsys, "check")
    assert rc == 2 and "removed in v2" in err
    (home / "config.toml").write_text('[spool\n')
    assert run(capsys, "status")[0] == 2
    (home / "config.toml").write_text("")
    lock = acquire_lock(str(home / "dispatch.lock"))
    try:
        assert run(capsys, "migrate")[0] == 3
    finally:
        lock.close()
    assert run(capsys, "intake")[0] == 2                                  # services arrive with T2/T3


def test_cli_refuses_newer_schema(home, capsys):
    assert run(capsys, "migrate")[0] == 0
    st = Store(str(home / "spool.db"), {"coalesce": {}})
    st._conn().execute("PRAGMA user_version=7")
    st.close()
    rc, _, err = run(capsys, "status")
    assert rc == 2 and "newer" in err
    rc, _, err = run(capsys, "check")
    assert rc == 2 and "newer" in err
