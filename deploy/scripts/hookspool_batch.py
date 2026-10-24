"""Re-export of ``hookspool.processor`` for the delta scripts in this directory (ARCHITECTURE 6.6, 14.3.4).

The batch protocol (the document, emits, staged state writes and the journal that makes a retry replay
instead of reading the outside system again) lives in ``hookspool.processor``; ``import hookspool_batch``
gives that very module. The scripts therefore need hookspool installed (or this repository checkout):
the stand-alone "copy three files" mode is retired.
"""
import importlib
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.isfile(os.path.join(_root, "hookspool", "processor.py")) and _root not in sys.path:
    sys.path.insert(0, _root)                     # a checkout: use its hookspool, not another installed copy

sys.modules[__name__] = importlib.import_module("hookspool.processor")
