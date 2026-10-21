# Changelog

## 0.2.0

- Lock configuration section above the code table (collapsed by default):
  PIN length, vacation mode, auto lock, beeper, alarm settings and the lock's
  counters, read from the lock only when you open it.
- The code table checks PINs against the lock's PIN length.
- When the PIN length changes, codes of the new length are written to the
  lock again, since the lock may erase them.

## 0.1.0

- First version: code table, sync on save, import from lock.
