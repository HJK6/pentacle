"""Startup binding must fail closed when asyncio skips a requested address."""

from __future__ import annotations

import asyncio
import errno
import logging
import socket

import pytest

import server as server_module
from server import Server


class _Socket:
    family = socket.AF_INET

    def __init__(self, host: str, port: int) -> None:
        self._address = (host, port)

    def getsockname(self) -> tuple[str, int]:
        return self._address


class _PartialServer:
    def __init__(self) -> None:
        self.sockets = (_Socket("127.0.0.1", 7791),)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_all_requested_bind_failure_reports_errno(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_serve(*_args: object, **_kwargs: object) -> _PartialServer:
        raise OSError("could not bind on any address")

    monkeypatch.setattr(server_module, "serve", fake_serve)

    async def exercise() -> None:
        daemon = Server(port=0, binds=["192.0.2.1"])
        with caplog.at_level(logging.ERROR, logger=server_module.log.name):
            with pytest.raises(OSError) as failure:
                await daemon.bind()
        assert failure.value.errno == errno.EADDRNOTAVAIL
        assert daemon._ws_server is None

    asyncio.run(exercise())

    assert "host=192.0.2.1" in caplog.text
    assert f"errno={errno.EADDRNOTAVAIL}" in caplog.text


def test_requested_bind_failure_is_fatal_and_closes_survivor(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    partial_server = _PartialServer()

    async def fake_serve(*_args: object, **_kwargs: object) -> _PartialServer:
        return partial_server

    monkeypatch.setattr(server_module, "serve", fake_serve)

    async def exercise() -> None:
        daemon = Server(
            port=7791,
            binds=["127.0.0.1", "192.0.2.1"],
        )
        try:
            with caplog.at_level(logging.ERROR, logger=server_module.log.name):
                with pytest.raises(OSError) as failure:
                    await daemon.bind()
            assert failure.value.errno == errno.EADDRNOTAVAIL
            assert daemon._ws_server is None
        finally:
            await daemon.close()

    asyncio.run(exercise())

    assert partial_server.closed
    assert "host=192.0.2.1" in caplog.text
    assert f"errno={errno.EADDRNOTAVAIL}" in caplog.text
