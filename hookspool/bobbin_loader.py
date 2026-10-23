"""Bobbins (ARCHITECTURE §14.3): discovery, manifests, and expansion into ordinary tables.

A bobbin is one directory with a ``bobbin.toml`` manifest, an optional ``source.py``
and ``processor.py``, captures and a README. Built-in bobbins live under ``hookspool/bobbins/``;
``[spool].path`` adds directories searched after them. A ``[bobbins.<instance>]`` table
enables a bobbin; ``expand`` turns every instance into a ``[sources.<instance>]`` table, a
``[destinations."script:<instance>"]`` table when the bobbin has a processor, and ``[[routes]]``
for a multi-output bobbin without one. ``load_config`` calls ``expand``, so the rest of
hookspool only ever sees ordinary tables.

Nothing here reads a secret: expansion copies secret references, never values.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import config as _config

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
INSTANCE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_NAME_RE = _config.ENV_NAME_RE
SETTING_TYPES = ("string", "int", "bool", "list", "path")
ALL_OUTPUTS = "*"
MANIFEST = "bobbin.toml"
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bobbins")
BUILTIN = "built-in"
BUILTIN_MODULE = "hookspool.bobbins"
PATH_MODULE_PREFIX = "_hookspool_bobbins."       # private names for code loaded by file path
KIT_MODULE_PREFIX = "_hookspool_bobbinkit."      # private names for code the conformance kit loads
TEMPLATE_MODULE = "hookspool.sources.template"
PROCESSOR_MODULE = "hookspool.processor"
DEFAULT_PROCESSOR_TIMEOUT_S = 300

MANIFEST_KEYS = {"name", "version", "summary", "source", "secrets", "settings", "processor", "outputs"}
SOURCE_KEYS = {"module", "template", "span", "sender_retries", "expect_every_s", "proxy"}
SECRET_KEYS = {"description", "processor"}
SETTING_KEYS = {"description", "type", "required", "default"}
PROCESSOR_KEYS = {"module", "timeout_s", "state"}
OUTPUT_KEYS = {"description"}
# window, liveness, dedup and body overrides copied onto the expanded source table
SOURCE_OVERRIDES = ("span", *_config.WINDOW_FIELDS, "expect_every_s", "dedup_window_s", "max_body_bytes")
PROCESSOR_OVERRIDES = ("timeout_s", "state_dir")
INSTANCE_KEYS = {"bobbin", "secrets", "settings", "outputs", "expect", *SOURCE_OVERRIDES, *PROCESSOR_OVERRIDES}
# keys of a manifest's source template that the instance or the manifest's own [source] sets
_TEMPLATE_RESERVED = {"module", "action", "secrets", "span", "expect_every_s", "dedup_window_s", "max_body_bytes",
                      *_config.WINDOW_FIELDS}
HOOKS_SETTING = "hooks"
ENV_SECRET_REFS = "HOOKSPOOL_SECRET_REFS"


class BobbinError(_config.ConfigError):
    """One or more ``[bobbins]`` / ``[spool]`` problems; ``errors`` lists every one."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


# ---- manifests -------------------------------------------------------------------------------------------
@dataclass
class Package:
    name: str
    dir: str
    origin: str                                   # "built-in" or the spool.bobbin_path directory
    manifest: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def builtin(self) -> bool:
        return self.origin == BUILTIN

    @property
    def module_name(self) -> str:
        return self.name.replace("-", "_")

    @property
    def version(self) -> str:
        return str(self.manifest.get("version", ""))

    @property
    def summary(self) -> str:
        return str(self.manifest.get("summary", ""))

    @property
    def source(self) -> dict:
        s = self.manifest.get("source")
        return s if isinstance(s, dict) else {}

    @property
    def secrets(self) -> dict:
        s = self.manifest.get("secrets")
        return s if isinstance(s, dict) else {}

    @property
    def settings(self) -> dict:
        s = self.manifest.get("settings")
        return s if isinstance(s, dict) else {}

    @property
    def processor(self) -> dict | None:
        p = self.manifest.get("processor")
        return p if isinstance(p, dict) else None

    @property
    def outputs(self) -> dict:
        o = self.manifest.get("outputs")
        return o if isinstance(o, dict) else {}

    def local_file(self, module: str) -> str:
        return os.path.join(self.dir, f"{module}.py")

    def code_module(self, module: str) -> str:
        """The import name of a module of this bobbin: ``hookspool.bobbins.<pkg>.<mod>`` for a
        built-in, a private name loaded by file path for a bobbin under ``spool.bobbin_path``."""
        if self.builtin:
            return f"{BUILTIN_MODULE}.{os.path.basename(self.dir)}.{module}"
        return f"{PATH_MODULE_PREFIX}{self.module_name}.{module}"

    def code_files(self) -> list[str]:
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return []
        return [os.path.join(self.dir, n) for n in names if n.endswith(".py") or n == MANIFEST]


def _is_local_module(v: str) -> bool:
    return "." not in v


def names_a_bobbin(module: str) -> bool:
    """True when a dotted module name is a bobbin's code: the built-in package, or the private names the
    runtime and the conformance kit give code loaded by file path (§14.3.1: a bobbin never imports another)."""
    return module == BUILTIN_MODULE or module.startswith((BUILTIN_MODULE + ".", PATH_MODULE_PREFIX, KIT_MODULE_PREFIX))


