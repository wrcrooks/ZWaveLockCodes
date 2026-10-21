"""
The code table and what has been written to the lock, persisted as one JSON file.

Each slot has three layers:

* ``rows``: what the user wants (name, PIN, enabled, notes).
* ``applied``: the PIN this add-on last confirmed on the lock for that slot.
  A slot missing from ``applied`` was never written by us, and is never
  touched unless the user puts a row on it.
* ``status``: the outcome of the last write, for display.

A slot needs a write exactly when what it should hold differs from
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


class ValidationError(Exception):
    """The submitted table is not something the lock can hold."""

    def __init__(self, errors: dict) -> None:
        super().__init__("invalid table")
        self.errors = errors


def wanted_pin(row: Optional[dict]) -> Optional[str]:
    """
    Return the PIN a slot should hold: a PIN, "" for empty, or None for "leave alone".

    A row imported from a lock that hides its codes has no PIN; until the user
    gives it one, the slot keeps whatever the lock has.
    """
    if row is None or not row.get("enabled", True):
        return ""
    return row.get("pin")


class Store:
    """The table, loaded once and written atomically on every change."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.rows: dict = {}
        self.applied: dict = {}
        self.status: dict = {}
        self.max_slots: Optional[int] = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.rows = {int(k): v for k, v in data.get("rows", {}).items()}
            self.applied = {int(k): v for k, v in data.get("applied", {}).items()}
            self.status = {int(k): v for k, v in data.get("status", {}).items()}
            self.max_slots = data.get("max_slots")

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "rows": self.rows,
                    "applied": self.applied,
                    "status": self.status,
                    "max_slots": self.max_slots,
                },
                fh,
                indent=1,
            )
        os.replace(tmp, self.path)

    def needs_write(self, slot: int) -> bool:
        want = wanted_pin(self.rows.get(slot))
        if want is None:
            return False
        if want == "":
            # Clearing a slot we never wrote would erase a code we don't own.
            return slot in self.applied
        return self.applied.get(slot) != want

    def pending_slots(self) -> list:
        return sorted(s for s in set(self.rows) | set(self.applied) if self.needs_write(s))

    def set_status(self, slot: int, state: str, message: str = "") -> None:
        self.status[slot] = {"state": state, "message": message, "at": int(time.time())}

    def replace_rows(self, submitted: list) -> list:
        """Validate and adopt a full table from the UI; return the slots that now need a write."""
        rows: dict = {}
        errors: dict = {}
        for raw in submitted:
            try:
                slot = int(raw.get("slot"))
            except (TypeError, ValueError):
                errors[str(raw.get("slot"))] = "Slot must be a number"
                continue
            problems = []
            if slot < 1 or (self.max_slots and slot > self.max_slots):
                problems.append(f"Slot must be 1–{self.max_slots or '…'}")
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
            rows[slot] = {"name": name, "pin": pin, "enabled": enabled, "notes": notes}

        active = {s: r["pin"] for s, r in rows.items() if r["enabled"] and r["pin"]}
        seen: dict = {}
        for slot, pin in sorted(active.items()):
            if pin in seen:
                errors[str(slot)] = f"Same PIN as slot {seen[pin]}"
            seen.setdefault(pin, slot)
        # Schlage locks hold one PIN length for every code (set on the lock).
        lengths = {len(p) for p in active.values()}
        if len(lengths) > 1:
            common = max(lengths, key=lambda n: sum(len(p) == n for p in active.values()))
            for slot, pin in active.items():
                if len(pin) != common:
                    errors.setdefault(str(slot), f"Every PIN must be {common} digits long")
        if errors:
            raise ValidationError(errors)

        self.rows = rows
        pending = self.pending_slots()
        for slot in pending:
            self.set_status(slot, PENDING)
        for slot in list(self.status):
            if slot in pending:
                continue
            if slot not in rows:
                del self.status[slot]
            elif self.status[slot]["state"] in (PENDING, WRITING, ERROR):
                # Edited back to what the lock already holds.
                self.set_status(slot, SYNCED)
        self.save()
        return pending

    def snapshot(self) -> dict:
        """The table as the UI shows it, one entry per slot in use."""
        out = []
        for slot in sorted(set(self.rows) | set(self.pending_slots())):
            row = self.rows.get(slot) or {"name": "", "pin": "", "enabled": False, "notes": ""}
            out.append(
                {
                    "slot": slot,
                    **row,
                    "deleted": slot not in self.rows,
                    "status": self.status.get(slot, {"state": SYNCED if slot in self.applied else PENDING}),
                }
            )
        return {"rows": out, "max_slots": self.max_slots}
