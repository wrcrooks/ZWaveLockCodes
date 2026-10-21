"""Minimal Home Assistant WebSocket client, opened only for the duration of a job."""

import asyncio
import logging
import os
from typing import Any, Callable, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)


class HAError(Exception):
    """A Home Assistant command failed or the connection was lost."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


def _endpoint() -> tuple:
    """Return (websocket URL, token): Supervisor when present, else HA_URL/HA_TOKEN."""
    if token := os.environ.get("SUPERVISOR_TOKEN"):
        return "ws://supervisor/core/websocket", token
    url = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
    return url.replace("http", "ws", 1) + "/api/websocket", os.environ.get("HA_TOKEN", "")


class HAClient:
    """
    One authenticated WebSocket session.

    Use as ``async with HAClient() as ha``. Nothing stays connected between
    jobs, so an idle add-on holds no socket and runs no tasks.
    """

    def __init__(self, timeout: float = 60) -> None:
        self._timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._reader: Optional[asyncio.Task] = None
        self._next_id = 1
        self._pending: dict = {}
        self._event_handlers: dict = {}

    async def __aenter__(self) -> "HAClient":
        url, token = _endpoint()
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(url, heartbeat=30, max_msg_size=0)
            msg = await self._ws.receive_json(timeout=10)
            if msg.get("type") != "auth_required":
                raise HAError(f"unexpected greeting: {msg.get('type')}")
            await self._ws.send_json({"type": "auth", "access_token": token})
            msg = await self._ws.receive_json(timeout=10)
            if msg.get("type") != "auth_ok":
                raise HAError(f"authentication failed: {msg.get('message', msg.get('type'))}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            await self._session.close()
            raise HAError(f"cannot reach Home Assistant: {err}") from err
        except BaseException:
            await self._session.close()
            raise
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def _read_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.json()
                if data.get("type") == "event":
                    if handler := self._event_handlers.get(data.get("id")):
                        handler(data["event"])
                elif (fut := self._pending.pop(data.get("id"), None)) and not fut.done():
                    fut.set_result(data)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError("connection to Home Assistant closed"))
            self._pending.clear()

    async def command(self, payload: dict) -> Any:
        """Send one command and return its result, raising HAError on failure."""
        msg_id = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({"id": msg_id, **payload})
        try:
            reply = await asyncio.wait_for(fut, self._timeout)
        except asyncio.TimeoutError as err:
            self._pending.pop(msg_id, None)
            raise HAError(f"{payload.get('type')} timed out") from err
        if not reply.get("success"):
            error = reply.get("error") or {}
            raise HAError(error.get("message") or error.get("code") or "unknown error", error.get("code"))
        return reply.get("result")

    async def call_service(
        self, domain: str, service: str, data: dict, entity_id: str, response: bool = False
    ) -> Any:
        """Call an action on one entity; return its response data when asked."""
        payload = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": data,
            "target": {"entity_id": entity_id},
        }
        if response:
            payload["return_response"] = True
        result = await self.command(payload)
        if not response:
            return None
        data = (result or {}).get("response") or {}
        # Entity-targeted actions key their response by entity ID.
        return data.get(entity_id, data)

    async def subscribe(self, event_type: str, handler: Callable[[dict], None]) -> None:
        """Deliver events of one type to handler until the session closes."""
        msg_id = self._next_id
        self._event_handlers[msg_id] = handler
        await self.command({"type": "subscribe_events", "event_type": event_type})

    async def device_id(self, entity_id: str) -> Optional[str]:
        """Return the device an entity belongs to, if any."""
        entry = await self.command({"type": "config/entity_registry/get", "entity_id": entity_id})
        return (entry or {}).get("device_id")
