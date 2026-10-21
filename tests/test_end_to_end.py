"""The real HAClient and web API against a fake Home Assistant WebSocket server."""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "zwave_lock_codes" / "app"))

from zwave_lock_codes.store import Store  # noqa: E402
from zwave_lock_codes.sync import Syncer  # noqa: E402
from zwave_lock_codes.web import build_app  # noqa: E402

LOCK = "lock.front_door"
TOKEN = "test-token"


def fake_home_assistant(codes: dict, log: list, config: dict, old_ha: bool = False) -> web.Application:
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
                if service == "get_lock_usercode" and old_ha:
                    await ws.send_json({"id": req["id"], "type": "result", "success": False,
                                        "error": {"code": "not_found",
                                                  "message": "Service zwave_js.get_lock_usercode not found."}})
                    continue
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
            elif req["type"] == "zwave_js/invoke_cc_api":
                if req["method_name"] == "getUsersCount":
                    result = len(codes)
                else:
                    code = codes[req["parameters"][0]]
                    result = {"userIdStatus": 1 if code else 0, "userCode": code}
            elif req["type"] == "zwave_js/get_config_parameters":
                assert req["device_id"] == "dev1"
                result = {
                    "7-112-0-4": {"property": 4, "property_key": None, "endpoint": 0, "value": config[4],
                                  "metadata": {"label": "Vacation Mode", "writeable": True,
                                               "states": {"0": "Disable", "255": "Enable"}}},
                    "7-112-0-16": {"property": 16, "property_key": None, "endpoint": 0, "value": config[16],
                                   "metadata": {"label": "User Code PIN Length", "writeable": True,
                                                "min": 4, "max": 8}},
                }
            elif req["type"] == "zwave_js/set_config_parameter":
                config[req["property"]] = req["value"]
                result = {"value_id": f"7-112-0-{req['property']}", "status": 255}
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


@pytest.mark.parametrize("old_ha", [False, True], ids=["ha-2026.3+", "older-ha"])
def test_end_to_end(tmp_path, monkeypatch, old_ha):
    async def go():
        codes = {n: "" for n in range(1, 31)}
        codes[7] = "7777"
        log: list = []
        config = {4: 0, 16: 4}
        ha_runner, ha_url = await serve(fake_home_assistant(codes, log, config, old_ha))
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("HA_URL", ha_url)
        monkeypatch.setenv("HA_TOKEN", TOKEN)

        store = Store(str(tmp_path / "codes.json"), [LOCK])
        syncer = Syncer(store)
        ui_runner, ui_url = await serve(build_app(store, syncer))
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(ui_url + "/") as res:
                    assert res.status == 200 and "<title>Lock Codes</title>" in await res.text()

                async with http.post(ui_url + "/api/import") as res:
                    assert (await res.json())["importing"] is True
                while syncer.importing:
                    await asyncio.sleep(0.01)
                async with http.get(ui_url + "/api/state") as res:
                    state = await res.json()
                assert [r["slot"] for r in state["rows"]] == [7]
                assert state["locks"] == [{"entity": LOCK, "label": "Front door", "max_slots": 30, "pin_length": None}]
                assert state["imported"]["added"] == 1

                rows = [
                    {"slot": 7, "name": "Alice", "pin": "7777", "enabled": True, "locks": [LOCK]},
                    {"slot": 2, "name": "Bob", "pin": "2222", "enabled": True, "locks": [LOCK]},
                ]
                async with http.put(ui_url + "/api/codes", json={"rows": rows}) as res:
                    assert res.status == 200
                await syncer.wait()
                assert codes[2] == "2222"

                bad = [{"slot": 3, "name": "x", "pin": "12", "enabled": True, "locks": [LOCK]}]
                async with http.put(ui_url + "/api/codes", json={"rows": bad}) as res:
                    assert res.status == 400 and "3" in (await res.json())["errors"]

                async with http.get(ui_url + "/api/state") as res:
                    state = await res.json()
                assert {r["slot"]: r["status"][LOCK]["state"] for r in state["rows"]} == {2: "synced", 7: "synced"}
                assert not state["syncing"] and state["error"] is None

                async with http.get(ui_url + "/api/config", params={"lock": LOCK}) as res:
                    data = await res.json()
                assert [p["label"] for p in data["params"]] == ["Vacation Mode", "User Code PIN Length"]
                assert store.info(LOCK, "pin_length") == 4

                async with http.get(ui_url + "/api/config", params={"lock": "lock.nope"}) as res:
                    assert res.status == 400

                body = {"lock": LOCK, "property": 4, "property_key": None, "endpoint": 0, "value": 255}
                async with http.put(ui_url + "/api/config", json=body) as res:
                    data = await res.json()
                assert config[4] == 255 and data["params"][0]["value"] == 255

            log = [r for r in log if not r["type"].startswith("zwave_js/")]
            writes = [r["service"] for r in log if r["type"] == "call_service" and r["service"] != "get_lock_usercode"]
            assert writes == ["set_lock_usercode"], "only slot 2 changed"

            monkeypatch.setenv("HA_TOKEN", "wrong")
            store.replace_rows(rows[:1])
            syncer.request()
            await syncer.wait()
            assert "authentication failed" in syncer.last_error
            assert store.status[LOCK][2]["state"] == "error"
        finally:
            await ui_runner.cleanup()
            await ha_runner.cleanup()

    asyncio.run(go())
