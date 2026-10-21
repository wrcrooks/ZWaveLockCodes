"""
The code table and what has been written to each lock, persisted as one JSON file.

One table of codes is shared by every lock. A code keeps the same slot number
on every lock it is ticked for. Per slot there are three layers:

* ``rows``: what the user wants (name, PIN, enabled, notes, which locks).
* ``applied[lock]``: the PIN this add-on last confirmed on that lock's slot.
  A slot missing there was never written by us, and is never touched on that
  lock unless the user ticks it. ``UNKNOWN`` marks a slot we wrote but can no
  longer vouch for (the lock's PIN length changed, which can erase codes), so
  it is written again.
* ``status[lock]``: the outcome of the last write, for display.

A lock's slot needs a write exactly when what it should hold differs from
``applied``. Nothing else ever triggers one.
"""

import json
import os
import re
import time
from typing import Optional

PIN_RE = re.compile(r"^\d{4,8}$")
MAX_NAME = 64
MAX_NOTES = 256

SYNCED = "synced"
UNVERIFIED = "unverified"
PENDING = "pending"
WRITING = "writing"
ERROR = "error"
SKIPPED = "skipped"

UNKNOWN = "?"


class ValidationError(Exception):
    """The submitted table is not something the locks can hold."""

    def __init__(self, errors: dict) -> None:
        super().__init__("invalid table")
        self.errors = errors


def _slots(mapping: dict) -> dict:
    return {int(k): v for k, v in mapping.items()}


def lock_label(entity_id: str) -> str:
    """Turn lock.front_door into "Front door" without asking Home Assistant."""
    return entity_id.split(".", 1)[-1].replace("_", " ").capitalize()


