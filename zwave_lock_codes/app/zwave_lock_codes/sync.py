"""Write table changes to the locks, one slot at a time, only when asked."""

import asyncio
import logging
from typing import Callable, Optional

from . import zwave
from .ha import HAClient, HAError
from .store import ERROR, SYNCED, UNVERIFIED, WRITING, Store, lock_label

_LOGGER = logging.getLogger(__name__)

# What a slot write came to.
DONE = "done"
EDITED = "edited"
LOCK_DOWN = "lock_down"


class Syncer:
    """
    Runs at most one sync at a time.

    ``request()`` starts a sync, or, if one is running, makes it go round once
    more when it finishes, so edits saved mid-sync are never dropped. Between
    requests there is no task, timer or connection.

    Locks are written one after another over a single connection: they share
    one Z-Wave radio, so writing them in parallel would not be faster.
    """

    def __init__(
        self,
        store: Store,
        client_factory: Callable[[], HAClient] = HAClient,
    ) -> None:
        self.store = store
        self._client_factory = client_factory
        self._task: Optional[asyncio.Task] = None
        self._again = False
        self._io = asyncio.Lock()
        self._writing_device: Optional[str] = None
        self._refused = False
        self._devices: dict = {}
        self.last_error: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def request(self) -> None:
        if self.busy:
            self._again = True
        elif self.store.pending():
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
                # Home Assistant itself was unreachable; every lock waits.
                self.last_error = str(err)
                _LOGGER.error("Sync stopped: %s", err)
                for lock, slot in self.store.pending():
                    self.store.set_status(lock, slot, ERROR, str(err))
                self.store.save()
            if not self._again:
                return

    async def _sync_pending(self) -> None:
        if not self.store.pending():
            return
        async with self._io, self._client_factory() as ha:
            await ha.subscribe("zwave_js_notification", self._on_notification)
            for lock in self.store.locks:
                if self.store.pending(lock):
                    await self._sync_lock(ha, lock)

    async def _sync_lock(self, ha: HAClient, lock: str) -> None:
        try:
            device_id = await self._device_id(ha, lock)
            await self._learn_lock(ha, lock, device_id)
        except HAError as err:
            self._fail_lock(lock, str(err))
            return
        # One attempt per slot per pass; a failure waits for the next save.
        tried: set = set()
        while todo := [s for _, s in self.store.pending(lock) if s not in tried]:
            slot = todo[0]
            tried.add(slot)
            outcome = await self._write_slot(ha, lock, device_id, slot)
            if outcome == EDITED:
                tried.discard(slot)
            elif outcome == LOCK_DOWN:
                self._fail_lock(lock, f"Skipped: {lock_label(lock)} did not respond")
                return

    def _fail_lock(self, lock: str, message: str) -> None:
        _LOGGER.warning("%s: %s", lock, message)
        for _, slot in self.store.pending(lock):
            if self.store.status.get(lock, {}).get(slot, {}).get("state") != ERROR:
                self.store.set_status(lock, slot, ERROR, message)
        self.store.save()

    def _on_notification(self, event: dict) -> None:
        if self._writing_device and zwave.is_duplicate_refusal(event.get("data", {}), self._writing_device):
            self._refused = True

    async def _device_id(self, ha: HAClient, lock: str) -> str:
        if lock not in self._devices:
            device_id = await ha.device_id(lock)
            if not device_id:
                raise HAError(f"{lock} is not a Z-Wave device in Home Assistant")
            self._devices[lock] = device_id
        return self._devices[lock]

    async def _learn_lock(self, ha: HAClient, lock: str, device_id: str) -> None:
        """Fill in slot count and PIN length the first time; both are cache reads."""
        if not self.store.info(lock, "max_slots"):
            codes = await self._read_codes(ha, lock)
            if codes:
                self.store.set_max_slots(lock, max(int(k) for k in codes))
        if not self.store.info(lock, "pin_length"):
            await self._read_config(ha, lock, device_id)

    async def _read_codes(self, ha: HAClient, lock: str, slot: Optional[int] = None) -> dict:
        data = {} if slot is None else {"code_slot": slot}
        return await ha.call_service("zwave_js", "get_lock_usercode", data, lock, response=True) or {}

    # Lock configuration

    async def read_config(self, lock: str) -> list:
        """Return a lock's configuration parameters from Z-Wave JS's cache."""
        async with self._io, self._client_factory() as ha:
            return await self._read_config(ha, lock, await self._device_id(ha, lock))

    async def set_config(
        self, lock: str, property_: int, property_key: Optional[int], endpoint: int, value: int
    ) -> list:
        """Write one configuration parameter and return the parameters as they now read."""
        async with self._io, self._client_factory() as ha:
            device_id = await self._device_id(ha, lock)
            # Read first so a PIN length changed at the lock is noticed before we change it again.
            await self._read_config(ha, lock, device_id)
            payload = {
                "type": "zwave_js/set_config_parameter",
                "device_id": device_id,
                "property": property_,
                "endpoint": endpoint,
                "value": value,
            }
            if property_key is not None:
                payload["property_key"] = property_key
            result = await ha.command(payload)
            if (result or {}).get("status") in zwave.FAILED_SET_STATUSES:
                raise HAError("The lock did not accept the setting")
            _LOGGER.info("%s: set configuration parameter %s to %s", lock, property_, value)
            return await self._read_config(ha, lock, device_id)

    async def _read_config(self, ha: HAClient, lock: str, device_id: str) -> list:
        params = zwave.config_params(
            await ha.command({"type": "zwave_js/get_config_parameters", "device_id": device_id})
        )
        if self.store.set_pin_length(lock, zwave.pin_length_from(params)):
            # The lock may have erased its codes; write ours back once this job ends.
            _LOGGER.info("%s: PIN length is now %s; re-writing codes", lock, self.store.info(lock, "pin_length"))
            self.request()
        return params

    # Writing one slot

    async def _write_slot(self, ha: HAClient, lock: str, device_id: str, slot: int) -> str:
        """Write one slot on one lock and record the outcome."""
        store = self.store
        want = store.wanted(lock, slot)
        if want and (problem := store.length_problem(lock, want)):
            self._fail(lock, slot, problem)
            return DONE
        store.set_status(lock, slot, WRITING)
        store.save()
        self._refused = False
        self._writing_device = device_id
        write_error = None
        try:
            if want:
                await ha.call_service(
                    "zwave_js", "set_lock_usercode", {"code_slot": slot, "usercode": want}, lock
                )
            else:
                await ha.call_service("zwave_js", "clear_lock_usercode", {"code_slot": slot}, lock)
        except HAError as err:
            write_error = str(err)
        finally:
            self._writing_device = None

        # The driver verifies each write with a Get, so the cached value read
        # here is the lock's answer; this costs no extra radio traffic.
        codes = await self._read_codes(ha, lock, slot)
        entry = codes.get(str(slot)) or codes.get(slot) or {}
        reading = zwave.read_slot(entry.get("usercode"), entry.get("in_use"))
        _LOGGER.info("%s slot %s: %s -> %s", lock, slot, "set" if want else "clear", reading.kind)

        # Re-read the wish: the table may have been saved while we wrote.
        if want != store.wanted(lock, slot):
            return EDITED
        if want and self._refused:
            self._fail(lock, slot, "The lock refused this PIN because another slot already uses it")
        elif want and reading.kind == zwave.KNOWN and reading.pin == want:
            self._done(lock, slot, want, SYNCED)
        elif want and reading.kind == zwave.HIDDEN:
            # The lock hides codes, so an occupied slot is as far as we can see.
            self._done(lock, slot, want, UNVERIFIED, "Lock hides codes; the slot is occupied")
        elif not want and reading.kind == zwave.EMPTY:
            self._done(lock, slot, "", SYNCED)
        else:
            self._fail(lock, slot, write_error or f"Lock reports the slot as {reading.kind} after the write")
            if write_error:
                # The command itself failed (the node is dead, asleep or out of
                # range), so the rest of this lock's writes would only time out.
                return LOCK_DOWN
        return DONE

    def _done(self, lock: str, slot: int, pin: str, state: str, message: str = "") -> None:
        applied = self.store.applied.setdefault(lock, {})
        if pin:
            applied[slot] = pin
        else:
            applied.pop(slot, None)
        row = self.store.rows.get(slot)
        if row is not None and lock in row.get("locks", []):
            self.store.set_status(lock, slot, state, message)
        else:
            self.store.status.get(lock, {}).pop(slot, None)
        self.store.save()

    def _fail(self, lock: str, slot: int, message: str) -> None:
        _LOGGER.warning("%s slot %s: %s", lock, slot, message)
        self.store.set_status(lock, slot, ERROR, message)
        self.store.save()

    # Importing

    async def import_from_locks(self) -> dict:
        """
        Adopt codes already on the locks into the table.

        The same PIN in the same slot on several locks becomes one row ticked
        for each of them. A slot holding a different code than the table
        already has for that slot is reported, never guessed at.
        """
        report: dict = {"added": 0, "merged": 0, "conflicts": [], "errors": []}
        found: dict = {}
        async with self._io, self._client_factory() as ha:
            for lock in self.store.locks:
                try:
                    found[lock] = await self._read_codes(ha, lock)
                except HAError as err:
                    report["errors"].append(f"{lock_label(lock)}: {err}")
        store = self.store
        for lock, codes in found.items():
            if codes:
                store.set_max_slots(lock, max(int(k) for k in codes))
            for key, entry in sorted(codes.items(), key=lambda kv: int(kv[0])):
                slot = int(key)
                reading = zwave.read_slot(entry.get("usercode"), entry.get("in_use"))
                if reading.kind == zwave.EMPTY:
                    continue
                pin = reading.pin if reading.kind == zwave.KNOWN else None
                row = store.rows.get(slot)
                if row is None:
                    store.rows[slot] = {
                        "name": f"Slot {slot}",
                        "pin": pin,
                        "enabled": True,
                        "notes": "",
                        "locks": [lock],
                    }
                    report["added"] += 1
                elif lock in row["locks"]:
                    continue
                elif row["pin"] == pin:
                    row["locks"] = [name for name in store.locks if name in row["locks"] or name == lock]
                    report["merged"] += 1
                else:
                    report["conflicts"].append(
                        f"{lock_label(lock)} slot {slot} holds "
                        + ("a hidden code" if pin is None else "a different code than the table")
                        + f"; tick {lock_label(lock)} on that row to replace it with the table's PIN"
                    )
                    continue
                store.applied.setdefault(lock, {})[slot] = pin
                store.set_status(lock, slot, SYNCED if pin else UNVERIFIED, "" if pin else "Lock hides this code")
        store.save()
        return report
