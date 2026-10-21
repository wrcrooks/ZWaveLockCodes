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
                "syncing": syncer.busy,
                "importing": syncer.importing,
                "progress": syncer.progress,
                "imported": syncer.last_import,
                "error": syncer.last_error,
            }
        )

    def lock_from(value) -> str:
        if value not in store.locks:
            raise web.HTTPBadRequest(
                text='{"message": "Unknown lock"}', content_type="application/json"
            )
        return value

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
        syncer.start_import()
        return state()

    async def get_config(request: web.Request) -> web.Response:
        lock = lock_from(request.query.get("lock"))
        try:
            params = await syncer.read_config(lock)
        except HAError as err:
            return web.json_response({"message": str(err)}, status=502)
        return web.json_response({"params": params})

    async def put_config(request: web.Request) -> web.Response:
        body = await request.json()
        lock = lock_from(body.get("lock"))
        try:
            property_ = int(body["property"])
            value = int(body["value"])
            endpoint = int(body.get("endpoint") or 0)
            key = body.get("property_key")
            key = None if key is None else int(key)
        except (KeyError, TypeError, ValueError):
            return web.json_response({"message": "property and value must be numbers"}, status=400)
        try:
            params = await syncer.set_config(lock, property_, key, endpoint, value)
        except HAError as err:
            return web.json_response({"message": str(err)}, status=502)
        return web.json_response({"params": params})

    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", get_state)
    app.router.add_put("/api/codes", put_codes)
    app.router.add_post("/api/retry", post_retry)
    app.router.add_post("/api/import", post_import)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    return app
