"""FIX TCP transport: socket, initiator, listener, server."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Awaitable

from mkfix.fix.message import FixMessage, FixMessageFactory, SOH
from mkfix.fix.parser import FixStreamParser

if TYPE_CHECKING:
    from mkfix.fix.session import FixSession

CONNECTION_RETRY_LIMIT = 30 * 60


class FixSocket:
    """Abstraction around an asyncio TCP connection for FIX messages."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        factory: FixMessageFactory,
    ):
        self.reader = reader
        self.writer = writer
        self.factory = factory
        self._parser = FixStreamParser(reader)
        self._closed = False

    async def read(self) -> FixMessage:
        return await self._parser.read_message()

    async def write(self, msg: FixMessage, seq_num: int) -> FixMessage:
        msg.sendprep(self.factory.dictionary, self.factory.sender, self.factory.target, seq_num,
                     timestamp_precision=self.factory.timestamp_precision)
        msg.raw = msg.serialize()
        self.writer.write(msg.raw)
        await self.writer.drain()
        return msg

    async def write_prepared(self, msg: FixMessage) -> FixMessage:
        """Write a message whose wire pairs are already final (a PossDup
        retransmission) — no sendprep, no sequence stamping."""
        if msg.raw is None:
            msg.raw = msg.serialize()
        self.writer.write(msg.raw)
        await self.writer.drain()
        return msg

    async def write_raw(self, raw: bytes) -> None:
        self.writer.write(raw)
        await self.writer.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    @property
    def closed(self) -> bool:
        return self._closed


class FixInitiator:
    """Initiates a TCP connection to a remote FIX acceptor."""

    def __init__(self, session: FixSession):
        self.session = session
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _connect_loop(self) -> None:
        host = self.session.config["host"]
        port = self.session.config["port"]
        attempts = 0

        await self.session.set_status("INITIATING")

        while not self._stopping:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                sock = FixSocket(reader, writer, self.session.factory)
                await self.session.on_connect(sock, is_initiator=True)
                return
            except OSError:
                attempts += 1
                if attempts >= CONNECTION_RETRY_LIMIT:
                    await self.session.set_status("ERROR", "Connection retry limit exceeded")
                    self.session.detach_transport(self)
                    return
                await asyncio.sleep(1)


class FixListener:
    """Registers with a shared FixServer to accept inbound connections."""

    def __init__(self, session: FixSession):
        self.session = session

    async def start(self) -> None:
        port = self.session.config["port"]
        sender = self.session.config["sender_comp_id"]
        target = self.session.config["target_comp_id"]

        server = FixServer.get_or_create(port)
        await server.add_listener(sender, target, self.session)
        await self.session.set_status("LISTENING")

    async def stop(self) -> None:
        port = self.session.config["port"]
        sender = self.session.config["sender_comp_id"]
        target = self.session.config["target_comp_id"]

        server = FixServer.get_by_port(port)
        if server:
            await server.remove_listener(sender, target)


class FixServer:
    """Shared TCP server that dispatches connections to FixListeners by CompID pair.

    Multiple sessions can listen on the same port with different CompIDs.
    """

    _servers: dict[int, FixServer] = {}

    def __init__(self, port: int):
        self.port = port
        self._server: asyncio.Server | None = None
        self._listeners: dict[str, FixSession] = {}

    @classmethod
    def get_or_create(cls, port: int) -> FixServer:
        if port not in cls._servers:
            cls._servers[port] = FixServer(port)
        return cls._servers[port]

    @classmethod
    def get_by_port(cls, port: int) -> FixServer | None:
        return cls._servers.get(port)

    async def add_listener(self, local_comp: str, remote_comp: str, session: FixSession) -> None:
        key = f"{local_comp}\x01{remote_comp}"
        self._listeners[key] = session

        if not self._server:
            self._server = await asyncio.start_server(self._on_connect, port=self.port)

    async def remove_listener(self, local_comp: str, remote_comp: str) -> None:
        key = f"{local_comp}\x01{remote_comp}"
        self._listeners.pop(key, None)

        if not self._listeners and self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
            self._servers.pop(self.port, None)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser = FixStreamParser(reader)

        try:
            msg = await asyncio.wait_for(parser.read_message(), timeout=10)
        except (asyncio.TimeoutError, ConnectionError):
            writer.close()
            return

        local_comp = msg["56"] or ""
        remote_comp = msg["49"] or ""
        key = f"{local_comp}\x01{remote_comp}"

        session = self._listeners.get(key)
        if not session:
            # Unknown session — send logout and close
            fix_version = msg["8"] or "FIX.4.2"
            reject = (
                f"8={fix_version}{SOH}9=0{SOH}35=5{SOH}"
                f"49={local_comp}{SOH}56={remote_comp}{SOH}"
                f"58=Unexpected session: {local_comp}<>{remote_comp}{SOH}10=000{SOH}"
            )
            writer.write(reject.encode())
            await writer.drain()
            writer.close()
            return

        sock = FixSocket(reader, writer, session.factory)
        # Push the already-read logon message back for the session to process
        sock._parser._buffer = (msg.raw or msg.serialize()) + sock._parser._buffer
        await session.on_connect(sock, is_initiator=False)
