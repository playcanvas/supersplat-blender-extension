"""Make accidental network or Keychain access fail loudly in tests."""

from __future__ import annotations

import socket


class NetworkBlocked(AssertionError):
    pass


def _blocked(*_args: object, **_kwargs: object) -> None:
    raise NetworkBlocked("Tests must not open network connections.")


def block_network() -> None:
    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def allow_only_loopback() -> None:
    """Let connections reach this machine only, for scripts that talk to scripts/mock_api.py."""
    create_connection, getaddrinfo = socket.create_connection, socket.getaddrinfo

    def guarded_create_connection(address, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if address[0] not in _LOOPBACK:
            raise NetworkBlocked(f"Only loopback connections are allowed, not {address[0]}.")
        return create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if host not in _LOOPBACK:
            raise NetworkBlocked(f"Only loopback connections are allowed, not {host}.")
        return getaddrinfo(host, *args, **kwargs)

    socket.create_connection = guarded_create_connection  # type: ignore[assignment]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]


def block_keychain() -> None:
    from supersplat import credentials

    class _NoKeychain:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("Tests must not touch the real Keychain.")

    credentials.SecurityFrameworkBackend = _NoKeychain  # type: ignore[misc,assignment]


def block_all() -> None:
    block_network()
    block_keychain()
