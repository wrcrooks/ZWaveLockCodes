# Z-Wave Lock Codes

One table of user codes for all your Z-Wave locks, opened from the Home
Assistant sidebar. Edit the table, press **Save & sync**, and only the slots
you changed are written to the locks.

## Setup

1. On the **Configuration** tab, add your lock entities under **Locks** (for
   example `lock.front_door` and `lock.back_door`), then start the add-on.
2. Open **Lock Codes** from the sidebar and press **Import from locks**. This
   copies the codes already on your locks into the table, so nothing gets
   overwritten. Give each one a name.

## One table, several locks

- Each code has a checkbox per lock and uses the same slot number on every
  lock it's ticked for. New codes start ticked for every lock.
- Unticking a lock clears that code from that lock only.
- Each code is checked against every lock it's ticked for: the PIN must have
  that lock's PIN length, the slot must exist on it, and no two codes on one
  lock may share a PIN.
- **Import from locks** merges what it finds. The same PIN in the same slot on
  several locks becomes one row with those locks ticked. If a lock holds a
  different code in a slot the table already uses, the import lists it and
  leaves it alone.

## How syncing works

- Nothing runs in the background. The add-on connects to Home Assistant only
  while it is writing or importing, then disconnects.
- On save, each slot whose PIN, enabled state or locks changed is written
  with `zwave_js.set_lock_usercode` or cleared with
  `zwave_js.clear_lock_usercode`, one at a time, lock by lock. Changing a
  name or notes never touches a lock.
- After each write the add-on reads the slot back from Z-Wave JS's cache (no
  extra radio traffic) and shows the result for each lock in **Status**:
  - **Synced**: the lock holds exactly that PIN.
  - **Synced (hidden)**: the lock hides its codes, but the slot is occupied.
  - **Not on lock**: the code was imported from a lock that hides it, so it
    can't be copied to another lock until you enter its PIN.
  - **Error**: the write failed, for example because another slot already has
    that PIN. Fix it and save again, or press **Retry**.
- If a lock doesn't respond (dead battery, out of range), its remaining
  writes are skipped and marked as errors; the other locks still sync.
- Slots that aren't in the table are never changed. Deleting a row, or
  unchecking **Enabled**, clears that slot on every lock it was on.
- Codes made at a lock's keypad aren't picked up automatically. Use
  **Import from locks** to bring them in.

## Lock configuration

Open **Lock configuration** above the table to change a lock's own settings;
pick the lock at the top when you have more than one. On a BE469ZP that's PIN
length, vacation mode, auto lock, beeper, lock & leave and the alarm
settings, plus read-only counters. Changes are sent to the lock as soon as
you pick a new value. The list comes from the lock itself, so other Z-Wave
locks show their own settings.

The settings are read from Z-Wave JS only when you open the section; nothing
is checked in the background.

## Schlage BE469ZP notes

- Every code on the lock must have the lock's PIN length (4 to 8 digits), so
  the table checks each PIN against it.
- Changing the PIN length may erase the codes on the lock. Afterwards the
  add-on writes back every code that already has the new length; edit the
  others to the new length and save. Other locks aren't affected.
- The 30 user slots are separate from the programming code on the back of the
  interior unit, which this add-on never touches.

## Upgrading from 0.2 or earlier

The single **Lock entity** setting is replaced by **Locks**. Your old setting
is still read, so the codes you already synced stay attached to that lock;
add any other locks under **Locks**.

## Privacy

PINs are stored in plain text in the add-on's private `/data/codes.json`,
which is included in Home Assistant backups. They are never written to the
log. The sidebar panel is admin-only.
