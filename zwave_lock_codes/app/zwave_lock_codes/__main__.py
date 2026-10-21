"""Entry point: read add-on options, then serve the UI until stopped."""

import json
import logging
import os

from aiohttp import web

from .store import Store
from .sync import Syncer
from .web import build_app

DATA_DIR = os.environ.get("DATA_DIR", "/data")


def configured_locks(options: dict) -> list:
    """
    Return the lock entities to manage, in order.

    ``lock_entity`` is the single-lock setting from before 0.3.0. It goes
    first, so codes synced by an older version stay attached to that lock.
    """
    locks = []
    env = os.environ.get("LOCKS", "")
    for lock in [options.get("lock_entity"), *(options.get("locks") or []), *env.split(",")]:
        lock = (lock or "").strip()
        if lock and lock not in locks:
            locks.append(lock)
    return locks


def main() -> None:
    options_path = os.path.join(DATA_DIR, "options.json")
    options = {}
    if os.path.exists(options_path):
        with open(options_path, encoding="utf-8") as fh:
            options = json.load(fh)
    logging.basicConfig(
        level=options.get("log_level", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    locks = configured_locks(options)
    if not locks:
        logging.getLogger(__name__).warning("No locks configured; add them on the Configuration tab")
    store = Store(os.path.join(DATA_DIR, "codes.json"), locks)
    web.run_app(
        build_app(store, Syncer(store)),
        port=int(os.environ.get("PORT", "8099")),
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