class Store:
    """The table, loaded once and written atomically on every change."""

    def __init__(self, path: str, locks: list) -> None:
        self.path = path
        self.locks = list(locks)
        self.rows: dict = {}
        self.applied: dict = {lock: {} for lock in self.locks}
        self.status: dict = {lock: {} for lock in self.locks}
        self.lock_info: dict = {lock: {} for lock in self.locks}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self._load(json.load(fh))

    def _load(self, data: dict) -> None:
        self.rows = _slots(data.get("rows", {}))
        if "lock_info" not in data:
            # Written by a single-lock version: everything belongs to the first lock.
            first = self.locks[0] if self.locks else None
            for row in self.rows.values():
                row.setdefault("locks", [first] if first else [])
            if first:
                self.applied[first] = _slots(data.get("applied", {}))
                self.status[first] = _slots(data.get("status", {}))
                self.lock_info[first] = {
                    "max_slots": data.get("max_slots"),
                    "pin_length": data.get("pin_length"),
                }
            return
        # Locks taken out of the configuration keep their records, in case
        # they come back; they are just not synced meanwhile.
        for lock, slots in data.get("applied", {}).items():
            self.applied[lock] = _slots(slots)
        for lock, slots in data.get("status", {}).items():
            self.status[lock] = _slots(slots)
        for lock, info in data.get("lock_info", {}).items():
            self.lock_info[lock] = info

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "rows": self.rows,
                    "applied": self.applied,
                    "status": self.status,
                    "lock_info": self.lock_info,
                },
                fh,
                indent=1,
            )
        os.replace(tmp, self.path)

    # What each lock should hold

    def wanted(self, lock: str, slot: int) -> Optional[str]:
        """
        Return the PIN a lock's slot should hold: a PIN, "" for empty, or None for "leave alone".

        A row imported from a lock that hides its codes has no PIN; until the
        user gives it one, the slot keeps whatever the lock has.
        """
        row = self.rows.get(slot)
        if row is None or not row.get("enabled", True) or lock not in row.get("locks", []):
            return ""
        return row.get("pin")

    def needs_write(self, lock: str, slot: int) -> bool:
        want = self.wanted(lock, slot)
        applied = self.applied.get(lock, {})
        if want is None:
            return False
        if want == "":
            # Clearing a slot we never wrote would erase a code we don't own.
            return slot in applied
        return applied.get(slot) != want

    def pending(self, lock: Optional[str] = None) -> list:
        """Return (lock, slot) pairs that need a write, lock by lock."""
        out = []
        for name in [lock] if lock else self.locks:
            slots = set(self.rows) | set(self.applied.get(name, {}))
            out.extend((name, slot) for slot in sorted(slots) if self.needs_write(name, slot))
        return out

    def set_status(self, lock: str, slot: int, state: str, message: str = "") -> None:
        self.status.setdefault(lock, {})[slot] = {
            "state": state,
            "message": message,
            "at": int(time.time()),
        }

    # What each lock can hold

    def info(self, lock: str, key: str) -> Optional[int]:
        return self.lock_info.get(lock, {}).get(key)

    def set_max_slots(self, lock: str, count: int) -> None:
        self.lock_info.setdefault(lock, {})["max_slots"] = count

    def set_pin_length(self, lock: str, length: Optional[int]) -> bool:
        """Record a lock's PIN length; return True if it changed from a known value."""
        known = self.info(lock, "pin_length")
        if length is None or length == known:
            return False
        self.lock_info.setdefault(lock, {})["pin_length"] = length
        if known is not None:
            applied = self.applied.setdefault(lock, {})
            for slot in applied:
                applied[slot] = UNKNOWN
            for _, slot in self.pending(lock):
                self.set_status(lock, slot, PENDING, f"Re-writing after PIN length changed to {length}")
        self.save()
        return known is not None

    def length_problem(self, lock: str, pin: str) -> Optional[str]:
        length = self.info(lock, "pin_length")
        if length and len(pin) != length:
            return f"PIN must be {length} digits for {lock_label(lock)}"
        return None

    # Editing

    def replace_rows(self, submitted: list) -> list:
        """Validate and adopt a full table from the UI; return the (lock, slot) pairs to write."""
        rows: dict = {}
        errors: dict = {}
        for raw in submitted:
            try:
                slot = int(raw.get("slot"))
            except (TypeError, ValueError):
                errors[str(raw.get("slot"))] = "Slot must be a number"
                continue
            locks = [lock for lock in self.locks if lock in (raw.get("locks") or [])]
            problems = []
            if slot < 1:
                problems.append("Slot must be 1 or more")
            for lock in locks:
                most = self.info(lock, "max_slots")
                if most and slot > most:
                    problems.append(f"{lock_label(lock)} has slots 1–{most}")
            if slot in rows:
                problems.append("Slot is used twice")
            pin = raw.get("pin")
            pin = None if pin is None else str(pin).strip()
            if pin == "" and slot in self.rows and self.rows[slot].get("pin") is None:
                pin = None  # still the hidden code the lock had
            enabled = bool(raw.get("enabled", True))
            if pin is not None and not PIN_RE.match(pin):
                problems.append("PIN must be 4–8 digits")
            name = str(raw.get("name") or "").strip()[:MAX_NAME]
            notes = str(raw.get("notes") or "").strip()[:MAX_NOTES]
            if problems:
                errors[str(slot)] = "; ".join(problems)
                continue
            rows[slot] = {
                "name": name,
                "pin": pin,
                "enabled": enabled,
                "notes": notes,
                "locks": locks,
            }

        for lock in self.locks:
            active = {
                s: r["pin"] for s, r in rows.items() if r["enabled"] and r["pin"] and lock in r["locks"]
            }
            seen: dict = {}
            for slot, pin in sorted(active.items()):
                if pin in seen:
                    errors.setdefault(str(slot), f"Same PIN as slot {seen[pin]}")
                seen.setdefault(pin, slot)
            # Schlage locks hold one PIN length for every code (configuration parameter 16).
            for slot, pin in active.items():
                if problem := self.length_problem(lock, pin):
                    errors.setdefault(str(slot), problem)
            lengths = {len(p) for p in active.values()}
            if len(lengths) > 1 and not self.info(lock, "pin_length"):
                common = max(lengths, key=lambda n: sum(len(p) == n for p in active.values()))
                for slot, pin in active.items():
                    if len(pin) != common:
                        errors.setdefault(
                            str(slot), f"Every PIN on {lock_label(lock)} must be {common} digits long"
                        )
        if errors:
            raise ValidationError(errors)

        self.rows = rows
        pending = self.pending()
        for lock, slot in pending:
            self.set_status(lock, slot, PENDING)
        for lock in self.locks:
            statuses = self.status.setdefault(lock, {})
            for slot in list(statuses):
                if (lock, slot) in pending:
                    continue
                row = rows.get(slot)
                if row is None or lock not in row["locks"]:
                    del statuses[slot]
                elif statuses[slot]["state"] in (PENDING, WRITING, ERROR, SKIPPED):
                    # Edited back to what the lock already holds.
                    self.set_status(lock, slot, SYNCED)
        self.save()
        return pending

    # Display

    def slot_status(self, lock: str, slot: int) -> Optional[dict]:
        """Return what the UI shows for one lock's slot, or None if it has nothing there."""
        row = self.rows.get(slot)
        ticked = row is not None and lock in row.get("locks", [])
        applied = self.applied.get(lock, {})
        if not ticked and slot not in applied:
            return None
        if self.wanted(lock, slot) is None and slot not in applied:
            return {"state": SKIPPED, "message": "Code is hidden on another lock; enter a PIN to add it here"}
        return self.status.get(lock, {}).get(slot) or {
            "state": SYNCED if slot in applied else PENDING,
            "message": "",
        }

    def snapshot(self) -> dict:
        """The table as the UI shows it, one entry per slot in use."""
        slots = set(self.rows) | {slot for _, slot in self.pending()}
        out = []
        for slot in sorted(slots):
            row = self.rows.get(slot) or {
                "name": "",
                "pin": "",
                "enabled": False,
                "notes": "",
                "locks": [],
            }
            statuses = {}
            for lock in self.locks:
                if (st := self.slot_status(lock, slot)) is not None:
                    statuses[lock] = st
            out.append({"slot": slot, **row, "deleted": slot not in self.rows, "status": statuses})
        locks = [
            {
                "entity": lock,
                "label": lock_label(lock),
                "max_slots": self.info(lock, "max_slots"),
                "pin_length": self.info(lock, "pin_length"),
            }
            for lock in self.locks
        ]
        return {"rows": out, "locks": locks}
