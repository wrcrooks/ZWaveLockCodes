# Z-Wave Lock Codes

A Home Assistant add-on with a table for your Z-Wave lock's user codes. Only
the slots you change get written to the lock. Built for a Schlage BE469ZP on
Z-Wave JS; it needs nothing else installed.

## Install

Settings → Add-ons → Add-on Store → ⋮ → Repositories, add
`https://github.com/wrcrooks/ZWaveLockCodes`, then install **Z-Wave Lock Codes**.

For development, copy `zwave_lock_codes/` to `/addons/` on your Home Assistant
host (Samba or SSH add-on). Then in the Add-on Store, open ⋮ → Check for
updates and look under **Local add-ons**.

See [zwave_lock_codes/DOCS.md](zwave_lock_codes/DOCS.md) for usage.

## Layout

- `repository.yaml`: tells Home Assistant this repository holds add-ons.
- `zwave_lock_codes/`: the add-on (its `config.yaml`, `Dockerfile` and app).
- `tests/`: tests, run outside the add-on image.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install aiohttp pytest
.venv/bin/pytest tests

# Run the UI locally against a real Home Assistant
mkdir -p /tmp/lc && cd zwave_lock_codes/app && DATA_DIR=/tmp/lc \
  HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  LOCK_ENTITY=lock.front_door python3 -m zwave_lock_codes
```

The Z-Wave slot-reading rules in `zwave_lock_codes/app/zwave_lock_codes/zwave.py`
are adapted from [raman325/lock_code_manager](https://github.com/raman325/lock_code_manager)
(MIT).
