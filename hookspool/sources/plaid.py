"""``hookspool.sources.plaid`` is the Plaid bobbin's source (``hookspool/bobbins/plaid/source.py``).

Kept importable so a config with ``[sources.plaid]`` (implicit module) still loads (ARCHITECTURE 14.3.9).
The module object is the bobbin's own, not a copy.
"""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hookspool.bobbins.plaid.source")
