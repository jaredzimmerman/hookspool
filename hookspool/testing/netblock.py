"""Block the network for code under test (the bobbin conformance kit).

``block_network()`` is a context manager that makes every outbound connection, UDP send and name
lookup of a non-loopback host fail with ``NetworkBlocked``, and records each attempt (host only,
never a path or a payload) in the list it yields. Loopback addresses, ``localhost`` and Unix
sockets stay usable, so a test's local stub server keeps working. ``block_network(loopback=False)``
refuses loopback too (only Unix sockets stay): the processor check runs under it, so any real
socket fails the check.

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
def block_network(*, loopback: bool = True) -> Iterator[list[str]]:
    """Block outbound connections, UDP sends and lookups; yield the attempts list.

    ``loopback=True`` (the default) lets loopback hosts through for a local stub server;
    ``loopback=False`` refuses every host, so only Unix sockets remain.
    """
    attempts: list[str] = []
    orig = {"connect": socket.socket.connect, "connect_ex": socket.socket.connect_ex,
            "sendto": socket.socket.sendto, "create_connection": socket.create_connection,
            "getaddrinfo": socket.getaddrinfo, "gethostbyname": socket.gethostbyname,
            "gethostbyname_ex": socket.gethostbyname_ex}
    unix = getattr(socket, "AF_UNIX", None)

    def allowed(host: Any) -> bool:
        return loopback and _local_host(host)

    def refuse(host: Any) -> NetworkBlocked:
        attempts.append(str(host)[:100])
        return NetworkBlocked(f"network blocked by the bobbin kit (host {str(host)[:100]})")

    def connect(self, address):
        if self.family != unix and not allowed(_host_of(address)):
            raise refuse(_host_of(address))
        return orig["connect"](self, address)

    def connect_ex(self, address):
        if self.family != unix and not allowed(_host_of(address)):
            raise refuse(_host_of(address))
        return orig["connect_ex"](self, address)

    def sendto(self, data, *args):
        address = args[-1] if args else None
        if self.family != unix and not allowed(_host_of(address)):
            raise refuse(_host_of(address))
        return orig["sendto"](self, data, *args)

    def create_connection(address, *a, **kw):
        if not allowed(_host_of(address)):
            raise refuse(_host_of(address))
        return orig["create_connection"](address, *a, **kw)

    def getaddrinfo(host, *a, **kw):
        if not allowed(host):
            raise refuse(host)
        return orig["getaddrinfo"](host, *a, **kw)

    def gethostbyname(host):
        if not allowed(host):
            raise refuse(host)
        return orig["gethostbyname"](host)

    def gethostbyname_ex(host):
        if not allowed(host):
            raise refuse(host)
        return orig["gethostbyname_ex"](host)

    socket.socket.connect = connect                 # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex           # type: ignore[method-assign]
    socket.socket.sendto = sendto                   # type: ignore[method-assign]
    socket.create_connection = create_connection    # type: ignore[assignment]
    socket.getaddrinfo = getaddrinfo                # type: ignore[assignment]
    socket.gethostbyname = gethostbyname            # type: ignore[assignment]
    socket.gethostbyname_ex = gethostbyname_ex      # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = orig["connect"]                 # type: ignore[method-assign]
        socket.socket.connect_ex = orig["connect_ex"]           # type: ignore[method-assign]
        socket.socket.sendto = orig["sendto"]                   # type: ignore[method-assign]
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
