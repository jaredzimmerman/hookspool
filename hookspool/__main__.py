"""hookspool CLI (ARCHITECTURE §16): ``hookspool [--home DIR] [--config FILE] [--json] <command>``.

Exit codes: 0 ok, 1 not found / nothing to do, 2 config or schema error, 3 lock held.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib

from . import config as _config
from .store import STATES, InvalidEvent, LockHeld, SchemaError, SpoolError, Store, acquire_lock

EXIT_OK, EXIT_NOTHING, EXIT_CONFIG, EXIT_LOCK = 0, 1, 2, 3


def _fmt_ts(t: float | None) -> str:
    return "-" if t is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _err(msg: str) -> None:
    print(f"hookspool: {msg}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hookspool", description="durable, coalescing webhook intake")
    ap.add_argument("--home", help="state directory (default: $HOOKSPOOL_HOME or /var/lib/hookspool)")
    ap.add_argument("--config", help="config.toml (default: $HOOKSPOOL_CONFIG or <home>/config.toml)")
    ap.add_argument("--db", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, h in (("intake", "run the intake HTTP server"), ("dispatch", "run the dispatcher"),
                    ("run", "intake and dispatcher in one process")):
        sub.add_parser(name, help=h)
    sub.add_parser("status", help="spool, dispatcher, destinations and sources")
    p = sub.add_parser("list", help="batches, newest first, no payloads")
    p.add_argument("--state", choices=STATES)
    p.add_argument("--source")
    p.add_argument("--destination")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("show", help="one batch")
    p.add_argument("batch", type=int)
    p.add_argument("--events", action="store_true", help="event ids, types, sizes, dedup ids, exclusion")
    p.add_argument("--payload", action="store_true", help="include payloads (may contain personal data)")
    p.add_argument("--attempts", action="store_true")
    p = sub.add_parser("replay", help="dead -> retry, due now (quarantine: reclassify)")
    p.add_argument("batches", type=int, nargs="*")
    p.add_argument("--dead", action="store_true", help="every dead batch matching the filters")
    p.add_argument("--destination")
    p.add_argument("--source")
    p.add_argument("--since", type=float, help="unix seconds (first_at)")
    p = sub.add_parser("retry", help="retry -> due now")
    p.add_argument("batch", type=int)
    p = sub.add_parser("split", help="cut a sealed/retry/dead batch into smaller batches")
    p.add_argument("batch", type=int)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--max-events", type=int)
    g.add_argument("--max-bytes", type=int)
    g.add_argument("--at", type=int, metavar="EVENT_ID")
    p = sub.add_parser("exclude", help="omit events from a batch's document")
    p.add_argument("batch", type=int)
    p.add_argument("event_ids", type=int, nargs="+")
    p.add_argument("--yes", action="store_true", required=True)
    p = sub.add_parser("drop", help="-> dropped (never automatic)")
    p.add_argument("batches", type=int, nargs="+")
    p.add_argument("--yes", action="store_true", required=True)
    p.add_argument("--reason", default="")
    p = sub.add_parser("pause", help="operator gate for a destination")
    p.add_argument("destination")
    p.add_argument("--reason", default="")
    p = sub.add_parser("resume", help="lift the operator gate")
    p.add_argument("destination")
    p = sub.add_parser("issues", help="list | ack | resolve | ignore <id>")
    p.add_argument("action", nargs="?", default="list", choices=("list", "ack", "resolve", "ignore"))
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--state")
    p = sub.add_parser("enqueue", help="manual or sweep injection, same validation as the intake")
    p.add_argument("--source", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--destination")
    p.add_argument("--route")
    p.add_argument("--event-type", default="")
    p.add_argument("--dedup-id")
    p.add_argument("--file", help="JSON payload file (default: stdin)")
    p = sub.add_parser("source", help="source tools")
    p.add_argument("subcmd", choices=("test",))
    p.add_argument("name")
    p.add_argument("--request", required=True, help=".http capture (start line, headers, blank line, body)")
    p.add_argument("--now", type=float, help="unix seconds to verify at (default: the capture's '# now:' or now)")
    p = sub.add_parser("bobbin", help="bobbins: list | show <bobbin> | expand [<instance>] | new <name> | check <dir>")
    p.add_argument("subcmd", choices=("list", "show", "expand", "new", "check"))
    p.add_argument("name", nargs="?", help="the bobbin, <vendor>.<product> (show), the instance (expand), the new"
                                           " bobbin's <vendor>.<product> name (new) or the bobbin directory (check)")
    p.add_argument("--dir", default=".", help="new: the parent directory; the bobbin goes in <dir>/<vendor>/<product>/"
                                              " (default: the current directory)")
    p.add_argument("--code", action="store_true", help="new: a source.py bobbin instead of a copy of the hookspool.webhook bobbin")
    p.add_argument("--template", action="store_true", help=argparse.SUPPRESS)       # the default now; kept for old scripts
    p.add_argument("--set", dest="set_", action="append", default=[], metavar="NAME=VALUE",
                   help="new: freeze a webhook setting (scheme, header, prefix, key, ...) into the template;"
                        " the value is TOML (--set 'key=[\"id\"]') or a bare string")
    p.add_argument("--processor", action="store_true", help="new: include a processor.py")
    p.add_argument("--markdown", action="store_true",
                   help="list: the catalogue table of docs/bobbins/README.md, generated from the manifests")
    p.add_argument("--report", choices=("text", "md", "json"), default="text",
                   help="check: the scorecard format (default text)")
    p.add_argument("--strict", action="store_true", help="check: advisory checks fail too")
    p.add_argument("--base", help="check: the git ref the change is compared with (for the scope check)")
    p.add_argument("--online", dest="fetch_links", action="store_true",
                   help="check: also fetch the [provider] pages (the advisory provider-links row; CI passes it)")
    sub.add_parser("check", help="validate config, secrets, plugins and the spool")
    sub.add_parser("reload", help="check, then touch the config so both processes reload")
    p = sub.add_parser("migrate", help="apply pending schema migrations")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("prune", help="run retention now")
    p.add_argument("--vacuum", action="store_true")
    p = sub.add_parser("export", help="batches + events + attempts as JSONL")
    p.add_argument("--state", choices=STATES)
    p.add_argument("--since", type=float)
    sub.add_parser("metrics", help="print the /metrics text")
    return ap


def _out(a, obj, text: str | None = None) -> None:
    if a.json or text is None:
        print(json.dumps(obj, indent=1, sort_keys=True, default=str))
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if a.cmd == "bobbin" and a.subcmd == "check":
        if not a.name:
            _err("bobbin check: give the bobbin directory")
            return EXIT_NOTHING
        from .testing import bobbinkit       # needs no config: it checks one directory
        return bobbinkit.main(a.name, report=a.report, strict=a.strict, base=a.base, online=a.fetch_links)
    try:
        cfg_path = _config.config_path(a.config, a.home)
        cfg = _config.load_config(cfg_path, cli_home=a.home, expand=a.cmd != "bobbin")
    except _config.ConfigError as e:
        errors = getattr(e, "errors", None)
        if errors:
            for m in errors:
                print(f"error: {m}", file=sys.stderr)
        else:
            _err(str(e))
        return EXIT_CONFIG
    if a.cmd == "bobbin":
        return _bobbin(a, cfg)
    home = _config.home_dir(cfg, a.home)
    db = _config.db_path(cfg, a.db, a.home)

    if a.cmd in ("intake", "dispatch", "run", "metrics") or (a.cmd == "source"):
        mod = {"intake": "intake", "dispatch": "dispatch", "run": "dispatch", "metrics": "intake",
               "source": "intake"}[a.cmd]
        try:
            m = __import__(f"hookspool.{mod}", fromlist=["cli"])
            entry = getattr(m, "cli")
        except (ImportError, AttributeError):
            _err(f"`{a.cmd}` is not available in this build")
            return EXIT_CONFIG
        return int(entry(a, cfg) or 0)

    if a.cmd in ("check", "reload"):
        rep = _config.check(cfg)
        if os.path.exists(db):
            try:
                st = Store(db, cfg)
                st.check_schema()
                if not st.quick_check():
                    rep.err(f"{db}: quick_check failed")
            except SchemaError as e:
                rep.err(str(e))
        for w in rep.warnings:
            print(f"warning: {w}", file=sys.stderr)
        for e in rep.errors:
            print(f"error: {e}", file=sys.stderr)
        if not rep.ok:
            return EXIT_CONFIG
        if a.cmd == "reload" and cfg.get("_path"):
            os.utime(cfg["_path"])
        print("ok" if a.cmd == "check" else "ok: config touched")
        return EXIT_OK

    st = Store(db, cfg)
    if a.cmd == "migrate":
        try:
            lock = acquire_lock(os.path.join(home, "dispatch.lock"))
        except LockHeld as e:
            _err(f"{e}; stop the dispatcher first")
            return EXIT_LOCK
        try:
            if a.dry_run:
                v = st.schema_version() if os.path.exists(db) else 0
                from .store import SCHEMA_VERSION, migrations
                pending = [n for n, _ in migrations() if n > v]
                print(f"schema {v}; pending {pending or 'none'} (target {SCHEMA_VERSION})")
                return EXIT_OK
            applied = st.ensure_schema()
            print(f"applied {applied}" if applied else "schema is current")
            return EXIT_OK
        except SchemaError as e:
            _err(str(e))
            return EXIT_CONFIG
        finally:
            lock.close()
    try:
        st.check_schema()
    except SchemaError as e:
        _err(str(e))
        return EXIT_CONFIG

    try:
        return _run(a, st, cfg)
    except (InvalidEvent, ValueError) as e:
        _err(str(e))
        return EXIT_NOTHING
    except SpoolError as e:
        _err(str(e))
        return EXIT_CONFIG


def _set_args(items: list[str]) -> dict | None:
    """``--set NAME=VALUE`` pairs: the value is a TOML value, else the bare string. A template key that takes
    only a string keeps the text as written (``prefix=1`` is the string ``1``, not an integer)."""
    from .bobbin_loader import TEMPLATE_KEYS
    out: dict = {}
    for item in items:
        k, eq, raw = item.partition("=")
        k = k.strip()
        if not eq or not k:
            raise ValueError(f"--set {item!r}: expected NAME=VALUE")
        try:
            v = tomllib.loads("v = " + raw)["v"]
        except tomllib.TOMLDecodeError:
            v = raw
        out[k] = raw if TEMPLATE_KEYS.get(k) == ("string",) and not isinstance(v, str) else v
    return out or None


def _bobbin(a, cfg: dict) -> int:
    """``hookspool bobbin list | show | expand | new`` (ARCHITECTURE §14.3.5). ``cfg`` is the
    file as written (not expanded)."""
    from . import bobbin_loader
    if a.subcmd in ("show", "new") and not a.name:
        _err(f"bobbin {a.subcmd}: give a {'bobbin' if a.subcmd == 'show' else 'name'}")
        return EXIT_NOTHING
    if a.subcmd == "new":
        if a.template and a.code:
            _err("--template is the default; it cannot be combined with --code")
            return EXIT_CONFIG
        try:
            d = bobbin_loader.scaffold(a.name, a.dir, code=a.code, processor=a.processor,
                                       settings=_set_args(a.set_))
        except ValueError as e:
            _err(str(e))
            return EXIT_CONFIG
        except FileExistsError as e:
            _err(f"{e.args[0] if e.args else os.path.abspath(a.dir)} already exists")
            return EXIT_NOTHING
        print(f"created {d}")
        return EXIT_OK
    if a.subcmd == "expand":
        try:
            ex = bobbin_loader.expand(cfg, cli_home=a.home)
        except bobbin_loader.BobbinError as e:
            for m in e.errors:
                print(f"error: {m}", file=sys.stderr)
            return EXIT_CONFIG
        if a.name is not None and a.name not in ex["_bobbins"]:
            _err(f"no [bobbins.{a.name}] in the config")
            return EXIT_NOTHING
        if not ex["_bobbins"]:
            _err("no [bobbins] in the config")
            return EXIT_NOTHING
        sys.stdout.write(bobbin_loader.render(ex, a.name))
        return EXIT_OK
    pkgs, errs = bobbin_loader.discover(cfg)
    for m in errs:
        print(f"warning: {m}", file=sys.stderr)
    services = cfg.get("bobbins") if isinstance(cfg.get("bobbins"), dict) else {}
    users: dict[str, list[str]] = {}
    for inst, spec in services.items():
        if isinstance(spec, dict) and "bobbin" in spec:
            users.setdefault(str(spec["bobbin"]), []).append(inst)
    if a.subcmd == "list" and a.markdown:
        sys.stdout.write(bobbin_loader.catalogue_markdown(pkgs.values()))
        return EXIT_OK if pkgs else EXIT_NOTHING
    if a.subcmd == "list":
        rows = [{"name": p.name, "version": p.version, "origin": p.origin, "summary": p.summary,
                 "instances": users.get(p.name, []), "valid": not p.errors} for p in pkgs.values()]
        lines = [f"{r['name']:<20} {r['version']:<8} {r['origin']:<10} {r['summary']}"
                 + (f"  [instances: {', '.join(r['instances'])}]" if r["instances"] else "")
                 + ("" if r["valid"] else "  (invalid)") for r in rows]
        _out(a, rows, "\n".join(lines))
        return EXIT_OK if rows else EXIT_NOTHING
    p = pkgs.get(a.name)
    if p is None:
        _err(f"no bobbin {a.name!r} (see `hookspool bobbin list`)")
        return EXIT_NOTHING
    src = p.source
    info = {"name": p.name, "version": p.version, "origin": p.origin, "dir": p.dir, "summary": p.summary,
            "provider": {k: v for k, v in p.provider.items() if isinstance(v, str)},
            "source": "template" if "template" in src else src.get("module"),
            "span": src.get("span"), "sender_retries": src.get("sender_retries"),
            "expect_every_s": src.get("expect_every_s"), "proxy": src.get("proxy"),
            "processor": p.processor, "instances": users.get(p.name, []), "errors": p.errors,
            "secrets": {k: {"description": v.get("description", ""), "processor": v.get("processor") is True}
                        for k, v in p.secrets.items() if isinstance(v, dict)},
            "settings": {k: {"type": v.get("type"), "required": v.get("required") is True,
                             "default": v.get("default"), "description": v.get("description", "")}
                         for k, v in p.settings.items() if isinstance(v, dict)},
            "outputs": {k: (v.get("description", "") if isinstance(v, dict) else "") for k, v in p.outputs.items()}}
    lines = [f"{p.name} {p.version} ({p.origin}: {p.dir})", f"  {p.summary}",
             f"  source: {info['source']}  span: {info['span'] or '-'}  sender_retries: {info['sender_retries']}"
             f"  expect_every_s: {info['expect_every_s'] or '-'}"]
    prov = info["provider"]
    if prov:
        lines.append(f"  provider: {prov.get('vendor', '-')} {prov.get('product', '-')}")
        lines += [f"    {k}: {prov[k]}" for k in bobbin_loader.PROVIDER_URL_KEYS if prov.get(k)]
    if info["proxy"]:
        lines.append(f"  proxy: {info['proxy']}")
    if p.processor is not None:
        lines.append(f"  processor: timeout_s={p.processor.get('timeout_s', bobbin_loader.DEFAULT_PROCESSOR_TIMEOUT_S)}"
                     f" state={p.processor.get('state') is True}")
    lines.append("  secrets:")
    lines += [f"    {k}{' (processor)' if v['processor'] else ''}: {v['description']}" for k, v in info["secrets"].items()]
    lines.append("  settings:")
    lines += [f"    {k}: {v['type']}" + (" required" if v["required"] else f" default={json.dumps(v['default'])}")
              + (f"  {v['description']}" if v["description"] else "") for k, v in info["settings"].items()]
    lines.append("  outputs:")
    lines += [f"    {k}: {v}" for k, v in info["outputs"].items()]
    lines += [f"  error: {e}" for e in p.errors]
    _out(a, info, "\n".join(lines))
    return EXIT_OK


def _run(a, st: Store, cfg: dict) -> int:
    if a.cmd == "status":
        _out(a, st.status())
        return EXIT_OK
    if a.cmd == "list":
        rows = st.list(a.state, source=a.source, destination=a.destination, limit=a.limit)
        lines = [f"{r['id']:>7} {r['state']:<9} {r['destination']:<26} n={r['count']:<4} att={r['attempts']:<3}"
                 f" first={_fmt_ts(r['first_at'])} next={_fmt_ts(r['next_attempt_at'])} key={r['key']}"
                 + (f" err={r['last_error'][:80]}" if r["last_error"] else "") for r in rows]
        _out(a, rows, "\n".join(lines))
        return EXIT_OK if rows else EXIT_NOTHING
    if a.cmd == "show":
        b = st.show(a.batch, events=a.events, payload=a.payload, attempts=a.attempts)
        if b is None:
            _err(f"batch {a.batch}: not found")
            return EXIT_NOTHING
        if a.payload and sys.stdout.isatty():
            print("warning: payloads may contain personal data or secrets", file=sys.stderr)
        _out(a, b)
        return EXIT_OK
    if a.cmd == "replay":
        if a.dead:
            ids = st.replay_dead(destination=a.destination, source=a.source, since=a.since)
            print(f"replayed {len(ids)} batch(es): {ids} (they deliver ahead of newer batches of their keys)")
            return EXIT_OK if ids else EXIT_NOTHING
        if not a.batches:
            _err("give batch ids or --dead")
            return EXIT_NOTHING
        rc = EXIT_OK
        for bid in a.batches:
            b = st.batch(bid)
            if b is not None and b["destination"] == "quarantine":
                from .intake import reclassifier
                try:
                    res = st.reclassify_quarantine(bid, reclassifier(cfg))
                except Exception as e:  # noqa: BLE001 - the plugin still fails: nothing changed
                    _err(f"batch {bid}: reclassify failed ({e.__class__.__name__}); the batch is unchanged")
                    rc = EXIT_NOTHING
                    continue
                print(f"batch {bid}: reclassified into {len(res)} event(s)")
                continue
            if st.replay(bid):
                print(f"batch {bid}: dead -> retry, due now")
            else:
                _err(f"batch {bid}: " + ("not found" if b is None else f"state is {b['state']}, not dead"))
                rc = EXIT_NOTHING
        return rc
    if a.cmd == "retry":
        if st.retry_now(a.batch):
            print(f"batch {a.batch}: due now")
            return EXIT_OK
        _err(f"batch {a.batch}: not in retry")
        return EXIT_NOTHING
    if a.cmd == "split":
        ids = st.split(a.batch, max_events=a.max_events, max_bytes=a.max_bytes, at=a.at)
        print(f"batch {a.batch}: split into {ids}")
        return EXIT_OK
    if a.cmd == "exclude":
        n = st.exclude(a.batch, a.event_ids)
        print(f"batch {a.batch}: excluded {n} event(s)")
        return EXIT_OK if n else EXIT_NOTHING
    if a.cmd == "drop":
        rc = EXIT_OK
        for bid in a.batches:
            if st.drop(bid, reason=a.reason):
                print(f"batch {bid}: dropped")
            else:
                _err(f"batch {bid}: cannot drop (missing, leased, delivered or already dropped)")
                rc = EXIT_NOTHING
        return rc
    if a.cmd == "pause":
        st.destination_set(a.destination, state="paused", paused_at=time.time(), paused_by=a.reason or "operator")
        print(f"{a.destination}: paused (events keep spooling)")
        return EXIT_OK
    if a.cmd == "resume":
        st.destination_set(a.destination, state="closed", consecutive_failures=0, opened_at=None,
                           cooldown_s=None, probe_until=None, paused_at=None, paused_by=None)
        print(f"{a.destination}: resumed")
        return EXIT_OK
    if a.cmd == "issues":
        if a.action == "list":
            rows = st.issue_list(a.state)
            _out(a, rows, "\n".join(f"{i['id']:>5} {i['state']:<8} {i['fingerprint']} x{i['count']}"
                                     f" last={_fmt_ts(i['last_at'])}" for i in rows))
            return EXIT_OK
        if a.id is None:
            _err("give an issue id")
            return EXIT_NOTHING
        ok = {"ack": st.issue_ack, "resolve": st.issue_resolve, "ignore": st.issue_ignore}[a.action](a.id)
        print(f"issue {a.id}: {a.action} " + ("done" if ok else "not applicable"))
        return EXIT_OK if ok else EXIT_NOTHING
    if a.cmd == "enqueue":
        raw = open(a.file, "rb").read() if a.file else sys.stdin.buffer.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            _err("payload is not JSON")
            return EXIT_NOTHING
        dest = a.destination or (_config.route_destination(cfg, a.route) if a.route else None) \
            or _config.source_cfg(cfg, a.source).get("action")
        if not dest:
            _err("no destination: give --destination, a configured --route, or a source with an action")
            return EXIT_NOTHING
        r = st.enqueue(a.source, a.key, dest, payload, route=a.route, event_type=a.event_type,
                       dedup_id=a.dedup_id)
        status = "replayed" if r.replayed else "duplicate" if r.duplicate else "queued"
        _out(a, {"status": status, "event_id": r.event_id, "batch_id": r.batch_id},
             f"{status}: event {r.event_id} batch {r.batch_id}")
        return EXIT_OK
    if a.cmd == "prune":
        out = st.prune()
        if a.vacuum:
            st.vacuum()
        _out(a, out, " ".join(f"{k}={v}" for k, v in out.items()))
        return EXIT_OK
    if a.cmd == "export":
        for b in st.export(state=a.state, since=a.since):
            sys.stdout.write(json.dumps(b, separators=(",", ":"), ensure_ascii=False, default=str) + "\n")
        return EXIT_OK
    _err(f"unknown command {a.cmd}")
    return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
