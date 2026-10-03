# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified: ported from the Muse Gadget SDK's Linux client (linux/src/musegadget/muse_api.py)
# to MicroPython for the Pimoroni Tufty 2350.

"""Client for the Muse device API: leased VM lookup and token rotation.

Ported from the Linux Device SDK's `muse_api.py`. `http` is a coroutine
`(method, url, headers, body) -> (status, body bytes)` that raises when no
HTTP response arrives.
"""

import json

API_BASE = "https://api.muse.ai"
FETCH_PATH = "/fetch_vms"
REFRESH_PATH = "/device_token/refresh"


def api_root(api_url_v2=""):
    """`api_url` is ignored: only older firmware reads it, adding `/hatch`."""
    return (api_url_v2 or API_BASE).rstrip("/")


async def fetch_vms(http, access_token, root, user_agent, log=print):
    """Leased VMs for the device token, plus the HTTP status if one arrived.

    A 401 means the device token was rejected, which a retry won't fix. A
    None status is a transport failure worth retrying.
    """
    try:
        status, body = await http("GET", root + FETCH_PATH, {
            "Authorization": "Bearer " + access_token,
            "X-API-Version": "1.0.0",
            "User-Agent": user_agent,
        }, None)
    except Exception as exc:
        log("VM fetch failed: %r" % (exc,))
        return [], None
    if status != 200:
        log("VM fetch failed: HTTP %d" % status)
        return [], status
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        log("VM fetch: unexpected response")
        return [], status
    if data.get("error_title") or data.get("backend_error_code"):
        log("VM fetch error: %s %s" % (data.get("error_title"), data.get("backend_error_code")))
        return [], status
    vm_list = data.get("vm_list")
    if not isinstance(vm_list, list):
        log("VM fetch: missing vm_list")
        return [], status
    vms = []
    for entry in vm_list:
        if not isinstance(entry, dict):
            continue
        vm_url = entry.get("vm_ws_url") or entry.get("vm_url")
        vm_token = entry.get("vm_auth_token")
        if vm_url and vm_token:
            vms.append({
                "vm_url": vm_url,
                "vm_auth_token": vm_token,
                "vm_name": entry.get("vm_name", ""),
                "vm_id": entry.get("vm_id", ""),
                "is_default": bool(entry.get("default", False)),
            })
    log("VM fetch: %d VMs" % len(vms))
    return vms, status


async def refresh_device_token(http, refresh_token, device_id, root, user_agent,
                               sdk_token=None, log=print):
    """Rotate the device token pair; returns (tokens or None, status or None).

    A 401 means the pairing is gone and the device must be paired again.

    The access token is never presented, as on the ESP32: the server can
    accept it and answer 200 with replacement tokens that every endpoint then
    rejects, which would overwrite working credentials.
    """
    # Apps now hand over refresh tokens that already carry the hatch_refresh:
    # prefix; doubling it makes the server reject it.
    raw_refresh = refresh_token.rsplit(":", 1)[-1]
    body = {"device_id": device_id}
    if sdk_token:
        body["sdk_token"] = sdk_token
    try:
        status, response = await http("POST", root + REFRESH_PATH, {
            "Authorization": "Bearer hatch_refresh:" + raw_refresh,
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        }, json.dumps(body).encode())
    except Exception as exc:
        log("token refresh failed: %r" % (exc,))
        return None, None
    if status != 200:
        log("token refresh failed: HTTP %d" % status)
        return None, status
    try:
        data = json.loads(response)
    except ValueError:
        return None, None
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        data = data["payload"]
    if isinstance(data, dict) and data.get("access_token") and data.get("refresh_token"):
        return data, 200
    log("token refresh response missing tokens")
    return None, 200
