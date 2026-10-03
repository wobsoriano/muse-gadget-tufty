# Badgeware (Tufty 2350) app API cheat sheet

Researched 2026-10-02, web/GitHub only, no device access.

## Sources and version mapping

- FW = https://github.com/pimoroni/tufty2350 at tag v3.1.1 (HEAD ee84772 equals v3.1.1). Paths below are repo-relative, e.g. `modules/common/badgeware/__init__.py`.
- `ci/micropython.sh` in v3.1.0 and v3.1.1 pins `MICROPYTHON_VERSION="bw-1.29.0-gc"` (fork https://github.com/pimoroni/micropython), pimoroni-pico commit f5ad5244, picovector-micropython v3.1.0. So device "bw-1.29.0" is v3.1.0 or v3.1.1. v3.1.1 only changes `board/pimoroni_tufty2350.h` and the crt0 rosc patch (overclock stability). v3.0.x used bw-1.28.0-3.
- DOCS = https://github.com/pimoroni/badgeware-docs (`api/*.md`, `guides/*.md`). This is the source of the badgewa.re site. I did not fetch badgewa.re itself.
- Also read from the pinned dependency repos: picovector-micropython v3.1.0 (`native/font_native.cpp`), pimoroni/micropython branches bw-1.29.0-gc and bw-1.29.0 (`ports/rp2`), pimoroni-pico f5ad5244.
- Marking convention. VERIFIED = read in source at the path given. DOC = stated by docs only. UNSURE = not proven.

---

## 1. App structure and lifecycle

VERIFIED (`modules/common/badgeware/__init__.py` L70-109, `launch`). An app is a package folder. The menu launches it with `os.chdir(path)`, `sys.path.insert(0, path)`, then `__import__(path)`. So `__init__.py` is simply run top to bottom at import time and "may block here". There is NO required `update()` or `init()` hook. The framework never calls `update` by name. You pass it to `run()` yourself.

Skeleton, adapted from `firmware/apps/menu/__init__.py` and `firmware/apps/iss_tracker/__init__.py`:

```python
import os, sys
os.chdir("/system/apps/my_app")              # optional, launch() already does chdir(path)
sys.path.insert(0, "/system/apps/my_app")    # optional, launch() already inserts it

badge.mode(HIRES)            # optional, default is LORES | VSYNC (160x120). Call BEFORE caching `screen`
screen.font = font.sins

def update():                # called once per frame, no args
    screen.text("hello", 10, 10)
    if badge.pressed(BUTTON_A):
        return "done"        # any non-None return ends run()

def on_exit():               # optional, see below
    pass

run(update)                  # blocks until update() returns non-None
```

`run` (VERIFIED, `__init__.py` L26-67, class `_run`):

```python
def __call__(self, update):
    badge.poll()
    self.start = badge.ticks
    parent = loop; builtins.loop = self
    try:
        while True:
            badge.clear()                       # clear to default_clear, pen=default_pen, cursor reset
            if (result := update()) is not None:
                self.result = result; return
            display.update()                    # push framebuffer, blocks on vsync/DMA
            badge.poll()                        # refresh buttons + ticks
            if self.duration is not None and self.ticks >= self.duration: return
    except Exception as e:
        fatal_error("Error!", get_exception(e)) # draws box, waits for a button, machine.reset()
    finally:
        badge.clear(); builtins.loop = parent
```

- Forms. `run(update)` returns the `_run` object (so `.result`), `@run` decorator works (`__init__` with one callable arg calls it), `run(duration=ms)(update)` for timed loops. DOC `api/builtins.md`.
- `loop` global is the active run object (`loop.ticks`, `loop.progress`, `loop.duration`, `loop.result`).
- Any uncaught `Exception` in `update()` goes to `fatal_error`, which blocks for a button press and then `machine.reset()`. KeyboardInterrupt is a BaseException so Ctrl-C passes through to the REPL (inferred from the `except Exception` clauses, UNSURE in practice).
- Frame rate. VERIFIED that there is no software frame limiter. The rate comes from `display.update()`. `badge.mode()` (`modules/common/badgeware/badge.py` L116-128) calls `display.set_vsync(bool(mode & VSYNC))` and `display.set_framerate(90)`. `set_framerate` picks the closest ST7789 FRCTRL2 rate, 90 Hz is an exact entry (`modules/c/st7789/st7789.cpp` L304-331). With VSYNC, `update()` spins until the TE pin is high (L204-210). Result is at most about 90 fps, lower when your update() is slow. Real fps is UNSURE (not measured). DOC `guides/time.md` says use `badge.ticks_delta` for frame-independent timing.
- Exiting back to the menu.
  - HOME button (rear) is wired by `launch()` to a falling-edge IRQ that calls `do_exit()` (your module's `on_exit` if callable) and then `reset()`, which waits for HOME to be released and calls `machine.reset()` (L14-23, L78-84). So HOME is a full hard reset into the menu. `BUTTON_HOME` is effectively reserved, do not use it for app logic.
  - Returning from your module (end of file, or `run()` returning) gives `launch` back `do_exit()`. `/system/main.py` (firmware/main.py) treats a non-None return value as a path to launch next, otherwise calls `reset()` which reboots to the menu. So just letting `run(update)` return exits the app.
  - `on_exit` is called if callable, else its value is returned verbatim by `launch` (comment in `firmware/apps/menu/__init__.py` L69). The menu sets `on_exit = run(update).result` to return the chosen app path.
  - After exit `launch` deletes every module that was newly imported (L99-109), so module state does not persist across launches. Use `State` or files.
- Globals injected as builtins (VERIFIED, no import needed), from `badgeware/__init__.py` L188-232 and `badge.py`:
  - From picovector (`for k, v in picovector.__dict__`): `image`, `color`, `brush`, `shape`, `vec2`, `mat3`, `rect`, `font`, `vector_font`, `pixel_font`, `spritesheet`, `algorithm`, `tween` (names confirmed in picovector-micropython `generated/picovector_bindings.c` L22-35, a couple of entries were not individually read).
  - `OFF X2 X4` (antialias), `LEFT CENTER RIGHT TOP MIDDLE BOTTOM CLIP ELLIPSES`.
  - `display`, `run`, `launch`, `loop`, `reset`, `fatal_error`.
  - `badge` (instance of `Badge`), `screen` (an `image` over the framebuffer, created inside `badge.mode()`), `LORES HIRES VSYNC FAST_UPDATE FULL_UPDATE MEDIUM_UPDATE DITHER`, `BUTTON_A BUTTON_B BUTTON_C BUTTON_UP BUTTON_DOWN BUTTON_HOME`.
  - `State`, `clamp`, `rnd`, `frnd`, `file_exists`, `is_dir`, `free`, `rtc`, `text` (has `text.scroll`), `add_glyph`, `add_sprite`.
- Must be imported: `badgeware` module for `set_brightness`, `message`, `DEFAULT_FONT` (`import badgeware`, or `from badgeware import State`), `wifi`, `secrets`, `fetch`, `requests`, `network`, `asyncio`, `powman`, `time`, `math`, `json`.
- `badge.mode()` rebuilds `screen` on every real mode change for Tufty (`badge.py` L133-138). Font and pen are carried over, but `screen.width/height` change. Set the mode first, before computing layout constants.

## 2. Drawing API

All VERIFIED in DOCS `api/image.md`, `api/font.md`, `api/color.md`, `api/shape.md`, plus `firmware/apps/menu/app.py` for real use. Signatures are DOC unless stated.

- Screen size. `screen.width`, `screen.height`, `badge.resolution` (tuple). VERIFIED `st7789_bindings.cpp` L113-123: LORES is 160x120 (default, every pixel is drawn doubled), HIRES (`badge.mode(HIRES)`, optionally `HIRES | VSYNC`) is 320x240. HIRES is 4 bytes per pixel RGBA (`get_framebuffer` len = 320*240*4).
- Clear. The loop clears for you every frame (`badge.default_clear = color.black`, can be set to `None`). Manual: `screen.pen = color.black; screen.clear()`.
- Pen. `screen.pen = color.white` or `color.rgb(r, g, b, a=255)`, `color.hsv(h,s,v,a)`, `color.oklch(l,c,h,a)`, or a `brush`. `screen.alpha = 0..255` is global alpha. Named palette (DawnBringer 16) such as `color.white color.black color.red color.lime color.orange color.yellow color.blue color.navy color.grey color.green color.grape color.brown color.taupe color.latte color.smoke color.cyan`. `c.with_alpha(a)`, `c.lighten(x)`, `c.darken(x)`, `c.mix(other, t)`.
- Raster primitives (integer, not antialiased).
  - `screen.put(x, y)`, `screen.get(x, y)`
  - `screen.rectangle(x, y, w, h)` or `screen.rectangle(rect(...))`
  - `screen.circle(x, y, r)` or `(vec2, r)`
  - `screen.line(x0, y0, x1, y1)` or `(vec2, vec2)`
  - `screen.triangle(x0,y0,x1,y1,x2,y2)` or three `vec2`
  - `screen.hspan(x, y, w)`, `screen.vspan(x, y, h)`
- Vector shapes (antialiased, sub-pixel). Set `screen.antialias = image.X2` or `image.X4` (or `OFF`). Build with `shape.rectangle(x,y,w,h)`, `shape.rounded_rectangle(x,y,w,h,r)` or `(x,y,w,h,r1,r2,r3,r4)`, `shape.circle(x,y,r)`, `shape.ellipse`, `shape.squircle(x,y,s,n)`, `shape.star(x,y,s,ro,ri)`, `shape.regular_polygon`, `shape.line(x1,y1,x2,y2,w)`, `shape.arc`, `shape.pie`, `shape.custom(points)`. Then `screen.shape(s)` (or a list), `screen.shapes([(shape, brush_or_color), ...])`. `s.stroke(width)` makes an outline. `s.transform = mat3().translate(x, y).scale(sx, sy)`. Real use in `firmware/apps/menu/app.py` L13, L65-82 and `modules/common/badgeware/__init__.py` L126-146 (`shape.rounded_rectangle(...)`, `screen.shape(...)`).
- Rect and vec2. `rect(x, y, w, h)` has `.x .y .w .h`, `.offset .deflate .inflate .intersection .intersects .contains .empty`. `vec2(x, y)`. `screen.window(x, y, w, h)` or `(rect)` returns a clipped sub-image (used by `message()` in `__init__.py` L122). `screen.clip` is a rect.
- Fonts.
  - ROM pixel fonts by attribute: `screen.font = font.sins` (default, `DEFAULT_FONT`), `font.ark`, `font.smart`, `font.absolute`, `font.nope`, `font.teatime`, `font.compass`, and about 36 more. List is `romfs/fonts/*.ppf` in the repo, 6px to 17px high. `font.<name>` resolves `/rom/fonts/<name>.ppf` (`picovector-micropython/native/font_native.cpp` L185).
  - `font.load(path_or_name)` handles `.af` vector and `.ppf` pixel. A bare name searches `/rom/fonts`, `/system/assets/fonts`, `/fonts`, `/assets`, then cwd, trying `.af` then `.ppf` (VERIFIED `font_native.cpp` L94-96). Vector fonts shipped in `/system/assets/fonts/`: `DynaPuff-Medium.af`, `IndieFlower-Regular.af`, `MonaSans-Medium.af` (VERIFIED in `firmware/assets/fonts/`). Raises `OSError` if not found.
  - Pixel font `size` argument is an integer scale (1 native, 2 double). Vector font `size` is points, default 12. Pixel fonts expose `.height` and `.name`.
- Text. `r = screen.text(msg, x, y, size=...)` or `(msg, vec2, size)`, returns the bounding `rect`. `screen.text(msg)` continues at `screen.cursor` (reset to 0,0 by `badge.clear()`).
  - Wrapped form: `screen.text(msg, rect(x,y,w,h), size, align=(image.CENTER, image.MIDDLE), overflow=image.ELLIPSES, line_height=1.0, word_spacing=1.0)`. Word wrap and inline markup only happen in the rect form. A `\n` always breaks.
  - Pitfall for networked data. In the rect form, `[pen:r,g,b]` and `[sprite:name]` (and any `add_glyph` name) are parsed as markup (DOC `api/text.md`, registry in `modules/common/badgeware/text.py` L36-39). A literal `[` in fetched text may be interpreted. Untested what an unknown tag does (UNSURE).
  - Measure: `w, h = screen.measure_text(msg, size)` or `screen.measure_text(msg, bounds_rect, size, line_height=..., word_spacing=...)` (wrapped height). Used in `firmware/apps/menu/app.py` L133 and `iss_tracker/__init__.py` L171. Measure with the same size you draw with.
  - Scroll helper: `upd = text.scroll(msg, font_face, font_size, target, speed, gap, align)` returns a function you call each frame (`modules/common/badgeware/text.py`).
  - Alignment constants: `image.LEFT/CENTER/RIGHT`, `image.TOP/MIDDLE/BOTTOM`, also hoisted to builtins `LEFT CENTER RIGHT TOP MIDDLE BOTTOM CLIP ELLIPSES`.
- Images.
  - `img = image.load("path.png")` for PNG, JPEG, GIF. `image.load(path, w, h)` decodes at a size (PNG and JPEG). `image.load(bytes)` decodes from memory (useful for fetched data). `img.load_into(path_or_bytes)` reuses the buffer, size must match. `image(w, h)` makes a blank off-screen image.
  - Blit: `screen.blit(img, x, y)` or `(img, vec2)`, scaled `screen.blit(img, rect(x,y,w,h), filter)`, cropped `screen.blit(img, src_rect, dst_rect, filter)`. Negative w or h flips. `image.NEAREST` is the default filter. Menu does `screen.blit(self.icon, rect(x, y, w, 24))` (`firmware/apps/menu/app.py` L89-97).
  - `img.width`, `img.height`, `img.alpha`, `img.spritesheet(cols, rows).sprite(c, r)`. Palettised PNGs are read-only.
  - Filters on screen: `screen.blur(r)`, `bloom`, `vignette`, `crt`, `dither`, etc. See DOC `api/image.md`.
  - `screen.batch([...])` batches commands.
- Pixel buffer: `memoryview(screen)` or `screen.raw`, RGBA premultiplied, 4 bytes per pixel (DOC `guides/performance.md`).

## 3. Input

VERIFIED `modules/common/badgeware/badge.py` L20-25, L178-196 and `modules/c/input/input.cpp`.

- Constants (builtins): `BUTTON_A BUTTON_B BUTTON_C BUTTON_UP BUTTON_DOWN BUTTON_HOME`. They are the `machine.Pin.board.BUTTON_*` Pin objects.
- `badge.pressed(btn=None)`, `badge.held(btn=None)`, `badge.released(btn=None)`, `badge.changed(btn=None)`. With an argument they return a bool (`button in _input.<x>`). With no argument they return a tuple of Pin objects, so `BUTTON_A in badge.pressed()` also works.
- Semantics are edge based per poll. `held = state`, `changed = state ^ previous`, `pressed = state & changed`, `released = ~state & changed` (`input.cpp` L44-58).
- `badge.poll()` (calls `_input.poll()`, `input.cpp` L92-121) does the following.
  - Samples the six GPIOs (active low).
  - Computes the changed mask against the previous poll.
  - Updates `badge.ticks` to `mp_hal_ticks_ms()`, and `ticks_delta` is the delta between polls.
  - On the very first poll only, folds in the wake-up button states (`powman_get_user_switches`) so the button that woke the board counts as pressed. `firmware/main.py` calls `badge.poll()` first to "eat" it.
  - So `pressed()` is only true for the one frame between two `poll()` calls. If your own loop does not call `poll()` regularly, pressed/held/released never update and `badge.ticks` is frozen. `run()` calls it for you after every frame.
- `badge.ticks` and `badge.ticks_delta` are frame-sampled, not live. Use `time.ticks_ms()` for live time.

## 4. Watchdog (the important one)

RESULT: I could NOT find any code that enables or feeds a hardware watchdog in the Badgeware firmware or its dependencies. This contradicts what you observed on the device, see UNSURE notes.

What I searched, all at the pinned versions:
- FW Python: `modules/common/**`, `modules/python/**`, `firmware/**`. No `WDT`, no `watchdog`.
- FW C: `modules/c/powman`, `modules/c/input`, `modules/c/st7789`, `board/`. The only watchdog uses are (a) `powman_wake_watchdog()` which returns `watchdog_caused_reboot()` (`powman.c` L53-55), exposed as `powman.WAKE_WATCHDOG` (`bindings.c` L29, L55), and (b) `powman_reset_into_msc()` calling `watchdog_reboot(0, SRAM_END, 0)` (`powman.c` L407-412). No `watchdog_enable` or `watchdog_update` anywhere in the repo.
- MicroPython fork `pimoroni/micropython` branches `bw-1.29.0-gc` (used by v3.1.x) and `bw-1.29.0`. `ports/rp2` only calls `watchdog_enable`/`watchdog_update` inside `machine_wdt.c` (L59, L66), which runs only when Python constructs `machine.WDT(0, timeout)`. Max timeout is 16777 ms on RP2350 (8388 ms on RP2040), `machine_wdt.c` L33-39. `machine.reset()` is itself `watchdog_reboot(0, SRAM_END, 0)` (`ports/rp2/modmachine.c` L75), so a reset by `machine.reset()` looks like a watchdog reset (`machine.reset_cause()` is WDT_RESET, and `powman.get_wake_reason()` returns `WAKE_WATCHDOG`).
- pimoroni-pico f5ad5244, picovector-micropython v3.1.0. No watchdog enable. (pimoroni-pico only has watchdog_reboot in badger2040/inky_frame libraries, not built for Tufty.)
- Badgeware Python never feeds anything. Neither `_run`, `badge.poll()`, `display.update()` nor `ST7789::update()` touch a WDT. The only non-Python service in the frame path is `mp_event_handle_nowait()` inside the vsync wait (`st7789.cpp` L204-210), which services USB, the scheduler and pending exceptions.

Hypotheses for what you saw (all UNSURE, none verified):
1. Something on your device or in your own code constructs `machine.WDT(0, timeout)`. If that happens, only `wdt.feed()` helps, nothing in `run()` or `poll()` does it. An ~8 s timeout would match an RP2040-style 8388 ms max, but this firmware allows up to 16.7 s so 8 s would have been chosen explicitly.
2. The reset is not a watchdog. For example HOME IRQ -> `reset()` (`launch()` L78-84), `fatal_error()` -> `machine.reset()` (`__init__.py` L185), or `wifi._tick()` failing a connect (below), which all end in `machine.reset()` and all report as a watchdog-ish reboot.
3. The device runs a build different from tag v3.1.x. I checked `bw-1.29.0` and `bw-1.29.0-gc` MicroPython branches only.
To settle this on device without guessing, read the watchdog CTRL register ENABLE bit (RP2350 `WATCHDOG_BASE`, I believe 0x400D8000, UNSURE, check `hardware/regs/addressmap.h`) before and after boot, and print `machine.reset_cause()` and `powman.get_wake_reason()` after the reset.

Practical rule until proven otherwise. If you run your own asyncio loop, do the equivalent of `run()` inside it each iteration (`badge.clear()`, draw, `display.update()`, `badge.poll()`), and if you find a WDT is armed, call `machine.WDT(0, <ms>).feed()` yourself (the rp2 `WDT` constructor re-arms, and `feed()` is `watchdog_update()`).

## 5. asyncio and networking

- asyncio exists. VERIFIED `board/manifest.py`: `include("$(MPY_DIR)/extmod/asyncio")`, plus `require("bundle-networking")` (mip, ntptime, ssl, requests, webrepl, urequests), `urllib.urequest`, `umqtt.simple`, `aioble`, `datetime`. And `freeze("../modules/python/")`, `freeze("../modules/common/")` (so `wifi`, `fetch`, `secrets`, `badgeware` are frozen).
- The framework does NOT use asyncio. VERIFIED by grep: no `asyncio` in `firmware/`, `modules/`, `examples/`, or DOCS. `run()` is a plain synchronous while loop with no scheduler hook. No documented asyncio pattern.
- To combine them (my inference, UNSURE, not run), skip `run()` and write your own loop in an `asyncio` task:
  ```python
  async def ui():
      while True:
          badge.clear(); draw(); display.update(); badge.poll()
          await asyncio.sleep_ms(0)
  asyncio.run(main())   # main gathers ui() and net tasks
  ```
  Caveats. `display.update()` blocks (DMA wait plus vsync wait) and does not yield to asyncio. HOME IRQ reset still works only when the VM runs scheduled callbacks.
- Official pattern for networking in a frame loop. Call `wifi.connect()` every frame until it returns True (as `iss_tracker` does) and do blocking `requests.get()` calls inside `update()` (`firmware/apps/iss_tracker/__init__.py` L136, L284-299). Or use the cooperative `fetch` module that slices work across frames.
- `wifi` module VERIFIED `modules/common/wifi.py`:
  - `wifi.connect(ssid=None, psk=None, timeout=60, retries=5)` returns bool and is non-blocking. First call creates `network.WLAN(STA_IF)`, `active(True)`, `wlan.connect()`. Later calls run `_tick()`. On timeout or error it retries, and after the retries are used up it calls `fatal_error(...)`, which resets the board. With no args it reads `secrets.WIFI_SSID` and `WIFI_PASSWORD`, and calls `fatal_error("Missing Details!", ...)` if the SSID is empty.
  - Timeouts use `badge.ticks`, which only moves when `badge.poll()` is called. In a custom loop, poll regularly or the timeout never fires.
  - Others: `wifi.is_connected()`, `status()` returns `(code, text)`, `disconnect()` (also powers down the radio), `ip()`/`ipv4()`, `ipv6()`, `subnet()`, `gateway()`, `nameserver()`, and the module attribute `wifi.wlan`.
  - If you want your own state machine or asyncio, use `network.WLAN` directly and skip this module.
- `secrets` VERIFIED `modules/common/secrets.py` and `firmware/secrets.py`. Format is a plain Python file:
  ```python
  WIFI_SSID = ""
  WIFI_PASSWORD = ""
  REGION = "eu"
  TIMEZONE = 0
  ```
  The frozen `secrets` module first tries `__import__("/secrets")` (root LittleFS, writable at runtime) and falls back to `/system/secrets`. Everything not starting with `__` is copied into the `secrets` module namespace. `secrets.require("KEY", ...)` calls `fatal_error` if a key is missing or empty. So you can add your own keys (API tokens) to the file and read them as `secrets.MY_KEY`. A copy in `/secrets.py` on the root volume wins over `/system/secrets.py`.
- `fetch` module VERIFIED `modules/common/fetch.py`, DOC `guides/networking.md`:
  ```python
  import fetch
  feed = fetch.url("https://api.example.com/x.json", every=60)   # every=0 or None means once
  # inside update():
  if feed:                       # truthiness pumps the fetch and is True on the frame a fresh response arrives
      data = feed.json()         # or feed.data() for text
  # feed.state is a status string, feed.error holds the last error
  fetch.url(url).to("/cache.bin")   # stream to a file, then read feed.path
  ```
  It calls `wifi.connect()` itself, uses non-blocking sockets (`setblocking(False)`, L219), TLS with `CERT_NONE` (no certificate verification, L233-238), a 10 s per-fetch timeout, and a 256 KB in-memory cap. There is also the lower level `fetch.AsyncFetch(host, port, use_tls)` with `fetch(path, ...)` and `update()`. It is a generator-based poller, not asyncio. DOCS example `if fetch:` looks like a typo, the instance is `feed` (`__bool__` is defined on the `url` class, L745).
- NTP. `rtc.time_from_ntp()` (`badgeware/rtc.py`, wraps `ntptime.settime()` then `localtime_to_rtc()`). `ntptime` gives UTC, TIMEZONE is applied by the clock app, not by the framework.

## 6. Persistent storage and `State`

VERIFIED `modules/common/badgeware/state.py` (also exported from `badgeware` and as builtin `State`).
- `State.load(name, defaults) -> bool`. Reads `/state/<name>.json`, does `defaults.update(data)` in place, returns True. If missing or invalid it writes `defaults` to the file and returns False.
- `State.save(name, data)` writes `json.dumps(data)` to `/state/<name>.json`. On OSError it creates `/state` and retries once.
- `State.modify(name, data)` is load, update, save. `State.delete(name)`.
- Values must be JSON types only.
- Filesystem layout VERIFIED `modules/common/_boot_fat.py` and `board/filesystem.cmake`.
  - `/` is a 1 MB LittleFS at the end of user flash, writable. `/state/`, `/secrets.py` (optional), and your own files go here. It has only about 256 blocks of 4 KB, so many tiny files are expensive (DOC `guides/filesystem.md`).
  - `/system` is a 12 MB FAT volume mounted `readonly=True` for MicroPython code. Only the host can write it in disk mode (double-tap RESET). Apps, `/system/apps`, `/system/assets`, `/system/main.py`, `/system/secrets.py` live here.
  - `/rom` is a 1 MB read-only ROMFS (fonts) baked into the firmware.
- Survives firmware update? CONFLICT, so UNSURE. `board/filesystem.cmake` builds `*-with-filesystem.uf2` with `dir2uf2 --fs-reserve ${PIMORONI_LFS_RESERVED}` plus a FAT image, so flashing that file replaces `/system` (apps, secrets) but appears to leave the reserved LittleFS area alone. Release v3.1.1 ships both `tufty-v3.1.1-micropython-with-filesystem.uf2` and `tufty-v3.1.1-micropython.uf2` (the latter has no FAT image). DOC `guides/filesystem.md` says `/` persists across firmware updates, but DOC `introduction/update-your-firmware.md` says an update "resets the filesystem, so anything stored on it will be wiped". Treat both `/system` and `/` as possibly wiped. Keep an off-device copy.
- `/system` and `/` also back up the FAT header: `modules/python/_msc.py` writes a `/.fsbackup` plus `.crc32` into the LittleFS root when entering disk mode.

## 7. Menu discovery and icons

VERIFIED `firmware/apps/menu/app.py` L100-115.
- No manifest, no `order.txt`. `Apps("/system/apps")` does `sorted(os.listdir(root))`, so apps are in alphabetical (byte) order of folder name (prefix with digits to reorder, the shipped `30_minutes_to_alpha_centauri` is an example).
- An entry is included if it is a directory, not named `menu`, and contains `__init__.py` or `__init__.mpy`.
- Display name is the folder name with `_` replaced by spaces and each word's first letter capitalised.
- Icon is `<app>/icon.png` if present, else `firmware/apps/menu/default_icon.png`. DOC says 24x24 PNG (`introduction/your-first-app.md`). Code draws it into `rect(x, y, width, 24)` with a coloured squircle behind it, so it is scaled to a height of 24. Any PNG that `image.load` accepts works (UNSURE about other sizes beyond the scaling).
- Menu grid: 3 columns x 2 rows per page, A/C move left/right, UP/DOWN move rows, B launches.

## 8. Backlight, caselights, battery, RTC, sleep

- Backlight. VERIFIED `__init__.py` L14-15: `badgeware.set_brightness(v)` is `display.backlight(v)`. Binding takes a float and does `(uint8)(v*255)` (`st7789_bindings.cpp` L72-76), 0.0 means off. A gamma 2.8 curve maps to PWM (`st7789.cpp` L271-285). Init sets 230/255, about 0.9 (L144). `set_brightness` is not a builtin, so `import badgeware` or call `display.backlight(0.5)` directly.
- Caselights (4 rear LEDs). VERIFIED `badge.py` L74-82, L198-205. `badge.caselights()` returns a list of 4 floats, `badge.caselights(0.5)` sets all, `badge.caselights(a, b, c, d)` sets each. Values 0.0 to 1.0, PWM at 500 Hz, gamma `v ** 2.2`.
- Battery. VERIFIED `badge.py` L142-160. `badge.battery_voltage()` (float, ADC with 1.1 V reference correction), `badge.battery_level()` (int 0-100 via a curve), `badge.usb_connected()`, `badge.is_charging()` (True only when USB present and CHARGE_STAT low). Also `badge.light_level()` (Tufty only, raw u16) and `badge.disk_free(mountpoint="/system")` returning `(total, used, free)`.
- RTC. VERIFIED `badgeware/rtc.py`. Builtin `rtc` wraps the PCF85063A: `rtc.datetime()` returns `(y, m, d, H, M, S, dow)`, `rtc.datetime(tuple)` sets, `localtime_to_rtc()`, `rtc_to_localtime()`, `time_from_ntp()`, `set_alarm(hours=, minutes=, seconds=)`, `alarm_status()`, `clear_alarm()`, `set_timer(secs)`, `timer_elapsed()`. The RP2350 clock is copied to or from the RTC at boot if the year is at least 2025.
- Sleep. VERIFIED `badge.py` L207-226 and `modules/c/powman/bindings.c`. `badge.sleep()` calls `powman.sleep()` and `badge.sleep(seconds)` calls `powman.goto_dormant_for(seconds)`. Both power the chip off through the POWMAN block. RAM is lost and wake is a full reboot, which ends at the menu. Wake helpers: `badge.wake_reason()`, `woken_by_button()`, `pressed_to_wake(btn)`, `woken_by_reset()`. Constants in `powman` (`WAKE_BUTTON_A..DOWN, WAKE_DOUBLETAP, WAKE_USER_SW, WAKE_VBUS_DETECT, WAKE_RTC, WAKE_ALARM, WAKE_RESET, WAKE_WATCHDOG, WAKE_UNKNOWN`).
- Preventing sleep. VERIFIED by absence: the Python framework and the C modules contain no idle or inactivity timer, so nothing sleeps or dims unless the app calls `badge.sleep()`, or the user long-presses RESET (`powman.c` `handle_long_press` / `long_press_sleep`, L414-480) or double-taps RESET (disk mode). Searched `modules/`, `firmware/` for idle, inactivity, auto sleep. Nothing found. There is no API to inhibit it because there is nothing to inhibit.

## 9. C++ source notes (watchdog and sleep)

- Power and sleep logic is `modules/c/powman/powman.c` (520 lines) and `bindings.c`. It runs as a constructor `powman_startup()` at boot (L482-521) handling double-tap RESET (disk mode), long press, and wake reason. `powman_off()` turns the chip off through POWMAN and relies on a wake source such as the button interrupt, the RTC alarm GPIO, or the POWMAN alarm timer. This is deep power off, not light sleep. `sleep()` and `goto_dormant_for()` never return.
- Watchdog: no enable or feed in this layer (section 4). Boot hang mitigations are an unrelated crt0 ROSC patch (`ci/pico-sdk-crt0-startup-rosc.patch`) and cyw43 bounded auth retry (`ci/cyw43-driver-bounded-auth-retry.patch`).
- Overclock. `board/pimoroni_tufty2350.h` has several `SYS_CLK_HZ` options (200, 250, 266, 399 MHz). Which is selected by default I did not resolve (the `#if` selectors were not read), UNSURE.
- HOME button also acts as BOOT. `reset()` busy-waits while HOME is held low before `machine.reset()` to avoid entering the bootloader (`__init__.py` L18-23).

## Quick idioms

```python
# text, centred, with wrap
screen.font = font.sins
r = rect(8, 8, screen.width - 16, screen.height - 16)
screen.pen = color.white
screen.text(msg, r, align=(image.CENTER, image.MIDDLE), overflow=image.ELLIPSES)

# buttons
if badge.pressed(BUTTON_A): ...        # edge, this frame
if badge.held(BUTTON_UP): ...          # level
if badge.released(BUTTON_C): ...

# cooperative HTTP inside the frame loop
import fetch, wifi
feed = fetch.url("https://example.com/data.json", every=30)
def update():
    if feed:
        data = feed.json()
    ...
run(update)
```