def setting_type_ok(t: str, v: Any) -> bool:
    if t == "string":
        return isinstance(v, str)
    if t == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "bool":
        return isinstance(v, bool)
    if t == "list":
        return isinstance(v, list)
    if t == "path":
        return isinstance(v, str) and (v == "" or os.path.isabs(v))
    return False


def _type_hint(t: str) -> str:
    return {"string": "a string", "int": "an integer", "bool": "true or false", "list": "a list",
            "path": "an absolute path (or \"\" for none)"}.get(t, t)


def validate_manifest(m: Any, dirname: str, spans: dict) -> list[str]:
    """Every problem with one manifest (ARCHITECTURE §14.3.2), as messages without a prefix."""
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["must be a table"]

    def unknown(where: str, table: Any, allowed: set[str]) -> bool:
        if not isinstance(table, dict):
            errs.append(f"{where}: must be a table")
            return False
        for k in table:
            if k not in allowed:
                errs.append(f"{where + '.' if where else ''}{k}: unknown key")
        return True

    unknown("", m, MANIFEST_KEYS)
    name = m.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errs.append(f"name: {name!r} must match {NAME_RE.pattern}")
    elif dirname not in (name, name.replace("-", "_")):
        errs.append(f"name: {name!r} does not match its directory {dirname!r}")
    for k in ("version", "summary"):
        if k in m and not isinstance(m[k], str):
            errs.append(f"{k}: must be a string")
    if "version" not in m:
        errs.append("version: required")

    src = m.get("source")
    if src is None:
        errs.append("source: required")
    elif unknown("source", src, SOURCE_KEYS):
        has_mod, has_tpl = "module" in src, "template" in src
        if has_mod == has_tpl:
            errs.append("source: exactly one of module or template is required")
        if has_mod and not (isinstance(src["module"], str) and src["module"]):
            errs.append("source.module: must be a module name")
        elif has_mod and names_a_bobbin(src["module"]):
            errs.append(f"source.module: {src['module']!r} is another bobbin's code; a bobbin never imports or"
                        " runs another bobbin (name this bobbin's own source.py, or a core module)")
        if has_tpl:
            t = src["template"]
            if not isinstance(t, dict):
                errs.append("source.template: must be a table of template source keys")
            else:
                for k in sorted(set(t) & _TEMPLATE_RESERVED):
                    errs.append(f"source.template.{k}: set by the instance or by [source], not the template")
        if "span" in src and src["span"] not in spans:
            errs.append(f"source.span: {src['span']!r} is not a span preset")
        if "sender_retries" in src and not isinstance(src["sender_retries"], bool):
            errs.append("source.sender_retries: must be true or false")
        e = src.get("expect_every_s")
        if e is not None and (isinstance(e, bool) or not isinstance(e, (int, float)) or e <= 0):
            errs.append("source.expect_every_s: must be a number > 0")
        if "proxy" in src and not isinstance(src["proxy"], str):
            errs.append("source.proxy: must be a string")

    secrets = m.get("secrets", {})
    if unknown("secrets", secrets, set(secrets) if isinstance(secrets, dict) else set()):
        for n, s in secrets.items():
            where = f"secrets.{n}"
            if not unknown(where, s, SECRET_KEYS):
                continue
            if "description" in s and not isinstance(s["description"], str):
                errs.append(f"{where}.description: must be a string")
            if "processor" in s and not isinstance(s["processor"], bool):
                errs.append(f"{where}.processor: must be true or false")

    settings = m.get("settings", {})
    if unknown("settings", settings, set(settings) if isinstance(settings, dict) else set()):
        for n, s in settings.items():
            where = f"settings.{n}"
            if not unknown(where, s, SETTING_KEYS):
                continue
            t = s.get("type")
            if t is None:
                errs.append(f"{where}.type: required ({' | '.join(SETTING_TYPES)})")
            elif t not in SETTING_TYPES:
                errs.append(f"{where}.type: {t!r} is not one of {', '.join(SETTING_TYPES)}")
            req = s.get("required", False)
            if not isinstance(req, bool):
                errs.append(f"{where}.required: must be true or false")
            if "default" in s:
                if t in SETTING_TYPES and not setting_type_ok(t, s["default"]):
                    errs.append(f"{where}.default: must be {_type_hint(t)} (type = {t!r})")
            elif req is not True:
                errs.append(f"{where}.default: required for a setting that is not required")
            if n == HOOKS_SETTING and t is not None and t != "path":
                errs.append(f"{where}.type: a hooks module is loaded by file path, so its type must be \"path\"")

    proc = m.get("processor")
    if proc is not None and unknown("processor", proc, PROCESSOR_KEYS):
        mod = proc.get("module", "processor")
        if mod != "processor":
            errs.append("processor.module: must be \"processor\" (processor.py in the bobbin)")
        t = proc.get("timeout_s")
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0):
            errs.append("processor.timeout_s: must be a number > 0")
        if "state" in proc and not isinstance(proc["state"], bool):
            errs.append("processor.state: must be true or false")

    outputs = m.get("outputs")
    if outputs is None:
        errs.append("outputs: at least one output is required")
    elif unknown("outputs", outputs, set(outputs) if isinstance(outputs, dict) else set()):
        if not outputs:
            errs.append("outputs: at least one output is required")
        for k, o in outputs.items():
            if k != ALL_OUTPUTS and not EVENT_TYPE_RE.match(k):
                errs.append(f"outputs.{k}: not an event-type token ({EVENT_TYPE_RE.pattern}) or \"*\"")
            if unknown(f"outputs.{k}", o, OUTPUT_KEYS) and "description" in o and not isinstance(o["description"], str):
                errs.append(f"outputs.{k}.description: must be a string")
        if proc is None and len(outputs) >= 2 and ALL_OUTPUTS not in outputs:
            errs.append('outputs: a bobbin with several outputs and no processor must declare "*"'
                        " (the source's action; an instance cannot be expanded without it)")
    return errs


