# Sends /state/muse/ask_prompt.txt to the Muse through the badge and prints
# the reply between markers, base64 encoded. Driven by tools/ask_muse.py.
import asyncio, binascii, sys
from musebadge import service
from musebadge.main import App, STATE_DIR

async def go():
    app = App()
    tasks = [asyncio.create_task(app.wifi.keep_connected()), asyncio.create_task(app.service.run())]
    for _ in range(240):
        if app.service.state == service.CONNECTED:
            break
        await asyncio.sleep(0.25)
    if app.service.state != service.CONNECTED:
        print("ASK-ERROR not connected:", app.service.state)
        return
    await asyncio.sleep(1)
    with open(STATE_DIR + "/ask_prompt.txt") as f:
        prompt = f.read()
    try:
        reply = await app.service.ask(prompt, on_text=lambda t: print("ASK-PART", len(t)))
    except Exception as exc:
        print("ASK-ERROR", repr(exc))
        return
    data = reply.encode()
    print("ASK-BEGIN", len(data))
    for i in range(0, len(data), 3072):
        sys.stdout.write(binascii.b2a_base64(data[i:i + 3072]).decode())
    print("ASK-END")
    await app.service.stop()
    for task in tasks:
        task.cancel()

asyncio.run(go())
