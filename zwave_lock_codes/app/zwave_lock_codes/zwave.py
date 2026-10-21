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

import re
from dataclasses import dataclass
from typing import Any, Optional

USER_CODE_CC = 99
# User Code CC userIdStatus values that say nothing about the slot's contents.
_STATUS_AVAILABLE = 0
_STATUS_NOT_AVAILABLE = 0xFE

# Access Control notification (Notification CC type 6), event 15:
# "New user code not added due to duplicate code".
NOTIFICATION_CC = 113
ACCESS_CONTROL = 6
DUPLICATE_CODE_EVENT = 15

# SetValueStatus codes from Z-Wave JS that mean the write did not happen.
FAILED_SET_STATUSES = {0, 2, 3, 4, 5}
_PIN_LENGTH_LABEL = re.compile(r"(pin|code).*length", re.IGNORECASE)

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


def usercode_entry(result: Any) -> dict:
    """Shape a User Code CC ``get`` result like a ``get_lock_usercode`` entry."""
    result = result if isinstance(result, dict) else {}
    code = result.get("userCode")
    if isinstance(code, dict) and code.get("type") == "Buffer":
        # Codes that aren't plain digits come back as a serialized Node.js Buffer.
        code = bytes(code.get("data") or []).decode("latin-1")
    status = result.get("userIdStatus")
    in_use = None if status in (None, _STATUS_NOT_AVAILABLE) else status != _STATUS_AVAILABLE
    return {"usercode": code or "", "in_use": in_use}


def is_duplicate_refusal(event_data: dict, device_id: Optional[str]) -> bool:
    """Return whether a zwave_js_notification is the lock refusing a duplicate."""
    return (
        event_data.get("command_class") == NOTIFICATION_CC
        and event_data.get("type") == ACCESS_CONTROL
        and event_data.get("event") == DUPLICATE_CODE_EVENT
        and (device_id is None or event_data.get("device_id") == device_id)
    )


def config_params(raw: dict) -> list:
    """Flatten ``zwave_js/get_config_parameters`` into a list ordered by parameter."""
    params = []
    for value_id, entry in (raw or {}).items():
        meta = entry.get("metadata") or {}
        states = meta.get("states") or {}
        params.append(
            {
                "id": value_id,
                "property": entry.get("property"),
                "property_key": entry.get("property_key"),
                "endpoint": entry.get("endpoint") or 0,
                "label": meta.get("label") or f"Parameter {entry.get('property')}",
                "description": meta.get("description") or "",
                "value": entry.get("value"),
                "default": meta.get("default"),
                "min": meta.get("min"),
                "max": meta.get("max"),
                "unit": meta.get("unit") or "",
                "writeable": meta.get("writeable", True) is not False,
                "states": [[int(k), v] for k, v in sorted(states.items(), key=lambda kv: int(kv[0]))],
                "pin_length": bool(_PIN_LENGTH_LABEL.search(meta.get("label") or "")),
            }
        )
    params.sort(key=lambda p: (p["property"] or 0, p["property_key"] or 0))
    return params


def pin_length_from(params: list) -> Optional[int]:
    """Return the lock's configured PIN length, if it has such a parameter."""
    for param in params:
        if param["pin_length"] and isinstance(param["value"], int):
            return param["value"]
    return None
