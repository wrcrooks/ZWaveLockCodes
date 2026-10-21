"""Ingress web UI and its small JSON API."""

import logging
from pathlib import Path

from aiohttp import web

from .ha import HAError
from .store import Store, ValidationError
from .sync import Syncer

_LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def build_app(store: Store, syncer: Syncer) -> web.Application:
    def state() -> web.Response:
        return web.json_response(
            {
                **store.snapshot(),
                "lock": syncer.lock_entity,
                "syncing": syncer.busy,
                "error": syncer.last_error,
            }
        )

    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def get_state(request: web.Request) -> web.Response:
        return state()

    async def put_codes(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            store.replace_rows(body.get("rows") or [])
        except ValidationError as err:
            return web.json_response({"errors": err.errors}, status=400)
        syncer.request()
        return state()

    async def post_retry(request: web.Request) -> web.Response:
        syncer.request()
        return state()

    async def post_import(request: web.Request) -> web.Response:
        if syncer.busy:
            return web.json_response({"message": "Wait for the current sync to finish"}, status=409)
        try:
            added = await syncer.import_from_lock()
        except HAError as err:
            return web.json_response({"message": str(err)}, status=502)
        _LOGGER.info("Imported %s codes from %s", added, syncer.lock_entity)
        return state()

    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", get_state)
    app.router.add_put("/api/codes", put_codes)
    app.router.add_post("/api/retry", post_retry)
    app.router.add_post("/api/import", post_import)
    return app
