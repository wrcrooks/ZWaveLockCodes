"""Sync and table tests against fake Z-Wave locks behind a fake Home Assistant."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "zwave_lock_codes" / "app"))

from zwave_lock_codes import zwave  # noqa: E402
from zwave_lock_codes.__main__ import configured_locks  # noqa: E402
from zwave_lock_codes.ha import HAError  # noqa: E402
from zwave_lock_codes.store import ERROR, SKIPPED, SYNCED, UNVERIFIED, Store, ValidationError  # noqa: E402
from zwave_lock_codes.sync import Syncer  # noqa: E402

FRONT = "lock.front_door"
BACK = "lock.back_door"


class FakeLock:
    def __init__(self, device_id, slots=30, masked=False, pin_length=4):
        self.device_id = device_id
        self.codes = {n: "" for n in range(1, slots + 1)}
        self.masked = masked
        self.writes = []
        self.fail_slots = set()
        self.dead = False
        # Configuration parameters as Z-Wave JS reports them for a BE469ZP.
        self.config = {4: 0, 15: 0, 16: pin_length}
        self.reject_config = False


class FakeHA:
    """Stands in for HAClient; records every write the add-on makes."""

    sessions = 0

    def __init__(self, locks):
        self.locks = locks
        self.handler = None

    async def __aenter__(self):
        FakeHA.sessions += 1
        return self

    async def __aexit__(self, *exc):
        pass

    async def device_id(self, entity_id):
        return self.locks[entity_id].device_id if entity_id in self.locks else None

    async def subscribe(self, event_type, handler):
        self.handler = handler

    def _by_device(self, device_id):
        return next(lock for lock in self.locks.values() if lock.device_id == device_id)

    async def command(self, payload):
        lock = self._by_device(payload["device_id"])
        if payload["type"] == "zwave_js/get_config_parameters":
            labels = {4: "Vacation Mode", 15: "Auto Lock", 16: "User Code PIN Length"}
            return {
                f"7-112-0-{n}": {
                    "property": n,
                    "property_key": None,
                    "endpoint": 0,
                    "value": value,
                    "metadata": {"label": labels[n], "writeable": True, "min": 0, "max": 255,
                                 **({} if n == 16 else {"states": {"0": "Disable", "255": "Enable"}})},
                }
                for n, value in lock.config.items()
            }
        if payload["type"] == "zwave_js/set_config_parameter":
            if lock.reject_config:
                return {"status": 5}
            n, value = payload["property"], payload["value"]
            if n == 16 and value != lock.config[16]:
                lock.codes = {s: "" for s in lock.codes}  # worst case: the lock wipes its codes
            lock.config[n] = value
            return {"status": 255}
        raise AssertionError(payload)

    def _entry(self, lock, slot):
        code = lock.codes[slot]
        shown = "*" * len(code) if (code and lock.masked) else code
        return {"usercode": shown, "in_use": bool(code)}

    async def call_service(self, domain, service, data, entity_id, response=False):
        lock = self.locks[entity_id]
        slot = data.get("code_slot")
        if service == "get_lock_usercode":
            slots = [slot] if slot else lock.codes
            return {str(s): self._entry(lock, s) for s in slots}
        lock.writes.append((service, slot))
        if lock.dead or slot in lock.fail_slots:
            raise HAError("node timed out")
        if service == "set_lock_usercode":
            pin = data["usercode"]
            if any(c == pin for s, c in lock.codes.items() if s != slot):
                event = {"command_class": 113, "type": 6, "event": 15, "device_id": lock.device_id}
                self.handler({"data": event})
                return None
            lock.codes[slot] = pin
        else:
            lock.codes[slot] = ""
        return None


def make(tmp_path, *specs):
    """Build a store and syncer over fake locks: make(tmp_path, (FRONT, {...}), ...)."""
    specs = specs or ((FRONT, {}),)
    locks = {entity: FakeLock(f"dev-{entity}", **kw) for entity, kw in specs}
    store = Store(str(tmp_path / "codes.json"), list(locks))
    syncer = Syncer(store, lambda: FakeHA(locks))
    return locks, store, syncer


def save(store, syncer, rows):
    store.replace_rows(rows)
    syncer.request()
    return syncer.wait()


def row(slot, pin, name="", enabled=True, locks=(FRONT,)):
    return {"slot": slot, "name": name, "pin": pin, "enabled": enabled, "locks": list(locks)}


BOTH = (FRONT, BACK)


# Single lock


def test_writes_only_changed_slots(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path)
        lock = locks[FRONT]
        await save(store, syncer, [row(1, "1111"), row(2, "2222")])
        assert lock.codes[1] == "1111" and lock.codes[2] == "2222"
        assert store.status[FRONT][1]["state"] == SYNCED
        lock.writes.clear()

        await save(store, syncer, [row(1, "1111", name="renamed"), row(2, "3333")])
        assert lock.writes == [("set_lock_usercode", 2)]

        sessions = FakeHA.sessions
        await save(store, syncer, [row(1, "1111", name="again"), row(2, "3333")])
        assert FakeHA.sessions == sessions, "a name-only change must not touch the lock"

    asyncio.run(go())


def test_disable_and_delete_clear_the_slot(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path)
        lock = locks[FRONT]
        await save(store, syncer, [row(1, "1111"), row(2, "2222")])
        await save(store, syncer, [row(1, "1111", enabled=False)])
        assert lock.codes[1] == "" and lock.codes[2] == ""
        assert 2 not in store.status[FRONT] and [r["slot"] for r in store.snapshot()["rows"]] == [1]
        lock.writes.clear()
        await save(store, syncer, [row(1, "1111")])
        assert lock.writes == [("set_lock_usercode", 1)] and lock.codes[1] == "1111"

    asyncio.run(go())


def test_never_clears_codes_it_did_not_write(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path)
        locks[FRONT].codes[5] = "5555"
        await save(store, syncer, [row(1, "1111")])
        await save(store, syncer, [])
        assert locks[FRONT].codes[5] == "5555"
        assert ("clear_lock_usercode", 5) not in locks[FRONT].writes

    asyncio.run(go())


def test_duplicate_refusal_is_reported(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path)
        locks[FRONT].codes[9] = "1234"  # someone else's code, not in the table
        await save(store, syncer, [row(1, "1234")])
        assert store.status[FRONT][1]["state"] == ERROR
        assert "another slot" in store.status[FRONT][1]["message"]
        assert 1 not in store.applied[FRONT]

    asyncio.run(go())


def test_masked_lock_counts_occupied_as_written(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {"masked": True}))
        await save(store, syncer, [row(1, "1111")])
        assert store.status[FRONT][1]["state"] == UNVERIFIED and store.applied[FRONT][1] == "1111"

    asyncio.run(go())


def test_failed_slot_skips_rest_of_that_lock_once(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path)
        lock = locks[FRONT]
        lock.fail_slots = {1}
        await save(store, syncer, [row(1, "1111"), row(2, "2222")])
        assert store.status[FRONT][1]["state"] == ERROR and "timed out" in store.status[FRONT][1]["message"]
        assert store.status[FRONT][2]["state"] == ERROR and "did not respond" in store.status[FRONT][2]["message"]
        assert lock.writes == [("set_lock_usercode", 1)]

        lock.fail_slots = set()
        syncer.request()
        await syncer.wait()
        assert store.status[FRONT][1]["state"] == SYNCED and store.status[FRONT][2]["state"] == SYNCED

    asyncio.run(go())


def test_persists_across_restart(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path)
        await save(store, syncer, [row(1, "1111", name="Alice")])
        again = Store(str(tmp_path / "codes.json"), [FRONT])
        assert again.rows[1]["name"] == "Alice" and again.applied[FRONT][1] == "1111"
        assert again.pending() == []

    asyncio.run(go())


def test_migrates_single_lock_file(tmp_path):
    path = tmp_path / "codes.json"
    path.write_text(json.dumps({
        "rows": {"3": {"name": "Alice", "pin": "3333", "enabled": True, "notes": ""}},
        "applied": {"3": "3333"},
        "status": {"3": {"state": "synced", "message": "", "at": 0}},
        "max_slots": 30,
        "pin_length": 4,
    }))
    store = Store(str(path), [FRONT, BACK])
    assert store.rows[3]["locks"] == [FRONT]
    assert store.applied[FRONT] == {3: "3333"} and store.applied[BACK] == {}
    assert store.info(FRONT, "pin_length") == 4 and store.pending() == []


def test_configured_locks_keeps_old_lock_first():
    options = {"lock_entity": "lock.front_door", "locks": ["lock.back_door", "lock.front_door"]}
    assert configured_locks(options) == ["lock.front_door", "lock.back_door"]
    assert configured_locks({"locks": []}) == []


# Several locks


def test_codes_go_only_to_ticked_locks(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {}), (BACK, {}))
        await save(store, syncer, [row(1, "1111", locks=BOTH), row(2, "2222", locks=(BACK,))])
        assert locks[FRONT].codes[1] == "1111" and locks[BACK].codes[1] == "1111"
        assert locks[FRONT].codes[2] == "" and locks[BACK].codes[2] == "2222"
        assert locks[FRONT].writes == [("set_lock_usercode", 1)]

        # Unticking a lock clears the code there and nowhere else.
        await save(store, syncer, [row(1, "1111", locks=(BACK,)), row(2, "2222", locks=(BACK,))])
        assert locks[FRONT].codes[1] == "" and locks[BACK].codes[1] == "1111"
        assert 1 not in store.applied[FRONT]
        snap = {r["slot"]: r for r in store.snapshot()["rows"]}
        assert set(snap[1]["status"]) == {BACK}

    asyncio.run(go())


def test_validation_is_per_lock(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {}), (BACK, {"pin_length": 6, "slots": 10}))
        await save(store, syncer, [row(1, "1111"), row(2, "222222", locks=(BACK,))])
        assert store.info(BACK, "pin_length") == 6 and store.info(BACK, "max_slots") == 10

        with pytest.raises(ValidationError) as err:
            store.replace_rows([row(1, "1111", locks=BOTH)])
        assert "6 digits for Back door" in err.value.errors["1"]

        with pytest.raises(ValidationError) as err:
            store.replace_rows([row(12, "123456", locks=(BACK,))])
        assert "Back door has slots 1–10" in err.value.errors["12"]

        # The same PIN can't be on one lock twice.
        with pytest.raises(ValidationError) as err:
            store.replace_rows([row(1, "222222", locks=(BACK,)), row(2, "222222", locks=(BACK,))])
        assert "Same PIN as slot 1" in err.value.errors["2"]

    asyncio.run(go())


def test_dead_lock_does_not_block_the_other(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {}), (BACK, {}))
        locks[FRONT].dead = True
        await save(store, syncer, [row(1, "1111", locks=BOTH), row(2, "2222", locks=BOTH)])
        assert locks[BACK].codes[1] == "1111" and locks[BACK].codes[2] == "2222"
        assert store.status[FRONT][1]["state"] == ERROR and store.status[FRONT][2]["state"] == ERROR
        assert len(locks[FRONT].writes) == 1, "the dead lock is tried once, not slot after slot"

    asyncio.run(go())


def test_unknown_entity_fails_only_that_lock(tmp_path):
    async def go():
        locks = {BACK: FakeLock("dev-back")}
        store = Store(str(tmp_path / "codes.json"), [FRONT, BACK])
        syncer = Syncer(store, lambda: FakeHA(locks))  # FRONT has no device
        await save(store, syncer, [row(1, "1111", locks=BOTH)])
        assert "not a Z-Wave device" in store.status[FRONT][1]["message"]
        assert locks[BACK].codes[1] == "1111"

    asyncio.run(go())


def test_duplicate_refusal_is_pinned_on_the_right_lock(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {}), (BACK, {}))
        locks[BACK].codes[9] = "1234"
        await save(store, syncer, [row(1, "1234", locks=BOTH)])
        assert store.status[FRONT][1]["state"] == SYNCED
        assert store.status[BACK][1]["state"] == ERROR

    asyncio.run(go())


def test_pin_length_change_rewrites_only_that_lock(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {}), (BACK, {}))
        await save(store, syncer, [row(1, "1111", locks=BOTH), row(2, "22222", locks=())])
        locks[FRONT].writes.clear()
        locks[BACK].writes.clear()

        await syncer.set_config(BACK, 16, None, 0, 5)
        await syncer.wait()
        assert locks[FRONT].writes == [] and locks[FRONT].codes[1] == "1111"
        assert "5 digits for Back door" in store.status[BACK][1]["message"]

        await save(store, syncer, [row(1, "1111", locks=(FRONT,)), row(2, "22222", locks=(BACK,))])
        assert locks[BACK].codes[2] == "22222" and locks[FRONT].writes == []

    asyncio.run(go())


def test_pin_length_change_restores_matching_codes(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {"pin_length": 5}))
        await save(store, syncer, [row(1, "11111")])
        locks[FRONT].config[16] = 4  # changed at the lock, back to 4 digits
        await syncer.set_config(FRONT, 16, None, 0, 5)  # and back to 5 here: the lock wipes
        await syncer.wait()
        assert locks[FRONT].codes[1] == "11111" and store.status[FRONT][1]["state"] == SYNCED

    asyncio.run(go())


def test_config_write_rejected(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path)
        locks[FRONT].reject_config = True
        with pytest.raises(HAError):
            await syncer.set_config(FRONT, 4, None, 0, 255)
        assert locks[FRONT].config[4] == 0

    asyncio.run(go())


# Import


def test_import_merges_matching_codes_across_locks(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {}), (BACK, {}))
        locks[FRONT].codes.update({1: "1111", 3: "3333"})
        locks[BACK].codes.update({1: "1111", 3: "9999", 4: "4444"})
        report = await syncer.import_from_locks()
        assert report["added"] == 3 and report["merged"] == 1
        assert store.rows[1]["locks"] == [FRONT, BACK]
        assert store.rows[3]["locks"] == [FRONT] and store.rows[4]["locks"] == [BACK]
        assert report["conflicts"] == [
            "Back door slot 3 holds a different code than the table; "
            "tick Back door on that row to replace it with the table's PIN"
        ]
        assert store.pending() == []

        # Deleting an imported row clears it from every lock that had it.
        await save(store, syncer, [row(3, "3333"), row(4, "4444", locks=(BACK,))])
        assert locks[FRONT].codes[1] == "" and locks[BACK].codes[1] == ""
        assert locks[BACK].codes[3] == "9999", "the conflicting code was never ours"

    asyncio.run(go())


def test_import_hidden_code_is_left_alone_until_given_a_pin(tmp_path):
    async def go():
        locks, store, syncer = make(tmp_path, (FRONT, {"masked": True}), (BACK, {}))
        locks[FRONT].codes[4] = "4444"
        await syncer.import_from_locks()
        assert store.rows[4]["pin"] is None
        # Saving the row back untouched (UI sends "") keeps the hidden code,
        # and ticking the other lock can't copy a code nobody can read.
        await save(store, syncer, [row(4, "", name="Dog walker", locks=BOTH)])
        assert locks[FRONT].writes == [] and locks[BACK].writes == []
        assert store.slot_status(BACK, 4)["state"] == SKIPPED
        await save(store, syncer, [row(4, "4545", locks=BOTH)])
        assert locks[FRONT].codes[4] == "4545" and locks[BACK].codes[4] == "4545"

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
    store = Store(str(tmp_path / "codes.json"), [FRONT])
    with pytest.raises(ValidationError) as err:
        store.replace_rows(rows)
    assert text in err.value.errors[slot]


def test_same_pin_allowed_on_different_locks(tmp_path):
    store = Store(str(tmp_path / "codes.json"), [FRONT, BACK])
    store.replace_rows([row(1, "1111", locks=(FRONT,)), row(2, "1111", locks=(BACK,))])


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


def test_config_params_shape():
    params = zwave.config_params(
        {
            "7-112-0-16": {"property": 16, "property_key": None, "endpoint": 0, "value": 4,
                           "metadata": {"label": "User Code PIN Length", "min": 4, "max": 8, "writeable": True}},
            "7-112-0-4": {"property": 4, "property_key": None, "endpoint": 0, "value": 255,
                          "metadata": {"label": "Vacation Mode", "states": {"255": "Enable", "0": "Disable"}}},
            "7-112-0-12": {"property": 12, "property_key": None, "endpoint": 0, "value": 87,
                           "metadata": {"label": "Electronic Transition Count", "writeable": False}},
        }
    )
    assert [p["property"] for p in params] == [4, 12, 16]
    assert params[0]["states"] == [[0, "Disable"], [255, "Enable"]]
    assert params[1]["writeable"] is False
    assert zwave.pin_length_from(params) == 4