def _validate_files(pkg: Package) -> list[str]:
    errs = []
    src = pkg.source
    mod = src.get("module")
    if isinstance(mod, str) and mod and _is_local_module(mod) and not os.path.isfile(pkg.local_file(mod)):
        errs.append(f"source.module: {mod}.py is not in the bobbin")
    if pkg.processor is not None and not os.path.isfile(pkg.local_file("processor")):
        errs.append("processor: processor.py is not in the bobbin")
    return errs


def load_package(pkg_dir: str, origin: str, spans: dict) -> Package:
    dirname = os.path.basename(os.path.normpath(pkg_dir))
    pkg = Package(dirname, os.path.abspath(pkg_dir), origin)
    path = os.path.join(pkg_dir, MANIFEST)
    try:
        m = tomllib.loads(_config.read_regular_text(path))
    except (OSError, UnicodeDecodeError) as e:
        pkg.errors.append(f"{MANIFEST} cannot be read ({e.__class__.__name__})")
        return pkg
    except tomllib.TOMLDecodeError as e:
        pkg.errors.append(f"{MANIFEST} is not valid TOML: {e}")
        return pkg
    pkg.manifest = m
    pkg.errors.extend(validate_manifest(m, dirname, spans))
    if isinstance(m.get("name"), str) and NAME_RE.match(m["name"]):
        pkg.name = m["name"]
    if not pkg.errors:
        pkg.errors.extend(_validate_files(pkg))
    return pkg


# ---- discovery -------------------------------------------------------------------------------------------
def package_dirs(cfg: dict) -> list[tuple[str, str]]:
    """(directory, origin) in search order: the built-ins, then each ``spool.bobbin_path`` entry."""
    out = [(BUILTIN_DIR, BUILTIN)]
    for p in _path_list(cfg):
        out.append((p, p))
    return out


def _path_list(cfg: dict) -> list[str]:
    p = (cfg.get("spool") or {}).get("bobbin_path") if isinstance(cfg.get("spool"), dict) else None
    if isinstance(p, list):
        return [x for x in p if isinstance(x, str)]
    return []


def _packages_table_errors(cfg: dict) -> list[str]:
    errs: list[str] = []
    sp = cfg.get("spool")
    p = sp.get("bobbin_path") if isinstance(sp, dict) else None
    if p is not None:
        if not isinstance(p, list) or not all(isinstance(x, str) for x in p):
            errs.append("spool.bobbin_path: must be a list of absolute directory paths")
        else:
            for x in p:
                if not os.path.isabs(x):
                    errs.append(f"spool.bobbin_path: {x!r} must be an absolute path")
    return errs


def _spans(cfg: dict) -> dict:
    s = cfg.get("spans")
    return s if isinstance(s, dict) else dict(_config.DEFAULTS["spans"])


def discover_tagged(cfg: dict) -> tuple[dict[str, Package], list[tuple[frozenset | None, str]]]:
    """Every bobbin found (built-ins first, then ``spool.bobbin_path`` in order) and every problem.

    A bobbin that shares a name with a built-in, or with an earlier ``spool.bobbin_path`` bobbin, is
    refused; so are two bobbins whose names differ only by ``-``/``_`` (they would share a module
    name). Manifest problems are reported as ``bobbin <name>: ...``; the bobbin is still returned
    (with ``errors``) so ``bobbin list`` can show it.
    """
    errs: list[tuple[frozenset | None, str]] = [(None, e) for e in _packages_table_errors(cfg)]
    spans = _spans(cfg)
    found: dict[str, Package] = {}
    by_module: dict[str, Package] = {}
    for root, origin in package_dirs(cfg):
        if origin != BUILTIN and not os.path.isabs(root):
            continue
        if not os.path.isdir(root):
            if origin != BUILTIN:
                errs.append((None, f"spool.bobbin_path: {root} is not a directory"))
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError as e:
            errs.append((None, f"spool.bobbin_path: cannot list {root} ({e.__class__.__name__})"))
            continue
        for d in entries:
            pkg_dir = os.path.join(root, d)
            if not os.path.isfile(os.path.join(pkg_dir, MANIFEST)):
                continue
            pkg = load_package(pkg_dir, origin, spans)
            mod = pkg.name.replace("-", "_")
            where = f"bobbin {pkg.name} ({pkg.dir})"
            prev = by_module.get(mod)
            if pkg.name in found:
                first = found[pkg.name]
                if first.builtin and not pkg.builtin:
                    errs.append((frozenset({pkg.name}), f"{where}: shadows the built-in bobbin {pkg.name!r}; a"
                                 " deployment never replaces a built-in (use settings, a hooks module, or another"
                                 " name)"))
                elif os.path.basename(first.dir) != d and first.origin == origin:
                    errs.append((frozenset({pkg.name}), f"{where}: {first.dir} has the same name; directory names"
                                 " that differ only by '-'/'_' are refused"))
                else:
                    errs.append((frozenset({pkg.name}), f"{where}: {pkg.name!r} is already provided by {first.dir}"))
                continue
            if prev is not None:
                errs.append((frozenset({pkg.name, prev.name}), f"{where}: its name differs from {prev.name!r}"
                             f" ({prev.dir}) only by '-'/'_'; the two would share a module name"))
                continue
            for e in pkg.errors:
                errs.append((frozenset({pkg.name}), f"bobbin {pkg.name}: {e}"))
            found[pkg.name] = pkg
            by_module[mod] = pkg
    return found, errs


