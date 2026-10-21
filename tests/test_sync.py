"""Sync and table tests against a fake Z-Wave lock behind a fake Home Assistant."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "zwave_lock_codes" / "app"))

from zwave_lock_codes import zwave  # noqa: E402
from zwave_lock_codes.ha import HAError  # noqa: E402
from zwave_lock_codes.store import ERROR, SYNCED, UNVERIFIED, Store, ValidationError  # noqa: E402
from zwave_lock_codes.sync import Syncer  # noqa: E402

LOCK = "lock.front_door"


class FakeLock:
    def __init__(self, slots=30, masked=False):
        self.codes = {n: "" for n in range(1, slots + 1)}
        self.masked = masked
        self.writes = []
        self.sessions = 0
        self.fail_slots = set()


class FakeHA:
    """Stands in for HAClient; records every write the add-on makes."""

    def __init__(self, lock):
        self.lock = lock
        self.handler = None

    async def __aenter__(self):
        self.lock.sessions += 1
        return self

    async def __aexit__(self, *exc):
        pass

    async def device_id(self, entity_id):
        return "dev1"

    async def subscribe(self, event_type, handler):
        self.handler = handler

    def _entry(self, slot):
        code = self.lock.codes[slot]
        shown = "*" * len(code) if (code and self.lock.masked) else code
        return {"usercode": shown, "in_use": bool(code)}

    async def call_service(self, domain, service, data, entity_id, response=False):
        assert entity_id == LOCK
        slot = data.get("code_slot")
        if service == "get_lock_usercode":
            slots = [slot] if slot else self.lock.codes
            return {str(s): self._entry(s) for s in slots}
        self.lock.writes.append((service, slot))
        if slot in self.lock.fail_slots:
            raise HAError("node timed out")
        if service == "set_lock_usercode":
            pin = data["usercode"]
            if any(c == pin for s, c in self.lock.codes.items() if s != slot):
                self.handler({"data": {"command_class": 113, "type": 6, "event": 15, "device_id": "dev1"}})
                return None
            self.lock.codes[slot] = pin
        else:
            self.lock.codes[slot] = ""
        return None


def make(tmp_path, **lock_kw):
    lock = FakeLock(**lock_kw)
    store = Store(str(tmp_path / "codes.json"))
    return lock, store, Syncer(store, LOCK, lambda: FakeHA(lock))


def save(store, syncer, rows):
    store.replace_rows(rows)
    syncer.request()
    return syncer.wait()


def row(slot, pin, name="", enabled=True):
    return {"slot": slot, "name": name, "pin": pin, "enabled": enabled}


def test_writes_only_changed_slots(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path)
        await save(store, syncer, [row(1, "1111"), row(2, "2222")])
        assert lock.codes[1] == "1111" and lock.codes[2] == "2222"
        assert store.status[1]["state"] == SYNCED
        lock.writes.clear()

        await save(store, syncer, [row(1, "1111", name="renamed"), row(2, "3333")])
        assert lock.writes == [("set_lock_usercode", 2)]

        sessions = lock.sessions
        await save(store, syncer, [row(1, "1111", name="again"), row(2, "3333")])
        assert lock.sessions == sessions, "a name-only change must not touch the lock"

    asyncio.run(go())


def test_disable_and_delete_clear_the_slot(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path)
        await save(store, syncer, [row(1, "1111"), row(2, "2222")])
        await save(store, syncer, [row(1, "1111", enabled=False)])
        assert lock.codes[1] == "" and lock.codes[2] == ""
        assert 2 not in store.status and store.snapshot()["rows"][0]["slot"] == 1
        lock.writes.clear()
        await save(store, syncer, [row(1, "1111")])
        assert lock.writes == [("set_lock_usercode", 1)] and lock.codes[1] == "1111"

    asyncio.run(go())


def test_never_clears_codes_it_did_not_write(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path)
        lock.codes[5] = "5555"
        await save(store, syncer, [row(1, "1111")])
        await save(store, syncer, [])
        assert lock.codes[5] == "5555"
        assert ("clear_lock_usercode", 5) not in lock.writes

    asyncio.run(go())


def test_duplicate_refusal_is_reported(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path)
        lock.codes[9] = "1234"  # someone else's code, not in the table
        await save(store, syncer, [row(1, "1234")])
        assert store.status[1]["state"] == ERROR
        assert "another slot" in store.status[1]["message"]
        assert 1 not in store.applied

    asyncio.run(go())


def test_masked_lock_counts_occupied_as_written(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path, masked=True)
        await save(store, syncer, [row(1, "1111")])
        assert store.status[1]["state"] == UNVERIFIED and store.applied[1] == "1111"

    asyncio.run(go())


def test_failed_slot_does_not_block_others_or_loop(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path)
        lock.fail_slots = {1}
        await save(store, syncer, [row(1, "1111"), row(2, "2222")])
        assert store.status[1]["state"] == ERROR and "timed out" in store.status[1]["message"]
        assert store.status[2]["state"] == SYNCED
        assert lock.writes.count(("set_lock_usercode", 1)) == 1

        lock.fail_slots = set()
        syncer.request()
        await syncer.wait()
        assert store.status[1]["state"] == SYNCED

    asyncio.run(go())


def test_import_adopts_existing_codes(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path)
        lock.codes[3] = "3333"
        assert await syncer.import_from_lock() == 1
        assert store.rows[3]["pin"] == "3333" and store.max_slots == 30
        assert store.pending_slots() == []
        # Deleting an imported row clears it from the lock.
        await save(store, syncer, [])
        assert lock.codes[3] == ""

    asyncio.run(go())


def test_import_hidden_code_is_left_alone_until_given_a_pin(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path, masked=True)
        lock.codes[4] = "4444"
        await syncer.import_from_lock()
        assert store.rows[4]["pin"] is None
        # Saving the row back untouched (UI sends "") keeps the hidden code.
        await save(store, syncer, [row(4, "", name="Dog walker")])
        assert lock.writes == [] and store.rows[4]["pin"] is None
        await save(store, syncer, [row(4, "4545")])
        assert lock.codes[4] == "4545"

    asyncio.run(go())


def test_persists_across_restart(tmp_path):
    async def go():
        lock, store, syncer = make(tmp_path)
        await save(store, syncer, [row(1, "1111", name="Alice")])
        again = Store(str(tmp_path / "codes.json"))
        assert again.rows[1]["name"] == "Alice" and again.applied[1] == "1111"
        assert again.pending_slots() == []

    asyncio.run(go())


@pytest.mark.parametrize(
    "rows, slot, text",
    [
        ([row(1, "12a4")], "1", "4–8 digits"),
        ([row(1, "123")], "1", "4–8 digits"),
        ([row(1, "1111"), row(2, "1111")], "2", "Same PIN as slot 1"),
        ([row(1, "1111"), row(2, "2222"), row(3, "333333")], "3", "4 digits long"),
        ([row(0, "1111")], "0", "Slot must be"),
        ([row(1, "1111"), row(1, "2222")], "1", "used twice"),
    ],
)
def test_validation(tmp_path, rows, slot, text):
    store = Store(str(tmp_path / "codes.json"))
    with pytest.raises(ValidationError) as err:
        store.replace_rows(rows)
    assert text in err.value.errors[slot]


@pytest.mark.parametrize(
    "usercode, in_use, kind",
    [
        ("1234", True, zwave.KNOWN),
        ("****", True, zwave.HIDDEN),
        ("****", None, zwave.HIDDEN),
        ("****", False, zwave.EMPTY),
        ("0000", False, zwave.EMPTY),
        ("0000", None, zwave.KNOWN),
        ("", True, zwave.HIDDEN),
        ("", False, zwave.EMPTY),
        (None, None, zwave.EMPTY),
    ],
)
def test_read_slot(usercode, in_use, kind):
    assert zwave.read_slot(usercode, in_use).kind == kind
