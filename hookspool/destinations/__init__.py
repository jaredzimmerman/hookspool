"""Destination registry (ARCHITECTURE §14.2).

``build(config, name)`` returns the ``Destination`` for a destination name:

- ``hermes:<route>`` and ``script:<name>`` are implicit destinations of the built-in
  ``hermes`` and ``script`` types; a ``[destinations."<name>"]`` table overrides their
  defaults.
- ``[destinations.<name>]`` with ``type = "http"`` (or another registered type) defines
  a named destination; ``module = "pkg.mod:ClassName"`` loads a third-party class.

Built-in types live in ``hookspool.destinations.<type>`` and are imported on first use.
A name or type that cannot be built becomes ``Unavailable``: every attempt is a
``config`` retry, never a lost or dead batch.
"""
from __future__ import annotations

import importlib

from .. import config as _config
from .base import DeliveryContext, Destination, Outcome, Unavailable, encode_body

__all__ = ["DeliveryContext", "Destination", "Outcome", "Unavailable", "encode_body", "TYPES", "register",
           "build"]

# type name -> "module:Class"
TYPES: dict[str, str] = {
    "hermes": "hookspool.destinations.hermes:HermesDestination",
    "script": "hookspool.destinations.script:ScriptDestination",
    "http": "hookspool.destinations.http:HttpDestination",
}


def register(type_name: str, target: str | type) -> None:
    """Register a destination type: a class, or ``"module:Class"`` imported on use."""
    if isinstance(target, type) and not issubclass(target, Destination):
        raise TypeError("a destination type must subclass hookspool.destinations.Destination")
    TYPES[type_name] = target  # type: ignore[assignment]


def _load(target: str | type) -> type:
    if isinstance(target, type):
        return target
    mod, _, cls = str(target).partition(":")
    if not mod or not cls:
        raise ImportError(f"{target!r} is not 'module:ClassName'")
    obj = getattr(importlib.import_module(mod), cls)
    if not isinstance(obj, type) or not issubclass(obj, Destination):
        raise TypeError(f"{target} is not a Destination subclass")
    return obj


def build(config: dict, name: str) -> Destination:
    if name in _config.RESERVED_DESTINATIONS:
        return Unavailable(name, config, f"{name!r} is a reserved destination name and never delivers")
    spec = (config.get("destinations") or {}).get(name) or {}
    if not isinstance(spec, dict):
        return Unavailable(name, config, f"destination {name!r} is not a table")
    target: str | type | None
    if spec.get("module"):
        target = str(spec["module"])
        label = target
    else:
        t = _config.destination_type(config, name)
        if t is None:
            return Unavailable(name, config, f"unknown destination {name!r}")
        target = TYPES.get(t)
        label = t
        if target is None:
            return Unavailable(name, config, f"unknown destination type {t!r} for {name!r}")
    try:
        cls = _load(target)
    except (ImportError, AttributeError, TypeError) as e:
        return Unavailable(name, config, f"destination type {label} is not available ({e.__class__.__name__})")
    try:
        return cls(name, spec, config)
    except Exception as e:      # a bad table must not take the dispatcher down
        return Unavailable(name, config, f"destination {name!r} cannot be built ({e.__class__.__name__})")
