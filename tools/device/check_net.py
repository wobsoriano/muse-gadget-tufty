import asyncio, time, socket
from musebadge import net, main, store
from musebadge.wifi import Wifi

async def go():
    files = store.Store(main.STATE_DIR)
    wifi = Wifi(files)
    task = asyncio.create_task(wifi.keep_connected())
    for _ in range(120):
        if wifi.is_online():
            break
        await asyncio.sleep(0.25)
    await asyncio.sleep(3)
    print("online", wifi.is_online(), "ssid", wifi.ssid(), "ifconfig", wifi.wlan.ifconfig(), "year", time.gmtime()[0])
    for host in ("api.muse.ai", "captive.apple.com"):
        try:
            print("dns", host, socket.getaddrinfo(host, 443)[0][-1])
        except Exception as e:
            print("dns", host, "FAILED", repr(e))
    try:
        status, body = await net.http_request("GET", "http://captive.apple.com/hotspot-detect.html", {"User-Agent": "CaptiveNetworkSupport"}, None, timeout=10)
        print("captive probe ->", status, repr(body[:120]))
    except Exception as e:
        print("captive probe FAILED", repr(e))
    ctx = main._tls_context()
    for url in ("https://api.muse.ai/fetch_vms", "https://hatch.metaaivm.com/v1/noise?vm_id=x"):
        t = time.ticks_ms()
        try:
            status, body = await net.http_request("GET", url, {"User-Agent": main.USER_AGENT}, None, ssl=ctx, timeout=20)
            print(url, "->", status, "in", time.ticks_diff(time.ticks_ms(), t), "ms")
        except Exception as e:
            print(url, "FAILED", repr(e), "after", time.ticks_diff(time.ticks_ms(), t), "ms")
    task.cancel()

asyncio.run(go())
