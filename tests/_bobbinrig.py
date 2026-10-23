"""A harness that runs a built-in bobbin's processor the way the dispatcher does (``hookspool.processor.run``:
journal, staged state, emits) against a scripted Google (``tests/_googlestub.py``), with real files for the
credentials, the state directory and a hooks module. Test values only."""
from __future__ import annotations

import io
import json
import os
import textwrap

from hookspool import processor as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOBBINS = os.path.join(ROOT, "hookspool", "bobbins")


def write_cred(path, port: int) -> str:
    """An OAuth credential whose token endpoint is the stub (loopback http is allowed to ``ctx.http``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"client_id": "test-client", "client_secret": "test-client-secret",
                                "refresh_token": "test-refresh", "token_uri": f"http://127.0.0.1:{port}/token"}))
    os.chmod(path, 0o600)
    return str(path)


def batch_doc(*events, bid: str = "9", source: str = "inst", key: str = "k") -> dict:
    return {"source": source, "key": key, "batch_id": bid, "delivery_id": f"hs_{source}_{bid}",
            "events": list(events)}


class Rig:
    """One bobbin instance in a temp deployment."""

    def __init__(self, tmp_path, bobbin: str, outputs, settings: dict, secrets: dict[str, str], *,
                 instance: str | None = None, hooks: str | None = None):
        self.tmp = tmp_path
        self.bobbin = bobbin
        self.instance = instance or bobbin
        self.state = tmp_path / "state"
        self.dir = os.path.join(BOBBINS, bobbin)
        if hooks is not None:
            hp = tmp_path / "hooks.py"
            hp.write_text(textwrap.dedent(hooks))
            os.chmod(hp, 0o644)
            settings = dict(settings, hooks=str(hp))
        self.env = {
            "HOOKSPOOL_BOBBIN": bobbin, "HOOKSPOOL_BOBBIN_INSTANCE": self.instance,
            "HOOKSPOOL_BOBBIN_DIR": self.dir, "HOOKSPOOL_STATE_DIR": str(self.state),
            "HOOKSPOOL_SETTINGS": json.dumps(settings),
            "HOOKSPOOL_DELTA_ROUTES": json.dumps({o: f"hermes:{o}" for o in outputs}),
            "HOOKSPOOL_SECRET_REFS": json.dumps({k: {"file": v} for k, v in secrets.items()})}

    def run(self, *events, bid: str = "9", routes: dict | None = None, **kw):
        """(exit code, the enqueue objects printed, the standard error text)."""
        out, err = io.StringIO(), io.StringIO()
        env = dict(self.env, HOOKSPOOL_BATCH_ID=bid, HOOKSPOOL_DELIVERY_ID=f"hs_test_{bid}")
        if routes is not None:
            env["HOOKSPOOL_DELTA_ROUTES"] = json.dumps(routes)
        rc = P.run(self.instance, batch_doc(*events, bid=bid, source=self.instance), env=env, out=out, err=err, **kw)
        return rc, [json.loads(x)["enqueue"] for x in out.getvalue().splitlines()], err.getvalue()

    def items(self, *events, **kw) -> list[dict]:
        """The payloads of a pass that succeeded."""
        rc, emits, err = self.run(*events, **kw)
        assert rc == 0, err
        return [e["payload"] for e in emits]

    def path(self, name: str) -> str:
        return str(self.state / f"{name}.json")

    def get_state(self, name: str):
        with open(self.path(name), encoding="utf-8") as f:
            return json.load(f)

    def seed_state(self, name: str, obj) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.path(name), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def has_state(self, name: str) -> bool:
        return os.path.exists(self.path(name))

    def journal(self, bid: str = "9"):
        return P.load_journal(str(self.state), bid)
