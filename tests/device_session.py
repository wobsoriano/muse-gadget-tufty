"""The badge's service loop against tests/fake_muse.py, over real sockets.

Runs unchanged under MicroPython and CPython:

    micropython tests/device_session.py API_PORT VM_PORT STATE_DIR
"""

import asyncio
import json
import sys

sys.path.insert(0, "app/muse")

from musebadge import net, service, store  # noqa: E402

api_port, vm_port, state_dir = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
events = []


svc = None


async def run_command(command, params, timeout_ms):
    events.append(["invoke", command, len(params.get("text", "")), timeout_ms])
    if command == "echo":
        return {"ok": True, "payload": {"text": params["text"]}}
    return {"ok": True, "payload": {"shown": params.get("text")}}


async def http(method, url, headers, body):
    return await net.http_request(method, url, headers, body)


async def connect(url, headers):
    return await net.ws_connect(url.replace("wss://", "ws://"), headers)


async def main():
    files = store.Store(state_dir)
    identity = store.load_identity(files, lambda n: bytes([0xAB, 0xCD, 0xEF, 0x01, 0x02, 0x03]))
    files.save(store.PAIRING_FILE, {
        "access_token": "old-access", "refresh_token": "hatch_refresh:r1",
        "api_url_v2": "http://127.0.0.1:%d" % api_port,
        "noise_host": "127.0.0.1:%d" % vm_port, "access_token_saved_at": 0,
    })
    global svc
    svc = service.Service(
        identity, files, http, connect, run_command, {"badge.show_message": {}},
        "0.1.0", "Test Badge", "musebadge-test", sdk_token="mgst_test",
        ssid=lambda: "TestNet",
        log=lambda message: events.append(["log", message]),
    )
    task = asyncio.create_task(svc.run())
    for _ in range(300):
        if svc.state == service.CONNECTED:
            break
        await asyncio.sleep(0.05)
    events.append(["state", svc.state])
    events.append(["chat", await svc.send_chat("hi from the badge", "side-1")])
    parts = []
    events.append(["ask", await svc.ask("what is up", on_text=parts.append), parts[-1]])
    events.append(["ask again", await svc.ask("and now")])
    await svc._session.send({"type": "evt", "event": "test.done"})
    for _ in range(600):
        if svc.state == service.UNPAIRED:
            break
        await asyncio.sleep(0.05)
    events.append(["state", svc.state])
    events.append(["pairing_file", files.load(store.PAIRING_FILE)])
    events.append(["node_id", identity.node_id, identity.ble_name])
    await svc.stop()
    task.cancel()
    print(json.dumps(events))


asyncio.run(main())
