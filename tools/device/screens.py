import sys, binascii
from musebadge import service
from musebadge.ui import Screen

def dump(name):
    display.update()
    raw = memoryview(screen)
    sys.stdout.write("SHOT %s %d %d %d\n" % (name, screen.width, screen.height, len(raw)))
    for i in range(0, len(raw), 3072):
        sys.stdout.write(binascii.b2a_base64(raw[i:i + 3072]).decode())
    sys.stdout.write("END\n")

import time

s = Screen("MuseGadget0A1B2C")

def settle(ms):
    # Let one-shot animations finish and loops advance to a mid frame.
    end = time.ticks_add(time.ticks_ms(), ms)
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        s.draw()

s.draw(); dump("boot")
settle(3500); s.draw(); dump("unpaired")
s.pairing = True; s.draw(); dump("pairing")
s.pairing = False; s.state = service.CONNECTING; settle(600); s.draw(); dump("connecting")
s.state = service.CONNECTED; settle(600); s.draw(); dump("connected")
s.pet(); settle(1200); s.draw(); dump("happy")
s.state = service.OFFLINE; settle(4000); s.draw(); dump("offline")
s.state = service.CONNECTED
s.show_message("Your 2pm with Dana moved to 3. You have time for coffee.", "Muse")
settle(500); s.draw(); dump("message")
s.dance(8000); settle(2600); s.draw(); dump("dance")