def discover(cfg: dict) -> tuple[dict[str, Package], list[str]]:
    """Every bobbin found and every problem with any of them (``check`` reports them all)."""
    pkgs, tagged = discover_tagged(cfg)
    return pkgs, [m for _, m in tagged]


# ---- path-loaded code ------------------------------------------------------------------------------------
def code_path_problem(path: str, *, want: str = "any") -> str | None:
    """Why code at ``path`` may not be loaded, or None (ARCHITECTURE §14.3.6): it must be an
    absolute path to a regular file or directory, not group- or other-writable, owned by root
    or by the user hookspool runs as."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return f"{path!r} must be an absolute path"
    try:
        st = os.stat(path)
    except OSError:
        return f"{path} does not exist"
    is_dir, is_reg = stat.S_ISDIR(st.st_mode), stat.S_ISREG(st.st_mode)
    if want == "file" and not is_reg:
        return f"{path} is not a regular file"
    if want == "dir" and not is_dir:
        return f"{path} is not a directory"
    if not (is_dir or is_reg):
        return f"{path} is not a regular file or directory"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path} is group- or other-writable"
    uid = os.geteuid()
    if st.st_uid not in (0, uid):
        return f"{path} is owned by uid {st.st_uid}, not root or the user hookspool runs as (uid {uid})"
    return None


_PATH_MODULES: dict[str, tuple[str, str]] = {}    # private module name -> (file, bobbin dir)
_LOADED: dict[str, tuple[tuple, Any]] = {}
_LOAD_LOCK = threading.Lock()


def register_path_module(name: str, file: str, pkg_dir: str) -> None:
    with _LOAD_LOCK:
        _PATH_MODULES[name] = (file, pkg_dir)


def load_path_module(name: str):
    """Load a ``spool.bobbin_path`` bobbin's module by file path under its private name (never
    through ``sys.path``). Loaded again when the file changes, so an intake reload picks up
    edited source code. Refuses code that fails the ownership rules."""
    with _LOAD_LOCK:
        entry = _PATH_MODULES.get(name)
        if entry is None:
            raise ImportError(f"{name}: no bobbin provides this module (is it in spool.bobbin_path?)")
        file, pkg_dir = entry
        for p, want in ((pkg_dir, "dir"), (file, "file")):
            problem = code_path_problem(p, want=want)
            if problem:
                raise ImportError(f"{name}: refused: {problem}")
        st = os.stat(file)
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        hit = _LOADED.get(name)
        if hit is not None and hit[0] == sig:
            return hit[1]
        spec = importlib.util.spec_from_file_location(name, file)
        if spec is None or spec.loader is None:
            raise ImportError(f"{name}: cannot load {file}")
        mod = importlib.util.module_from_spec(spec)
        prev = sys.modules.get(name)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            if prev is not None:
                sys.modules[name] = prev
            else:
                sys.modules.pop(name, None)
            raise
        _LOADED[name] = (sig, mod)
        return mod


# ---- expansion -------------------------------------------------------------------------------------------
def _env_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _settings_for(pkg: Package, given: Any, where: str, errs: list[str]) -> dict:
    out: dict[str, Any] = {}
    if given is None:
        given = {}
    if not isinstance(given, dict):
        errs.append(f"{where}.settings: must be a table")
        given = {}
    decl = pkg.settings
    for k in given:
        if k not in decl:
            errs.append(f"{where}.settings.{k}: bobbin {pkg.name!r} has no such setting")
    for k, d in decl.items():
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if k in given:
            v = given[k]
            if not setting_type_ok(t, v):
                errs.append(f"{where}.settings.{k}: must be {_type_hint(t)} (type = {t!r})")
                continue
            out[k] = copy.deepcopy(v)
        elif d.get("required") is True:
            errs.append(f"{where}.settings.{k}: required by bobbin {pkg.name!r}")
        else:
            out[k] = copy.deepcopy(d.get("default"))
    return out


def _secrets_for(pkg: Package, given: Any, where: str, errs: list[str]) -> dict:
    if given is None:
        given = {}
    if not isinstance(given, dict):
        errs.append(f"{where}.secrets: must be a table of secret references")
        return {}
    for k in given:
        if k not in pkg.secrets:
            errs.append(f"{where}.secrets.{k}: bobbin {pkg.name!r} has no such secret")
    for k in pkg.secrets:
        if k not in given:
            errs.append(f"{where}.secrets.{k}: required by bobbin {pkg.name!r}")
    return {k: copy.deepcopy(v) for k, v in given.items() if k in pkg.secrets}


def _outputs_for(pkg: Package, given: Any, where: str, errs: list[str]) -> dict:
    if given is None:
        given = {}
    if not isinstance(given, dict):
        errs.append(f"{where}.outputs: must be a table of event_type = destination")
        return {}
    out = {}
    for k, v in given.items():
        if k not in pkg.outputs:
            errs.append(f"{where}.outputs.{k}: bobbin {pkg.name!r} has no such output")
            continue
        if not isinstance(v, str) or not v:
            errs.append(f"{where}.outputs.{k}: must be a destination name")
            continue
        out[k] = v
    return out


def _expand_one(cfg: dict, inst: str, spec: Any, pkgs: dict[str, Package], home: str,
                errs: list[str]) -> tuple[dict, dict | None, list[dict], dict] | None:
    where = f"bobbins.{inst}"
    if not INSTANCE_RE.match(inst):
        errs.append(f"{where}: the instance name must match {INSTANCE_RE.pattern} (it is the URL /hooks/<instance>)")
        return None
    if not isinstance(spec, dict):
        errs.append(f"{where}: must be a table")
        return None
    n0 = len(errs)
    for k in spec:
        if k not in INSTANCE_KEYS:
            errs.append(f"{where}.{k}: unknown key")
    pname = spec.get("bobbin", inst)
    if not isinstance(pname, str) or pname not in pkgs:
        errs.append(f"{where}.bobbin: unknown bobbin {pname!r} (see `hookspool bobbin list`)")
        return None
    pkg = pkgs[pname]
    if pkg.errors:
        errs.append(f"{where}: bobbin {pname!r} is invalid (see the bobbin {pname} errors)")
        return None
    settings = _settings_for(pkg, spec.get("settings"), where, errs)
    secrets = _secrets_for(pkg, spec.get("secrets"), where, errs)
    outputs = _outputs_for(pkg, spec.get("outputs"), where, errs)
    proc = pkg.processor
    msrc = pkg.source

    # the source table
    src: dict[str, Any] = {}
    if "template" in msrc:
        src["module"] = TEMPLATE_MODULE
        src.update(copy.deepcopy(msrc["template"]))
        if "sender_retries" in msrc:
            src["sender_retries"] = msrc["sender_retries"]
    else:
        mod = msrc["module"]
        if _is_local_module(mod):
            src["module"] = pkg.code_module(mod)
            if not pkg.builtin:
                register_path_module(src["module"], pkg.local_file(mod), pkg.dir)
        else:
            src["module"] = mod
    if msrc.get("span") is not None:
        src["span"] = msrc["span"]
    expect = spec.get("expect")
    if expect is not None and not isinstance(expect, bool):
        errs.append(f"{where}.expect: must be true or false")
    elif expect is True:
        if msrc.get("expect_every_s") is None and spec.get("expect_every_s") is None:
            errs.append(f"{where}.expect: bobbin {pname!r} recommends no expect_every_s; set expect_every_s")
        elif msrc.get("expect_every_s") is not None:
            src["expect_every_s"] = msrc["expect_every_s"]
    for k in SOURCE_OVERRIDES:
        if k in spec:
            src[k] = copy.deepcopy(spec[k])
    src["secrets"] = secrets

    dest: dict | None = None
    routes: list[dict] = []
    proc_secrets = sorted(k for k, s in pkg.secrets.items() if isinstance(s, dict) and s.get("processor") is True)
    if proc is not None:
        dname = f"script:{inst}"
        src["action"] = dname
        dest = {"processor": inst, "max_inflight": 1,
                "timeout_s": spec.get("timeout_s", proc.get("timeout_s", DEFAULT_PROCESSOR_TIMEOUT_S))}
        if proc.get("state") is True:
            sd = spec.get("state_dir")
            dest["state_dir"] = sd if sd is not None else os.path.join(home, "services", inst)
        elif "state_dir" in spec:
            errs.append(f"{where}.state_dir: bobbin {pname!r} keeps no state")
        dest["env"] = {
            "HOOKSPOOL_BOBBIN": pkg.name,
            "HOOKSPOOL_BOBBIN_INSTANCE": inst,
            "HOOKSPOOL_BOBBIN_DIR": pkg.dir,
            "HOOKSPOOL_SETTINGS": _env_json(settings),
            "HOOKSPOOL_DELTA_ROUTES": _env_json(outputs),
            ENV_SECRET_REFS: _env_json({k: secrets[k] for k in proc_secrets if k in secrets}),
        }
    else:
        for k in PROCESSOR_OVERRIDES:
            if k in spec:
                errs.append(f"{where}.{k}: only for a bobbin with a processor")
        declared = list(pkg.outputs)
        if len(declared) == 1:
            only = declared[0]
            if only not in outputs:
                errs.append(f"{where}.outputs.{only}: required (the bobbin's only output is the source's action)")
            else:
                src["action"] = outputs[only]
        else:
            if ALL_OUTPUTS not in outputs:
                errs.append(f"{where}.outputs.\"*\": required for a bobbin with several outputs and no processor,"
                            " so that no event reaches a route without an action")
            else:
                src["action"] = outputs[ALL_OUTPUTS]
            for et, d in outputs.items():
                if et == ALL_OUTPUTS:
                    continue
                routes.append({"name": f"{inst}-{et}", "source": inst, "match": {"event_type": et}, "destination": d})

    hooks = None
    hdecl = pkg.settings.get(HOOKS_SETTING)
    if isinstance(hdecl, dict) and hdecl.get("type") == "path" and settings.get(HOOKS_SETTING):
        hooks = settings[HOOKS_SETTING]
    meta = {"bobbin": pkg.name, "dir": pkg.dir, "origin": pkg.origin, "processor": proc is not None,
            "source": inst, "destination": f"script:{inst}" if proc is not None else None,
            "routes": [r["name"] for r in routes], "processor_secrets": proc_secrets, "hooks": hooks,
            "outputs": dict(outputs)}
    if len(errs) > n0:
        return None
    return src, dest, routes, meta


def expand(cfg: dict, *, cli_home: str | None = None) -> dict:
    """``cfg`` with every ``[bobbins.<instance>]`` expanded into ordinary tables (§14.3.3).

    Returns a new dict; raises ``BobbinError`` listing every problem. A config without
    ``[bobbins]`` comes back unchanged (``_bobbins`` is empty)."""
    errs = _packages_table_errors(cfg)
    services = cfg.get("bobbins")
    out = copy.deepcopy(cfg)
    out["_bobbins"] = {}
    if services is None or services == {}:
        if errs:
            raise BobbinError(errs)
        return out
    if not isinstance(services, dict):
        raise BobbinError(errs + ["bobbins: must be a table of [bobbins.<instance>] tables"])
    if errs:
        raise BobbinError(errs)
    pkgs, tagged = discover_tagged(cfg)
    # loading fails only on problems that concern a bobbin in use (or the search path itself);
    # `check` reports the rest, so a broken unused bobbin never stops a running deployment
    used = {str(s.get("bobbin", i)) if isinstance(s, dict) else str(i) for i, s in services.items()}
    errs.extend(m for names, m in tagged if names is None or names & used)
    home = _config.home_dir(cfg, cli_home)
    sources = out.get("sources") if isinstance(out.get("sources"), dict) else {}
    dests = out.get("destinations") if isinstance(out.get("destinations"), dict) else {}
    file_routes = out.get("routes") if isinstance(out.get("routes"), list) else []
    file_route_names = {r.get("name") for r in file_routes if isinstance(r, dict)}
    gen_routes: list[dict] = []
    for inst, spec in services.items():
        if inst in sources:
            errs.append(f"sources.{inst}: also defined by [bobbins.{inst}]; remove one (an expansion is never"
                        " silently overridden)")
            continue
        res = _expand_one(cfg, inst, spec, pkgs, home, errs)
        if res is None:
            continue
        src, dest, routes, meta = res
        if dest is not None and meta["destination"] in dests:
            errs.append(f"destinations.\"{meta['destination']}\": also defined by [bobbins.{inst}] (its processor);"
                        " remove the table")
            continue
        clash = [r["name"] for r in routes if r["name"] in file_route_names]
        if clash:
            errs.append(f"routes.{clash[0]}: also generated by [bobbins.{inst}] for its output; rename the route")
            continue
        sources[inst] = src
        if dest is not None:
            dests[meta["destination"]] = dest
        gen_routes.extend(routes)
        out["_bobbins"][inst] = meta
    if errs:
        raise BobbinError(errs)
    out["sources"] = sources
    out["destinations"] = dests
    out["routes"] = gen_routes + list(file_routes)
    return out


# ---- check -----------------------------------------------------------------------------------------------
def check(cfg: dict, rep) -> None:
    """The bobbin rules of ``hookspool check`` (§14.3.2, 14.3.3, 14.3.6) on an expanded config.
    Adds to ``rep`` (a ``config.Report``)."""
    services = cfg.get("bobbins")
    metas = cfg.get("_bobbins")
    if services and metas is None:
        try:
            cfg = expand(cfg)
        except BobbinError as e:
            for m in e.errors:
                rep.err(m)
            return
        metas = cfg["_bobbins"]
    metas = metas or {}
    if not services and not _path_list(cfg):
        return
    pkgs, errs = discover(cfg)
    for e in errs:
        rep.err(e)

    # code loaded by path: the spool.bobbin_path directories, the packages in use, hooks modules
    for p in _path_list(cfg):
        problem = code_path_problem(p, want="dir") if os.path.isdir(p) else None
        if problem:
            rep.err(f"spool.bobbin_path: {problem}")
    for inst, meta in metas.items():
        if meta.get("origin") != BUILTIN:
            for p in [meta["dir"], *Package(meta["bobbin"], meta["dir"], meta["origin"]).code_files()]:
                problem = code_path_problem(p)
                if problem:
                    rep.err(f"bobbins.{inst}: bobbin code {problem}")
        if meta.get("hooks"):
            problem = code_path_problem(meta["hooks"], want="file")
            if problem:
                rep.err(f"bobbins.{inst}.settings.{HOOKS_SETTING}: {problem}")

    for inst, meta in metas.items():
        if meta.get("routes"):
            _warn_no_event_type_attr(cfg, inst, rep)

    routes = cfg.get("routes") if isinstance(cfg.get("routes"), list) else []
    for inst, meta in metas.items():
        if not meta.get("processor"):
            continue
        dname = meta["destination"]
        # a non-tee route on the instance that does not go through the processor bypasses it
        for r in routes:
            if not isinstance(r, dict) or r.get("source") != inst or r.get("tee") is True:
                continue
            d = r.get("destination")
            if d is not None and d != dname:
                rep.err(f"routes.{r.get('name')}: sends {inst} events to {d}, bypassing the bobbin's processor"
                        f" ({dname}); route its outputs with [bobbins.{inst}].outputs, or make it a tee")
        for et, d in (meta.get("outputs") or {}).items():
            e = _config._action_ok(cfg, d)
            if e is None and d.startswith("script:") and d not in (cfg.get("destinations") or {}):
                e = f"no [destinations.\"{d}\"] table"
            if e:
                rep.err(f"bobbins.{inst}.outputs.{et}: {e}")
        sec = ((cfg.get("sources") or {}).get(inst) or {}).get("secrets") or {}
        for name in meta.get("processor_secrets") or ():
            ref = sec.get(name)
            if isinstance(ref, dict) and "env" in ref:
                rep.err(f"bobbins.{inst}.secrets.{name}: the processor needs this secret and does not inherit the"
                        " dispatcher's environment; use a file or env_file reference")


def _warn_no_event_type_attr(cfg: dict, inst: str, rep) -> None:
    """The routes generated for a multi-output bobbin match ``event_type`` in ``attrs`` (§14.3.3 step 3);
    a source that does not expose it sends every event to the ``"*"`` output. Warn, since the plugin's
    ``attrs`` can only be judged by running it."""
    s = (cfg.get("sources") or {}).get(inst) or {}
    if s.get("module") == TEMPLATE_MODULE:
        attrs = s.get("attrs")
        ok = isinstance(attrs, dict) and "event_type" in attrs
    else:
        try:
            plugin = _config.default_plugin_loader(inst, s.get("module"))
        except Exception:  # noqa: BLE001 - check reports an unloadable plugin itself
            return
        ok = getattr(getattr(plugin, "module", plugin), "attrs", None) is not None
    if not ok:
        rep.warn(f"bobbins.{inst}: its source does not expose event_type in attrs, so the generated routes"
                 " (one per output) never match and every event reaches the \"*\" output")


def is_expanded_destination(cfg: dict, name: str) -> bool:
    return any(m.get("destination") == name for m in (cfg.get("_bobbins") or {}).values())


# ---- TOML output (bobbin expand) ------------------------------------------------------------------------
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_key(str(k))} = {_val(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v))


def _table(header: str, t: dict, *, array: bool = False) -> list[str]:
    lines = [f"[[{header}]]" if array else f"[{header}]"]
    subs = []
    for k, v in t.items():
        if isinstance(v, dict) and k in ("env",):
            subs.append((k, v))
            continue
        lines.append(f"{_key(k)} = {_val(v)}")
    for k, v in subs:
        lines.append("")
        lines.append(f"[{header}.{_key(k)}]")
        for kk, vv in v.items():
            lines.append(f"{_key(kk)} = {_val(vv)}")
    return lines


def render(cfg: dict, instance: str | None = None) -> str:
    """The expanded tables of one instance (or all), as TOML. Secret references stay
    references; ``HOOKSPOOL_SECRET_REFS`` is shown by secret name only. No value is read."""
    metas = cfg.get("_bobbins") or {}
    names = [instance] if instance is not None else list(metas)
    out: list[str] = []
    for inst in names:
        meta = metas[inst]
        if out:
            out.append("")
        out.append(f"# [bobbins.{inst}]: bobbin {meta['bobbin']} ({meta['origin']})")
        out.extend(_table(f"sources.{_key(inst)}", cfg["sources"][inst]))
        if meta.get("destination"):
            d = copy.deepcopy(cfg["destinations"][meta["destination"]])
            env = d.get("env") or {}
            if ENV_SECRET_REFS in env:
                names_only = sorted(json.loads(env[ENV_SECRET_REFS]))
                env[ENV_SECRET_REFS] = "<references of: " + ", ".join(names_only) + ">" if names_only else "{}"
            out.append("")
            out.extend(_table(f"destinations.{_key(meta['destination'])}", d))
        for rname in meta.get("routes") or ():
            r = next(r for r in cfg["routes"] if isinstance(r, dict) and r.get("name") == rname)
            out.append("")
            out.extend(_table("routes", r, array=True))
    return "\n".join(out) + ("\n" if out else "")


# ---- scaffold (bobbin new) ------------------------------------------------------------------------------
SCAFFOLD_SECRET = "test-secret-bobbin"            # the bobbin kit's default test secret
_SCAFFOLD_BODY = '{"id":"evt_1","type":"example","note":"made-up test payload"}'


def scaffold(name: str, parent: str, *, template: bool = False, processor: bool = False) -> str:
    """Write a new bobbin directory ``<parent>/<name>`` and return its path. Refuses an
    invalid name or an existing directory (``ValueError`` / ``FileExistsError``)."""
    if not NAME_RE.match(name):
        raise ValueError(f"bobbin name {name!r} must match {NAME_RE.pattern}")
    from . import verify as _verify
    d = os.path.join(os.path.abspath(parent), name)
    os.makedirs(d)
    os.makedirs(os.path.join(d, "captures"))
    sig = "sha256=" + _verify.hmac_hex_sign(SCAFFOLD_SECRET, _SCAFFOLD_BODY.encode())
    if template:
        src = ('[source]\ntemplate = { scheme = "hmac-sha256-hex", header = "X-Signature", prefix = "sha256=",'
               ' key = "id", event_type = "type", attrs = { event_type = "type" } }\n')
    else:
        src = '[source]\nmodule = "source"\n'
    outputs = ('[outputs.changed]\ndescription = "TODO: what the processor produces"\n' if processor else
               '[outputs."*"]\ndescription = "every accepted event"\n')
    manifest = (f'name = "{name}"\nversion = "1"\nsummary = "TODO: one line on what the outside service sends"\n\n'
                f'{src}span = "burst"\nsender_retries = false\n\n'
                '[secrets.secret]\ndescription = "TODO: the signing secret the provider shows for the webhook"\n\n')
    if processor:
        manifest += '[processor]\nmodule = "processor"\ntimeout_s = 300\nstate = true\n\n'
    manifest += outputs
    bad = "sha256=" + _verify.hmac_hex_sign("wrong-test-secret", _SCAFFOLD_BODY.encode())
    out_line = (f'outputs = {{ changed = "hermes:{name}-changed" }}' if processor else
                f'outputs = {{ "*" = "hermes:{name}" }}')
    table = (f'[bobbins.{name}]\nsecrets.secret = {{ file = "/etc/hookspool/secrets/{name}_secret" }}\n{out_line}\n')
    files = {MANIFEST: manifest,
             os.path.join("captures", "example.http"):
                 "# capture: TODO describe the request; signed with " + SCAFFOLD_SECRET + "\n# expect: queued\n"
                 f"POST /hooks/{name} HTTP/1.1\nContent-Type: application/json\nX-Signature: {sig}\n"
                 f"Content-Length: {len(_SCAFFOLD_BODY)}\n\n{_SCAFFOLD_BODY}\n",
             os.path.join("captures", "bad_signature.http"):
                 "# capture: the same request signed with another secret\n# expect: unverified\n"
                 f"POST /hooks/{name} HTTP/1.1\nContent-Type: application/json\nX-Signature: {bad}\n"
                 f"Content-Length: {len(_SCAFFOLD_BODY)}\n\n{_SCAFFOLD_BODY}\n",
             "SETUP.md": f"# Set up {name}\n\nTODO: what to create at the provider (the webhook and its signing"
                         " secret), with a link to the provider's webhook documentation.\n\n"
                         "## Secrets\n\n"
                         f"- `secret`: TODO the signing secret the provider shows. Save it to"
                         f" `/etc/hookspool/secrets/{name}_secret`, mode 0600, owned by the hookspool user.\n\n"
                         "## Config\n\nAdd to `config.toml`, then run `hookspool check` and `hookspool reload`:\n\n"
                         f"```toml\n{table}```\n\n"
                         f"Proxy: forward `/hooks/{name}` to the intake (see `deploy/nginx.example.conf`).\n\n"
                         "## Verify\n\nSend a test delivery from the provider, then"
                         f" `hookspool list --source {name}` shows a batch.\n\n"
                         "## Roll back\n\nDelete the webhook at the provider, remove the table and run"
                         " `hookspool reload`.\n",
             "AGENTS.md": f"# Set up {name} (for an agent)\n\nThe same procedure as SETUP.md, as steps.\n\n"
                          "## Needs from the human\n\n"
                          "- TODO: an account at the provider that may create webhooks\n"
                          f"- the signing secret `secret`, written by the human to"
                          f" `/etc/hookspool/secrets/{name}_secret` (never pass its value to the agent)\n\n"
                          "## Steps\n\n"
                          "1. **Add the config table.** Goal: hookspool accepts the deliveries. Add to"
                          " `config.toml`, then run `hookspool check` and `hookspool reload`:\n\n"
                          f"   ```toml\n" + "".join("   " + ln + "\n" for ln in table.splitlines()) + "   ```\n\n"
                          "   Check: `hookspool check` exits 0, then `hookspool reload` exits 0 (without the"
                          " reload the running intake does not know the table). If `hookspool check` fails: fix"
                          " what it names, then run both again.\n"
                          "2. **Register the webhook at the provider** (`outward`: ask the human first). Goal: the"
                          f" provider sends to `https://<HOOKS_HOST>/hooks/{name}`. TODO: the provider's API call"
                          " or console page.\n"
                          "   Check: the provider shows the webhook as active. If not: report what it shows.\n"
                          "3. **Verify.** Goal: one delivery arrives. Ask the provider for a test delivery, then run"
                          f" `hookspool list --source {name}`.\n"
                          "   Check: a batch is listed. If not: run `hookspool status` and report it.\n",
             "README.md": f"# {name}\n\nTODO: what the outside service sends and what this bobbin does with it.\n\n"
                          "## Setup at the provider\n\nTODO\n\n## Secrets\n\n- `secret`: TODO\n\n"
                          "## Settings\n\nNone yet.\n\n## Outputs\n\nTODO\n\n"
                          f"The capture in `captures/` is signed with the test secret `{SCAFFOLD_SECRET}`.\n"}
    if not template:
        files["source.py"] = (
            f'"""{name} webhooks: verify the signature and choose the coalescing key."""\n'
            "from hookspool.plugin import BadPayload, Request, Secrets, verify as v\n\n"
            f'name = "{name}"\nsecrets = ("secret",)\nauth_headers = ("X-Signature",)\n\n\n'
            "def verify(req: Request, secrets: Secrets) -> bool:\n"
            '    return v.hmac_prefixed(req.header("X-Signature"), "sha256=", secrets["secret"], req.body)\n\n\n'
            "def key(payload: dict, req: Request) -> str:\n"
            '    if not isinstance(payload, dict) or not payload.get("id"):\n'
            '        raise BadPayload("no id")\n'
            '    return str(payload["id"])\n\n\n'
            "def event_type(payload: dict, req: Request) -> str:\n"
            '    return str(payload.get("type") or "")\n\n\n'
            "def attrs(payload: dict, req: Request) -> dict:\n"
            '    return {"event_type": event_type(payload, req)}\n')
    if processor:
        files["processor.py"] = (
            f'"""{name} processor: turns a batch of events into outputs."""\n\n\n'
            "def process(batch, ctx) -> list[dict]:\n"
            '    """Return items, each {"event_type": ..., <payload fields>}; [] means nothing to do."""\n'
            "    return []\n")
    for rel, text in files.items():
        with open(os.path.join(d, rel), "x", encoding="utf-8") as f:
            f.write(text)
    return d
