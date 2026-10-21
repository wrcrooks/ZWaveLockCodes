"""Entry point: read add-on options, then serve the UI until stopped."""

import json
import logging
import os

from aiohttp import web

from .store import Store
from .sync import Syncer
from .web import build_app

DATA_DIR = os.environ.get("DATA_DIR", "/data")


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
    lock_entity = options.get("lock_entity") or os.environ.get("LOCK_ENTITY", "lock.front_door")
    store = Store(os.path.join(DATA_DIR, "codes.json"))
    syncer = Syncer(store, lock_entity)
    web.run_app(
        build_app(store, syncer),
        port=int(os.environ.get("PORT", "8099")),
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
