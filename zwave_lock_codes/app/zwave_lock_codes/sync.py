"""Write table changes to the lock, one slot at a time, only when asked."""

import asyncio
import logging
from typing import Callable, Optional

from . import zwave
from .ha import HAClient, HAError
from .store import ERROR, SYNCED, UNVERIFIED, WRITING, Store, wanted_pin

_LOGGER = logging.getLogger(__name__)


class Syncer:
    """
    Runs at most one sync at a time.

    ``request()`` starts a sync, or, if one is running, makes it go round once
    more when it finishes, so edits saved mid-sync are never dropped. Between
    requests there is no task, timer or connection.
    """

    def __init__(
        self,
        store: Store,
        lock_entity: str,
        client_factory: Callable[[], HAClient] = HAClient,
    ) -> None:
        self.store = store
        self.lock_entity = lock_entity
        self._client_factory = client_factory
        self._task: Optional[asyncio.Task] = None
        self._again = False
        self._io = asyncio.Lock()
        self._refused = False
        self.last_error: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def request(self) -> None:
        if self.busy:
            self._again = True
        elif self.store.pending_slots():
            self._task = asyncio.create_task(self._run())

    async def wait(self) -> None:
        while self.busy:
            await asyncio.shield(self._task)

    async def _run(self) -> None:
        while True:
            self._again = False
            try:
                await self._sync_pending()
                self.last_error = None
            except HAError as err:
                self.last_error = str(err)
                _LOGGER.error("Sync stopped: %s", err)
                for slot in self.store.pending_slots():
                    self.store.set_status(slot, ERROR, str(err))
                self.store.save()
            if not self._again:
                return

    async def _sync_pending(self) -> None:
        if not self.store.pending_slots():
            return
        async with self._io, self._client_factory() as ha:
            device_id = await ha.device_id(self.lock_entity)
            await ha.subscribe("zwave_js_notification", lambda e: self._on_notification(e, device_id))
            await self._learn_slot_count(ha)
            # One attempt per slot per pass; a failure waits for the next save.
            tried: set = set()
            while todo := [s for s in self.store.pending_slots() if s not in tried]:
                tried.add(todo[0])
                if await self._write_slot(ha, todo[0]) is False:
                    tried.discard(todo[0])

    def _on_notification(self, event: dict, device_id: Optional[str]) -> None:
        if zwave.is_duplicate_refusal(event.get("data", {}), device_id):
            self._refused = True

    async def _learn_slot_count(self, ha: HAClient) -> None:
        if self.store.max_slots:
            return
        codes = await ha.call_service(
            "zwave_js", "get_lock_usercode", {}, self.lock_entity, response=True
        )
        if codes:
            self.store.max_slots = max(int(k) for k in codes)

    async def _write_slot(self, ha: HAClient, slot: int) -> bool:
        """Write one slot and record the outcome; False if it was edited meanwhile."""
        store = self.store
        want = wanted_pin(store.rows.get(slot))
        store.set_status(slot, WRITING)
        store.save()
        self._refused = False
        write_error = None
        try:
            if want:
                await ha.call_service(
                    "zwave_js",
                    "set_lock_usercode",
                    {"code_slot": slot, "usercode": want},
                    self.lock_entity,
                )
            else:
                await ha.call_service(
                    "zwave_js", "clear_lock_usercode", {"code_slot": slot}, self.lock_entity
                )
        except HAError as err:
            write_error = str(err)

        # The driver verifies each write with a Get, so the cached value read
        # here is the lock's answer; this costs no extra radio traffic.
        codes = await ha.call_service(
            "zwave_js", "get_lock_usercode", {"code_slot": slot}, self.lock_entity, response=True
        )
        entry = codes.get(str(slot)) or codes.get(slot) or {}
        reading = zwave.read_slot(entry.get("usercode"), entry.get("in_use"))
        _LOGGER.info("Slot %s: %s -> %s", slot, "set" if want else "clear", reading.kind)

        # Re-read the wish: the table may have been saved while we wrote.
        if want != wanted_pin(store.rows.get(slot)):
            return False
        if want and self._refused:
            self._fail(slot, "The lock refused this PIN because another slot already uses it")
        elif want and reading.kind == zwave.KNOWN and reading.pin == want:
            self._done(slot, want, SYNCED)
        elif want and reading.kind == zwave.HIDDEN:
            # The lock hides codes, so an occupied slot is as far as we can see.
            self._done(slot, want, UNVERIFIED, "Lock hides codes; the slot is occupied")
        elif not want and reading.kind == zwave.EMPTY:
            self._done(slot, "", SYNCED)
        else:
            self._fail(slot, write_error or f"Lock reports the slot as {reading.kind} after the write")
        return True

    def _done(self, slot: int, pin: str, state: str, message: str = "") -> None:
        if pin:
            self.store.applied[slot] = pin
        else:
            self.store.applied.pop(slot, None)
        if slot in self.store.rows:
            self.store.set_status(slot, state, message)
        else:
            self.store.status.pop(slot, None)
        self.store.save()

    def _fail(self, slot: int, message: str) -> None:
        _LOGGER.warning("Slot %s: %s", slot, message)
        self.store.set_status(slot, ERROR, message)
        self.store.save()

    async def import_from_lock(self) -> int:
        """Adopt codes already on the lock into empty table rows; return how many."""
        async with self._io, self._client_factory() as ha:
            codes = await ha.call_service(
                "zwave_js", "get_lock_usercode", {}, self.lock_entity, response=True
            )
        store = self.store
        if codes:
            store.max_slots = max(int(k) for k in codes)
        added = 0
        for key, entry in sorted((codes or {}).items(), key=lambda kv: int(kv[0])):
            slot = int(key)
            reading = zwave.read_slot(entry.get("usercode"), entry.get("in_use"))
            if reading.kind == zwave.EMPTY or slot in store.rows:
                continue
            pin = reading.pin if reading.kind == zwave.KNOWN else None
            store.rows[slot] = {"name": f"Slot {slot}", "pin": pin, "enabled": True, "notes": ""}
            store.applied[slot] = pin
            store.set_status(slot, SYNCED if pin else UNVERIFIED, "" if pin else "Lock hides this code")
            added += 1
        store.save()
        return added

