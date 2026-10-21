# Z-Wave Lock Codes

A table of your Z-Wave lock's user codes, opened from the Home Assistant
sidebar. Edit the table, press **Save & sync**, and only the slots you changed
are written to the lock.

## Setup

1. Set **Lock entity** to your lock (for example `lock.front_door`) and start
   the add-on.
2. Open **Lock Codes** from the sidebar and press **Import from lock**. This
   copies the codes already on the lock into the table, so nothing gets
   overwritten. Give each one a name.

## How syncing works

- Nothing runs in the background. The add-on connects to Home Assistant only
  while it is writing or importing, then disconnects.
- On save, each slot whose PIN or enabled state changed is written with
  `zwave_js.set_lock_usercode` or cleared with `zwave_js.clear_lock_usercode`,
  one at a time. Changing a name or notes never touches the lock.
- After each write the add-on reads the slot back from Z-Wave JS's cache (no
  extra radio traffic) and shows the result in **Status**:
  - **Synced**: the lock holds exactly that PIN.
  - **Synced (hidden)**: the lock hides its codes, but the slot is occupied.
  - **Error**: the write failed, for example because another slot already has
    that PIN. Fix it and save again, or press **Retry**.
- Slots that aren't in the table are never changed. Deleting a row, or
  unchecking **Enabled**, clears that slot on the lock.
- Codes made at the lock's keypad aren't picked up automatically. Use
  **Import from lock** to bring them in.

## Lock configuration

Open **Lock configuration** above the table to change the lock's own
settings. On a BE469ZP that's PIN length, vacation mode, auto lock, beeper,
lock & leave and the alarm settings, plus read-only counters. Changes are
sent to the lock as soon as you pick a new value. The list comes from the
lock itself, so other Z-Wave locks show their own settings.

The settings are read from Z-Wave JS only when you open the section; nothing
is checked in the background.

## Schlage BE469ZP notes

- Every code on the lock must have the lock's PIN length (4 to 8 digits), so
  the table checks each PIN against it.
- Changing the PIN length may erase the codes on the lock. Afterwards the
  add-on writes back every code that already has the new length; edit the
  others to the new length and save.
- The 30 user slots are separate from the programming code on the back of the
  interior unit, which this add-on never touches.

## Privacy

PINs are stored in plain text in the add-on's private `/data/codes.json`,
which is included in Home Assistant backups. They are never written to the
log. The sidebar panel is admin-only.
