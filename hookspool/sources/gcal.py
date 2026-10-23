"""``hookspool.sources.gcal`` is the Calendar bobbin's source (``hookspool/bobbins/gcal/source.py``).

Kept importable so a config with ``[sources.gcal]`` (implicit module) still loads (ARCHITECTURE 14.3.9).
The module object is the bobbin's own, not a copy.
"""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hookspool.bobbins.gcal.source")
