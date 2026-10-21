"""The real HAClient and web API against a fake Home Assistant WebSocket server."""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "zwave_lock_codes" / "app"))

from zwave_lock_codes.store import Store  # noqa: E402
from zwave_lock_codes.sync import Syncer  # noqa: E402
from zwave_lock_codes.web import build_app  # noqa: E402

LOCK = "lock.front_door"
TOKEN = "test-token"


def fake_home_assistant(codes: dict, log: list) -> web.Application:
    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "bad token"})
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            req = json.loads(msg.data)
            log.append(req)
            result = None
            if req["type"] == "config/entity_registry/get":
                result = {"entity_id": req["entity_id"], "device_id": "dev1"}
            elif req["type"] == "call_service":
                data, service = req["service_data"], req["service"]
                assert req["target"] == {"entity_id": LOCK}
                if service == "get_lock_usercode":
                    slots = [data["code_slot"]] if "code_slot" in data else codes
                    resp = {str(s): {"usercode": codes[s], "in_use": bool(codes[s])} for s in slots}
                    result = {"context": {}, "response": {LOCK: resp}}
                elif service == "set_lock_usercode":
                    codes[data["code_slot"]] = data["usercode"]
                    result = {"context": {}}
                elif service == "clear_lock_usercode":
                    codes[data["code_slot"]] = ""
                    result = {"context": {}}
            elif req["type"] == "subscribe_events":
                result = None
            await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": result})
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", websocket)
    return app


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def test_end_to_end(tmp_path, monkeypatch):
    async def go():
        codes = {n: "" for n in range(1, 31)}
        codes[7] = "7777"
        log: list = []
        ha_runner, ha_url = await serve(fake_home_assistant(codes, log))
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("HA_URL", ha_url)
        monkeypatch.setenv("HA_TOKEN", TOKEN)

        store = Store(str(tmp_path / "codes.json"))
        syncer = Syncer(store, LOCK)
        ui_runner, ui_url = await serve(build_app(store, syncer))
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(ui_url + "/") as res:
                    assert res.status == 200 and "<title>Lock Codes</title>" in await res.text()

                async with http.post(ui_url + "/api/import") as res:
                    state = await res.json()
                assert [r["slot"] for r in state["rows"]] == [7] and state["max_slots"] == 30

                rows = [
                    {"slot": 7, "name": "Alice", "pin": "7777", "enabled": True},
                    {"slot": 2, "name": "Bob", "pin": "2222", "enabled": True},
                ]
                async with http.put(ui_url + "/api/codes", json={"rows": rows}) as res:
                    assert res.status == 200
                await syncer.wait()
                assert codes[2] == "2222"

                bad = [{"slot": 3, "name": "x", "pin": "12", "enabled": True}]
                async with http.put(ui_url + "/api/codes", json={"rows": bad}) as res:
                    assert res.status == 400 and "3" in (await res.json())["errors"]

                async with http.get(ui_url + "/api/state") as res:
                    state = await res.json()
                assert {r["slot"]: r["status"]["state"] for r in state["rows"]} == {2: "synced", 7: "synced"}
                assert not state["syncing"] and state["error"] is None

            writes = [r["service"] for r in log if r["type"] == "call_service" and r["service"] != "get_lock_usercode"]
            assert writes == ["set_lock_usercode"], "only slot 2 changed"

            monkeypatch.setenv("HA_TOKEN", "wrong")
            store.replace_rows(rows[:1])
            syncer.request()
            await syncer.wait()
            assert "authentication failed" in syncer.last_error
            assert store.status[2]["state"] == "error"
        finally:
            await ui_runner.cleanup()
            await ha_runner.cleanup()

    asyncio.run(go())
