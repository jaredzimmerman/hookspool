"""Block the network for code under test (the bobbin conformance kit).

``block_network()`` is a context manager that makes every outbound connection and every name
lookup of a non-loopback host fail with ``NetworkBlocked``, and records each attempt (host only,
never a path or a payload) in the list it yields. Loopback addresses, ``localhost`` and Unix
sockets stay usable, so a test's local stub server keeps working.

Loaded as a pytest plugin (``-p hookspool.testing.netblock``) it blocks the network for the whole
session and fails the run at the end if any test tried to reach it, even when the code under test
caught the error.
"""
from __future__ import annotations

import ipaddress
import socket
from contextlib import contextmanager
from typing import Any, Iterator


class NetworkBlocked(OSError):
    """Raised for a connection or lookup the kit does not allow."""


_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _local_host(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    h = str(host).strip("[]").lower()
    if h in _LOCAL_NAMES or h.endswith(".localhost") or h == "":
        return True
    try:
        return ipaddress.ip_address(h.split("%")[0]).is_loopback
    except ValueError:
        return False


def _host_of(address: Any) -> Any:
    if isinstance(address, tuple) and address:
        return address[0]
    return address


@contextmanager
def block_network() -> Iterator[list[str]]:
    """Block outbound connections and lookups of non-loopback hosts; yield the attempts list."""
    attempts: list[str] = []
    orig = {"connect": socket.socket.connect, "connect_ex": socket.socket.connect_ex,
            "create_connection": socket.create_connection, "getaddrinfo": socket.getaddrinfo,
            "gethostbyname": socket.gethostbyname, "gethostbyname_ex": socket.gethostbyname_ex}

    def refuse(host: Any) -> NetworkBlocked:
        attempts.append(str(host)[:100])
        return NetworkBlocked(f"network blocked by the bobbin kit (host {str(host)[:100]})")

    def connect(self, address):
        if self.family != getattr(socket, "AF_UNIX", None) and not _local_host(_host_of(address)):
            raise refuse(_host_of(address))
        return orig["connect"](self, address)

    def connect_ex(self, address):
        if self.family != getattr(socket, "AF_UNIX", None) and not _local_host(_host_of(address)):
            raise refuse(_host_of(address))
        return orig["connect_ex"](self, address)

    def create_connection(address, *a, **kw):
        if not _local_host(_host_of(address)):
            raise refuse(_host_of(address))
        return orig["create_connection"](address, *a, **kw)

    def getaddrinfo(host, *a, **kw):
        if not _local_host(host):
            raise refuse(host)
        return orig["getaddrinfo"](host, *a, **kw)

    def gethostbyname(host):
        if not _local_host(host):
            raise refuse(host)
        return orig["gethostbyname"](host)

    def gethostbyname_ex(host):
        if not _local_host(host):
            raise refuse(host)
        return orig["gethostbyname_ex"](host)

    socket.socket.connect = connect                 # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex           # type: ignore[method-assign]
    socket.create_connection = create_connection    # type: ignore[assignment]
    socket.getaddrinfo = getaddrinfo                # type: ignore[assignment]
    socket.gethostbyname = gethostbyname            # type: ignore[assignment]
    socket.gethostbyname_ex = gethostbyname_ex      # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = orig["connect"]                 # type: ignore[method-assign]
        socket.socket.connect_ex = orig["connect_ex"]           # type: ignore[method-assign]
        socket.create_connection = orig["create_connection"]    # type: ignore[assignment]
        socket.getaddrinfo = orig["getaddrinfo"]                # type: ignore[assignment]
        socket.gethostbyname = orig["gethostbyname"]            # type: ignore[assignment]
        socket.gethostbyname_ex = orig["gethostbyname_ex"]      # type: ignore[assignment]


# ---- as a pytest plugin ---------------------------------------------------------------------------
_session: dict[str, Any] = {}


def pytest_configure(config) -> None:
    cm = block_network()
    _session["cm"] = cm
    _session["attempts"] = cm.__enter__()


def pytest_sessionfinish(session, exitstatus) -> None:
    attempts = _session.get("attempts") or []
    if attempts:
        print(f"\nhookspool bobbin kit: {len(attempts)} network attempt(s) blocked: "
              + ", ".join(sorted(set(attempts))[:5]))
        session.exitstatus = 1


def pytest_unconfigure(config) -> None:
    cm = _session.pop("cm", None)
    if cm is not None:
        cm.__exit__(None, None, None)
