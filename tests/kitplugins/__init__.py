"""Example source plugins used by the test kit (tests/sourcekit.py) and the intake tests.

They exercise the three shapes the plugin API supports: a plain HMAC header (``signed``),
a timestamped signature with a verified handshake (``stamped``), and a handshake that
carries no signature at all (``headonly``). They are test fixtures, not built-in sources.
"""
