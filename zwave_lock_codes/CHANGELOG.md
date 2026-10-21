# Changelog

## 0.3.1

- Works with Home Assistant 2024.11 and later. Versions before 2026.3 don't
  have `zwave_js.get_lock_usercode`, so the add-on now asks the lock directly
  instead of failing with "Service zwave_js.get_lock_usercode not found".
- Import runs in the background and shows which slot it's reading, since
  reading a lock slot by slot can take a minute.

## 0.3.0

- Several locks share one code table. Each code has a checkbox per lock and
  keeps the same slot number on every lock it's ticked for.
- Status is shown per lock, and a lock that doesn't respond no longer holds
  up the others.
- Import from locks merges matching codes into one row and lists conflicts
  instead of guessing.
- Lock configuration has a lock picker.
- The **Lock entity** option is replaced by **Locks**; the old setting is
  still read so existing codes stay attached to that lock.

## 0.2.0

- Lock configuration section above the code table (collapsed by default):
  PIN length, vacation mode, auto lock, beeper, alarm settings and the lock's
  counters, read from the lock only when you open it.
- The code table checks PINs against the lock's PIN length.
- When the PIN length changes, codes of the new length are written to the
  lock again, since the lock may erase them.

## 0.1.0

- First version: code table, sync on save, import from lock.
