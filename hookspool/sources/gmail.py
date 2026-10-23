"""``hookspool.sources.gmail`` is the Gmail bobbin's source (``hookspool/bobbins/gmail/source.py``).

Kept importable so a config with ``[sources.gmail]`` (implicit module) still loads (ARCHITECTURE 14.3.9).
The module object is the bobbin's own, not a copy.
"""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hookspool.bobbins.gmail.source")
