"""``hookspool.sources.zoom`` is the Zoom bobbin's source (``hookspool/bobbins/zoom/meetings/source.py``).

Kept importable so a config with ``[sources.zoom]`` (implicit module) still loads (ARCHITECTURE 14.3.9).
The module object is the bobbin's own, not a copy.
"""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hookspool.bobbins.zoom.meetings.source")
