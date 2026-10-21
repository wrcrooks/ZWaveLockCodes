"""
How to read what a Z-Wave lock reports about a code slot.

The rules here are ported from raman325/lock_code_manager (MIT License,
Copyright (c) 2023 raman325), trimmed to the User Code Command Class path
that Home Assistant's ``zwave_js.*_lock_usercode`` actions use:

* A lock that withholds codes answers with one asterisk per digit. That is
  a refusal, not a value, so it must never be compared against a PIN.
* All zeros in a slot the lock says is not in use means empty, but zeros
  from a slot whose status is unknown are not trusted as cleared.
* User Code CC has no in-band duplicate result. A lock refusing a PIN that
  another slot already holds says so only with Access Control notification
  15, which arrives while the write is still in flight.
"""

from dataclasses import dataclass
from typing import Any, Optional

# Access Control notification (Notification CC type 6), event 15:
# "New user code not added due to duplicate code".
NOTIFICATION_CC = 113
ACCESS_CONTROL = 6
DUPLICATE_CODE_EVENT = 15

EMPTY = "empty"
KNOWN = "known"
HIDDEN = "hidden"


@dataclass(frozen=True)
class SlotReading:
    """What a lock holds in one slot: nothing, a readable PIN, or a hidden one."""

    kind: str
    pin: str = ""


def is_masked_code(code: str) -> bool:
    """Return whether a code is the lock's all-asterisk placeholder."""
    return bool(code) and set(code) == {"*"}


def read_slot(usercode: Any, in_use: Optional[bool]) -> SlotReading:
    """Project a ``get_lock_usercode`` entry onto a SlotReading."""
    code = "" if usercode is None else str(usercode).strip()
    if in_use is False and (not code or code.strip("0") == ""):
        return SlotReading(EMPTY)
    if not code:
        # An occupied slot reporting no value is the lock withholding it.
        return SlotReading(HIDDEN) if in_use else SlotReading(EMPTY)
    if is_masked_code(code):
        return SlotReading(HIDDEN) if in_use is not False else SlotReading(EMPTY)
    return SlotReading(KNOWN, code)


def is_duplicate_refusal(event_data: dict, device_id: Optional[str]) -> bool:
    """Return whether a zwave_js_notification is the lock refusing a duplicate."""
    return (
        event_data.get("command_class") == NOTIFICATION_CC
        and event_data.get("type") == ACCESS_CONTROL
        and event_data.get("event") == DUPLICATE_CODE_EVENT
        and (device_id is None or event_data.get("device_id") == device_id)
    )
