"""
SamsungTVWS - Samsung Smart TV WS API wrapper

Copyright (C) 2019 DSR! <xchwarze@gmail.com>

SPDX-License-Identifier: LGPL-3.0
"""

import asyncio
import contextlib
import json
import logging
from types import TracebackType
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union

from websockets.client import WebSocketClientProtocol, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from . import connection, exceptions, helper
from .command import SamsungTVCommand, SamsungTVSleepCommand
from .event import (
    IGNORE_EVENTS_AT_STARTUP,
    MS_CHANNEL_CONNECT_EVENT,
    MS_CHANNEL_UNAUTHORIZED,
    MS_CHANNEL_TIMEOUT
)
from .helper import get_ssl_context

_LOGGING = logging.getLogger(__name__)


class SamsungTVWSAsyncConnection(connection.SamsungTVWSBaseConnection):
    connection: Optional[WebSocketClientProtocol]
    _recv_loop: Optional["asyncio.Task[None]"]

    async def __aenter__(self) -> "SamsungTVWSAsyncConnection":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    async def open(self) -> WebSocketClientProtocol:
        if self.is_alive():
            # someone else already created a new connection
            return self.connection

        url = self._format_websocket_url(self.endpoint)

        _LOGGING.debug("WS url %s", url)
        connect_kwargs: Dict[str, Any] = {}
        if self._is_ssl_connection():
            connect_kwargs["ssl"] = get_ssl_context()
        connection = await connect(url, open_timeout=self.timeout, **connect_kwargs)

        event: Optional[str] = None
        while event is None or event in IGNORE_EVENTS_AT_STARTUP:
            data = await connection.recv()
            response = helper.process_api_response(data)
            event = response.get("event", "*")
            assert event
            self._websocket_event(event, response)

        if event == MS_CHANNEL_UNAUTHORIZED:
            await self.close()
            raise exceptions.UnauthorizedError(response)

        if event != MS_CHANNEL_CONNECT_EVENT:
            # Unexpected event received during connection routine
            await self.close()
            if event == MS_CHANNEL_TIMEOUT:
                _LOGGING.debug("connection not accepted on TV, or token missing/incorrect")
            raise exceptions.ConnectionFailure(response)

        self._check_for_token(response)

        self.connection = connection
        return connection

    async def start_listening(
        self, callback: Optional[Callable[[str, Any], Optional[Awaitable[None]]]] = None
    ) -> None:
        """Open, and start listening."""
        if not self._recv_loop:
            if not self.is_alive():
                self.connection = await self.open()

            self._recv_loop = asyncio.ensure_future(
                self._do_start_listening(callback, self.connection)
            )
            return True
        return False

    async def _do_start_listening(
        self,
        callback: Optional[Callable[[str, Any], Optional[Awaitable[None]]]],
        connection: WebSocketClientProtocol,
    ) -> None:
        """Do start listening."""
        _LOGGING.debug("Listening Connection Started")
        with contextlib.suppress(ConnectionClosed):
            while True:
                data = await connection.recv()
                response = helper.process_api_response(data)
                event = response.get("event", "*")
                self._websocket_event(event, response)
                if callback:
                    awaitable = callback(event, response)
                    if awaitable:
                        await awaitable
        _LOGGING.debug("Listening Connection closed")
        self._recv_loop = None

    async def close(self) -> None:
        if self.is_alive():
            await self.connection.close()
            if self._recv_loop:
                await self._recv_loop

        self.connection = None
        _LOGGING.debug("Connection closed.")

    async def send_commands(
        self,
        commands: Sequence[Union[SamsungTVCommand, Dict[str, Any]]],
        key_press_delay: Optional[float] = None,
    ) -> None:
        if not self.is_alive():
            self.connection = await self.open()

        delay = self.key_press_delay if key_press_delay is None else key_press_delay

        for command in commands:
            await self._send_command(self.connection, command, delay)

    async def send_command(
        self,
        command: Union[List[SamsungTVCommand], SamsungTVCommand, Dict[str, Any]],
        key_press_delay: Optional[float] = None,
    ) -> None:
        if isinstance(command, list):
            _LOGGING.warn(
                "Using send_command to send multiple commands is deprecated, "
                "please use send_commands."
            )
            await self.send_commands(command, key_press_delay)
            return

        await self.send_commands([command], key_press_delay)

    @staticmethod
    async def _send_command(
        connection: WebSocketClientProtocol,
        command: Union[SamsungTVCommand, Dict[str, Any]],
        delay: float,
    ) -> None:
        if isinstance(command, SamsungTVSleepCommand):
            await asyncio.sleep(command.delay)
            return

        if isinstance(command, SamsungTVCommand):
            payload = command.get_payload()
        else:
            payload = json.dumps(command)
        _LOGGING.debug("SamsungTVWS websocket command: %s", payload)
        await connection.send(payload)

        await asyncio.sleep(delay)

    def is_alive(self) -> bool:
        return self.connection is not None and self.connection.state is State.OPEN
