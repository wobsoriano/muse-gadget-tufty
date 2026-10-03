"""End to end: the service loop on real sockets, under MicroPython and CPython.

    uv run --with pytest --with cryptography --with websockets pytest tests/test_session.py
"""

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNTIMES = [[sys.executable]]
if shutil.which("micropython"):
    RUNTIMES.append(["micropython", "-X", "heapsize=8M"])


@pytest.mark.parametrize("runtime", RUNTIMES, ids=lambda r: pathlib.Path(r[0]).name)
def test_service_session(runtime, tmp_path):
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "fake_muse.py")],
        stdout=subprocess.PIPE, text=True, cwd=ROOT)
    try:
        ports = json.loads(server.stdout.readline())
        device = subprocess.run(
            runtime + ["tests/device_session.py", str(ports["api"]), str(ports["vm"]), str(tmp_path)],
            capture_output=True, text=True, cwd=ROOT, timeout=90)
        assert device.returncode == 0, device.stderr + device.stdout
        events = json.loads(device.stdout.strip().splitlines()[-1])
        transcript = json.loads(server.stdout.readline())
    finally:
        server.kill()

    api = transcript["api"]
    assert [(c["method"], c["path"]) for c in api] == [
        ("POST", "/device_token/refresh"), ("GET", "/fetch_vms")]
    assert api[0]["authorization"] == "Bearer hatch_refresh:r1"
    assert json.loads(api[0]["body"]) == {"device_id": "homelink-010203", "sdk_token": "mgst_test"}
    assert api[1]["authorization"] == "Bearer new-access"
    assert api[1]["user_agent"] == "musebadge-test"

    vm = transcript["vm"]
    assert vm[0] == {"path": "/v1/noise?vm_id=vm%201", "authorization": "Bearer vmtok"}
    assert vm[1] == {"control": ["POST", "/link-control", False]}
    register = vm[2]["register"]
    assert (register["type"], register["method"]) == ("req", "link.register")
    assert register["params"]["node_id"] == "homelink-010203"
    assert (register["params"]["platform"], register["params"]["device_family"]) == ("linux", "homehub")
    assert vm[3] == {"result": {"method": "link.result", "id": "inv-1", "ok": True,
                                "payload": {"shown": "hello"}}}
    assert register["params"]["metadata"] == {"network_ssid": "TestNet"}
    assert vm[4] == {"device_done": {"type": "evt", "event": "test.done"}}
    assert vm[5] == {"big_result_ok": True, "big_echo_matches": True}
    assert transcript["chats"] == [
        [{"message": "hi from the badge", "output_modality": "text",
          "device_id": "homelink-010203", "session_id": "side-1"}, "musegadget", True],
        [{"message": "what is up", "output_modality": "text",
          "device_id": "homelink-010203"}, "musegadget", True],
        [{"message": "and now", "output_modality": "text",
          "device_id": "homelink-010203"}, "musegadget", True],
    ]
    assert transcript["subscribes"] == 1    # one stream serves every question

    named = [e for e in events if e[0] != "log"]
    assert named == [
        ["invoke", "badge.show_message", 5, 5000],
        ["state", "connected"],
        ["chat", {"ok": True, "status": 200,
                  "response": {"ok": True, "result": {"message_id": "u1"}}}],
        ["ask", "you said: what is up", "you said: what is up"],
        ["ask again", "you said: and now"],
        ["invoke", "echo", 120000, None],
        ["state", "unpaired"],
        ["pairing_file", None],
        ["node_id", "homelink-010203", "MuseGadget010203"],
    ]
    logs = [e[1] for e in events if e[0] == "log"]
    assert "device token rotated" in logs and "registered with the Muse" in logs
