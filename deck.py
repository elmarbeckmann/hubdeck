#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["streamdeck>=0.9.6", "pillow>=10.1", "websocket-client>=1.7"]
# ///
"""Stream Deck control deck for the smart home.

It talks to the hubs directly: Philips Hue Bridge (CLIP v2), IKEA DIRIGERA (local API) and tado
(cloud). A source adapter per hub translates it into one internal model (home, rooms, accessories,
services, HAP characteristics) and translates writes back. Rooms with the same name from different
hubs merge into one room.

Home page: home, scenes, status, automations, all lights off, rooms. Room page: devices. A short
press does the main action, a long press (0.5 s) opens the detail page. Locks, garage doors and the
security system need a 1 s hold. Writes show at once and revert (key flashes red) when they fail.

  uv run deck.py pair hue|ikea|tado [--host IP]   # one-time pairing, asks for a button press
  uv run deck.py status                           # paired hubs, reachability, counts
  uv run deck.py unpair hue|ikea|tado             # forget the credentials of a hub
  uv run deck.py names                            # rooms, devices, scenes with source, id, current name (names.json)
  uv run deck.py serve   # owns the Stream Deck (Elgato app must be closed)
  uv run deck.py demo    # same deck against a fake home, no hub needed
  uv run deck.py test    # self-check, no hardware and no network needed
  uv run deck.py shots [folder]   # write PNG renders of some pages (default: a new private folder, printed)
  uv run deck.py shots --live     # the same for the real home (paired hubs, not tado)
"""
import argparse
import base64
import colorsys
import copy
import fcntl
import fnmatch
import functools
import hashlib
import hmac
import http.client
import json
import math
import os
import queue
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.parse
from collections import namedtuple

CRED_DIR = os.path.expanduser("~/.config/stream-deck-home")
CRED_FILE = os.path.join(CRED_DIR, "credentials.json")
NAMES_FILE = os.path.join(CRED_DIR, "names.json")  # optional: name aliases and hidden items. No secrets in it.
DEFAULT_HIDE = ["dirigera_integration_*"]  # hidden when names.json does not exist: automatic DIRIGERA integration scenes
NAMES_POLL = 2.0  # seconds between two checks of the mtime of names.json
FULL_REFRESH = 600  # seconds between two full fetches from Hue and DIRIGERA (safety net for missed events)
BRIGHTNESS = 60
DIM = 10  # brightness while idle
IDLE_MINUTES = 5  # no key press for this long: dim the deck. The first press only wakes it.
LONG = 0.5  # seconds: a long press opens the detail page
HOLD = 1.0  # seconds: locks, garage doors and the security system act after this hold
REPEAT_DELAY, REPEAT = 0.5, 0.2  # auto-repeat of the +/- keys while held
FLASH = 0.6  # seconds a key flashes after a scene or a failed write
RETRY, RETRY_MAX = 2.0, 60.0  # seconds before a hub is tried again; doubles up to the maximum
HTTP_TIMEOUT = 10.0  # seconds for one request to a hub
SSE_TIMEOUT = 300.0  # seconds without a byte from the Hue event stream before it reconnects
WS_PING = 30.0  # seconds of silence on the DIRIGERA WebSocket before a ping
COALESCE = 0.05  # seconds: writes to one device within this time become one request
HUE_INTERVAL = 0.1  # seconds between Hue light commands (Hue allows about 10 per second)
HUE_GROUP_INTERVAL = 1.0  # seconds between Hue grouped_light commands (Hue allows about 1 per second)
IKEA_INTERVAL = 0.1  # seconds between DIRIGERA commands
TADO_POLL = 15 * 60  # seconds between tado polls. Free accounts get about 100 requests per day: calibration knob
TADO_COALESCE = 3.0  # seconds: tado target presses within this time become one request
RELOCATE_TRIES = 5  # most addresses that relocate checks per run
TADO_RATE_WAIT = 3600.0  # seconds to wait after HTTP 429 when tado sends no Retry-After
TADO_TERMINATION = {"type": "TADO_MODE"}  # overlay ends at the next time block, like the tado app's default
TADO_TERMINATION_FALLBACK = {"type": "MANUAL"}  # used once when tado answers HTTP 422 to the line above
TADO_DEFAULT_TARGET = 21.0  # degrees when "Heat" is chosen and tado has no target yet
IKEA_BLINDS_INVERTED = True  # DIRIGERA: blindsTargetLevel 0 = open, 100 = closed (unverified). HAP: 100 = open
PAIR_SECONDS, PAIR_STEP = 180, 2  # pairing: how long and how often the hub is asked
TADO_CLIENT = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

# HAP short UUIDs of the services and characteristics (Apple's base UUID is added by HomeKit).
KINDS = {"43": "light", "47": "outlet", "49": "switch", "40": "fan", "B7": "fan", "4A": "thermostat",
         "BC": "heater", "45": "lock", "41": "garage", "8C": "cover", "81": "cover", "8B": "cover",
         "7E": "security", "BB": "purifier", "D0": "valve", "8A": "temp", "82": "humidity",
         "80": "contact", "85": "motion", "86": "occupancy", "83": "leak", "87": "smoke", "7F": "co",
         "84": "lux", "8D": "air"}  # 96 (battery) is no device: it only sets the low battery badge
ON, BRI, HUE, SAT, CT, ACTIVE, SPEED = "25", "8", "13", "2F", "CE", "B0", "29"
CURT, TARGT, CURMODE, TARMODE, HUM = "11", "35", "F", "33", "10"
LOCKC, LOCKT, DOORC, DOORT, OBST, POSC, POST, SECC, SECT = "1D", "1E", "E", "32", "24", "6D", "7C", "66", "67"
HMODE, HEATTH, COOLTH = "B2", "12", "D"
CONTACT, MOTION, OCC, LEAK, SMOKE, CO, LUX, LOWBAT, BATLVL, CHARGING = "6A", "22", "71", "70", "76", "69", "6B", "79", "68", "8F"
PM25 = "C6"
BASE_UUID = "-0000-1000-8000-0026BB765291"
# Optimistic mirror: writing the target char also moves the current-state char at once.
MIRROR = {LOCKT: (LOCKC, lambda v: v), POST: (POSC, lambda v: v), SECT: (SECC, lambda v: v),
          DOORT: (DOORC, lambda v: 2 if v == 0 else 3)}  # garage: opening / closing
INT_FORMATS = {"int", "uint8", "uint16", "uint32", "uint64"}
TOGGLE = {"light", "outlet", "switch", "fan", "purifier", "valve"}  # short press toggles
HOLD_KINDS = {"lock", "garage", "security"}  # short press does nothing, 1 s hold acts
OPENS_DETAIL = {"thermostat", "heater", "cover"}  # short press opens the detail page too
SENSORS = {"temp", "humidity", "contact", "motion", "occupancy", "leak", "smoke", "co", "lux", "air"}
HAZARDS = {"leak": LEAK, "smoke": SMOKE, "co": CO}
LOW_BATTERY = 20  # percent

BLACK, DARK, OFF = (0, 0, 0), (38, 38, 38), (45, 45, 45)
GREY, BLUE, WARM, GREEN, RED, ORANGE = (75, 75, 75), (0, 95, 170), (190, 110, 20), (20, 130, 55), (200, 30, 30), (210, 120, 0)
WHITE, PURPLE = (255, 255, 255), (110, 55, 160)
HUES = [("Red", 0), ("Orange", 30), ("Yellow", 55), ("Green", 120), ("Cyan", 180), ("Blue", 240),
        ("Purple", 280), ("Pink", 330)]
GLYPH = {"security": "shield", "leak": "drop", "humidity": "drop", "smoke": "cloud", "occupancy": "person",
         "lux": "sun", "air": "cloud", "heater": "flame", "thermostat": "thermo", "cover": "blinds", "light": "bulb"}  # kind -> glyph
MODES = [("Off", None), ("Heat", 1), ("Cool", 2), ("Auto", 0)]  # name, HeaterCooler target state

# cat, on and rgb only matter for the look (see paint_*): cat is the kind of key (a navigation key such as
# "home", "scenes", "alert", "auto", "off", "back", or "room", "light", "dev", "climate", "sensor", or ""
# for a plain button), on is lit or not, rgb is the light color or the category color of the device.
Key = namedtuple("Key", "label glyph fill bar badge low sub hint ident tap hold long repeat sel cat on rgb note",
                 defaults=("", None, DARK, None, "", False, "", "", "", None, None, None, False, False, "", False, None, ""))
# note: a second small line under the label (the room name, when two keys show the same name)
THEME = "graphite"  # key look: graphite, vivid or neon. Change with --theme or STREAM_DECK_HOME_THEME (docs/themes.html)
THEMES = ("graphite", "vivid", "neon")
DEVICE_RGB = {"outlet": (52, 199, 89), "switch": (52, 199, 89), "fan": (90, 200, 250), "purifier": (90, 200, 250),
              "valve": (90, 200, 250), "cover": (90, 200, 250), "garage": (255, 159, 10)}  # devices without a light color
ALERT_RGB, SAFE_RGB, WARN_RGB, INFO_RGB = (255, 69, 58), (48, 209, 88), (255, 159, 10), (10, 132, 255)
EMPTY = Key(fill=BLACK)
Dev = namedtuple("Dev", "sid name acc room kind")


def short(uuid):
    """HAP short UUID ('4A') of a full UUID; other UUIDs stay as they are."""
    head, _, rest = uuid.upper().partition("-")
    return f"{int(head, 16):X}" if "-" + rest == BASE_UUID else uuid


def full(short_id):
    return f"{int(short_id, 16):08X}{BASE_UUID}"


def clamp(ch, value):
    """Round to the characteristic's step, limit to its min/max, convert to its format."""
    if ch.get("format") == "bool":
        return bool(value)
    step, lo, hi = ch.get("step"), ch.get("min"), ch.get("max")
    if step:
        value = round(round(value / step) * step, 4)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return int(round(value)) if ch.get("format") in INT_FORMATS else value


def light_color(on, hue=None, sat=None, bri=None, ct=None):
    """Key color of a light: its real color scaled by brightness. Dark grey when off."""
    if not on:
        return OFF
    level = 0.2 + 0.8 * (1.0 if bri is None else max(0, min(100, bri)) / 100)  # a dim light stays visible
    if hue is not None and sat is not None:
        rgb = [c * 255 for c in colorsys.hsv_to_rgb(hue / 360, sat / 100, 1)]
    elif ct is not None:  # warm 370 mireds to cool 154 mireds
        t = max(0.0, min(1.0, (370 - ct) / (370 - 154)))
        rgb = [a + (b - a) * t for a, b in zip((255, 160, 60), (190, 215, 255))]
    else:
        rgb = [255, 200, 120]
    return tuple(round(c * level) for c in rgb)


class Model:
    """The home as the bridge reports it, with live values."""

    def __init__(self):
        self.homes, self.svc, self.accs, self.devs = [], {}, {}, {}

    def load(self, snap):
        self.homes = snap.get("homes", [])
        self.svc, self.accs, self.devs = {}, {}, {}
        for home in self.homes:
            devs = []
            for acc in home["accessories"]:
                self.accs[acc["id"]] = acc
                found = []
                for s in acc["services"]:
                    self.svc[s["id"]] = {"acc": acc["id"], "chars": {short(c["type"]): c for c in s["chars"]}}
                    if short(s["type"]) in KINDS:
                        found.append(s)
                for s in found:
                    name = acc["name"] if len(found) == 1 or not s["name"] else s["name"]
                    devs.append(Dev(s["id"], name, acc["id"], acc["room"], KINDS[short(s["type"])]))
            self.devs[home["id"]] = sorted(devs, key=lambda d: (d.name.lower(), d.sid))

    def char(self, sid, ch):
        return self.svc.get(sid, {}).get("chars", {}).get(ch)

    def val(self, sid, ch, default=None):
        c = self.char(sid, ch)
        return default if c is None or c["value"] is None else c["value"]

    def set(self, sid, ch, value):
        if (c := self.char(sid, ch)) is not None:
            c["value"] = value

    def power(self, sid):
        return ON if self.char(sid, ON) else ACTIVE

    def reachable(self, acc):
        return self.accs.get(acc, {}).get("reachable", True)

    def low(self, acc):
        """True when a service of the accessory reports a low battery."""
        for s in self.svc.values():
            if s["acc"] == acc:
                level, flag = s["chars"].get(BATLVL), s["chars"].get(LOWBAT)
                charging = s["chars"].get(CHARGING)
                if flag and flag["value"] == 1 or (level and level["value"] is not None and level["value"] < LOW_BATTERY
                                                   and not (charging and charging["value"] == 1)):
                    return True
        return False

    def apply(self, msg):
        if msg["type"] == "char":
            if (c := self.char(msg.get("service"), short(msg.get("char", "")))) is not None:
                c["value"] = msg.get("value")
        elif msg["type"] == "reach" and msg.get("accessory") in self.accs:
            self.accs[msg["accessory"]]["reachable"] = bool(msg.get("reachable"))


class UI:
    """All deck logic without hardware: pages, key presses, optimistic writes. Time is passed in."""

    def __init__(self, link, now=0.0):
        self.link, self.model = link, Model()
        self.connected = link.connected
        self.hid = None
        self.stack = [("home", 0)]
        self.flash = {}  # ident -> (until, color)
        self.pending = {}  # request id -> (undo functions, ident, kind)
        self.req = 0
        self.held, self.swallow, self.cur = None, None, [EMPTY] * 15
        self.last_input, self.alerts_seen = now, 0
        if self.connected:
            self.send({"op": "snapshot"})

    # --- bridge ---

    def send(self, msg):
        self.req += 1
        return self.req if self.link.send({**msg, "req": self.req}) else None

    def handle(self, ev, now):
        kind = ev[0]
        if kind == "key":
            self.key_event(ev[1], ev[2], now)
        elif kind == "conn":
            self.connected = ev[1]
            if ev[1]:
                self.send({"op": "snapshot"})
            else:  # answers of the lost connection never come
                for req in list(self.pending):
                    self.finish(req, False, now)
        elif kind == "msg":
            self.on_msg(ev[1], now)

    def on_msg(self, msg, now):
        t = msg.get("type")
        if t == "snapshot":
            self.model.load(msg)
            ids = [h["id"] for h in self.model.homes]
            if self.hid not in ids:
                self.hid = next((h["id"] for h in self.model.homes if h.get("primary")), ids[0] if ids else None)
            self.validate()
            if (count := self.alerts()) > self.alerts_seen:
                self.last_input = now  # something needs attention: wake the deck
            self.alerts_seen = count
        elif t in ("char", "reach"):
            self.model.apply(msg)
        elif t == "result" and msg.get("req") in self.pending:
            self.finish(msg["req"], msg.get("ok", False), now)

    def finish(self, req, ok, now):
        undo, ident, kind = self.pending.pop(req)
        if not ok:
            for fn in undo:
                fn()
        if kind == "scene" or not ok:
            self.flash[ident] = (now + FLASH, WHITE if ok else RED)

    def request(self, msg, undo, ident, kind, now):
        req = self.send(msg)
        if req is None:  # bridge offline: revert at once
            for fn in undo:
                fn()
            self.flash[ident] = (now + FLASH, RED)
        else:
            self.pending[req] = (undo, ident, kind)

    def write(self, writes, ident, now):
        """Apply writes locally at once and send them. A failed result reverts them."""
        for sid, ch, value in writes:
            c = self.model.char(sid, ch)
            if c is None or not c.get("writable", True):
                continue
            value = clamp(c, value)
            touched = [(sid, ch)]
            if ch in MIRROR and self.model.char(sid, MIRROR[ch][0]):
                touched.append((sid, MIRROR[ch][0]))
            old = {t: self.model.val(*t) for t in touched}
            self.model.set(sid, ch, value)
            if ch in MIRROR and len(touched) == 2:
                self.model.set(sid, touched[1][1], MIRROR[ch][1](value))
            undo = [functools.partial(self.model.set, t[0], t[1], v) for t, v in old.items()]
            self.request({"op": "write", "service": sid, "char": c["type"], "value": value}, undo, ident, "write", now)

    # --- state ---

    def home(self):
        return next((h for h in self.model.homes if h["id"] == self.hid), None)

    def devs(self):
        return self.model.devs.get(self.hid, [])

    @property
    def page(self):
        return self.stack[-1]

    def validate(self):
        """Drop pages whose room or device vanished from the snapshot. True when the stack changed."""
        rooms = {r["id"] for r in (self.home() or {"rooms": []})["rooms"]}
        sids = {d.sid for d in self.devs()}
        old = self.stack
        self.stack = [p for p in old if (p[0] != "room" or p[1] in rooms) and (p[0] != "detail" or p[1] in sids)] or [("home", 0)]
        return self.stack != old

    def is_on(self, d):
        m, v = self.model, lambda c, default=None: self.model.val(d.sid, c, default)
        k = d.kind
        if k in TOGGLE or k == "heater":
            return bool(v(m.power(d.sid)))
        return {"thermostat": lambda: v(TARMODE, 0) != 0, "lock": lambda: v(LOCKC) == 0,
                "garage": lambda: v(DOORC) not in (None, 1), "cover": lambda: v(POSC, 0) > 0,
                "security": lambda: v(SECC, 3) != 3}.get(k, lambda: False)()

    def hazard(self, d):
        return d.kind in HAZARDS and bool(self.model.val(d.sid, HAZARDS[d.kind]))

    def alerts(self):
        """Accessories that are unreachable or low on battery, plus detected leaks, smoke and CO."""
        accs = {a for a in {d.acc for d in self.devs()} if not self.model.reachable(a) or self.model.low(a)}
        return len(accs) + sum(self.hazard(d) for d in self.devs())

    # --- keys ---

    def keys(self, now):
        if not self.connected or not self.model.homes:
            out = [EMPTY] * 15
            if not self.connected:  # text only, so that the hub list has room
                out[0] = Key(getattr(self.link, "offline", "Offline"), None, RED, ident="conn")
            else:
                out[0] = Key("Loading", "alert", GREY, ident="conn")
            return out
        p = self.page  # on_msg validates after each snapshot, so the page is not stale
        out = {"home": p_home, "room": p_room, "detail": p_detail, "scenes": p_scenes, "autos": p_autos,
               "status": p_status}[p[0]](self)
        for i, k in enumerate(out):
            if k.ident in self.flash and self.flash[k.ident][0] > now:
                out[i] = k._replace(fill=self.flash[k.ident][1], sel=False, cat="", on=False, rgb=None)  # plain key in the flash color
        return out

    def brightness(self, now):
        return DIM if now - self.last_input >= IDLE_MINUTES * 60 else BRIGHTNESS

    # --- key presses ---

    def key_event(self, i, down, now):
        if down:
            if self.brightness(now) == DIM:  # first press only wakes the deck
                self.last_input, self.swallow = now, i
                return
            self.last_input = now
            if self.connected and self.model.homes and self.validate():
                return  # the page was stale: the user did not see the new page, so ignore the press
            self.cur = k = self.keys(now)[i]
            if k.repeat and k.tap:
                self.act(k.tap, k.ident, now)
                self.held = {"key": i, "k": k, "t0": now, "fired": False, "next": now + REPEAT_DELAY}
            elif k.hold or k.long:
                self.held = {"key": i, "k": k, "t0": now, "fired": False}
            elif k.tap:
                self.act(k.tap, k.ident, now)
            return
        if self.swallow == i:
            self.swallow = None
            return
        h, self.held = self.held, None
        if not h or h["key"] != i or h["fired"]:
            return
        k, dur = h["k"], now - h["t0"]
        if k.hold or k.repeat:
            if k.long and dur >= LONG:
                self.act(k.long, k.ident, now)
        elif dur >= LONG:
            self.act(k.long, k.ident, now)
        elif k.tap:
            self.act(k.tap, k.ident, now)

    def tick(self, now):
        self.flash = {i: f for i, f in self.flash.items() if f[0] > now}
        h = self.held
        if not h:
            return
        k, dur = h["k"], now - h["t0"]
        if k.repeat:
            while self.held and now >= h["next"]:
                self.act(k.tap, k.ident, now)
                h["next"] += REPEAT
        elif k.hold:
            if not h["fired"] and dur >= HOLD:
                h["fired"] = True
                self.act(k.hold, k.ident, now)
        elif not h["fired"] and dur >= LONG:
            h["fired"] = True
            self.act(k.long, k.ident, now)

    def next_deadline(self, now):
        """Time of the next timed event (hold, repeat, flash end, dimming), or None."""
        times = [until for until, _ in self.flash.values()]
        times.append(self.last_input + IDLE_MINUTES * 60)
        if (h := self.held) and not h["fired"]:
            k = h["k"]
            times.append(h["next"] if k.repeat else h["t0"] + (HOLD if k.hold else LONG))
        return min(t for t in times if t > now) if any(t > now for t in times) else None

    def go(self, page=None, back=False):
        if self.held:
            self.swallow, self.held = self.held["key"], None
        if back and len(self.stack) > 1:
            self.stack.pop()
        elif page:
            self.stack.append(page)

    def act(self, a, ident, now):
        op, m = a[0], self.model
        if op == "goto":
            self.go(a[1])
        elif op == "back":
            self.go(back=True)
        elif op == "home_next":
            ids = [h["id"] for h in m.homes]
            self.hid, self.stack = ids[(ids.index(self.hid) + 1) % len(ids)], [("home", 0)]
        elif op == "flip":
            self.stack[-1] = self.page[:-1] + ((min(self.page[-1], a[1] - 1) + 1) % a[1],)
        elif op == "room_of":
            room = m.accs.get(a[1], {}).get("room")
            if any(r["id"] == room for r in self.home()["rooms"]):
                self.stack = [("home", 0), ("room", room, 0)]
        elif op == "toggle":
            ch = m.power(a[1])
            self.write([(a[1], ch, not m.val(a[1], ch))], ident, now)
        elif op == "power":
            self.write([(sid, m.power(sid), a[2]) for sid in a[1]], ident, now)
        elif op == "w":
            self.write(list(a[1]), ident, now)
        elif op == "adjust":  # sid, char, delta
            sid, ch, delta = a[1:]
            c = m.char(sid, ch)
            if c is not None:
                writes = [(sid, ch, m.val(sid, ch, 0) + delta)]
                if ch == BRI and delta > 0 and m.char(sid, m.power(sid)) and not m.val(sid, m.power(sid)):
                    writes.append((sid, m.power(sid), True))  # a dimmed-up light switches on
                self.write(writes, ident, now)
        elif op == "scene":
            self.request({"op": "scene", "id": a[1]}, [], ident, "scene", now)
        elif op == "trigger":  # id, enabled
            trig = next((t for h in m.homes for t in h["triggers"] if t["id"] == a[1]), None)
            if trig is None:  # the automation vanished since the key was drawn
                return
            was = trig["enabled"]
            trig["enabled"] = a[2]
            self.request({"op": "trigger", "id": a[1], "enabled": a[2]},
                         [functools.partial(trig.__setitem__, "enabled", was)], ident, "trigger", now)


# --- pages: each returns 15 keys ---

def place(out, items, first, n):
    """Put items on keys first..14. With too many items the last key pages and items use first..13."""
    slots = 15 - first
    per = slots if len(items) <= slots else slots - 1
    pages = max(1, -(-len(items) // per))
    n = min(n, pages - 1)
    for i, k in enumerate(items[n * per:(n + 1) * per]):
        out[first + i] = k
    if pages > 1:
        out[14] = Key(f"Page\n{n + 1}/{pages}", "next", tap=("flip", pages))
    return out


def back_key():
    return Key("Back", "back", tap=("back",), cat="back", on=True)


def light_rgb(m, sid):
    """The real color of a light at full brightness: hue and saturation, else white from mireds."""
    return light_color(True, m.val(sid, HUE), m.val(sid, SAT), 100, m.val(sid, CT))


def p_home(ui):
    m, home = ui.model, ui.home()
    lights = [d for d in ui.devs() if d.kind == "light"]
    on = sum(ui.is_on(d) for d in lights)
    alerts = ui.alerts()
    out = [EMPTY] * 15
    out[0] = Key(home["name"], "home", WARM if on else DARK, sub=f"{on} on", ident="home", cat="home", on=True,
                 tap=("home_next",) if len(m.homes) > 1 else None)
    out[1] = Key("Scenes", "play", tap=("goto", ("scenes", 0)), cat="scenes", on=True)
    out[2] = Key("Status", "info", RED if alerts else DARK, badge=str(alerts) if alerts else "", tap=("goto", ("status", 0)),
                 cat="alert" if alerts else "off", on=True)
    out[3] = Key("Automations", "clock", tap=("goto", ("autos", 0)), cat="auto", on=True)
    out[4] = Key("All\nlights off", "power", tap=("power", tuple(d.sid for d in lights), False), cat="off", on=True)
    rooms = []
    for r in home["rooms"]:
        ds = [d for d in ui.devs() if d.room == r["id"] and d.kind not in SENSORS]
        lit = [d for d in ds if d.kind == "light" and ui.is_on(d)]
        cols = [light_rgb(m, d.sid) for d in lit]
        avg = tuple(round(sum(c[i] for c in cols) / len(cols)) for i in range(3)) if cols else None  # mean light color
        rooms.append(Key(r["name"], None, WARM if lit else DARK, sub=f"{sum(ui.is_on(d) for d in ds)}/{len(ds)}",
                         tap=("goto", ("room", r["id"], 0)), cat="room", on=bool(lit), rgb=avg))
    return place(out, rooms, 5, ui.page[1])


def p_room(ui):
    _, rid, n = ui.page
    ds = [d for d in ui.devs() if d.room == rid]
    switchable = [d for d in ds if d.kind in TOGGLE and ui.model.reachable(d.acc)]
    any_on = any(ui.is_on(d) for d in switchable)
    out = [EMPTY] * 15
    out[0] = back_key()
    if switchable:
        out[1] = Key("All off" if any_on else "All on", "power", tap=("power", tuple(d.sid for d in switchable), not any_on),
                     cat="off", on=True)
    return place(out, [dev_key(ui, d) for d in ds], 2, n)


def dev_key(ui, d):
    """Key of one device: glyph, fill, bar, badges and the actions for press, hold and long press."""
    m, sid, kind = ui.model, d.sid, d.kind
    v = lambda c, default=None: m.val(sid, c, default)
    reach = m.reachable(d.acc)
    k = dict(label=d.name, glyph=GLYPH.get(kind, kind), ident=sid, low=m.low(d.acc))
    detail = ("goto", ("detail", sid, 0))
    if kind == "light":
        on = ui.is_on(d)
        k.update(glyph="bulb", fill=light_color(on, v(HUE), v(SAT), v(BRI), v(CT)),
                 bar=v(BRI, 100) / 100 if on and m.char(sid, BRI) else None, tap=("toggle", sid), long=detail)
    elif kind in TOGGLE:
        k.update(fill=BLUE if ui.is_on(d) else OFF, tap=("toggle", sid), long=detail)
    elif kind == "thermostat" or kind == "heater":
        mode = v(TARMODE, 0) if kind == "thermostat" else (v(HMODE, 0) if v(ACTIVE) else None)
        k.update(glyph="thermo" if kind == "thermostat" else "flame",
                 sub=f"{v(CURT, 0):.1f}°" if m.char(sid, CURT) else "",
                 fill=ORANGE if mode == 1 else BLUE if mode == 2 else DARK,
                 tap=detail, long=detail)
    elif kind == "cover":
        pos = v(POSC, v(POST, 0))
        k.update(glyph="blinds", sub=f"{pos:.0f}%", bar=pos / 100, tap=detail, long=detail)
    elif kind == "lock":
        state = v(LOCKC)
        k.update(glyph="lock" if state == 1 else "unlock", fill=GREEN if state == 1 else RED if state in (2, 3) else ORANGE,
                 hint="hold", hold=("w", ((sid, LOCKT, 0 if state == 1 else 1),)), long=detail)
    elif kind == "garage":
        state = v(DOORC)
        k.update(glyph="garage" if state == 1 else "garage_open", fill=DARK if state == 1 else ORANGE,
                 hint="hold", hold=("w", ((sid, DOORT, 1 if state == 0 else 0),)), long=detail)
    elif kind == "security":
        state = v(SECC, 3)
        k.update(fill=RED if state == 4 else DARK if state == 3 else BLUE, hint="hold",
                 hold=("w", ((sid, SECT, 3 if state != 3 else 1),)), long=detail)
    elif kind == "purifier" or kind == "fan":
        k.update(fill=BLUE if ui.is_on(d) else OFF, tap=("toggle", sid), long=detail)
    else:  # sensors
        k.update(**sensor_face(ui, d))
    # look: what the themes need
    if kind == "light":
        k.update(cat="light", on=ui.is_on(d), rgb=light_rgb(m, sid))
    elif kind in ("thermostat", "heater"):
        k.update(cat="climate", on=True)
    elif kind in SENSORS:
        hot = ui.hazard(d) or (kind == "contact" and v(CONTACT) == 1)
        moving = kind in ("motion", "occupancy") and bool(v(MOTION if kind == "motion" else OCC))
        k.update(cat="sensor", on=bool(hot or moving), rgb=ALERT_RGB if hot else WARN_RGB if moving else None)
    else:
        rgb = {"lock": SAFE_RGB if v(LOCKC) == 1 else WARN_RGB, "security": INFO_RGB if v(SECC, 3) != 3 else SAFE_RGB}.get(kind)
        k.update(cat="dev", on=ui.is_on(d) or kind in HOLD_KINDS, rgb=rgb or DEVICE_RGB.get(kind, SAFE_RGB))
    if not reach:
        k.update(fill=GREY, badge="!", bar=None, tap=None, hold=None, long=None, hint="", cat="light" if kind == "light" else "dev",
                 on=False, rgb=None)
    return Key(**k)


def sensor_face(ui, d):
    v = lambda c: ui.model.val(d.sid, c)
    kind = d.kind
    if kind == "temp":
        return dict(sub=f"{v(CURT) or 0:.1f}°")
    if kind == "humidity":
        return dict(sub=f"{v(HUM) or 0:.0f}%")
    if kind == "lux":
        return dict(sub=f"{v(LUX) or 0:.0f}lx")
    if kind == "air":
        return dict(sub=f"{v(PM25) or 0:.0f}µg")
    if kind == "contact":
        return dict(glyph="door_open", fill=RED, sub="Open") if v(CONTACT) == 1 else dict(glyph="door", sub="Closed")
    if kind in ("motion", "occupancy"):
        on = bool(v(MOTION if kind == "motion" else OCC))
        return dict(fill=ORANGE if on else DARK, sub="Motion" if on else "Clear")
    if on := ui.hazard(d):
        return dict(fill=RED, sub="ALARM")
    return dict(sub="ok")


def power_items(ui, d):
    on = ui.is_on(d)
    return [Key("On" if on else "Off", "power", BLUE if on else OFF, ident=f"{d.sid}:power", tap=("toggle", d.sid))]


def readout(label, glyph, sub, fill=DARK, bar=None, **kw):
    return Key(label, glyph, fill, bar=bar, sub=sub, **kw)


def p_detail(ui):
    _, sid, n = ui.page
    d = next(d for d in ui.devs() if d.sid == sid)
    m = ui.model
    v = lambda c, default=None: m.val(sid, c, default)
    has = lambda c: m.char(sid, c) is not None
    kk = lambda label, tap=None, **kw: Key(label, ident=f"{sid}:{label}", tap=tap, **kw)
    w = lambda c, val: ("w", ((sid, c, val),))
    items = []
    if d.kind == "light":
        on = ui.is_on(d)
        power = m.power(sid)
        bri = v(BRI, 100)
        items.append(Key(f"{'On' if on else 'Off'}" + (f"\n{bri:.0f}%" if has(BRI) else ""), "power",
                         light_color(on, v(HUE), v(SAT), v(BRI), v(CT)), bar=bri / 100 if on and has(BRI) else None,
                         ident=f"{sid}:power", tap=("toggle", sid)))
        if has(BRI):
            items += [kk("-10", ("adjust", sid, BRI, -10), repeat=True), kk("+10", ("adjust", sid, BRI, 10), repeat=True)]
            items += [kk(f"{p} %", ("w", ((sid, power, True), (sid, BRI, p))), sel=on and abs(bri - p) < 5) for p in (10, 30, 60, 100)]
        if has(CT):
            ch = m.char(sid, CT)
            for name, mired in (("Warm", 370), ("Neutral", 250), ("Cool", 154)):
                c = clamp(ch, mired)
                items.append(kk(name, ("w", ((sid, power, True), (sid, CT, c))), fill=light_color(True, ct=c), sel=on and v(CT) == c))
        if has(HUE) and has(SAT):
            for name, hue in HUES:
                items.append(kk(name, ("w", ((sid, power, True), (sid, HUE, hue), (sid, SAT, 100))), fill=light_color(True, hue, 100, 100),
                                sel=on and abs((v(HUE, -99) - hue + 180) % 360 - 180) < 10 and v(SAT, 0) > 50))
    elif d.kind in ("thermostat", "heater"):
        items.append(readout("Now", "thermo", f"{v(CURT, 0):.1f}°"))
        sp = TARGT if d.kind == "thermostat" else (COOLTH if v(HMODE) == 2 and has(COOLTH) else HEATTH)
        if has(sp):
            items += [kk("-0.5", ("adjust", sid, sp, -0.5), repeat=True), readout("Target", "flame", f"{v(sp, 0):.1f}°"),
                      kk("+0.5", ("adjust", sid, sp, 0.5), repeat=True)]
        valid = (m.char(sid, TARMODE) or {}).get("valid")  # modes the device supports; None = all
        for name, mode in MODES:
            if d.kind == "thermostat":
                code = {"Off": 0, "Heat": 1, "Cool": 2, "Auto": 3}[name]
                if valid is not None and code not in valid:
                    continue
                act, sel = w(TARMODE, code), v(TARMODE) == code
            elif mode is None:
                act, sel = w(ACTIVE, 0), not v(ACTIVE)
            else:
                act, sel = ("w", ((sid, ACTIVE, 1), (sid, HMODE, mode))), bool(v(ACTIVE)) and v(HMODE) == mode
            items.append(kk(name, act, sel=sel, glyph={"Off": "power", "Heat": "flame", "Cool": "snow", "Auto": "auto"}[name]))
        if has(HUM):
            items.append(readout("Humidity", "drop", f"{v(HUM, 0):.0f}%"))
    elif d.kind == "cover":
        pos = v(POSC, v(POST, 0))
        items.append(readout("Position", "blinds", f"{pos:.0f}%", bar=pos / 100))
        items += [kk("Open", w(POST, 100), glyph="blinds"), kk("Close", w(POST, 0), glyph="blinds")]
        items += [kk(f"{p} %", w(POST, p)) for p in (25, 50, 75)]
        items += [kk("-10", ("adjust", sid, POST, -10), repeat=True), kk("+10", ("adjust", sid, POST, 10), repeat=True)]
    elif d.kind in ("fan", "purifier"):
        items += power_items(ui, d)
        if has(SPEED):
            items += [kk(f"{p} %", ("w", ((sid, m.power(sid), True), (sid, SPEED, p))), sel=ui.is_on(d) and abs(v(SPEED, 0) - p) < 13)
                      for p in (25, 50, 75, 100)]
    elif d.kind == "lock":
        state = v(LOCKC)
        items += [readout("State", "lock" if state == 1 else "unlock", {0: "open", 1: "locked", 2: "jammed"}.get(state, "?"),
                          GREEN if state == 1 else RED if state == 2 else ORANGE),
                  kk("Lock", w(LOCKT, 1), glyph="lock"), kk("Unlock", glyph="unlock", hint="hold", hold=w(LOCKT, 0))]
    elif d.kind == "garage":
        state = {0: "open", 1: "closed", 2: "opening", 3: "closing", 4: "stuck"}.get(v(DOORC), "?")
        if v(OBST):
            state = "stuck"
        items += [readout("State", "garage", state, DARK if state == "closed" else ORANGE),
                  kk("Open", glyph="garage_open", hint="hold", hold=w(DOORT, 0)), kk("Close", glyph="garage", hint="hold", hold=w(DOORT, 1))]
    elif d.kind == "security":
        state = v(SECC, 3)
        items += [readout("State", "shield", {0: "stay", 1: "away", 2: "night", 3: "off", 4: "ALARM"}.get(state, "?"),
                          RED if state == 4 else DARK if state == 3 else BLUE)]
        items += [kk(name, w(SECT, i), sel=v(SECT) == i and state == i) for i, name in enumerate(("Stay", "Away", "Night"))]
        items.append(kk("Disarm", glyph="unlock", hint="hold", hold=w(SECT, 3)))
    else:  # outlet, switch, valve
        items += power_items(ui, d)
    out = [EMPTY] * 15
    out[0] = back_key()
    return place(out, items, 1, n)


def mark_dups(items):
    """[(Key, owner, room name)] to keys. A name that more than one owner shows gets the room as a second small line."""
    owners = {}
    for k, owner, _ in items:
        owners.setdefault(k.label.casefold(), set()).add(owner)
    return [k._replace(note=room) if len(owners[k.label.casefold()]) > 1 else k for k, _, room in items]


def p_scenes(ui):
    out = [EMPTY] * 15
    out[0] = back_key()
    return place(out, mark_dups([(Key(s["name"], "play", BLUE, ident=f"scene:{s['id']}", tap=("scene", s["id"])), s["id"], s.get("room", ""))
                                 for s in ui.home()["scenes"]]), 1, ui.page[1])


def p_autos(ui):
    out = [EMPTY] * 15
    out[0] = back_key()
    return place(out, [Key(t["name"], "clock", GREEN if t["enabled"] else GREY, ident=f"trigger:{t['id']}",
                           tap=("trigger", t["id"], not t["enabled"])) for t in ui.home()["triggers"]], 1, ui.page[1])


def p_status(ui):
    m = ui.model
    out = [EMPTY] * 15
    out[0] = back_key()
    rooms = {r["id"]: r["name"] for r in ui.home()["rooms"]}
    room = lambda a: rooms.get(m.accs[a]["room"], "")
    items = [(dev_key(ui, d)._replace(tap=None, hold=None, long=None, hint=""), d.acc, room(d.acc)) for d in ui.devs() if d.kind in SENSORS]
    accs = {d.acc for d in ui.devs()}
    names = lambda ids: sorted(ids, key=lambda a: (m.accs[a]["name"].lower(), a))
    items += [(Key(m.accs[a]["name"], "alert", GREY, badge="!", ident=f"acc:{a}", tap=("room_of", a)), a, room(a))
              for a in names(a for a in accs if not m.reachable(a))]
    items += [(Key(m.accs[a]["name"], "battery", DARK, low=True, ident=f"acc:{a}", tap=("room_of", a)), a, room(a))
              for a in names(a for a in accs if m.low(a))]
    return place(out, mark_dups(items), 1, ui.page[1])


# --- drawing ---

SYS = "/System/Library/Fonts/"
FONT_CHAIN = {  # weight -> (file, variation name of a variable font or face index of a collection), first that loads wins
    "label": [(SYS + "SFNS.ttf", "Medium"), (SYS + "HelveticaNeue.ttc", 10), (SYS + "Supplemental/Arial Bold.ttf", 0)],
    "bold": [(SYS + "SFNS.ttf", "Bold"), (SYS + "HelveticaNeue.ttc", 1), (SYS + "Supplemental/Arial Bold.ttf", 0)]}


@functools.lru_cache(maxsize=None)
def font(size, weight="label"):
    """System font of a weight ("label" = semibold/medium, "bold"); the PIL default when none loads."""
    from PIL import ImageFont
    for path, how in FONT_CHAIN[weight]:
        try:
            f = ImageFont.truetype(path, size, index=how if isinstance(how, int) else 0)
            if isinstance(how, str):
                f.set_variation_by_name(how)
            return f
        except (OSError, ValueError, AttributeError):
            continue
    return ImageFont.load_default(size=size)


def bold(d, xy, text, size, fill, anchor="mm"):
    d.text(xy, text, font=font(size, "bold"), fill=fill, anchor=anchor)


def wrap(text, f, width=68, n=2):
    """Lines of `text` (\\n breaks too) that fit `width` px; more than n lines end with an ellipsis."""
    rows = []
    for part in text.split("\n"):
        cur = ""
        for word in part.split():
            if f.getlength(word) > width:  # a word never breaks: it ends with an ellipsis (fit() shrinks the font first)
                while word and f.getlength(word + "…") > width:
                    word = word[:-1]
                word += "…"
            if cur and f.getlength(f"{cur} {word}") > width:
                rows.append(cur)
                cur = ""
            cur = f"{cur} {word}".strip()
        rows.append(cur)
    if len(rows) > n:
        rows = rows[:n]
        last = rows[-1]
        while last and f.getlength(last + "…") > width:
            last = last[:-1]
        rows[-1] = last.rstrip() + "…"
    return rows


def fit(text, size, width=68, n=2, floor=9):
    """(font, rows): `text` at `size`, or at the largest smaller size down to `floor` px where no word is wider
    than `width`. Only a word that still does not fit ends with an ellipsis (see wrap)."""
    words = text.split()
    while size > floor and any(font(size).getlength(w) > width for w in words):
        size -= 1
    return font(size), wrap(text, font(size), width, n)


def glyph(d, name, cx, cy, size, fg):
    """Draw a simple icon in a box of +-size around (cx, cy). Shapes are in a -14..14 grid."""
    u = size / 14

    def P(*xy):
        return [(cx + x * u, cy + y * u) for x, y in zip(xy[::2], xy[1::2])]

    def box(a, b, c, e):
        return (cx + a * u, cy + b * u, cx + c * u, cy + e * u)

    W = max(1, round(1.6 * u))
    if name == "bulb":
        d.ellipse(box(-8, -13, 8, 3), fill=fg)
        d.rectangle(box(-4, 4, 4, 7), fill=fg)
        d.rectangle(box(-3, 9, 3, 11), fill=fg)
    elif name == "outlet":
        d.rounded_rectangle(box(-11, -11, 11, 11), 4 * u, outline=fg, width=W)
        d.rectangle(box(-5, -5, -3, 1), fill=fg)
        d.rectangle(box(3, -5, 5, 1), fill=fg)
        d.rectangle(box(-1, 4, 1, 8), fill=fg)
    elif name == "switch":
        d.rounded_rectangle(box(-12, -6, 12, 6), 6 * u, outline=fg, width=W)
        d.ellipse(box(3, -4, 10, 4), fill=fg)
    elif name == "fan":
        for a in (0, 120, 240):
            d.pieslice(box(-12, -12, 12, 12), a, a + 50, fill=fg)
        d.ellipse(box(-2, -2, 2, 2), fill=fg)
    elif name == "purifier":
        d.rounded_rectangle(box(-9, -12, 9, 12), 3 * u, outline=fg, width=W)
        for y in (-6, -1, 4):
            d.line(P(-5, y, 5, y), fill=fg, width=W)
    elif name == "valve":
        d.polygon(P(-12, -8, 0, 0, -12, 8), fill=fg)
        d.polygon(P(12, -8, 0, 0, 12, 8), fill=fg)
        d.line(P(0, 0, 0, -10), fill=fg, width=W)
        d.line(P(-5, -10, 5, -10), fill=fg, width=W)
    elif name == "thermo":
        d.ellipse(box(-11, -11, 11, 11), outline=fg, width=W)
        d.line(P(0, 0, 5, -6), fill=fg, width=W)
    elif name == "flame":
        d.polygon(P(0, -13, 8, -1, 7, 8, 0, 12, -7, 8, -8, -1), fill=fg)
    elif name in ("lock", "unlock"):
        d.rectangle(box(-9, -3, 9, 11), fill=fg)
        d.arc(box(-6, -9, 6, 3) if name == "lock" else box(-1, -12, 11, 0), 180, 360, fill=fg, width=W + 1)
    elif name in ("garage", "garage_open"):
        d.polygon(P(-12, -4, 0, -12, 12, -4), outline=fg)
        d.rectangle(box(-10, -4, 10, 11), outline=fg, width=W)
        for y in ((0, 4, 8) if name == "garage" else (-2,)):
            d.line(P(-10, y, 10, y), fill=fg, width=W)
    elif name == "blinds":
        d.rectangle(box(-11, -12, 11, -10), fill=fg)
        for y in (-6, -1, 4, 9):
            d.line(P(-10, y, 10, y), fill=fg, width=W + 1)
    elif name == "shield":
        d.polygon(P(0, -12, 10, -8, 9, 3, 0, 12, -9, 3, -10, -8), outline=fg, width=W)
    elif name == "temp":
        d.rectangle(box(-2, -12, 2, 3), fill=fg)
        d.ellipse(box(-5, 2, 5, 12), fill=fg)
    elif name == "drop":
        d.polygon(P(0, -13, 8, 2, -8, 2), fill=fg)
        d.ellipse(box(-8, -3, 8, 12), fill=fg)
    elif name == "door":
        d.rectangle(box(-6, -11, 6, 11), outline=fg, width=W)
        d.ellipse(box(2, 0, 4, 2), fill=fg)
    elif name == "door_open":
        d.polygon(P(-6, -11, 6, -8, 6, 8, -6, 11), outline=fg, width=W)
    elif name == "motion":
        d.ellipse(box(-2, -2, 2, 2), fill=fg)
        for r in (6, 11):
            d.arc(box(-r, -r, r, r), -50, 50, fill=fg, width=W)
            d.arc(box(-r, -r, r, r), 130, 230, fill=fg, width=W)
    elif name == "person":
        d.ellipse(box(-4, -12, 4, -4), fill=fg)
        d.pieslice(box(-9, -2, 9, 22), 180, 360, fill=fg)
    elif name == "cloud":
        d.ellipse(box(-12, -2, -2, 8), fill=fg)
        d.ellipse(box(-6, -10, 6, 4), fill=fg)
        d.ellipse(box(0, -3, 12, 9), fill=fg)
        d.rectangle(box(-7, 2, 7, 9), fill=fg)
    elif name == "co":
        d.arc(box(-11, -8, 3, 8), 40, 320, fill=fg, width=W + 1)
        d.ellipse(box(3, -8, 12, 1), outline=fg, width=W)
    elif name == "sensor":
        glyph(d, "motion", cx, cy, size, fg)
    elif name == "plug":
        glyph(d, "outlet", cx, cy, size, fg)
    elif name == "sun":
        d.ellipse(box(-5, -5, 5, 5), fill=fg)
        for dx, dy in ((0, 1), (1, 0), (1, 1), (1, -1)):
            d.line(P(dx * 8, dy * 8, dx * 12, dy * 12), fill=fg, width=W)
            d.line(P(-dx * 8, -dy * 8, -dx * 12, -dy * 12), fill=fg, width=W)
    elif name == "battery":
        d.rectangle(box(-11, -6, 9, 6), outline=fg, width=W)
        d.rectangle(box(9, -2, 12, 2), fill=fg)
        d.rectangle(box(-9, -4, -4, 4), fill=fg)
    elif name == "back":
        d.polygon(P(-12, 0, 0, -10, 0, -4, 12, -4, 12, 4, 0, 4, 0, 10), fill=fg)
    elif name == "next":
        d.polygon(P(-8, -11, 8, 0, -8, 11), fill=fg)
    elif name == "home":
        d.polygon(P(-12, -1, 0, -12, 12, -1), fill=fg)
        d.rectangle(box(-8, -1, 8, 11), fill=fg)
    elif name == "play":
        d.polygon(P(-6, -11, 11, 0, -6, 11), fill=fg)
    elif name == "info":
        d.ellipse(box(-11, -11, 11, 11), outline=fg, width=W)
        d.rectangle(box(-1.3, -1, 1.3, 7), fill=fg)
        d.rectangle(box(-1.3, -7, 1.3, -4.5), fill=fg)
    elif name == "clock":
        d.ellipse(box(-11, -11, 11, 11), outline=fg, width=W)
        d.line(P(0, 0, 0, -7), fill=fg, width=W)
        d.line(P(0, 0, 5, 3), fill=fg, width=W)
    elif name == "power":
        d.arc(box(-9, -9, 9, 9), -60, 240, fill=fg, width=W + 1)
        d.line(P(0, -12, 0, -2), fill=fg, width=W + 1)
    elif name == "alert":
        d.polygon(P(0, -12, 12, 10, -12, 10), outline=fg, width=W)
        d.line(P(0, -4, 0, 3), fill=fg, width=W)
    elif name == "snow":
        for a in ((0, 12), (10, 6), (10, -6)):
            d.line(P(-a[0], -a[1], a[0], a[1]), fill=fg, width=W)
    elif name == "auto":  # circular arrows
        d.arc(box(-10, -10, 10, 10), 200, 340, fill=fg, width=W + 1)
        d.arc(box(-10, -10, 10, 10), 20, 160, fill=fg, width=W + 1)
        d.polygon(P(7, -12, 12, -4, 3, -5), fill=fg)
        d.polygon(P(-7, 12, -12, 4, -3, 5), fill=fg)


# --- themes: the rules of docs/themes.html (themes[...].key(k)) ported to PIL ---

CAT_RGB = {"home": (255, 159, 10), "scenes": (255, 55, 95), "alert": (255, 69, 58), "auto": (10, 132, 255),
           "off": (142, 142, 147), "back": (142, 142, 147)}
VIVID_HUE = {"home": 30, "scenes": 330, "alert": 2, "auto": 212}
CLIMATE_RGB, SENSOR_RGB = (255, 120, 60), (100, 210, 255)
CRT = 9  # corner radius in px at 72 px


def hexrgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def hsl(h, s, l):
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb((h % 360) / 360, max(0, min(100, l)) / 100, s / 100))


def dim(c, f):
    return tuple(round(v * f) for v in c)


def room_hue(name):
    return sum(ord(c) * (i + 7) for i, c in enumerate(name)) % 360  # stable hue per room name


def look(key):
    """A key as the themes see it: (cat, on, rgb). Keys without a category (detail page buttons, flashes)
    are lit with their fill color."""
    if key.cat:
        return key.cat, key.on, key.rgb
    lit = key.fill not in (BLACK, DARK, OFF, GREY)
    return "dev", lit or key.sel, key.fill if lit else (WHITE if key.sel else None)


@functools.lru_cache(maxsize=512)
def gradient(c1, c2, size):
    """Diagonal gradient (about 145 deg) from c1 (top left) to c2."""
    from PIL import Image
    img = Image.new("RGB", (size, size))
    img.putdata([tuple(round(a + (b - a) * (x + y) / (2 * size - 2)) for a, b in zip(c1, c2)) for y in range(size) for x in range(size)])
    return img


def vertical(c1, c2, size):
    from PIL import Image
    img = Image.new("RGB", (size, size), c2)
    d = ImageDraw_(img)
    for y in range(size):
        d.line((0, y, size, y), fill=tuple(round(a + (b - a) * y / (size - 1)) for a, b in zip(c1, c2)))
    return img


def ImageDraw_(img):
    from PIL import ImageDraw
    return ImageDraw.Draw(img)


def glow(img, rgb, alpha, rx, ry, cx, cy):
    """Radial glow of `rgb` (alpha at the center, 0 at the edge of the ellipse) over img."""
    from PIL import Image, ImageOps
    w, h = max(1, round(2 * rx)), max(1, round(2 * ry))
    mask = ImageOps.invert(Image.radial_gradient("L")).resize((w, h)).point(lambda v: round(v * alpha))
    img.paste(Image.new("RGB", (w, h), rgb), (round(cx - rx), round(cy - ry)), mask)


def ring(img, rgb, size):
    """Neon: dark key, faint fill, 2 px ring and an inner glow."""
    from PIL import Image, ImageDraw, ImageFilter
    base = hexrgb("#0b0b0d")
    img.paste(base, (0, 0, size, size))
    glow(img, rgb, .14, size * .9, size * .9, size / 2, size / 2)
    edge = Image.new("L", (size, size), 0)
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, size - 1, size - 1), round(CRT * size / 72), outline=255, width=4)
    img.paste(Image.new("RGB", (size, size), rgb), (0, 0), edge.filter(ImageFilter.GaussianBlur(size / 12)).point(lambda v: round(v * .6)))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, size - 1, size - 1), round(CRT * size / 72), outline=rgb, width=2)


def paint(img, key, theme):
    """Draw the background of a key. Returns (icon and value color, label color, bar color, badge color)."""
    size, (cat, on, rgb) = img.width, look(key)
    offline = key.badge == "!"
    level = key.bar * 100 if key.bar is not None else 80
    if theme == "graphite":
        base = lambda: img.paste(vertical(hexrgb("#22252c"), hexrgb("#14161b"), size))
        if offline:
            base()
            return hexrgb("#555a63"), hexrgb("#555a63"), None, hexrgb("#ff9f0a")
        if cat in CAT_RGB:
            base()
            return CAT_RGB[cat], hexrgb("#d8dbe0"), None, WHITE
        if cat in ("room", "climate") and (on or cat == "climate"):
            img.paste(vertical(hexrgb("#1a1c22"), hexrgb("#1a1c22"), size))
            glow(img, rgb if cat == "room" else CLIMATE_RGB, .85 if cat == "room" else .8, size * .9, size * .63, size / 2, size * 1.2)
            return WHITE, WHITE, None, WHITE
        if cat == "sensor" and not on:
            base()
            return hexrgb("#64d2ff"), hexrgb("#d8dbe0"), None, WHITE
        if on and rgb:
            img.paste(vertical(hexrgb("#16181d"), hexrgb("#16181d"), size))
            glow(img, rgb, .25 + .6 * level / 100, size * .975, size * .75, size / 2, size * 1.25)
            return rgb, WHITE, rgb, WHITE
        base()
        return hexrgb("#4d525b"), hexrgb("#8b919c"), None, WHITE
    if theme == "vivid":
        g = lambda h, s=80, l=52: img.paste(gradient(hsl(h, s, l + 8), hsl(h + 18, s, l - 14), size))
        if offline or (not on and cat not in CAT_RGB and cat not in ("climate", "room", "sensor")):
            img.paste(hexrgb("#1c1c1e"), (0, 0, size, size))
            return hexrgb("#6e6e73"), hexrgb("#6e6e73"), None, hexrgb("#ff453a")
        if cat in ("off", "back"):
            img.paste(gradient(hexrgb("#48484a"), hexrgb("#2c2c2e"), size))
            return WHITE, WHITE, None, WHITE
        if cat in VIVID_HUE:
            g(VIVID_HUE[cat])
            return WHITE, WHITE, None, WHITE
        if cat == "room":
            h = room_hue(key.label)
            if on:
                g(h)
                return WHITE, WHITE, None, WHITE
            img.paste(gradient(hsl(h, 25, 22), hsl(h, 25, 12), size))
            return hsl(h, 40, 70), hsl(h, 40, 70), None, WHITE
        if cat == "climate":
            g(18, 90, 55)
            return WHITE, WHITE, None, WHITE
        if cat == "sensor" and not on:
            g(190, 70, 40)
            return WHITE, WHITE, None, WHITE
        c = rgb or WHITE
        light = c[0] * .3 + c[1] * .59 + c[2] * .11 > 170
        img.paste(gradient(c, dim(c, .55), size))
        fg = hexrgb("#1c1c1e") if light else WHITE
        return fg, fg, fg, WHITE
    # neon
    if offline or (not on and cat not in CAT_RGB and cat not in ("climate", "sensor")):
        img.paste(hexrgb("#0b0b0d"), (0, 0, size, size))
        ImageDraw_(img).rounded_rectangle((0, 0, size - 1, size - 1), round(CRT * size / 72), outline=hexrgb("#2a2a2e"), width=2)
        return hexrgb("#5a5a60"), hexrgb("#5a5a60"), None, hexrgb("#ff453a")
    c = CAT_RGB.get(cat) or (CLIMATE_RGB if cat == "climate" else SENSOR_RGB if cat == "sensor" and not on else rgb or WHITE)
    ring(img, c, size)
    return c, hexrgb("#e5e5ea") if cat in CAT_RGB else WHITE, c, WHITE


def face(key, size=72, theme=None):
    """Draw one key image (PIL, RGB) from a Key in a theme (graphite, vivid or neon)."""
    from PIL import Image, ImageDraw

    theme = theme or THEME
    img = Image.new("RGB", (size, size), (0, 0, 0))
    if key.label == "" and key.glyph is None and not key.sub and not key.badge:  # empty key
        img.paste(hexrgb("#0e0e10"), (0, 0, size, size))
    else:
        ink, txt, bar, badge = paint(img, key, theme)
        d = ImageDraw.Draw(img)
        # Layout at 72 px. Zone A (y 0-40): an icon (26 px) or a value (20 px bold), or a small 14 px icon at the
        # top left plus the value. Zone B (y 40-68): the label, at most 2 lines of 11 px. The bar is at the very bottom.
        if key.glyph is None and not key.sub:  # text only: centered
            f, rows = fit(key.label, 15, 64, 4)
            if len(rows) > 2:
                f, rows = fit(key.label, 12, 64, 4)
            d.multiline_text((size / 2, size / 2), "\n".join(rows), font=f, fill=ink, anchor="mm", align="center")
        else:
            baseline = size - 11 if key.bar is not None else size - 7  # last label line; its descenders end above the bar
            if key.note:  # second small line: the label keeps one line above it
                d.text((size / 2, baseline), wrap(key.note, font(9), 68, 1)[0], font=font(9), fill=txt, anchor="ms")
                baseline -= 11
            f, rows = fit(key.label, 11, 68, 1 if key.note else 2)
            for i, row in enumerate(reversed(rows)):
                d.text((size / 2, baseline - i * 13), row, font=f, fill=txt, anchor="ms")
            if key.sub:
                px = 20
                while px > 10 and font(px, "bold").getlength(key.sub) > 64:
                    px -= 1
                if key.glyph:
                    glyph(d, key.glyph, 11, 11, 7, ink)
                bold(d, (size / 2, 25 if key.glyph else 20), key.sub, px, ink)
            else:
                glyph(d, key.glyph, size / 2, 20, 13, ink)
        if key.hint:
            d.text((3, 2), key.hint, font=font(9), fill=(255, 210, 0))
        if key.bar is not None:  # 3 px level bar
            d.rounded_rectangle((8, size - 7, size - 9, size - 5), 1, fill=tuple(round(c * .3 + 25) for c in ink))
            d.rounded_rectangle((8, size - 7, 8 + (size - 17) * max(0.0, min(1.0, key.bar)), size - 5), 1, fill=bar or ink)
        if key.low:
            glyph(d, "battery", size - 28 if key.badge else size - 11, 10, 6, (255, 210, 0))
        if key.badge:
            bold(d, (size - 9, 9), key.badge, 13, badge)
        if key.sel:
            d.rounded_rectangle((0, 0, size - 1, size - 1), round(CRT * size / 72), outline=WHITE, width=3)
    mask = Image.new("L", (size, size), 0)  # rounded corners
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), round(CRT * size / 72), fill=255)
    out = Image.new("RGB", (size, size), (0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def sheet(keys, gap=8, size=72, theme=None):
    """All 15 keys in a 5x3 grid, like the deck."""
    from PIL import Image

    img = Image.new("RGB", (5 * size + 4 * gap, 3 * size + 2 * gap), (10, 10, 10))
    for i, k in enumerate(keys):
        img.paste(face(k, size, theme), ((i % 5) * (size + gap), (i // 5) * (size + gap)))
    return img


# --- hubs: credentials, TLS, discovery, helpers ---

def log(msg):
    print(msg, flush=True)


def describe(error):
    """Short text of an exception for logs. Our own messages never contain tokens."""
    return f"{type(error).__name__}: {error}"


class Creds:
    """~/.config/stream-deck-home/credentials.json: mode 0600 in a folder with mode 0700.
    These tokens control the house (locks too). They are never printed or logged."""

    def __init__(self, path=CRED_FILE):
        self.path, self.lock = path, threading.Lock()

    def load(self):
        try:
            if os.stat(self.path).st_mode & 0o077:
                os.chmod(self.path, 0o600)  # somebody widened the mode: close it again
            with open(self.path) as f:
                data = json.load(f)  # a corrupt file raises: never overwrite tokens we cannot read
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, name):
        return self.load().get(name)

    def put(self, name, value):
        """Save the entry of one hub (None removes it). The file is replaced in one step."""
        with self.lock:
            data = self.load()
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
            folder = os.path.dirname(self.path)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            os.chmod(folder, 0o700)
            fd, tmp = tempfile.mkstemp(dir=folder)  # created with mode 0600
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                    if hasattr(fcntl, "F_FULLFSYNC"):  # macOS: fsync does not reach the disk
                        fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise


class PinError(OSError):
    pass


def fingerprint(der):
    return hashlib.sha256(der).hexdigest()


def pinned_socket(host, port, pin, first_use=False, timeout=HTTP_TIMEOUT):
    """TLS socket to a hub with a self-signed certificate. The certificate must have the SHA-256
    fingerprint `pin`; this is checked before anything is sent. Without a pin the call fails, unless
    first_use=True (pairing only: the caller saves the fingerprint, see sock_pin)."""
    if pin is None and not first_use:
        raise PinError("no pinned certificate for this hub: pair it again")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # replaced by the pin check below
    sock = ctx.wrap_socket(socket.create_connection((host, port), timeout))
    if pin is not None and not hmac.compare_digest(sock_pin(sock), pin):
        sock.close()
        raise PinError("the certificate of the hub changed (hub reset?): pair it again")
    return sock


def sock_pin(sock):
    return fingerprint(sock.getpeercert(True))


def https(host, port, pin, first_use=False):
    """HTTPS connection to a hub, pinned like pinned_socket. conn.pin is the fingerprint."""
    conn = http.client.HTTPSConnection(host, port, timeout=HTTP_TIMEOUT)
    conn.sock = pinned_socket(host, port, pin, first_use)
    conn.pin = sock_pin(conn.sock)
    return conn


def request(conn, method, path, body=None, headers=None, form=None):
    """One request. Returns (status, parsed JSON or None, response headers)."""
    h, data = dict(headers or {}), None
    if form is not None:
        data, h["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    elif body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    conn.request(method, path, data, h)
    r = conn.getresponse()
    raw = r.read()
    try:
        obj = json.loads(raw) if raw else None
    except ValueError:
        obj = None
    return r.status, obj, r.headers


def hub_call(host, port, pin, method, path, first_use=False, **kw):
    conn = https(host, port, pin, first_use)
    try:
        return request(conn, method, path, **kw)
    finally:
        conn.close()


def cloud_call(host, method, path, **kw):
    """Request to a cloud service with the normal certificate check."""
    conn = http.client.HTTPSConnection(host, timeout=HTTP_TIMEOUT)
    try:
        return request(conn, method, path, **kw)
    finally:
        conn.close()


def ok_resp(status, obj):
    return status in (200, 201, 202, 204) and not (isinstance(obj, dict) and obj.get("errors"))


def dns_sd(args, wait):
    """Run `dns-sd` for `wait` seconds and return what it printed."""
    p = subprocess.Popen(["dns-sd", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    threading.Timer(wait, p.kill).start()
    return p.communicate()[0]


def parse_browse(out):
    """Instance names from `dns-sd -B` output."""
    return re.findall(r"^\S+\s+Add\s+\d+\s+\d+\s+\S+\s+\S+\s+(.+?)\s*$", out, re.M)


def parse_lookup(out):
    """(host, port, TXT dict) from `dns-sd -L` output, or None."""
    m = re.search(r"can be reached at (\S+?)\.?:(\d+)", out)
    if not m:
        return None
    rest = out[m.end():].split("\n")[1:2]  # the TXT record is on the next line
    return m.group(1), int(m.group(2)), dict(re.findall(r"(\w+)=(\S+)", rest[0])) if rest else {}


def mdns(stype, wait=3.0):
    """Hubs that announce `stype` on the local network: [{"name", "ip", "port", "txt"}]."""
    found = []
    try:
        for name in parse_browse(dns_sd(["-B", stype, "local"], wait)):
            hit = parse_lookup(dns_sd(["-L", name, stype, "local"], 2.0))
            if hit:
                host, port, txt = hit
                try:
                    ip = txt.get("ipv4address") or socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
                except OSError:
                    continue
                found.append({"name": name, "ip": ip, "port": port, "txt": txt})
    except OSError:  # no dns-sd
        pass
    return found


def find_hue(bridge_id=None):
    """[{"host", "bridge_id"}] of the Hue Bridges found (only the one with `bridge_id` when given)."""
    want = bridge_id.upper() if bridge_id else None
    found = [{"host": h["ip"], "bridge_id": h["txt"].get("bridgeid", "").upper()} for h in mdns("_hue._tcp")]
    found = [h for h in found if want is None or h["bridge_id"] == want]
    if found:
        return found
    try:  # fallback: the Hue cloud lists the bridges behind your public address
        _, obj, _ = cloud_call("discovery.meethue.com", "GET", "/")
        return [{"host": b["internalipaddress"], "bridge_id": b["id"].upper()}
                for b in obj or [] if want is None or b["id"].upper() == want]
    except (OSError, KeyError, TypeError):
        return []


def find_ikea():
    """IPs of the DIRIGERA hubs found."""
    ips = [h["ip"] for h in mdns("_ihsp._tcp") if h["txt"].get("type", "DIRIGERA") == "DIRIGERA"]
    return ips or [h["ip"] for h in mdns("_hap._tcp") if "DIRIGERA" in h["name"].upper()]


def pinned_host(hosts, port, pin, limit=RELOCATE_TRIES):
    """First of at most `limit` hosts whose certificate has the fingerprint `pin`, or None.
    A new address is only saved after this check."""
    for host in hosts[:limit]:
        try:
            pinned_socket(host, port, pin).close()
            return host
        except OSError:  # PinError is an OSError too
            continue
    return None


def dig(d, *keys, default=None):
    """d[k1][k2]... or default when a key is missing or the value is None."""
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return default if d is None else d


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v


# --- color math ---

GAMUT_C = ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475))  # Hue color lights (red, green, blue corner)


def _gamma(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _linear(c):
    return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92


def xy_to_hs(x, y):
    """CIE xy (Hue wide-gamut RGB) to (hue 0-360, saturation 0-100) at full brightness."""
    if y <= 0:
        return 0.0, 0.0
    X, Z = x / y, (1 - x - y) / y  # Y = 1
    rgb = [max(0.0, c) for c in (X * 1.656492 - 0.354851 - Z * 0.255038,
                                 -X * 0.707196 + 1.655397 + Z * 0.036152,
                                 X * 0.051713 - 0.121364 + Z * 1.011530)]
    top = max(rgb) or 1.0
    h, s, _ = colorsys.rgb_to_hsv(*[_gamma(c / top) for c in rgb])
    return round(h * 360, 1), round(s * 100, 1)


def to_gamut(x, y, tri):
    """The point (x, y), or the nearest point of the triangle `tri` when it lies outside."""
    (ax, ay), (bx, by), (cx, cy) = tri
    d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    l1 = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d
    l2 = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d
    if l1 >= 0 and l2 >= 0 and l1 + l2 <= 1:
        return x, y
    best = None
    for (px, py), (qx, qy) in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
        dx, dy = qx - px, qy - py
        t = max(0.0, min(1.0, ((x - px) * dx + (y - py) * dy) / (dx * dx + dy * dy)))
        c = (px + t * dx, py + t * dy)
        dist = (c[0] - x) ** 2 + (c[1] - y) ** 2
        if best is None or dist < best[0]:
            best = (dist, c)
    return best[1]


def hs_to_xy(h, s, gamut=None):
    """(hue 0-360, saturation 0-100) to CIE xy, moved into `gamut` (three (x, y) corners) when given."""
    r, g, b = [_linear(c) for c in colorsys.hsv_to_rgb((h % 360) / 360, max(0.0, min(100.0, s)) / 100, 1)]
    X = r * 0.664511 + g * 0.154324 + b * 0.162028
    Y = r * 0.283881 + g * 0.668433 + b * 0.047685
    Z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = (X + Y + Z) or 1.0
    x, y = X / total, Y / total
    return to_gamut(x, y, gamut) if gamut else (x, y)


def mired(kelvin):
    return round(1e6 / kelvin)


def mired_range(a, b):
    """(min, max) mireds of a Kelvin range. The order of a and b does not matter."""
    lo, hi = sorted((a, b))
    return mired(hi), mired(lo)


def mired_to_kelvin(m, a, b):
    """Kelvin for a mired value, limited to the range a..b (either order)."""
    lo, hi = sorted((a, b))
    return max(lo, min(hi, round(1e6 / max(m, 1))))


# --- model builders ---

def hchar(s, value, fmt="bool", lo=None, hi=None, step=None, units=None, w=True, valid=None):
    """A characteristic of the internal model. `valid` lists the allowed values, when not all are."""
    c = {"type": full(s), "value": value, "format": fmt, "min": lo, "max": hi, "step": step, "units": units,
         "readable": True, "writable": w}
    if valid is not None:
        c["valid"] = valid
    return c


def hsvc(id, name, s, chars):
    return {"id": id, "name": name, "type": full(s), "primary": True, "chars": chars}


def hacc(id, name, room, services, reachable=True):
    """`room` is a room name here. The hub merges the names into room ids."""
    return {"id": id, "name": name, "room": room, "reachable": reachable, "services": services}


class Names:
    """Optional user file names.json: aliases for devices, rooms and scenes, and glob patterns to hide them.
    {"devices": {"<hub name or prefixed id>": "alias"}, "rooms": {...}, "scenes": {...}, "hide": ["pattern"]}
    No file: nothing is renamed and DEFAULT_HIDE is hidden. A file without "hide" hides nothing.
    Names(None) has no file (tests). check() loads the file again when its mtime changed."""

    def __init__(self, path=NAMES_FILE):
        self.path, self.stamp = path, False  # False never equals a real mtime or None (no file)
        self.load({})
        self.check()

    def load(self, raw):
        table = lambda key: {str(a).strip().casefold(): b.strip() for a, b in raw[key].items()
                             if isinstance(b, str) and b.strip()} if isinstance(raw.get(key), dict) else {}  # null or empty: no alias
        hide = raw.get("hide", [])
        self.data = {"devices": table("devices"), "rooms": table("rooms"), "scenes": table("scenes"),
                     "hide": [str(p) for p in hide] if isinstance(hide, list) else []}  # one assignment: threads see all or nothing

    def check(self):
        """Read the file again when its mtime changed. True when the names changed. A broken file is logged
        once and the last good names stay."""
        if self.path is None:
            return False
        try:
            stamp = os.stat(self.path).st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self.stamp:
            return False
        self.stamp = stamp
        raw = {"hide": DEFAULT_HIDE}
        if stamp is not None:
            try:
                with open(self.path) as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("the top level must be an object")
            except (OSError, ValueError) as error:
                log(f"names.json: {describe(error)}. The last good names stay.")
                return False
        self.load(raw)
        return True

    def device(self, id, name):
        d = self.data["devices"]
        return d.get(id.strip().casefold()) or d.get(name.strip().casefold()) or name

    def room(self, name):
        return self.data["rooms"].get(name.strip().casefold()) or name.strip()

    def scene(self, id, name):
        d = self.data["scenes"]
        return d.get(id.strip().casefold()) or d.get(name.strip().casefold()) or name

    def hidden(self, *names):
        """True when a pattern of `hide` matches one of the names (hub name or alias; case does not matter)."""
        return any(fnmatch.fnmatchcase(n.casefold(), p.casefold()) for n in names if n for p in self.data["hide"])


def merge(parts, names=None):
    """[(source, online, {"rooms", "accessories", "scenes", "triggers"})] to one home, with the aliases and hidden
    items of `names`. Room aliases apply first, then rooms with the same name (any case) from different sources
    become one room. A hidden room hides its devices. A source that is offline marks its accessories unreachable."""
    names = names or Names(None)
    nm, order, accs, scenes, trigs = {}, [], [], [], []

    def room(n):
        """(id, name) of a room after aliasing, or None when it is hidden."""
        shown = names.room(n or "Other")
        return None if names.hidden(n, shown) else ("room:" + shown.strip().casefold(), shown.strip())

    for _, online, p in parts:
        for n in p["rooms"]:
            if r := room(n):
                nm.setdefault(*r)
                order.append(r[0])
        for a in p["accessories"]:
            r, name = room(a["room"]), names.device(a["id"], a["name"])
            if r is None or names.hidden(a["name"], name):
                continue
            nm.setdefault(*r)
            order.append(r[0])
            svcs = [{**s, "name": name + s["name"][len(a["name"]):]} if s["name"].startswith(a["name"]) else s for s in a["services"]]
            accs.append({**a, "name": name, "services": svcs, "room": r[0], "reachable": a["reachable"] and online})
        for s in p["scenes"]:
            name = names.scene(s["id"], s["name"])
            r = room(s["room"]) if s.get("room") else ("", "")
            if r is not None and not names.hidden(s["name"], name):  # a scene of a hidden room is dropped
                scenes.append({**s, "name": name, "room": r[1]})
        trigs += p["triggers"]
    used = {a["room"] for a in accs}
    return {"id": "home", "name": "Home", "primary": True, "accessories": accs, "scenes": scenes, "triggers": trigs,
            "rooms": [{"id": r, "name": nm[r]} for r in dict.fromkeys(order) if r in used]}


def snap_diff(old, new):
    """Messages that turn the UI's copy of `old` into `new`: a full snapshot when the structure changed,
    else char and reach messages."""
    def flat(snap):
        h = snap["homes"][0]
        shape, chars, reach = [], {}, {}
        for a in h["accessories"]:
            reach[a["id"]] = a["reachable"]
            shape.append((a["id"], a["name"], a["room"]))
            for s in a["services"]:
                shape.append((s["id"], s["name"], s["type"]))
                for c in s["chars"]:
                    shape.append((c["type"], c["min"], c["max"], c["step"], c["writable"], str(c.get("valid"))))
                    chars[(s["id"], c["type"])] = c["value"]
        shape += [(r["id"], r["name"]) for r in h["rooms"]] + [(s["id"], s["name"], s.get("room")) for s in h["scenes"]]
        shape += [(t["id"], t["name"], t["enabled"]) for t in h["triggers"]]
        return shape, chars, reach

    (s0, c0, r0), (s1, c1, r1) = flat(old), flat(new)
    if s0 != s1:
        return [("msg", copy.deepcopy(new))]
    out = [("msg", {"type": "reach", "accessory": a, "reachable": v}) for a, v in r1.items() if r0[a] != v]
    out += [("msg", {"type": "char", "service": s, "char": c, "value": v}) for (s, c), v in c1.items() if c0[(s, c)] != v]
    return out


# --- source adapters ---

class Coalescer:
    """Collects writes per key. Writes to one key within `delay` seconds become one request that carries
    the latest value of each field (rapid +/- presses end as one write). A worker sends what is due:
    send({key: fields}) returns {key: ok}. Every submit gets its callback done(ok)."""

    def __init__(self, send, delay=COALESCE):
        self.send, self.delay, self.pending, self.cv = send, delay, {}, threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, key, fields, done):
        with self.cv:
            item = self.pending.setdefault(key, [{}, [], time.monotonic()])
            item[0].update(fields)
            item[1].append(done)
            self.cv.notify()

    def _run(self):
        while True:
            with self.cv:
                while True:
                    due = [k for k, i in self.pending.items() if i[2] + self.delay <= time.monotonic()]
                    if due:
                        break
                    nxt = min((i[2] + self.delay for i in self.pending.values()), default=None)
                    self.cv.wait(None if nxt is None else max(0.0, nxt - time.monotonic()))
                items = {k: self.pending.pop(k) for k in due}
            try:
                got = self.send({k: i[0] for k, i in items.items()})
            except Exception as error:
                log(f"write failed: {describe(error)}")
                got = {}
            for k, i in items.items():
                for done in i[1]:
                    done(bool(got.get(k)))


class Source:
    """One hub. Subclasses fill `fetch` (fetch everything, then call `load`), `stream` (live updates, blocks),
    `translate` (cache to model parts) and `_flush` (one coalesced write)."""

    name = label = ""
    delay = COALESCE
    interval = 0.0  # seconds between two requests of a batch
    periodic = True  # a thread fetches everything every FULL_REFRESH seconds (tado polls on its own)

    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.online, self.lock = False, threading.RLock()
        self.last_good, self.last_error = None, None  # last translate() result; last logged error text
        self.notify = lambda: None
        self.backlog, self.fetching = None, threading.Lock()  # backlog: events that arrive during a fetch
        self.co = Coalescer(self._flush_batch, self.delay)

    def refresh(self):
        """Fetch everything. The events that arrive meanwhile are applied again after the load."""
        with self.fetching:
            with self.lock:
                self.backlog = []
            try:
                self.fetch()
            finally:
                with self.lock:
                    self.backlog = None

    def replay(self):
        """Apply the events of the backlog to the new cache. Call with `lock` held."""
        b, self.backlog = self.backlog, None
        for ev in b or []:
            self.apply_event(ev)

    def save(self):
        self.store.put(self.name, self.cfg)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        if self.periodic:
            threading.Thread(target=self._full, daemon=True).start()

    def full_refresh(self):
        """Fetch everything again, in case an event was missed. Offline: the loop fetches after it reconnects."""
        if self.online:
            try:
                self.refresh()
                self.notify()
            except Exception as error:
                log(f"{self.name}: {describe(error)}")

    def _full(self):
        while True:
            time.sleep(FULL_REFRESH)
            self.full_refresh()

    def _loop(self):
        wait = RETRY
        while True:
            try:
                self.refresh()
                self.online, wait = True, RETRY
                self.notify()
                while True:  # stream() returns on a harmless timeout and raises on errors
                    self.stream()  # after a timeout connect again; the `_full` thread covers missed events
            except Exception as error:
                log(f"{self.name}: {describe(error)}")
            self.online = False
            self.notify()
            try:
                self.relocate()
            except Exception as error:
                log(f"{self.name}: {describe(error)}")
            time.sleep(wait)
            wait = min(wait * 2, RETRY_MAX)

    def relocate(self):
        """Find the hub again after a failure (its IP may have changed)."""

    def key_of(self, service):
        return service.split(":")[1]

    def write(self, service, ch, value, done):
        """Queue the write of one characteristic. False when the hub is offline."""
        if not self.online:
            return False
        self.co.submit(self.key_of(service), {ch: value}, done)
        return True

    def run(self, fn, done, *args):
        def job():
            try:
                ok = fn(*args)
            except Exception as error:
                log(f"{self.name}: {describe(error)}")
                ok = False
            done(ok)
        threading.Thread(target=job, daemon=True).start()

    def _flush_batch(self, batch):
        out = {}
        for key, fields in batch.items():
            try:
                out[key] = self._flush(key, fields)
            except Exception as error:
                log(f"{self.name}: {describe(error)}")
                out[key] = False
            time.sleep(self.interval)
        return out

    def scene(self, sid):
        return False

    def trigger(self, sid, enabled):
        return False


HUE_GROUPS = {"room", "zone", "scene", "device"}  # an update of their children or services changes the structure


class Hue(Source):
    """Philips Hue Bridge, CLIP API v2 over HTTPS with a pinned certificate, live state by SSE."""

    name, label, interval = "hue", "Hue", HUE_INTERVAL

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self.res, self.last_group = {}, 0.0  # res: resource type -> id -> resource

    def call(self, method, path, body=None):
        return hub_call(self.cfg["host"], 443, self.cfg["pin"], method, "/clip/v2" + path, body=body,
                        headers={"hue-application-key": self.cfg["key"]})

    def fetch(self):
        st, obj, _ = self.call("GET", "/resource")
        if st != 200 or not isinstance(obj, dict):
            raise OSError(f"Hue Bridge answered HTTP {st}")
        self.load(obj.get("data", []))

    def load(self, data):
        res = {}
        for r in data:
            if isinstance(r, dict) and "id" in r and "type" in r:
                res.setdefault(r["type"], {})[r["id"]] = r
        with self.lock:
            self.res = res
            self.replay()

    def relocate(self):
        host = pinned_host([h["host"] for h in find_hue(self.cfg.get("bridge_id"))], 443, self.cfg["pin"])
        if host and host != self.cfg["host"]:
            self.cfg["host"] = host
            self.save()

    def stream(self):
        conn = https(self.cfg["host"], 443, self.cfg["pin"])
        try:
            conn.sock.settimeout(SSE_TIMEOUT)
            conn.request("GET", "/eventstream/clip/v2",
                         headers={"hue-application-key": self.cfg["key"], "Accept": "text/event-stream"})
            r = conn.getresponse()
            if r.status != 200:
                raise OSError(f"Hue event stream answered HTTP {r.status}")
            while True:
                line = r.readline()
                if not line:
                    raise OSError("Hue event stream closed")
                line = line.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        events = json.loads(line[5:])
                    except ValueError:
                        continue
                    if self.apply_event(events):
                        self.refresh()
                    self.notify()
        except socket.timeout:
            return  # no byte for SSE_TIMEOUT: connect again
        finally:
            conn.close()

    def apply_event(self, events):
        """Merge a message of the event stream into the cache. True when everything must be fetched
        again (a resource was added, deleted or renamed, or a room, zone, scene or device changed its members)."""
        again = False
        with self.lock:
            if self.backlog is not None:
                self.backlog.append(events)  # a fetch is in flight: load() replays it
            for ev in events if isinstance(events, list) else []:
                if ev.get("type") != "update":
                    again = again or ev.get("type") in ("add", "delete")
                    continue
                for r in ev.get("data", []):
                    cur = self.res.get(r.get("type"), {}).get(r.get("id"))
                    if cur is not None:
                        deep_merge(cur, r)
                    if "metadata" in r or (r.get("type") in HUE_GROUPS and ("children" in r or "services" in r)):
                        again = True
        return again

    # --- model ---

    def translate(self):
        with self.lock:
            R = self.res
            g = lambda t: R.get(t, {})
            place = {}  # device id -> room name. A zone only places lights that no room holds.
            for z in g("zone").values():
                for c in z.get("children", []):
                    dev = dig(g("light").get(c.get("rid"), {}), "owner", "rid")
                    if dev:
                        place.setdefault(dev, dig(z, "metadata", "name"))
            rooms = []
            for r in g("room").values():
                n = dig(r, "metadata", "name")
                rooms.append(n)
                for c in r.get("children", []):
                    place[c.get("rid")] = n
            owned = {}
            for t in ("light", "motion", "temperature", "light_level", "contact", "device_power", "zigbee_connectivity"):
                for r in g(t).values():
                    owned.setdefault(dig(r, "owner", "rid"), []).append((t, r))
            accs = []
            for dev in g("device").values():
                name = dig(dev, "metadata", "name", default=dev["id"])
                items = owned.get(dev["id"], [])
                reach = all(r.get("status") == "connected" for t, r in items if t == "zigbee_connectivity")
                plug = dig(dev, "product_data", "product_archetype") == "plug"
                svcs = [s for t, r in items for s in self.services(t, r, name, plug)]
                if any(short(s["type"]) in KINDS for s in svcs):
                    accs.append(hacc("hue:" + dev["id"], name, place.get(dev["id"]), svcs, reach))
            raw = [r for t in ("scene", "smart_scene") for r in g(t).values() if dig(r, "metadata", "name")]
            scenes = []
            for r in raw:
                grp = R.get(dig(r, "group", "rtype"), {}).get(dig(r, "group", "rid"), {})
                scenes.append({"id": "hue:" + r["id"], "name": r["metadata"]["name"], "kind": "custom",
                               "room": dig(grp, "metadata", "name", default="")})  # the pages add the room to equal names
            trigs = [{"id": "hue:" + r["id"], "name": r["metadata"]["name"], "enabled": bool(r.get("enabled"))}
                     for r in g("behavior_instance").values() if dig(r, "metadata", "name")]
        return {"rooms": [n for n in rooms if n], "accessories": accs, "scenes": scenes, "triggers": trigs}

    def services(self, t, r, name, plug):
        sid = "hue:" + r["id"]
        if t == "light":
            return [self.light_service(sid, r, name, plug)]
        if t == "motion":
            v = dig(r, "motion", "motion_report", "motion", default=dig(r, "motion", "motion"))
            return [hsvc(sid, f"{name} motion", "85", [hchar(MOTION, bool(v), w=False)])]
        if t == "temperature":
            v = dig(r, "temperature", "temperature_report", "temperature", default=dig(r, "temperature", "temperature"))
            return [hsvc(sid, f"{name} temperature", "8A", [hchar(CURT, v, "float", -50, 100, 0.1, "celsius", False)])]
        if t == "light_level":
            v = dig(r, "light", "light_level_report", "light_level", default=dig(r, "light", "light_level"))
            lux = 10 ** ((v - 1) / 10000) if v else 0.0  # Hue sends 10000 * log10(lux) + 1
            return [hsvc(sid, f"{name} light", "84", [hchar(LUX, round(lux, 1), "float", w=False)])]
        if t == "contact":
            state = dig(r, "contact_report", "state")
            return [hsvc(sid, f"{name} contact", "80", [hchar(CONTACT, 1 if state == "no_contact" else 0, "uint8", w=False)])]
        if t == "device_power":
            return [hsvc(sid, f"{name} battery", "96",
                         [hchar(BATLVL, dig(r, "power_state", "battery_level"), "uint8", 0, 100, 1, "percentage", False)])]
        return []

    def light_service(self, sid, r, name, plug):
        chars = [hchar(ON, bool(dig(r, "on", "on", default=False)))]
        if plug or dig(r, "metadata", "archetype") == "plug":
            return hsvc(sid, name, "47", chars)
        if "dimming" in r:
            b = dig(r, "dimming", "brightness")
            chars.append(hchar(BRI, None if b is None else round(b), "int", 1, 100, 1, "percentage"))
        ct = r.get("color_temperature")
        ct_mode = ct is not None and ct.get("mirek") is not None and ct.get("mirek_valid", True)
        if "color" in r:  # in color temperature mode the UI shows the white, not the xy of that white
            xy = dig(r, "color", "xy")
            h, s = (None, None) if ct_mode or not xy else xy_to_hs(xy["x"], xy["y"])
            chars += [hchar(HUE, h, "float", 0, 360, 1, "arcdegrees"), hchar(SAT, s, "float", 0, 100, 1, "percentage")]
        if ct is not None:
            lo, hi = dig(ct, "mirek_schema", "mirek_minimum", default=153), dig(ct, "mirek_schema", "mirek_maximum", default=500)
            chars.append(hchar(CT, ct.get("mirek") if ct_mode else None, "uint32", lo, hi, 1))
        return hsvc(sid, name, "43", chars)

    # --- writes ---

    def light_body(self, lid, f):
        """Request body of PUT /resource/light/{id} for the fields {char: value}, or None."""
        with self.lock:
            r = self.res.get("light", {}).get(lid)
        if r is None:
            return None
        body = {}
        if ON in f:
            body["on"] = {"on": bool(f[ON])}
        if BRI in f:
            body["dimming"] = {"brightness": float(max(1, min(100, f[BRI])))}
        if HUE in f or SAT in f:  # one color: the half that is not written comes from the light
            xy = dig(r, "color", "xy")
            cur = xy_to_hs(xy["x"], xy["y"]) if xy else (0.0, 0.0)
            x, y = hs_to_xy(f.get(HUE, cur[0]), f.get(SAT, cur[1]), gamut_of(r))
            body["color"] = {"xy": {"x": round(x, 4), "y": round(y, 4)}}
        elif CT in f:
            lo = dig(r, "color_temperature", "mirek_schema", "mirek_minimum", default=153)
            hi = dig(r, "color_temperature", "mirek_schema", "mirek_maximum", default=500)
            body["color_temperature"] = {"mirek": int(max(lo, min(hi, f[CT])))}
        return body

    def groups(self):
        """[(grouped_light id, reachable light ids)]: the whole home first, then each room."""
        R, out = self.res, []
        lights = {lid: dig(light, "owner", "rid") for lid, light in R.get("light", {}).items()}
        bad = {dig(z, "owner", "rid") for z in R.get("zigbee_connectivity", {}).values() if z.get("status") != "connected"}
        gid = lambda res: next((s["rid"] for s in res.get("services", []) if s.get("rtype") == "grouped_light"), None)
        home = next(iter(R.get("bridge_home", {}).values()), None)
        if home and gid(home):
            out.append((gid(home), {lid for lid, o in lights.items() if o not in bad}))
        for room in R.get("room", {}).values():
            kids = {c.get("rid") for c in room.get("children", [])}
            if gid(room):
                out.append((gid(room), {lid for lid, o in lights.items() if o in kids and o not in bad}))
        return out

    def _flush_batch(self, batch):
        """One grouped_light write replaces the writes to all lights of a room (or of the home) when the
        batch only sets on/off, to the same value, on every reachable light of the group. The rest goes
        light by light. Hue allows about 10 light and 1 group command per second."""
        out, left = {}, dict(batch)
        with self.lock:
            groups = self.groups()
        for gid, members in groups:
            if not members or not members.issubset(left):
                continue
            vals = {left[k].get(ON) if list(left[k]) == [ON] else None for k in members}
            if len(vals) != 1 or None in vals:
                continue
            time.sleep(max(0.0, self.last_group + HUE_GROUP_INTERVAL - time.monotonic()))
            self.last_group = time.monotonic()
            try:
                ok = ok_resp(*self.call("PUT", f"/resource/grouped_light/{gid}", {"on": {"on": bool(vals.pop())}})[:2])
            except Exception as error:
                log(f"hue: {describe(error)}")
                ok = False
            for k in members:
                out[k] = ok
                left.pop(k)
        for k, f in left.items():
            try:
                out[k] = self._flush(k, f)
            except Exception as error:
                log(f"hue: {describe(error)}")
                out[k] = False
            time.sleep(self.interval)
        return out

    def _flush(self, lid, f):
        body = self.light_body(lid, f)
        return bool(body) and ok_resp(*self.call("PUT", f"/resource/light/{lid}", body)[:2])

    def scene(self, sid):
        rid = sid.split(":", 1)[1]
        smart = rid in self.res.get("smart_scene", {})
        action = {"recall": {"action": "activate" if smart else "active"}}
        return ok_resp(*self.call("PUT", f"/resource/{'smart_scene' if smart else 'scene'}/{rid}", action)[:2])

    def trigger(self, sid, enabled):
        rid = sid.split(":", 1)[1]
        return ok_resp(*self.call("PUT", f"/resource/behavior_instance/{rid}", {"enabled": bool(enabled)})[:2])


def gamut_of(light):
    g = dig(light, "color", "gamut")
    try:
        return tuple((g[k]["x"], g[k]["y"]) for k in ("red", "green", "blue")) if g else None
    except (KeyError, TypeError):
        return None


def fan_mode(active, speed):
    """DIRIGERA fanMode for the purifier's on/off and speed (unverified names)."""
    if active == 0 or speed == 0:
        return "off"
    if speed:
        return "low" if speed <= 25 else "medium" if speed <= 50 else "high"
    return "auto"


class Ikea(Source):
    """IKEA DIRIGERA, local REST API on port 8443 with a pinned certificate, live state by WebSocket.
    Attribute names below follow the public API as other clients use it; those marked unverified are guesses."""

    name, label, interval = "ikea", "IKEA", IKEA_INTERVAL
    PORT = 8443

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self.dev, self.scn = {}, {}

    def call(self, method, path, body=None):
        return hub_call(self.cfg["host"], self.PORT, self.cfg["pin"], method, "/v1" + path, body=body,
                        headers={"Authorization": "Bearer " + self.cfg["token"]})

    def fetch(self):
        st, devs, _ = self.call("GET", "/devices")
        if st != 200 or not isinstance(devs, list):
            raise OSError(f"DIRIGERA answered HTTP {st}")
        st, scn, _ = self.call("GET", "/scenes")
        scn = scn if st == 200 and isinstance(scn, list) else []
        self.load(devs, scn)

    def load(self, devs, scn):
        with self.lock:
            self.dev = {d["id"]: d for d in devs if isinstance(d, dict) and "id" in d}
            self.scn = {s["id"]: s for s in scn if isinstance(s, dict) and "id" in s}
            self.replay()

    def relocate(self):
        ip = pinned_host(find_ikea(), self.PORT, self.cfg["pin"])
        if ip and ip != self.cfg["host"]:
            self.cfg["host"] = ip
            self.save()

    def stream(self):
        import websocket
        sock = pinned_socket(self.cfg["host"], self.PORT, self.cfg["pin"])  # checked before the token is sent
        ws = websocket.WebSocket()
        try:
            ws.connect(f"wss://{self.cfg['host']}:{self.PORT}/v1", socket=sock,
                       header=["Authorization: Bearer " + self.cfg["token"]], redirect_limit=0)
            ws.settimeout(WS_PING)
            while True:
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    ws.ping()
                    continue
                if not msg:
                    raise OSError("DIRIGERA WebSocket closed")
                try:
                    ev = json.loads(msg)
                except ValueError:
                    continue
                if self.apply_event(ev):
                    self.refresh()
                self.notify()
        finally:
            ws.close()

    def apply_event(self, ev):
        """Merge a WebSocket message into the cache. True when everything must be fetched again."""
        if not isinstance(ev, dict):
            return False
        d = ev.get("data")
        with self.lock:
            if self.backlog is not None:
                self.backlog.append(ev)  # a fetch is in flight: load() replays it
            if ev.get("type") == "deviceStateChanged" and isinstance(d, dict) and d.get("id") in self.dev:
                old = self.dev[d["id"]]
                new = dig(d, "attributes", default={})
                moved = "room" in d and d["room"] != old.get("room")
                renamed = "customName" in new and new["customName"] != (old.get("attributes") or {}).get("customName")
                deep_merge(old, d)
                return moved or renamed  # renamed or moved: fetch again
        return ev.get("type") in ("deviceAdded", "deviceRemoved", "sceneCreated", "sceneUpdated", "sceneDeleted",
                                  "roomCreated", "roomUpdated", "roomDeleted")

    # --- model ---

    @staticmethod
    def pos(level):
        """HAP position (100 = open) from a DIRIGERA blinds level."""
        return None if level is None else 100 - level if IKEA_BLINDS_INVERTED else level

    def translate(self):
        with self.lock:
            devs, scn = list(self.dev.values()), list(self.scn.values())
        accs, rooms = [], []
        for d in devs:
            a = d.get("attributes") or {}
            kind, did = d.get("deviceType"), d["id"]
            name = a.get("customName") or a.get("model") or did
            room = dig(d, "room", "name")
            if room:
                rooms.append(room)
            sv = lambda suffix, s, chars, label="": hsvc(f"ikea:{did}:{suffix}", f"{name} {label}".strip(), s, chars)
            svcs = []
            if kind == "light":
                chars = [hchar(ON, bool(a.get("isOn")))]
                ct_mode = a.get("colorMode") != "color"
                if "lightLevel" in a:
                    chars.append(hchar(BRI, a["lightLevel"], "int", 1, 100, 1, "percentage"))
                if "colorHue" in a and "colorSaturation" in a:
                    chars += [hchar(HUE, None if ct_mode else a["colorHue"], "float", 0, 360, 1, "arcdegrees"),
                              hchar(SAT, None if ct_mode else round(a["colorSaturation"] * 100, 1), "float", 0, 100, 1, "percentage")]
                if "colorTemperature" in a:
                    # IKEA names the warmest value "Max" and the coolest "Min": sort, so that the order does not matter
                    lo, hi = mired_range(a.get("colorTemperatureMin", 4000), a.get("colorTemperatureMax", 2202))
                    chars.append(hchar(CT, mired(a["colorTemperature"]) if ct_mode else None, "uint32", lo, hi, 1))
                svcs.append(sv("light", "43", chars))
            elif kind == "outlet":
                svcs.append(sv("outlet", "47", [hchar(ON, bool(a.get("isOn")))]))
            elif kind == "blinds":
                svcs.append(sv("blinds", "8C", [hchar(POSC, self.pos(a.get("blindsCurrentLevel")), "uint8", 0, 100, 1, "percentage", False),
                                                hchar(POST, self.pos(a.get("blindsTargetLevel")), "uint8", 0, 100, 1, "percentage")]))
            elif kind == "airPurifier":
                mode = a.get("fanMode")  # off, low, medium, high, auto (unverified)
                speed = {"off": 0, "low": 25, "medium": 50, "high": 100}.get(mode)
                svcs.append(sv("purifier", "BB", [hchar(ACTIVE, 0 if mode in (None, "off") else 1, "uint8", 0, 1, 1),
                                                  hchar(SPEED, speed, "float", 0, 100, 25, "percentage")]))
                if "currentPM25" in a:
                    svcs.append(sv("pm25", "8D", [hchar(PM25, a["currentPM25"], "float", w=False)], "PM2.5"))
            elif kind == "motionSensor":
                svcs.append(sv("motion", "85", [hchar(MOTION, bool(a.get("isDetected")), w=False)]))
            elif kind == "openCloseSensor":
                svcs.append(sv("contact", "80", [hchar(CONTACT, 1 if a.get("isOpen") else 0, "uint8", w=False)]))
            elif kind == "environmentSensor":
                if "currentTemperature" in a:
                    svcs.append(sv("temp", "8A", [hchar(CURT, a["currentTemperature"], "float", -50, 100, 0.1, "celsius", False)], "temperature"))
                if "currentRH" in a:
                    svcs.append(sv("rh", "82", [hchar(HUM, a["currentRH"], "float", 0, 100, 1, "percentage", False)], "humidity"))
                if "currentPM25" in a:
                    svcs.append(sv("pm25", "8D", [hchar(PM25, a["currentPM25"], "float", w=False)], "PM2.5"))
            elif kind == "waterSensor":  # attribute name unverified
                svcs.append(sv("leak", "83", [hchar(LEAK, 1 if a.get("waterLeakDetected") else 0, "uint8", w=False)]))
            if svcs and a.get("batteryPercentage") is not None:
                svcs.append(sv("battery", "96", [hchar(BATLVL, a["batteryPercentage"], "uint8", 0, 100, 1, "percentage", False)], "battery"))
            if svcs:
                accs.append(hacc("ikea:" + did, name, room, svcs, d.get("isReachable", True)))
        scenes = [{"id": "ikea:" + s["id"], "name": dig(s, "info", "name") or s.get("name") or s["id"], "kind": "custom"} for s in scn]
        return {"rooms": rooms, "accessories": accs, "scenes": scenes, "triggers": []}

    # --- writes ---

    def body(self, did, f):
        """Request body of PATCH /v1/devices/{id} for the fields {char: value}, or None."""
        with self.lock:
            d = self.dev.get(did)
        if d is None:
            return None
        a, out = d.get("attributes") or {}, {}
        if ON in f:
            out["isOn"] = bool(f[ON])
        if BRI in f:
            out["lightLevel"] = int(max(1, min(100, round(f[BRI]))))
        if HUE in f or SAT in f:  # both together: the half that is not written comes from the light
            out["colorHue"] = round(float(f.get(HUE, a.get("colorHue", 0))) % 360, 1)
            out["colorSaturation"] = round(max(0.0, min(100.0, f.get(SAT, a.get("colorSaturation", 0) * 100))) / 100, 3)
        elif CT in f:
            out["colorTemperature"] = mired_to_kelvin(f[CT], a.get("colorTemperatureMin", 4000), a.get("colorTemperatureMax", 2202))
        if POST in f:
            p = int(max(0, min(100, round(f[POST]))))
            out["blindsTargetLevel"] = 100 - p if IKEA_BLINDS_INVERTED else p
        if ACTIVE in f or SPEED in f:
            out["fanMode"] = fan_mode(f.get(ACTIVE), f.get(SPEED))
        return [{"attributes": out}] if out else None

    def _flush(self, did, f):
        body = self.body(did, f)
        return bool(body) and ok_resp(*self.call("PATCH", f"/devices/{did}", body)[:2])

    def scene(self, sid):
        return ok_resp(*self.call("POST", f"/scenes/{sid.split(':', 1)[1]}/trigger")[:2])


class RateLimited(OSError):
    pass


def tado_mode(state):
    """(mode, target) of a zone state: mode 0 off, 1 heat (manual overlay), 3 auto (schedule)."""
    target = dig(state, "setting", "temperature", "celsius")
    if dig(state, "setting", "power") == "OFF":
        return 0, target
    return (1 if state.get("overlay") else 3), target


def retry_after(value):
    """Seconds to wait after HTTP 429: the header value, or TADO_RATE_WAIT when it is missing, not a
    finite number or negative. At most one day."""
    try:
        wait = float(value or "")
    except ValueError:
        return TADO_RATE_WAIT
    return min(wait, 86400.0) if math.isfinite(wait) and wait >= 0 else TADO_RATE_WAIT


class Tado(Source):
    """tado, cloud API. Free accounts get about 100 requests per day, so it polls every TADO_POLL seconds,
    refreshes once after a write and stops for the Retry-After time after HTTP 429."""

    name, label, delay, periodic = "tado", "tado", TADO_COALESCE, False
    HOST = "my.tado.com"

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self.zones, self.states, self.access, self.expires, self.blocked = [], {}, None, 0.0, 0.0

    def token(self):
        """Access token. Refresh tokens rotate: the new one is saved at once."""
        with self.lock:
            if self.access and time.time() < self.expires - 30:
                return self.access
            stored = self.store.get(self.name)  # another process may have rotated the refresh token
            if isinstance(stored, dict) and stored.get("refresh_token") and stored["refresh_token"] != self.cfg["refresh_token"]:
                self.cfg["refresh_token"] = stored["refresh_token"]
                if stored.get("access_token") and time.time() < stored.get("expires_at", 0) - 30:
                    self.access, self.expires = stored["access_token"], stored["expires_at"]
                    return self.access
            st, obj, _ = cloud_call("login.tado.com", "POST", "/oauth2/token", form={
                "client_id": TADO_CLIENT, "grant_type": "refresh_token", "refresh_token": self.cfg["refresh_token"]})
            if st != 200 or not isinstance(obj, dict) or "access_token" not in obj:
                raise OSError(f"tado refused the login (HTTP {st}): pair tado again if this stays")
            self.access, self.expires = obj["access_token"], time.time() + obj.get("expires_in", 600)
            self.cfg["refresh_token"] = obj.get("refresh_token", self.cfg["refresh_token"])
            self.cfg["access_token"], self.cfg["expires_at"] = self.access, self.expires  # for a second process
            self.save()
            return self.access

    def api(self, method, path, body=None):
        if time.time() < self.blocked:
            raise RateLimited("tado rate limit: waiting")
        st, obj, headers = cloud_call(self.HOST, method, "/api/v2" + path, body=body,
                                      headers={"Authorization": "Bearer " + self.token()})
        if st == 429:
            wait = retry_after(headers.get("Retry-After"))
            self.blocked = time.time() + wait
            raise RateLimited(f"tado answered HTTP 429: no requests for {wait / 60:.0f} minutes")
        return st, obj

    def fetch(self):
        hid = self.cfg["home_id"]
        try:
            if not self.zones:
                st, zones = self.api("GET", f"/homes/{hid}/zones")
                if st != 200 or not isinstance(zones, list):
                    raise OSError(f"tado answered HTTP {st}")
                self.zones = zones
            st, obj = self.api("GET", f"/homes/{hid}/zoneStates")
            if st != 200 or not isinstance(obj, dict):
                raise OSError(f"tado answered HTTP {st}")
            with self.lock:
                self.states = obj.get("zoneStates", {})
        except RateLimited:
            if not self.states:
                raise  # nothing to show yet; otherwise keep the last state

    def stream(self):
        while True:
            time.sleep(TADO_POLL)
            self.refresh()
            self.notify()

    def key_of(self, service):
        return ":".join(service.split(":")[1:3])

    def translate(self):
        with self.lock:
            zones, states = list(self.zones), dict(self.states)
        hid, accs = self.cfg["home_id"], []
        for z in zones:
            s = states.get(str(z.get("id")))
            if z.get("type") != "HEATING" or not s:
                continue
            mode, target = tado_mode(s)
            cur = dig(s, "sensorDataPoints", "insideTemperature", "celsius")
            hum = dig(s, "sensorDataPoints", "humidity", "percentage")
            heating = (dig(s, "activityDataPoints", "heatingPower", "percentage", default=0) or 0) > 0
            chars = [hchar(CURT, cur, "float", -50, 100, 0.1, "celsius", False),
                     hchar(TARGT, target, "float", 5, 25, 0.5, "celsius"),
                     hchar(CURMODE, 1 if heating else 0, "uint8", 0, 2, 1, w=False),
                     hchar(TARMODE, mode, "uint8", 0, 3, 1, valid=[0, 1, 3])]  # tado has no cooling in a heating zone
            if hum is not None:
                chars.append(hchar(HUM, hum, "float", 0, 100, 1, "percentage", False))
            aid, name = f"tado:{hid}:{z['id']}", f"{z.get('name', 'Zone')} heating"
            accs.append(hacc(aid, name, z.get("name"), [hsvc(aid + ":th", name, "4A", chars)],
                             dig(s, "link", "state", default="ONLINE") == "ONLINE"))
        return {"rooms": [], "accessories": accs, "scenes": [], "triggers": []}

    def overlay(self, key, f):
        """(method, path, body) for the fields {char: value} of one zone. Auto removes the overlay."""
        hid, zid = key.split(":")
        mode0, target0 = tado_mode(self.states.get(zid) or {})
        mode = f.get(TARMODE, 1 if TARGT in f else mode0)  # a new target means manual heat
        path = f"/homes/{hid}/zones/{zid}/overlay"
        if mode == 3:
            return "DELETE", path, None
        setting = {"type": "HEATING", "power": "OFF"} if mode == 0 else {
            "type": "HEATING", "power": "ON", "temperature": {"celsius": float(f.get(TARGT, target0 or TADO_DEFAULT_TARGET))}}
        return "PUT", path, {"setting": setting, "termination": dict(TADO_TERMINATION)}

    def _flush(self, key, f):
        method, path, body = self.overlay(key, f)
        st, _ = self.api(method, path, body)
        if st == 422 and body:  # tado names the end of an overlay in more than one way: try "until changed" once
            body["termination"] = dict(TADO_TERMINATION_FALLBACK)
            st, _ = self.api(method, path, body)
        ok = st in (200, 201, 202, 204)
        if ok:
            try:
                self.refresh()  # one refresh after each write
                self.notify()
            except Exception as error:
                log(f"tado: {describe(error)}")
        return ok


SOURCES = {"hue": Hue, "ikea": Ikea, "tado": Tado}


class Hub:
    """The link of the UI for real hubs: runs the sources, merges them into one home and sends changes as
    snapshot, char and reach messages. Writes go to the source that owns the id prefix."""

    def __init__(self, events, sources, names=None):
        self.events, self.sources, self.names = events, sources, names or Names(None)
        self.by_name = {s.name: s for s in sources}
        self.lock, self.last, self.was = threading.Lock(), None, False
        for s in sources:
            s.notify = self.update

    @property
    def connected(self):
        return any(s.online for s in self.sources)

    @property
    def offline(self):
        """Text of key 0 when no hub answers: which hubs are down."""
        if not self.sources:
            return "No hub\npaired"
        return "Offline:\n" + " ".join(s.label for s in self.sources)

    def start(self):
        for s in self.sources:
            s.start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        """An edit of names.json shows within NAMES_POLL seconds, also when no hub sends an event."""
        while True:
            time.sleep(NAMES_POLL)
            if self.names.check():
                self.update()

    @staticmethod
    def part(s):
        """translate() of one source. When it raises, the last good result stays (empty before the first one),
        so one broken source does not stop the others. The error is logged once per distinct text."""
        try:
            s.last_good, s.last_error = s.translate(), None
        except Exception as error:
            text = describe(error)
            if text != s.last_error:
                s.last_error = text
                log(f"{s.name}: {text}")
        return s.last_good or {"rooms": [], "accessories": [], "scenes": [], "triggers": []}

    def snapshot(self):
        return {"type": "snapshot", "homes": [merge([(s.name, s.online, self.part(s)) for s in self.sources], self.names)]}

    def update(self):
        """A source changed: send what differs from the last state."""
        self.names.check()  # every refresh and event also looks at the mtime of names.json
        with self.lock:
            new = self.snapshot()
            msgs = [("msg", copy.deepcopy(new))] if self.last is None else snap_diff(self.last, new)
            self.last = new
            for m in msgs:
                self.events.put(m)
            if self.connected != self.was:
                self.was = self.connected
                self.events.put(("conn", self.was))

    def send(self, msg):
        op = msg.get("op")
        if op == "snapshot":
            self.events.put(("msg", self.snapshot()))
            return True
        sid = str(msg.get("service") or msg.get("id"))
        src = self.by_name.get(sid.partition(":")[0])
        if src is None or not src.online:
            return False
        done = lambda ok: self.events.put(("msg", {"type": "result", "req": msg.get("req"), "ok": bool(ok)}))
        if op == "write":
            return src.write(msg["service"], short(msg["char"]), msg["value"], done)
        if op == "scene":
            src.run(src.scene, done, sid)
        elif op == "trigger":
            src.run(src.trigger, done, sid, msg["enabled"])
        else:
            return False
        return True


def fake_snapshot():
    ch, sv, ac = hchar, hsvc, hacc

    def light(id, name, room, on, bri, color=None, ct=None, reachable=True):
        chars = [ch(ON, on), ch(BRI, bri, "int", 0, 100, 1, "percentage")]
        if color:
            chars += [ch(HUE, color[0], "float", 0, 360, 1, "arcdegrees"), ch(SAT, color[1], "float", 0, 100, 1, "percentage")]
        if ct:
            chars.append(ch(CT, ct, "uint32", 140, 500, 1))
        return ac("acc-" + id, name, room, [sv(id, name, "43", chars)], reachable)

    def outlet(id, name, room, on):
        return ac("acc-" + id, name, room, [sv(id, name, "47", [ch(ON, on)])])

    def sensor(id, name, room, s, char, value, fmt="bool", extra=()):
        return ac("acc-" + id, name, room, [sv(id, name, s, [ch(char, value, fmt, w=False), *extra])])

    rooms = ["living", "kitchen", "bedroom", "office", "hall"]
    accessories = [
        light("floor", "Floor lamp", "living", True, 60, (30, 80), 300),
        light("ceiling", "Ceiling", "living", True, 80),
        outlet("tv", "TV plug", "living", True),
        ac("acc-blinds", "Living blinds", "living", [sv("blinds", "Living blinds", "8C", [ch(POSC, 50, "uint8", 0, 100, 1, w=False), ch(POST, 50, "uint8", 0, 100, 1)])]),
        ac("acc-thermo", "Thermostat", "living", [sv("thermo", "Thermostat", "4A", [
            ch(CURT, 21.5, "float", 0, 100, 0.1, "celsius", False), ch(TARGT, 22.0, "float", 10, 30, 0.5, "celsius"),
            ch(CURMODE, 1, "uint8", w=False), ch(TARMODE, 1, "uint8", 0, 3, 1), ch(HUM, 46, "float", 0, 100, 1, w=False)])]),
        sensor("living-motion", "Living motion", "living", "85", MOTION, False),
        light("counter", "Counter", "kitchen", True, 100, None, 250),
        light("pendant", "Pendant", "kitchen", False, 40),
        outlet("coffee", "Coffee machine", "kitchen", False),
        sensor("leak", "Sink leak", "kitchen", "83", LEAK, 0, "uint8"),
        sensor("smoke", "Smoke alarm", "kitchen", "87", SMOKE, 0, "uint8"),
        light("bed-l", "Bedside left", "bedroom", False, 30, (0, 0), 370),
        light("bed-r", "Bedside right", "bedroom", False, 30, None, None, False),
        ac("acc-fan", "Bedroom fan", "bedroom", [sv("fan", "Bedroom fan", "40", [ch(ON, True), ch(SPEED, 50, "float", 0, 100, 25, "percentage")])]),
        ac("acc-air", "Air purifier", "bedroom", [sv("air", "Air purifier", "BB", [ch(ACTIVE, 1, "uint8", 0, 1, 1), ch(SPEED, 50, "float", 0, 100, 25)])]),
        light("desk", "Desk lamp", "office", True, 70, (45, 20), 280),  # writes to this one fail
        sensor("window", "Window contact", "office", "80", CONTACT, 1, "uint8", [ch(LOWBAT, 1, "uint8", w=False)]),
        sensor("lux", "Daylight", "office", "84", LUX, 120.0, "float"),
        sensor("office-temp", "Office temp", "office", "8A", CURT, 20.5, "float"),
        ac("acc-lock", "Front door", "hall", [sv("lock", "Front door", "45", [ch(LOCKC, 1, "uint8", 0, 3, 1, w=False), ch(LOCKT, 1, "uint8", 0, 1, 1)])]),
        ac("acc-garage", "Garage", "hall", [sv("garage", "Garage", "41", [ch(DOORC, 1, "uint8", 0, 4, 1, w=False), ch(DOORT, 1, "uint8", 0, 1, 1), ch(OBST, False, w=False)])]),
        ac("acc-alarm", "Alarm", "hall", [sv("alarm", "Alarm", "7E", [ch(SECC, 3, "uint8", 0, 4, 1, w=False), ch(SECT, 3, "uint8", 0, 3, 1)])]),
        sensor("hall-motion", "Hall motion", "hall", "85", MOTION, True),
        sensor("presence", "Presence", "hall", "86", OCC, 0, "uint8"),
    ]
    scenes = [{"id": f"scene-{i}", "name": n, "kind": "custom"} for i, n in
              enumerate(("Good morning", "Movie", "Dinner", "Good night", "Away", "Party"))]
    triggers = [{"id": "trig-1", "name": "Sunset lights", "enabled": True}, {"id": "trig-2", "name": "Weekday wake", "enabled": True},
                {"id": "trig-3", "name": "Away mode", "enabled": False}]
    return {"type": "snapshot", "homes": [{"id": "home-1", "name": "Home", "primary": True,
                                           "rooms": [{"id": r, "name": n} for r, n in zip(rooms, ("Living Room", "Kitchen", "Bedroom", "Office", "Hallway"))],
                                           "accessories": accessories, "scenes": scenes, "triggers": triggers}]}


class FakeBridge:
    """In-process stand-in for the bridge. Writes succeed after 100 ms; the Desk lamp and the Party scene fail."""

    FAILS = {"desk", "scene-5"}
    connected = True

    def __init__(self, events):
        self.events, self.snap = events, fake_snapshot()
        self.model = Model()
        self.model.load(self.snap)

    def start(self):
        self.events.put(("conn", True))

    def send(self, msg):
        threading.Timer(0.1, self._answer, [msg]).start()
        return True

    def _answer(self, msg):
        out, op, ok = [], msg.get("op"), True
        if op == "snapshot":
            out.append(("msg", copy.deepcopy(self.snap)))  # the UI keeps live values in its copy
        elif op == "write":
            sid = msg["service"]
            ok = sid not in self.FAILS and self.model.char(sid, short(msg["char"])) is not None
            if ok:
                ch = short(msg["char"])
                self.model.set(sid, ch, msg["value"])
                out.append(("msg", {"type": "char", "service": sid, "char": msg["char"], "value": msg["value"]}))
                if ch in MIRROR and self.model.char(sid, MIRROR[ch][0]):
                    cur = MIRROR[ch][0]  # the fake device reaches the target at once
                    self.model.set(sid, cur, msg["value"])
                    out.append(("msg", {"type": "char", "service": sid, "char": full(cur), "value": msg["value"]}))
        elif op == "scene":
            ok = msg["id"] not in self.FAILS
        elif op == "trigger":
            for t in self.snap["homes"][0]["triggers"]:
                if t["id"] == msg["id"]:
                    t["enabled"] = msg["enabled"]
        out.append(("msg", {"type": "result", "req": msg["req"], "ok": ok, **({} if ok else {"error": "no response"})}))
        for ev in out:
            self.events.put(ev)


# --- hardware loop ---

def run(deck, events, link):
    """Drive one connected deck until it disconnects (raises TransportError) or Ctrl+C."""
    from StreamDeck.ImageHelpers import PILHelper

    deck.open()
    deck.reset()
    deck.set_brightness(BRIGHTNESS)
    size = deck.key_image_format()["size"][0]
    ui, shown, brightness = UI(link, time.time()), None, BRIGHTNESS
    deck.set_key_callback(lambda _deck, key, down: events.put(("key", key, down)))
    print(f"{deck.deck_type()} ready", flush=True)
    try:
        while True:
            now = time.time()
            ui.tick(now)
            wake = ui.next_deadline(now)
            try:
                ev = events.get(timeout=min(1.0, max(0.0, wake - now)) if wake else 1.0)
                while True:
                    ui.handle(ev, time.time())
                    ev = events.get_nowait()
            except queue.Empty:
                pass
            now = time.time()
            ui.tick(now)
            if (want := ui.brightness(now)) != brightness:
                brightness = want
                with deck:
                    deck.set_brightness(brightness)
            new = ui.keys(now)
            if new != shown:
                with deck:
                    for i, k in enumerate(new):
                        if shown is None or k != shown[i]:
                            deck.set_key_image(i, PILHelper.to_native_key_format(deck, face(k, size)))
                shown = new
    finally:
        try:
            with deck:
                deck.reset()
                deck.close()
        except Exception:
            pass  # the deck is already gone


def paired_hub(events, skip=()):
    """The Hub of all paired hubs (except the `skip` names), not started yet."""
    store = Creds()
    return Hub(events, [cls(cfg, store) for name, cls in SOURCES.items() if name not in skip and (cfg := store.get(name))], Names())


def serve(demo=False):
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.Transport.Transport import TransportError

    events = queue.Queue()
    if demo:
        link = FakeBridge(events)
    else:
        link = paired_hub(events)
        if not link.sources:
            log("No hub is paired. Run: stream-deck-home pair hue|ikea|tado")
    link.start()
    waiting_printed = False
    try:
        while True:  # reconnect after an unplug or a USB reset
            decks = DeviceManager().enumerate()
            if not decks:
                if not waiting_printed:
                    print("Waiting for a Stream Deck. Plug it in and quit the Elgato Stream Deck app.", flush=True)
                    waiting_printed = True
                time.sleep(2)
                continue
            waiting_printed = False
            try:
                run(decks[0], events, link)
            except TransportError as error:
                print(f"Stream Deck disconnected ({error}), reconnecting.", flush=True)
                time.sleep(2)
    except KeyboardInterrupt:
        pass


class Stub:
    """Link for tests: records what the UI sends."""
    connected = True

    def __init__(self):
        self.sent, self.ok = [], True

    def send(self, msg):
        self.sent.append(msg)
        return self.ok


def test_ui(snap=None):
    ui = UI(Stub(), 0.0)
    ui.on_msg(snap or fake_snapshot(), 0.0)
    return ui


def label(keys):
    return [k.label.replace("\n", " ") for k in keys]


HUE_GAMUT = {"red": {"x": 0.6915, "y": 0.3083}, "green": {"x": 0.17, "y": 0.7}, "blue": {"x": 0.1532, "y": 0.0475}}


def hue_sample():
    """Resources as GET /clip/v2/resource returns them (trimmed to the fields we read)."""
    own = lambda d: {"rid": d, "rtype": "device"}
    zig = lambda i, d, status="connected": {"id": i, "type": "zigbee_connectivity", "owner": own(d), "status": status}
    dev = lambda i, name, arch="sultan_bulb": {"id": i, "type": "device", "metadata": {"name": name}, "product_data": {"product_archetype": arch}}
    return [
        dev("d1", "Floor lamp"),
        {"id": "l1", "type": "light", "owner": own("d1"), "metadata": {"name": "Floor lamp"}, "on": {"on": True},
         "dimming": {"brightness": 54.3, "min_dim_level": 0.2},
         "color_temperature": {"mirek": None, "mirek_valid": False, "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500}},
         "color": {"xy": {"x": 0.4, "y": 0.45}, "gamut": HUE_GAMUT, "gamut_type": "C"}}, zig("z1", "d1"),
        dev("d5", "Hall lamp"),
        {"id": "l5", "type": "light", "owner": own("d5"), "metadata": {"name": "Hall lamp"}, "on": {"on": True},
         "dimming": {"brightness": 100}}, zig("z5", "d5"),
        dev("d3", "TV plug", "plug"),
        {"id": "l3", "type": "light", "owner": own("d3"), "metadata": {"name": "TV plug", "archetype": "plug"}, "on": {"on": False}},
        zig("z3", "d3", "connectivity_issue"),
        dev("d2", "Hall sensor", "unknown_archetype"),
        {"id": "m1", "type": "motion", "owner": own("d2"), "motion": {"motion_report": {"motion": True}}},
        {"id": "t1", "type": "temperature", "owner": own("d2"), "temperature": {"temperature_report": {"temperature": 19.25}}},
        {"id": "ll1", "type": "light_level", "owner": own("d2"), "light": {"light_level_report": {"light_level": 20001}}},
        {"id": "p1", "type": "device_power", "owner": own("d2"), "power_state": {"battery_state": "low", "battery_level": 12}},
        zig("z2", "d2"),
        dev("d4", "Door sensor"),
        {"id": "c1", "type": "contact", "owner": own("d4"), "contact_report": {"state": "no_contact"}}, zig("z4", "d4"),
        {"id": "r1", "type": "room", "metadata": {"name": "Living Room"}, "children": [own("d1"), own("d3")],
         "services": [{"rid": "g1", "rtype": "grouped_light"}]},
        {"id": "r2", "type": "room", "metadata": {"name": "Hall"}, "children": [own("d2"), own("d4"), own("d5")],
         "services": [{"rid": "g2", "rtype": "grouped_light"}]},
        {"id": "bh", "type": "bridge_home", "services": [{"rid": "gh", "rtype": "grouped_light"}]},
        {"id": "s1", "type": "scene", "metadata": {"name": "Relax"}, "group": {"rid": "r1", "rtype": "room"}},
        {"id": "s2", "type": "scene", "metadata": {"name": "Relax"}, "group": {"rid": "r2", "rtype": "room"}},
        {"id": "ss1", "type": "smart_scene", "metadata": {"name": "Wake up"}, "group": {"rid": "r2", "rtype": "room"}},
        {"id": "b1", "type": "behavior_instance", "enabled": True, "metadata": {"name": "Sunset"}},
        {"id": "b2", "type": "behavior_instance", "enabled": False, "metadata": {"name": "Away"}},
        {"id": "x1", "type": "bridge", "owner": own("d1")},  # unknown types are ignored
    ]


IKEA_DEVICES = [
    {"id": "i1", "deviceType": "light", "isReachable": True, "room": {"id": "x", "name": "living room"},
     "attributes": {"customName": "Ceiling", "isOn": True, "lightLevel": 70, "colorTemperature": 2700,
                    "colorTemperatureMin": 4000, "colorTemperatureMax": 2202, "colorMode": "temperature",
                    "colorHue": 30.0, "colorSaturation": 0.5}},
    {"id": "i2", "deviceType": "blinds", "isReachable": True, "room": {"name": "Bedroom"},
     "attributes": {"customName": "Blind", "blindsCurrentLevel": 20, "blindsTargetLevel": 20, "batteryPercentage": 15}},
    {"id": "i3", "deviceType": "environmentSensor", "isReachable": True, "room": {"name": "Bedroom"},
     "attributes": {"customName": "Air", "currentTemperature": 21.5, "currentRH": 40, "currentPM25": 7}},
    {"id": "i4", "deviceType": "airPurifier", "isReachable": True, "room": {"name": "Bedroom"},
     "attributes": {"customName": "Purifier", "fanMode": "medium", "currentPM25": 3}},
    {"id": "i5", "deviceType": "outlet", "isReachable": False, "room": {"name": "Bedroom"}, "attributes": {"customName": "Lamp plug", "isOn": False}},
    {"id": "i6", "deviceType": "motionSensor", "room": {"name": "Bedroom"}, "attributes": {"customName": "Motion", "isDetected": False}},
    {"id": "i7", "deviceType": "openCloseSensor", "room": {"name": "Bedroom"}, "attributes": {"customName": "Window", "isOpen": True}},
    {"id": "i8", "deviceType": "waterSensor", "room": {"name": "Bedroom"}, "attributes": {"customName": "Leak", "waterLeakDetected": False}},
    {"id": "i9", "deviceType": "gateway", "attributes": {"customName": "Hub"}},  # unknown device types are ignored
    {"id": "i10", "deviceType": "newThing", "attributes": {"surprise": [1, 2]}},
]
TADO_ZONES = [{"id": 1, "name": "Living Room", "type": "HEATING"}, {"id": 0, "name": "Hot water", "type": "HOT_WATER"}]


def tado_states():
    return {"1": {"setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": 21.0}}, "overlay": None,
                  "link": {"state": "ONLINE"}, "sensorDataPoints": {"insideTemperature": {"celsius": 20.44}, "humidity": {"percentage": 48.2}},
                  "activityDataPoints": {"heatingPower": {"percentage": 40}}},
            "0": {"setting": {"type": "HOT_WATER", "power": "ON"}}}


def drain(events):
    out = []
    while not events.empty():
        out.append(events.get_nowait())
    return out


def test_hubs():
    # credentials: mode 0600 in a folder with mode 0700, atomic, no wider mode survives a load
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "conf", "credentials.json")
        store = Creds(path)
        store.put("hue", {"key": "secret"})
        store.put("ikea", {"token": "t"})
        assert oct(os.stat(path).st_mode & 0o777) == "0o600" and oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
        os.chmod(path, 0o644)
        assert store.get("hue") == {"key": "secret"} and os.stat(path).st_mode & 0o777 == 0o600
        store.put("hue", None)
        assert store.get("hue") is None and store.get("ikea") == {"token": "t"} and not os.listdir(os.path.dirname(path)) == []
        try:
            pinned_socket("127.0.0.1", 9, None)  # no pin and no first_use: refused before any connection
            raise AssertionError("expected PinError")
        except PinError:
            pass
        # color math
        for h, s in ((30, 60), (120, 50), (200, 40), (280, 45), (60, 70)):
            x, y = hs_to_xy(h, s, GAMUT_C)
            h2, s2 = xy_to_hs(x, y)
            assert abs((h2 - h + 180) % 360 - 180) < 4 and abs(s2 - s) < 6, (h, s, h2, s2)
        red, red_c = hs_to_xy(0, 100), hs_to_xy(0, 100, GAMUT_C)
        assert red != red_c and to_gamut(*red_c, GAMUT_C) == red_c  # a color outside the gamut moves in
        assert to_gamut(0.3, 0.3, GAMUT_C) == (0.3, 0.3) and xy_to_hs(0.3, 0.0) == (0.0, 0.0)
        assert mired_range(4000, 2202) == mired_range(2202, 4000) == (250, 454) and mired(2700) == 370
        assert mired_to_kelvin(154, 2202, 4000) == 4000 and mired_to_kelvin(900, 4000, 2202) == 2202 and mired_to_kelvin(370, 2202, 4000) == 2703
        assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        # discovery output of dns-sd
        browse = "14:27:52.330  Add        2  13 local.               _hue._tcp.           Hue Bridge - 123456\n"
        assert parse_browse(browse) == ["Hue Bridge - 123456"]
        look = ("14:27:56.438  Hue\\032Bridge._hue._tcp.local. can be reached at 001788123456.local.:443 (interface 13)\n"
                " bridgeid=001788fffe123456 modelid=BSB002\n")
        assert parse_lookup(look) == ("001788123456.local", 443, {"bridgeid": "001788fffe123456", "modelid": "BSB002"})
        assert parse_lookup("nothing") is None
        # Hue: translation
        hue = Hue({"host": "h", "pin": "p", "key": "k"}, store)
        hue.load(hue_sample())
        hue.interval = 0
        t = hue.translate()
        by = {a["name"]: a for a in t["accessories"]}
        assert t["rooms"] == ["Living Room", "Hall"] and set(by) == {"Floor lamp", "Hall lamp", "TV plug", "Hall sensor", "Door sensor"}, set(by)
        chars = lambda a, s: {short(c["type"]): c for c in a["services"][s]["chars"]}
        lamp = chars(by["Floor lamp"], 0)
        assert by["Floor lamp"]["id"] == "hue:d1" and by["Floor lamp"]["services"][0]["id"] == "hue:l1" and by["Floor lamp"]["room"] == "Living Room"
        assert lamp[ON]["value"] is True and lamp[BRI]["value"] == 54 and lamp[CT]["value"] is None and (lamp[CT]["min"], lamp[CT]["max"]) == (153, 500)
        assert 50 < lamp[HUE]["value"] < 100 and lamp[SAT]["value"] > 30
        assert by["Hall lamp"]["services"][0]["chars"][1]["value"] == 100 and len(by["Hall lamp"]["services"][0]["chars"]) == 2
        assert short(by["TV plug"]["services"][0]["type"]) == "47" and by["TV plug"]["reachable"] is False and by["Floor lamp"]["reachable"] is True
        kinds = {short(s["type"]): {short(c["type"]): c["value"] for c in s["chars"]} for s in by["Hall sensor"]["services"]}
        assert kinds["85"] == {MOTION: True} and kinds["8A"] == {CURT: 19.25} and kinds["84"][LUX] == 100.0 and kinds["96"] == {BATLVL: 12}
        assert {short(c["type"]): c["value"] for c in by["Door sensor"]["services"][0]["chars"]} == {CONTACT: 1}
        assert [(s["name"], s["room"]) for s in t["scenes"]] == [("Relax", "Living Room"), ("Relax", "Hall"), ("Wake up", "Hall")]
        assert t["scenes"][0]["id"] == "hue:s1"
        # a Hue plug is an outlet: on/off only, whichever of the two archetypes says plug
        for plug, arch in ((True, None), (False, "plug")):
            r = {"id": "x", "on": {"on": True}, "dimming": {"brightness": 50}, "metadata": {"archetype": arch}}
            svc = hue.light_service("hue:x", r, "Plug", plug)
            assert short(svc["type"]) == "47" and [short(c["type"]) for c in svc["chars"]] == [ON]
        assert t["triggers"] == [{"id": "hue:b1", "name": "Sunset", "enabled": True}, {"id": "hue:b2", "name": "Away", "enabled": False}]
        # Hue: write translation, ct mode, gamut, group writes
        assert hue.light_body("l1", {ON: True, BRI: 30}) == {"on": {"on": True}, "dimming": {"brightness": 30.0}}
        assert hue.light_body("l1", {BRI: 0})["dimming"]["brightness"] == 1.0 and hue.light_body("l1", {CT: 100}) == {"color_temperature": {"mirek": 153}}
        body = hue.light_body("l1", {HUE: 0, SAT: 100})["color"]["xy"]
        assert to_gamut(body["x"], body["y"], GAMUT_C) == (body["x"], body["y"]) and hue.light_body("nope", {ON: True}) is None
        assert hue.light_body("l1", {HUE: 200})["color"]["xy"] == hue.light_body("l1", {HUE: 200, SAT: xy_to_hs(0.4, 0.45)[1]})["color"]["xy"]
        calls = []
        hue.call = lambda method, path, body=None: (calls.append((method, path, body)) or (200, {"data": [], "errors": []}, None))
        globals()["HUE_GROUP_INTERVAL"] = 0
        off = lambda ids, **extra: {i: {ON: False, **extra} for i in ids}
        assert hue._flush_batch(off(["l1", "l5"])) == {"l1": True, "l5": True} and calls == [("PUT", "/resource/grouped_light/gh", {"on": {"on": False}})]
        calls.clear()  # not every light of the home: the room group of the Living Room (the plug is unreachable)
        hue._flush_batch(off(["l1"]))
        assert calls == [("PUT", "/resource/grouped_light/g1", {"on": {"on": False}})]
        calls.clear()  # more than on/off: light by light
        hue._flush_batch(off(["l1", "l5"], **{BRI: 5}))
        assert [c[1] for c in calls] == ["/resource/light/l1", "/resource/light/l5"]
        calls.clear()  # different values: light by light
        hue._flush_batch({"l1": {ON: False}, "l5": {ON: True}})
        assert [c[1] for c in calls] == ["/resource/grouped_light/g1", "/resource/grouped_light/g2"]
        # Hue: event stream message to char updates (deep merge keeps the gamut)
        events = queue.Queue()
        hub = Hub(events, [hue])
        hue.online = True
        hub.update()
        first = drain(events)
        assert first[0][1]["type"] == "snapshot" and first[1] == ("conn", True)
        again = hue.apply_event([{"id": "e", "type": "update", "data": [
            {"id": "l1", "type": "light", "on": {"on": False}, "dimming": {"brightness": 20}, "color": {"xy": {"x": 0.3, "y": 0.3}}},
            {"id": "m1", "type": "motion", "motion": {"motion_report": {"motion": False}}}, {"id": "zzz", "type": "bridge"}]}])
        hub.update()
        got = {(m[1]["service"], short(m[1]["char"])): m[1]["value"] for m in drain(events)}
        assert again is False and got[("hue:l1", ON)] is False and got[("hue:l1", BRI)] == 20 and got[("hue:m1", MOTION)] is False
        assert gamut_of(hue.res["light"]["l1"]) == GAMUT_C[0:1] + GAMUT_C[1:2] + GAMUT_C[2:3]
        assert hue.apply_event([{"type": "add", "data": []}]) is True and hue.apply_event("junk") is False
        # IKEA
        ikea = Ikea({"host": "i", "pin": "p", "token": "k"}, store)
        ikea.load(IKEA_DEVICES, [{"id": "sc1", "info": {"name": "Evening"}}])
        t = ikea.translate()
        by = {a["name"]: a for a in t["accessories"]}
        assert set(by) == {"Ceiling", "Blind", "Air", "Purifier", "Lamp plug", "Motion", "Window", "Leak"}, set(by)
        ch = lambda a, s: {short(c["type"]): c for c in a["services"][s]["chars"]}
        c = ch(by["Ceiling"], 0)
        assert (c[CT]["min"], c[CT]["max"], c[CT]["value"]) == (250, 454, 370) and c[HUE]["value"] is None and c[BRI]["value"] == 70
        assert by["Ceiling"]["room"] == "living room" and by["Ceiling"]["services"][0]["id"] == "ikea:i1:light"
        assert ch(by["Blind"], 0)[POSC]["value"] == (80 if IKEA_BLINDS_INVERTED else 20) and by["Blind"]["services"][1]["chars"][0]["value"] == 15
        assert [short(s["type"]) for s in by["Air"]["services"]] == ["8A", "82", "8D"] and ch(by["Air"], 2)[PM25]["value"] == 7
        assert (ch(by["Purifier"], 0)[ACTIVE]["value"], ch(by["Purifier"], 0)[SPEED]["value"]) == (1, 50)
        assert by["Lamp plug"]["reachable"] is False and ch(by["Window"], 0)[CONTACT]["value"] == 1 and ch(by["Leak"], 0)[LEAK]["value"] == 0
        assert t["scenes"] == [{"id": "ikea:sc1", "name": "Evening", "kind": "custom"}] and ikea.key_of("ikea:i1:light") == "i1"
        attr = lambda f, d="i1": ikea.body(d, f)[0]["attributes"]
        assert attr({ON: False, BRI: 150}) == {"isOn": False, "lightLevel": 100} and attr({CT: 154})["colorTemperature"] == 4000
        assert attr({CT: 900})["colorTemperature"] == 2202 and attr({HUE: 400, SAT: 60}) == {"colorHue": 40.0, "colorSaturation": 0.6}
        assert attr({SAT: 100}) == {"colorHue": 30.0, "colorSaturation": 1.0}
        assert attr({POST: 100}, "i2") == {"blindsTargetLevel": 0 if IKEA_BLINDS_INVERTED else 100}
        assert attr({ACTIVE: 0}, "i4") == {"fanMode": "off"} and attr({SPEED: 75}, "i4") == {"fanMode": "high"} and attr({ACTIVE: 1}, "i4") == {"fanMode": "auto"}
        assert ikea.body("nope", {ON: True}) is None and ikea.body("i1", {}) is None
        # IKEA: WebSocket message to char updates; unknown events and devices do not break anything
        events = queue.Queue()
        hub = Hub(events, [ikea])
        ikea.online = True
        hub.update()
        drain(events)
        assert ikea.apply_event({"type": "deviceStateChanged", "data": {"id": "i1", "attributes": {"isOn": False, "lightLevel": 10}}}) is False
        assert ikea.apply_event({"type": "deviceStateChanged", "data": {"id": "unknown", "attributes": {}}}) is False
        assert ikea.apply_event({"type": "deviceAdded", "data": {}}) is True and ikea.apply_event(["junk"]) is False and ikea.apply_event({"type": "pong"}) is False
        hub.update()
        got = {(m[1]["service"], short(m[1]["char"])): m[1]["value"] for m in drain(events)}
        assert got == {("ikea:i1:light", ON): False, ("ikea:i1:light", BRI): 10}, got
        # tado
        tado = Tado({"home_id": 123, "refresh_token": "r"}, store)
        tado.zones, tado.states = TADO_ZONES, tado_states()
        t = tado.translate()
        a = t["accessories"][0]
        c = {short(x["type"]): x for x in a["services"][0]["chars"]}
        assert len(t["accessories"]) == 1 and a["id"] == "tado:123:1" and a["room"] == "Living Room" and short(a["services"][0]["type"]) == "4A"
        assert (c[CURT]["value"], c[TARGT]["value"], c[HUM]["value"], c[CURMODE]["value"], c[TARMODE]["value"]) == (20.44, 21.0, 48.2, 1, 3)
        assert c[TARMODE]["valid"] == [0, 1, 3] and (c[TARGT]["min"], c[TARGT]["max"], c[TARGT]["step"]) == (5, 25, 0.5)
        path = "/homes/123/zones/1/overlay"
        assert tado.key_of("tado:123:1:th") == "123:1"
        assert tado.overlay("123:1", {TARGT: 22.5}) == ("PUT", path, {"setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": 22.5}}, "termination": {"type": "TADO_MODE"}})
        assert tado.overlay("123:1", {TARMODE: 3, TARGT: 22}) == ("DELETE", path, None)
        assert tado.overlay("123:1", {TARMODE: 0})[2]["setting"] == {"type": "HEATING", "power": "OFF"}
        assert tado.overlay("123:1", {TARMODE: 1})[2]["setting"]["temperature"] == {"celsius": 21.0}
        tado.states["1"]["setting"] = {"type": "HEATING", "power": "OFF"}
        tado.states["1"]["overlay"] = {"type": "MANUAL"}
        assert tado.overlay("123:1", {TARMODE: 1})[2]["setting"]["temperature"] == {"celsius": TADO_DEFAULT_TARGET}
        assert tado.translate()["accessories"][0]["services"][0]["chars"][3]["value"] == 0
        tado.states["1"]["setting"] = {"type": "HEATING", "power": "ON", "temperature": {"celsius": 19.0}}
        assert tado.translate()["accessories"][0]["services"][0]["chars"][3]["value"] == 1
        tado.states["1"]["link"] = {"state": "OFFLINE"}
        assert tado.translate()["accessories"][0]["reachable"] is False
        tado.blocked = time.time() + 100  # after HTTP 429 no request goes out, the last state stays
        try:
            tado.api("GET", "/x")
            raise AssertionError("expected RateLimited")
        except RateLimited:
            pass
        tado.refresh()
        tado.blocked = 0
        # thermostat detail page shows only the modes tado supports
        ui = UI(Stub(), 0.0)
        ui.on_msg({"type": "snapshot", "homes": [merge([("tado", True, tado.translate())])]}, 0.0)
        ui.stack = [("home", 0), ("detail", "tado:123:1:th", 0)]
        assert label(ui.keys(0)[:9]) == ["Back", "Now", "-0.5", "Target", "+0.5", "Off", "Heat", "Auto", "Humidity"], label(ui.keys(0)[:9])
        # three sources: rooms with the same name merge, an offline source marks its devices unreachable
        tado.states = tado_states()
        events = queue.Queue()
        hub = Hub(events, [hue, ikea, tado])
        for s in hub.sources:
            s.online = True
        home = hub.snapshot()["homes"][0]
        assert [r["name"] for r in home["rooms"]] == ["Living Room", "Hall", "Bedroom"], home["rooms"]
        living = {a["name"] for a in home["accessories"] if a["room"] == "room:living room"}
        assert living == {"Floor lamp", "TV plug", "Ceiling", "Living Room heating"}, living
        assert all(a["reachable"] for a in home["accessories"] if a["id"].startswith("ikea:i1"))
        assert hub.connected and hub.offline == "Offline:\nHue IKEA tado"
        ikea.online = False
        hub.update()
        drain(events)
        assert not any(a["reachable"] for a in hub.snapshot()["homes"][0]["accessories"] if a["id"].startswith("ikea:")) and hub.connected
        hue.online = tado.online = False
        hub.update()
        assert drain(events)[-1] == ("conn", False) and not hub.connected
        assert Hub(events, []).offline == "No hub\npaired"
        # a trigger or scene change is a structure change: full snapshot
        old = hub.snapshot()
        new = copy.deepcopy(old)
        new["homes"][0]["triggers"][0]["enabled"] = False
        assert snap_diff(old, new)[0][1]["type"] == "snapshot" and snap_diff(old, old) == []
        # writes through the hub: routing, coalescing (hue + sat in one request), results
        events = queue.Queue()
        calls.clear()
        hub = Hub(events, [hue, ikea])
        hue.online = True
        ikea.online = False
        assert hub.send({"op": "write", "service": "hue:l1", "char": full(HUE), "value": 120, "req": 1})
        assert hub.send({"op": "write", "service": "hue:l1", "char": full(SAT), "value": 50, "req": 2})
        assert hub.send({"op": "write", "service": "hue:l1", "char": full(BRI), "value": 40, "req": 3})
        assert not hub.send({"op": "write", "service": "ikea:i1:light", "char": full(ON), "value": True, "req": 4})  # offline
        assert not hub.send({"op": "write", "service": "other:1", "char": full(ON), "value": True, "req": 5})
        time.sleep(0.5)
        assert len(calls) == 1 and calls[0][0] == "PUT" and calls[0][1] == "/resource/light/l1" and set(calls[0][2]) == {"color", "dimming"}, calls
        assert sorted((m[1]["req"], m[1]["ok"]) for m in drain(events)) == [(1, True), (2, True), (3, True)]
        hub.send({"op": "scene", "service": None, "id": "hue:s1", "req": 6})
        hub.send({"op": "trigger", "id": "hue:b1", "enabled": False, "req": 7})
        time.sleep(0.3)
        assert calls[1:] == [("PUT", "/resource/scene/s1", {"recall": {"action": "active"}}),
                             ("PUT", "/resource/behavior_instance/b1", {"enabled": False})] or calls[1:] == [
            ("PUT", "/resource/behavior_instance/b1", {"enabled": False}), ("PUT", "/resource/scene/s1", {"recall": {"action": "active"}})]
        hub.send({"op": "scene", "id": "hue:ss1", "req": 8})
        time.sleep(0.2)
        assert ("PUT", "/resource/smart_scene/ss1", {"recall": {"action": "activate"}}) in calls
        # coalescer: rapid repeats become one request with the latest value
        seen = []
        co = Coalescer(lambda batch: (seen.append(dict(batch)) or {k: True for k in batch}), 0.1)
        results = []
        for v in (10, 20, 30, 40):
            co.submit("k", {BRI: v}, results.append)
        co.submit("k2", {ON: True}, results.append)
        time.sleep(0.4)
        assert seen == [{"k": {BRI: 40}, "k2": {ON: True}}] and results == [True] * 5, (seen, results)


def test_hardening():
    global cloud_call, pinned_socket, find_hue
    saved = cloud_call, pinned_socket, find_hue
    try:
        # tado: a refresh token that another process saved is used, and its valid access token too (no network)
        class FakeStore:
            def __init__(self, entry):
                self.entry, self.puts = entry, []

            def get(self, name):
                return self.entry

            def put(self, name, value):
                self.puts.append(copy.deepcopy(value))

        def no_network(*a, **k):
            raise AssertionError("network call")

        cloud_call = no_network
        fresh = {"refresh_token": "new", "home_id": 1, "access_token": "A2", "expires_at": time.time() + 600}
        tado = Tado({"home_id": 1, "refresh_token": "old"}, FakeStore(fresh))
        assert tado.token() == "A2" and tado.cfg["refresh_token"] == "new"
        sent = []
        cloud_call = lambda host, method, path, **k: (sent.append(k["form"]["refresh_token"]) or
                                                      (200, {"access_token": "A3", "refresh_token": "newer", "expires_in": 600}, {}))
        store = FakeStore({"refresh_token": "new", "home_id": 1})  # newer token, no valid access token: renew with it
        tado = Tado({"home_id": 1, "refresh_token": "old"}, store)
        assert tado.token() == "A3" and sent == ["new"] and store.puts[-1]["refresh_token"] == "newer"
        # Retry-After: only a finite number from 0 to one day is used
        assert [retry_after(v) for v in ("inf", "nan", "-5", "1e9", "abc", None, "30", "0")] == [
            TADO_RATE_WAIT, TADO_RATE_WAIT, TADO_RATE_WAIT, 86400.0, TADO_RATE_WAIT, TADO_RATE_WAIT, 30.0, 0.0]
        # snapshot: a source whose translate raises keeps its last good part
        tado = Tado({"home_id": 123, "refresh_token": "r"}, FakeStore(None))
        hub = Hub(queue.Queue(), [tado])
        tado.zones, tado.states = TADO_ZONES, tado_states()
        assert len(hub.snapshot()["homes"][0]["accessories"]) == 1
        tado.translate = lambda: 1 / 0
        assert len(hub.snapshot()["homes"][0]["accessories"]) == 1 and tado.last_error.startswith("ZeroDivisionError")
        tado.last_good = None
        assert hub.snapshot()["homes"][0]["accessories"] == []
        # Creds.put: no temp file is left after a failing dump, and the old file stays
        with tempfile.TemporaryDirectory() as tmp:
            store = Creds(os.path.join(tmp, "c.json"))
            store.put("a", {"k": 1})
            try:
                store.put("b", {"k": object()})
                raise AssertionError("expected TypeError")
            except TypeError:
                pass
            assert os.listdir(tmp) == ["c.json"] and store.get("a") == {"k": 1} and store.get("b") is None
        # relocate: a candidate whose pin fails is not saved, the next candidate that matches is
        class Sock:
            def close(self):
                pass

        def fake_pin(host, port, pin, *a, **k):
            if host != "10.0.0.3":
                raise PinError("wrong certificate")
            return Sock()

        pinned_socket = fake_pin
        find_hue = lambda bridge_id=None: [{"host": "10.0.0.2", "bridge_id": "B"}]
        saves = []
        hue = Hue({"host": "10.0.0.1", "bridge_id": "B", "pin": "p", "key": "k"}, FakeStore(None))
        hue.save = lambda: saves.append(hue.cfg["host"])
        hue.relocate()
        assert saves == [] and hue.cfg["host"] == "10.0.0.1"
        find_hue = lambda bridge_id=None: [{"host": "10.0.0.2", "bridge_id": "B"}, {"host": "10.0.0.3", "bridge_id": "B"}]
        hue.relocate()
        assert saves == ["10.0.0.3"]
        assert pinned_host([f"10.1.0.{i}" for i in range(9)] + ["10.0.0.3"], 443, "p") is None  # only 5 are tried
    finally:
        cloud_call, pinned_socket, find_hue = saved
    print("ok")


def test_names():
    lamp = lambda id, name, room, reach=True: hacc(id, name, room, [hsvc(id + ":l", name, "43", [hchar(ON, False)])], reach)
    sensor = hacc("hue:s", "Hall sensor", "Woonkamer", [hsvc("hue:s:t", "Hall sensor temperature", "8A", [hchar(CURT, 20.0, "float", w=False)])])
    scene = lambda id, name, room="": {"id": id, "name": name, "kind": "custom", "room": room}
    parts = [("hue", True, {"rooms": ["Woonkamer"], "accessories": [lamp("hue:1", "Hanglamp", "Woonkamer"), sensor],
                            "scenes": [scene("hue:a", "Relax", "Woonkamer"), scene("hue:b", "Relax", "Garage")], "triggers": []}),
             ("ikea", True, {"rooms": ["Woonkamer", "Garage"], "triggers": [],
                             "accessories": [lamp("ikea:a", "Bureau", "Garage", False), lamp("ikea:b", "Bureau", "Woonkamer", False)],
                             "scenes": [scene("ikea:s1", "dirigera_integration_x"), scene("ikea:s2", "Avond")]})]
    has = lambda home: {a["id"]: a["name"] for a in home["accessories"]}
    # no aliases, no hide: everything stays, rooms merge by name
    home = merge(parts)
    assert [r["name"] for r in home["rooms"]] == ["Woonkamer", "Garage"] and len(home["scenes"]) == 4 and len(home["accessories"]) == 4
    # aliases: by hub name, by prefixed id (two devices share a name), rooms before the merge, scenes; services follow the device
    n = Names(None)
    n.load({"devices": {"Hanglamp": "Kitchen pendant", "ikea:b": "Desk", "Hall sensor": "Hallway sensor"},
            "rooms": {"Woonkamer": "Living room"}, "scenes": {"Avond": "Evening", "hue:b": "Relax 2"}, "hide": ["dirigera_integration_*"]})
    home = merge(parts, n)
    assert has(home) == {"hue:1": "Kitchen pendant", "hue:s": "Hallway sensor", "ikea:a": "Bureau", "ikea:b": "Desk"}, has(home)
    assert next(a for a in home["accessories"] if a["id"] == "hue:s")["services"][0]["name"] == "Hallway sensor temperature"
    assert [r["name"] for r in home["rooms"]] == ["Living room", "Garage"], home["rooms"]
    assert [(s["name"], s["room"]) for s in home["scenes"]] == [("Relax", "Living room"), ("Relax 2", "Garage"), ("Evening", "")]
    # hide: globs on the hub name or the alias, for devices, scenes and rooms (a hidden room hides its devices)
    n.load({"hide": ["Bu*", "dirigera_*", "kitchen*"], "devices": {"Hanglamp": "Kitchen pendant"}})
    home = merge(parts, n)
    assert has(home) == {"hue:s": "Hall sensor"} and [s["name"] for s in home["scenes"]] == ["Relax", "Relax", "Avond"], has(home)
    n.load({"hide": ["garage"]})
    home = merge(parts, n)
    assert [r["name"] for r in home["rooms"]] == ["Woonkamer"] and set(has(home)) == {"hue:1", "hue:s", "ikea:b"}
    assert [(s["id"], s["room"]) for s in home["scenes"]] == [("hue:a", "Woonkamer"), ("ikea:s1", ""), ("ikea:s2", "")]  # the Garage scene is dropped
    # lookups ignore case and outer spaces; null and empty aliases are off; an alias for "Other" applies
    n.load({"devices": {" HANGLAMP ": " Pendant ", "ikea:a": None, "ikea:b": "  "}, "rooms": {"woonkamer ": "Salon", "other": "Misc"}})
    assert n.device("hue:1", "hanglamp ") == "Pendant" and n.device("ikea:a", "Bureau") == "Bureau" and n.device("ikea:b", "Bureau") == "Bureau"
    assert n.room(" WOONKAMER") == "Salon" and n.room("Garage ") == "Garage"
    home = merge([("hue", True, {"rooms": [], "accessories": [lamp("hue:9", "Lamp", "")], "scenes": [], "triggers": []})], n)
    assert [r["name"] for r in home["rooms"]] == ["Misc"]
    # the default hide list applies without a file only; a file without "hide" hides nothing
    assert Names("/nonexistent/names.json").hidden("dirigera_integration_emma") and not Names(None).hidden("dirigera_integration_emma")
    # the file: aliases load, an edit shows when the mtime changes, a broken file keeps the last good names
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "names.json")
        with open(path, "w") as f:
            json.dump({"devices": {"Hanglamp": "A"}}, f)
        n = Names(path)
        assert n.device("hue:1", "Hanglamp") == "A" and not n.hidden("dirigera_integration_x") and n.check() is False
        for text, alias, changed in (('{"devices": {"Hanglamp": "B"}}', "B", True), ("{broken", "B", False), ('[1]', "B", False)):
            with open(path, "w") as f:
                f.write(text)
            os.utime(path, ns=(n.stamp + 10**9, n.stamp + 10**9))
            assert n.check() is changed and n.device("hue:1", "Hanglamp") == alias, text
        # the hub picks the edit up on its next update, without a restart
        events = queue.Queue()
        src = Hue({"host": "h", "pin": "p", "key": "k"}, None)
        src.load(hue_sample())
        src.online = True
        hub = Hub(events, [src], n)
        hub.update()
        drain(events)
        with open(path, "w") as f:
            json.dump({"devices": {"Hanglamp": "C", "Floor lamp": "Standing lamp"}}, f)
        os.utime(path, ns=(n.stamp + 10**9, n.stamp + 10**9))
        hub.update()
        assert "Standing lamp" in has(next(m[1] for m in drain(events) if m[0] == "msg" and m[1]["type"] == "snapshot")["homes"][0]).values()
    # pages: equal names get the room as a second small line, on the scenes page and on the status page
    ui = UI(Stub(), 0.0)
    ui.on_msg({"type": "snapshot", "homes": [merge(parts, Names(None))]}, 0.0)
    ui.stack = [("home", 0), ("scenes", 0)]
    assert [(k.label, k.note) for k in ui.keys(0)[1:7]] == [("Relax", "Woonkamer"), ("Relax", "Garage"), ("dirigera_integration_x", ""), ("Avond", ""),
                                                             ("", ""), ("", "")]
    ui.stack = [("home", 0), ("status", 0)]
    assert [(k.label, k.note) for k in ui.keys(0) if k.badge == "!"] == [("Bureau", "Garage"), ("Bureau", "Woonkamer")]
    # the label fits: a long word shrinks to 9 px before it ends with an ellipsis, and it never breaks mid-word
    f, rows = fit("Koffieapparaat", 11)
    assert rows == ["Koffieapparaat"] and f.size < 11 and f.getlength(rows[0]) <= 68
    f, rows = fit("Koffieapparaatmetheleveelletters", 11)
    assert f.size == 9 and len(rows) == 1 and rows[0].endswith("…") and f.getlength(rows[0]) <= 68
    assert fit("Spot keukenrek", 11)[1] == ["Spot", "keukenrek"] and fit("Spot keukenrek", 11)[0].size == 11
    # structure events: renames, room changes, members
    hue = Hue({"host": "h", "pin": "p", "key": "k"}, None)
    hue.load(hue_sample())
    upd = lambda **r: hue.apply_event([{"type": "update", "data": [r]}])
    assert upd(id="l1", type="light", on={"on": False}) is False
    assert upd(id="d1", type="device", metadata={"name": "New name"}) is True
    assert upd(id="r1", type="room", children=[]) is True and upd(id="r1", type="room", services=[]) is True
    assert upd(id="s1", type="scene", metadata={"name": "Other"}) is True and upd(id="z", type="zone", children=[]) is True
    assert upd(id="x1", type="bridge", children=[]) is False
    ikea = Ikea({"host": "i", "pin": "p", "token": "k"}, None)
    ikea.load(IKEA_DEVICES, [])
    ev = lambda **d: ikea.apply_event({"type": "deviceStateChanged", "data": {"id": "i1", **d}})
    assert ev(attributes={"isOn": True}) is False and ev(attributes={"customName": "New"}) is True and ev(room={"name": "Hall"}) is True
    assert ev(attributes={"customName": "New"}) is False and ev(room=dict(ikea.dev["i1"]["room"])) is False  # unchanged: no refetch
    # an event that arrives during a fetch survives the load (replayed on the new cache)
    ikea.fetch = lambda: (ev(attributes={"isOn": True}), ikea.load(IKEA_DEVICES, []))
    ikea.dev["i1"]["attributes"]["isOn"] = False
    ikea.refresh()
    assert ikea.dev["i1"]["attributes"]["isOn"] is True and ikea.backlog is None
    ikea.fetch = lambda: 1 / 0
    try:
        ikea.refresh()
    except ZeroDivisionError:
        pass
    assert ikea.backlog is None and not ikea.fetching.locked()
    assert all(ikea.apply_event({"type": t}) for t in ("roomCreated", "roomUpdated", "roomDeleted", "deviceRemoved", "sceneUpdated"))
    # the full refresh runs for Hue and DIRIGERA, only while online; tado follows its poll interval
    seen = []
    hue.refresh, hue.notify = lambda: seen.append("refresh"), lambda: seen.append("notify")
    hue.full_refresh()
    assert seen == []
    hue.online = True
    hue.full_refresh()
    assert seen == ["refresh", "notify"] and FULL_REFRESH == 600
    assert Hue.periodic and Ikea.periodic and not Tado.periodic


def test():
    test_hubs()
    test_hardening()
    test_names()
    # UUIDs
    assert short("0000004A-0000-1000-8000-0026BB765291") == "4A" and short("0000000E-0000-1000-8000-0026BB765291") == "E"
    assert short("ABCD1234-0000-0000-0000-000000000000") == "ABCD1234-0000-0000-0000-000000000000"
    assert full("4A") == "0000004A-0000-1000-8000-0026BB765291" and short(full("B7")) == "B7"
    # clamping and colors
    ct = {"format": "uint32", "min": 140, "max": 500, "step": 1}
    assert clamp(ct, 154) == 154 and clamp(ct, 100) == 140 and clamp(ct, 900) == 500 and clamp(ct, 250.4) == 250
    assert clamp({"format": "int", "min": 0, "max": 100, "step": 1}, 112) == 100 and clamp({"format": "int", "min": 0, "max": 100}, -5) == 0
    assert clamp({"format": "float", "min": 10, "max": 30, "step": 0.5}, 21.74) == 21.5 and clamp({"format": "bool"}, 1) is True
    assert light_color(False, 120, 100, 100) == OFF and light_color(True, 120, 100, 100) == (0, 255, 0)
    assert light_color(True, 0, 100, 0) == (51, 0, 0) and light_color(True, None, None, 100, 370)[0] > light_color(True, None, None, 100, 154)[0]
    assert light_color(True, None, None, 50) == (round(255 * .6), round(200 * .6), round(120 * .6))
    # home page
    ui = test_ui()
    k = ui.keys(0)
    assert len(k) == 15 and (k[0].label, k[0].sub) == ("Home", "4 on") and k[0].tap is None  # floor, ceiling, counter, desk
    assert label(k[1:5]) == ["Scenes", "Status", "Automations", "All lights off"]
    assert k[2].badge == "2" and k[2].fill == RED  # unreachable lamp + low battery contact
    assert label(k[5:10]) == ["Living Room", "Kitchen", "Bedroom", "Office", "Hallway"] and k[10] == EMPTY
    assert k[5].sub == "5/5", k[5].sub  # floor, ceiling, TV, blinds, thermostat; the motion sensor is not counted
    assert k[5].fill == WARM and k[7].fill == DARK  # bedroom: no light on
    assert k[4].tap == ("power", tuple(d.sid for d in ui.devs() if d.kind == "light"), False)
    assert UI(Stub(), 0).keys(0)[1:] == [EMPTY] * 14 and ui.keys(0)[0].glyph == "home"
    off = UI(Stub(), 0)
    off.connected = False
    assert off.keys(0)[0].label == "Offline"
    # paging of rooms: 12 rooms -> 9 per page and a page key
    big = fake_snapshot()
    big["homes"][0]["rooms"] = [{"id": f"r{i}", "name": f"Room {i}"} for i in range(12)]
    uib = test_ui(big)
    kb = uib.keys(0)
    assert kb[14].label == "Page\n1/2" and label(kb[5:14])[-1] == "Room 8"
    uib.act(kb[14].tap, "", 0)
    assert label(uib.keys(0)[5:8]) == ["Room 9", "Room 10", "Room 11"] and uib.keys(0)[14].label == "Page\n2/2"
    # room page
    ui.act(k[5].tap, "", 0)
    r = ui.keys(0)
    assert r[0].label == "Back" and r[1].label == "All off" and r[1].tap[2] is False
    assert label(r[2:8]) == ["Ceiling", "Floor lamp", "Living blinds", "Living motion", "Thermostat", "TV plug"], label(r)
    assert r[14] == EMPTY  # no page key when everything fits
    ceiling = r[2]
    assert ceiling.tap[0] == "toggle" and ceiling.long == ("goto", ("detail", "ceiling", 0)) and ceiling.fill == light_color(True, None, None, 80)
    assert r[3].fill == light_color(True, 30, 80, 60, 300) and r[3].bar == 0.6
    assert r[5].tap is None and r[5].long is None and r[5].sub == "Clear"  # sensor does nothing
    assert r[4].tap == r[4].long and r[6].tap[0] == "goto"  # blinds and thermostat open the detail page
    ui.act(("back",), "", 0)
    ui.act(k[9].tap, "", 0)  # hallway: lock, garage, security need a hold
    h = {x.label.replace("\n", " "): x for x in ui.keys(0)}
    for name in ("Front door", "Garage", "Alarm"):
        assert h[name].hint == "hold" and h[name].tap is None and h[name].hold[0] == "w" and h[name].long[0] == "goto", name
    assert h["Front door"].fill == GREEN and h["Front door"].hold == ("w", (("lock", LOCKT, 0),))
    ui.act(("back",), "", 0)
    # more than 12 devices in a room: page key
    many = fake_snapshot()
    many["homes"][0]["accessories"] = [a for a in many["homes"][0]["accessories"]] + [
        {**copy.deepcopy(many["homes"][0]["accessories"][0]), "id": f"a{i}", "name": f"Extra {i:02d}",
         "services": [{**copy.deepcopy(many["homes"][0]["accessories"][0]["services"][0]), "id": f"x{i}"}]} for i in range(12)]
    uim = test_ui(many)
    uim.act(("goto", ("room", "living", 0)), "", 0)
    assert uim.keys(0)[14].label == "Page\n1/2" and uim.keys(0)[13].label != ""
    # unreachable device and low battery
    ui2 = test_ui()
    ui2.act(("goto", ("room", "bedroom", 0)), "", 0)
    right = next(x for x in ui2.keys(0) if x.label == "Bedside right")
    assert right.fill == GREY and right.badge == "!" and right.tap is None and right.long is None
    ui2.stack = [("home", 0), ("room", "office", 0)]
    assert next(x for x in ui2.keys(0) if x.label == "Window contact").low and next(x for x in ui2.keys(0) if x.label == "Window contact").fill == RED
    # status page
    ui2.stack = [("home", 0), ("status", 0)]
    s = ui2.keys(0)
    assert s[0].label == "Back" and all(x.tap is None for x in s[1:] if x.badge == "" and not x.low)
    assert [x.label for x in s if x.badge == "!"] == ["Bedside right"] and [x.label for x in s if x.glyph == "battery"] == ["Window contact"]
    assert any(x.label == "Office temp" and x.sub == "20.5°" for x in s) and any(x.label == "Daylight" and x.sub == "120lx" for x in s)
    assert any(x.label == "Window contact" and x.sub == "Open" and x.fill == RED for x in s)
    ui2.act(next(x for x in s if x.badge == "!").tap, "", 0)
    assert ui2.page == ("room", "bedroom", 0) and len(ui2.stack) == 2
    # optimistic update, success and failure
    ui3 = test_ui()
    ui3.last_input = 0
    stub = ui3.link
    ui3.act(("toggle", "ceiling"), "ceiling", 0)
    assert ui3.model.val("ceiling", ON) is False and stub.sent[-1] == {"op": "write", "service": "ceiling", "char": full(ON), "value": False, "req": ui3.req}
    ui3.handle(("msg", {"type": "result", "req": ui3.req, "ok": True}), 0.1)
    assert ui3.model.val("ceiling", ON) is False and not ui3.pending and not ui3.flash
    ui3.act(("toggle", "desk"), "desk", 1)
    assert ui3.model.val("desk", ON) is False
    ui3.handle(("msg", {"type": "result", "req": ui3.req, "ok": False, "error": "x"}), 1.1)
    assert ui3.model.val("desk", ON) is True and ui3.flash["desk"][1] == RED
    ui3.stack = [("home", 0), ("room", "office", 0)]
    assert next(x for x in ui3.keys(1.2) if x.label == "Desk lamp").fill == RED
    assert next(x for x in ui3.keys(2.0) if x.label == "Desk lamp").fill != RED  # flash ends
    stub.ok = False  # bridge offline: revert at once
    ui3.act(("toggle", "desk"), "desk", 5)
    assert ui3.model.val("desk", ON) is True and ui3.flash["desk"][1] == RED
    stub.ok = True
    ui3.act(("w", (("lock", LOCKT, 0),)), "lock", 6)  # mirror moves the current state at once, revert restores both
    assert ui3.model.val("lock", LOCKC) == 0
    ui3.handle(("msg", {"type": "result", "req": ui3.req, "ok": False}), 6.1)
    assert ui3.model.val("lock", LOCKC) == 1 and ui3.model.val("lock", LOCKT) == 1
    ui3.act(("w", (("garage", DOORT, 0),)), "garage", 7)
    assert ui3.model.val("garage", DOORC) == 2
    ui3.act(("w", (("floor", CT, 100),)), "floor", 7)  # mireds clamp to the characteristic range
    assert ui3.model.val("floor", CT) == 140 and stub.sent[-1]["value"] == 140
    ui3.act(("adjust", "ceiling", BRI, 50), "ceiling", 8)  # brightness clamps at 100, switches the light on
    assert ui3.model.val("ceiling", BRI) == 100 and ui3.model.val("ceiling", ON) is True
    ui3.act(("adjust", "ceiling", BRI, -500), "ceiling", 8)
    assert ui3.model.val("ceiling", BRI) == 0
    ui3.handle(("msg", {"type": "char", "service": "ceiling", "char": full(BRI), "value": 42}), 9)  # push event
    ui3.handle(("msg", {"type": "reach", "accessory": "acc-ceiling", "reachable": False}), 9)
    assert ui3.model.val("ceiling", BRI) == 42 and not ui3.model.reachable("acc-ceiling")
    ui3.handle(("conn", False), 10)  # lost connection reverts pending writes
    assert not ui3.pending and ui3.keys(10)[0].label == "Offline"
    # snapshot push keeps the page, drops vanished rooms
    ui3.handle(("conn", True), 11)
    ui3.stack = [("home", 0), ("room", "office", 0)]
    gone = fake_snapshot()
    gone["homes"][0]["rooms"] = [r for r in gone["homes"][0]["rooms"] if r["id"] != "office"]
    ui3.on_msg(gone, 12)
    assert ui3.stack == [("home", 0)]
    # a device vanishes while its detail page is open: back to the room (one level up), no crash
    ui3.stack = [("home", 0), ("room", "living", 0), ("detail", "floor", 0)]
    gone = fake_snapshot()
    gone["homes"][0]["accessories"] = [a for a in gone["homes"][0]["accessories"] if a["id"] != "acc-floor"]
    ui3.on_msg(gone, 13)
    assert ui3.stack == [("home", 0), ("room", "living", 0)] and ui3.keys(13)[0].label == "Back"
    ui3.stack = [("home", 0), ("detail", "floor", 0)]  # a stale page, for example a key press during the snapshot
    ui3.last_input, ui3.cur = 13, None
    ui3.key_event(0, True, 14)  # the press is ignored: the user did not see the new page
    assert ui3.stack == [("home", 0)] and ui3.cur is None and ui3.held is None and not ui3.validate()
    ui3.key_event(0, False, 14)
    assert ui3.keys(14)[0].label != "Back"
    ui3.act(("trigger", "gone", True), "", 14)  # an automation that vanished: nothing happens
    # trigger toggle: optimistic and revert
    ui4 = test_ui()
    ui4.act(("trigger", "trig-1", False), "trigger:trig-1", 0)
    assert ui4.home()["triggers"][0]["enabled"] is False
    ui4.handle(("msg", {"type": "result", "req": ui4.req, "ok": False}), 1)
    assert ui4.home()["triggers"][0]["enabled"] is True
    ui4.act(("scene", "scene-0"), "scene:scene-0", 2)
    ui4.handle(("msg", {"type": "result", "req": ui4.req, "ok": True}), 2.1)
    assert ui4.flash["scene:scene-0"][1] == WHITE
    ui4.act(("scene", "scene-5"), "scene:scene-5", 3)
    ui4.handle(("msg", {"type": "result", "req": ui4.req, "ok": False}), 3.1)
    assert ui4.flash["scene:scene-5"][1] == RED
    ui4.stack = [("home", 0), ("autos", 0)]
    a = ui4.keys(5)
    assert [x.fill for x in a[1:4]] == [GREEN, GREEN, GREY] and a[3].tap == ("trigger", "trig-3", True)
    ui4.stack = [("home", 0), ("scenes", 0)]
    assert label(ui4.keys(5)[1:7]) == ["Good morning", "Movie", "Dinner", "Good night", "Away", "Party"]
    # detail pages: only controls whose characteristics exist
    def detail(sid):
        u = test_ui()
        u.stack = [("home", 0), ("detail", sid, 0)]
        return u, u.keys(0)
    u, d = detail("ceiling")  # white-only dimmable light: no color, no color temperature
    assert label(d[:9]) == ["Back", "On 80%", "-10", "+10", "10 %", "30 %", "60 %", "100 %", ""] and d[3].repeat and d[2].tap == ("adjust", "ceiling", BRI, -10)
    u, d = detail("counter")  # color temperature only
    assert label(d[:12]) == ["Back", "On 100%", "-10", "+10", "10 %", "30 %", "60 %", "100 %", "Warm", "Neutral", "Cool", ""]
    assert d[9].tap[1][1] == ("counter", CT, 250) and d[8].tap[1][1] == ("counter", CT, 370) and d[10].tap[1][1] == ("counter", CT, 154)
    assert d[9].sel and not d[8].sel
    u, d = detail("floor")  # full color light: 18 controls need two pages
    assert d[14].label == "Page\n1/2" and label(d[:6]) == ["Back", "On 60%", "-10", "+10", "10 %", "30 %"]
    u.act(d[14].tap, "", 0)
    d2 = u.keys(0)
    assert label(d[11:14]) == ["Red", "Orange", "Yellow"] and label(d2[:6]) == ["Back", "Green", "Cyan", "Blue", "Purple", "Pink"], label(d2)
    assert d2[14].label == "Page\n2/2" and d[11].tap[1] == (("floor", ON, True), ("floor", HUE, 0), ("floor", SAT, 100))
    u, d = detail("thermo")
    assert label(d[:9]) == ["Back", "Now", "-0.5", "Target", "+0.5", "Off", "Heat", "Cool", "Auto"] and d[1].sub == "21.5°" and d[3].sub == "22.0°"
    assert label(d[9:10]) == ["Humidity"] and d[6].sel and not d[5].sel and d[4].repeat
    u.act(d[4].tap, "", 0)
    assert u.model.val("thermo", TARGT) == 22.5
    u.act(("adjust", "thermo", TARGT, 100), "", 0)
    assert u.model.val("thermo", TARGT) == 30
    u, d = detail("blinds")
    assert label(d[:9]) == ["Back", "Position", "Open", "Close", "25 %", "50 %", "75 %", "-10", "+10"] and d[1].sub == "50%"
    u, d = detail("fan")
    assert label(d[:6]) == ["Back", "On", "25 %", "50 %", "75 %", "100 %"]
    u, d = detail("air")
    assert d[1].label in ("On", "Off") and d[1].tap == ("toggle", "air")
    u.act(d[1].tap, "", 0)
    assert u.model.val("air", ACTIVE) == 0
    u, d = detail("lock")
    assert label(d[:4]) == ["Back", "State", "Lock", "Unlock"] and d[1].sub == "locked" and d[2].tap and d[3].hold and d[3].hint == "hold" and d[3].tap is None
    u, d = detail("garage")
    assert d[1].sub == "closed" and d[2].hold == ("w", (("garage", DOORT, 0),)) and d[3].hold == ("w", (("garage", DOORT, 1),))
    u, d = detail("alarm")
    assert label(d[:6]) == ["Back", "State", "Stay", "Away", "Night", "Disarm"] and d[5].hold == ("w", (("alarm", SECT, 3),)) and d[3].tap == ("w", (("alarm", SECT, 1),))
    u, d = detail("coffee")
    assert label(d[:2]) == ["Back", "Off"] and d[1].tap == ("toggle", "coffee")
    # presses: tap, long, hold, repeat, auto-repeat, wake
    p = test_ui()
    p.last_input = 100
    p.stack = [("home", 0), ("room", "living", 0)]
    p.key_event(3, True, 100.0)  # floor lamp: tap on release
    assert [m["op"] for m in p.link.sent] == ["snapshot"]  # only the start-up request
    p.key_event(3, False, 100.2)
    assert p.link.sent[-1]["op"] == "write" and p.model.val("floor", ON) is False
    p.key_event(3, True, 101.0)  # long press: detail page at 0.5 s, release does nothing more
    p.tick(101.5)
    assert p.page == ("detail", "floor", 0)
    n = len(p.link.sent)
    p.key_event(3, False, 101.8)
    assert len(p.link.sent) == n and p.page == ("detail", "floor", 0)
    p.key_event(3, True, 102.0)  # +10 key: acts at once, repeats after 0.5 s every 0.2 s
    assert p.link.sent[-1]["op"] == "write"
    n = len(p.link.sent)
    p.tick(102.3)
    assert len(p.link.sent) == n
    p.tick(102.9)
    assert len(p.link.sent) - n == 3 and p.next_deadline(102.9) is not None
    p.key_event(3, False, 103.0)
    n = len(p.link.sent)
    p.tick(104)
    assert len(p.link.sent) == n
    p.stack = [("home", 0), ("room", "hall", 0)]
    lock = next(i for i, x in enumerate(p.keys(105)) if x.label == "Front door")
    p.key_event(lock, True, 105.0)  # lock: a short press does nothing
    p.key_event(lock, False, 105.2)
    assert p.model.val("lock", LOCKC) == 1
    p.key_event(lock, True, 106.0)  # 1 s hold acts while held
    p.tick(106.5)
    assert p.model.val("lock", LOCKC) == 1
    p.tick(107.1)
    assert p.model.val("lock", LOCKC) == 0 and p.link.sent[-1]["value"] == 0
    p.key_event(lock, False, 107.5)
    assert p.link.sent[-1]["op"] == "write" and p.page == ("room", "hall", 0)
    p.key_event(lock, True, 108.0)  # 0.5-1 s on a hold key: detail page on release
    p.key_event(lock, False, 108.7)
    assert p.page == ("detail", "lock", 0)
    p.stack = [("home", 0)]
    p.last_input = 0  # idle: dimmed, the first press only wakes
    assert p.brightness(IDLE_MINUTES * 60) == DIM and p.brightness(IDLE_MINUTES * 60 - 1) == BRIGHTNESS
    n = len(p.link.sent)
    p.key_event(4, True, 400.0)
    p.key_event(4, False, 400.1)
    assert len(p.link.sent) == n and p.brightness(400.2) == BRIGHTNESS
    p.key_event(4, True, 401.0)
    assert len(p.link.sent) > n  # all lights off now works
    assert p.model.val("floor", ON) is False and p.model.val("ceiling", ON) is False
    # a new alert wakes the dimmed deck, an unchanged snapshot does not
    leak = fake_snapshot()
    next(c for a in leak["homes"][0]["accessories"] if a["id"] == "acc-leak" for c in a["services"][0]["chars"])["value"] = 1
    p.last_input = 0
    p.on_msg(leak, 500)
    assert p.alerts() == 3 and p.brightness(500) == BRIGHTNESS
    p.last_input = 0
    p.on_msg(leak, 501)
    assert p.brightness(501) == DIM
    # every page renders: home, each room, every detail page, scenes, automations, status
    shots = [ui.keys(0)]
    base = test_ui()
    pages = [("home", 0), ("scenes", 0), ("autos", 0), ("status", 0)] + [("room", r["id"], 0) for r in base.home()["rooms"]]
    for dev in base.devs():
        pages.append(("detail", dev.sid, 0)) if dev.kind not in SENSORS else None
    for pg in pages:
        base.stack = [("home", 0), pg]
        first = base.keys(0)
        shots.append(first)
        if first[14].tap and first[14].tap[0] == "flip":
            base.act(first[14].tap, "", 0)
            shots.append(base.keys(0))
    base.stack = [("home", 0)]
    shots.append(base.keys(0))
    for keys in shots:
        assert len(keys) == 15
        for theme in THEMES:  # every page in every theme
            img = sheet(keys, theme=theme)
            assert img.size == (5 * 72 + 32, 3 * 72 + 16)
    # the fake bridge answers: snapshot, write with failure, scene failure
    events = queue.Queue()
    fake = FakeBridge(events)
    fake.send({"op": "write", "service": "desk", "char": full(ON), "value": False, "req": 1})
    fake.send({"op": "write", "service": "ceiling", "char": full(ON), "value": False, "req": 2})
    fake.send({"op": "scene", "id": "scene-5", "req": 3})
    time.sleep(0.4)
    got = [events.get_nowait()[1] for _ in range(events.qsize())]
    results = {m["req"]: m["ok"] for m in got if m["type"] == "result"}
    assert results == {1: False, 2: True, 3: False} and any(m["type"] == "char" and m["service"] == "ceiling" for m in got)
    print("ok")


def private_dir():
    """New folder for renders, mode 0700 (the home layout is private). Its path is printed."""
    out = tempfile.mkdtemp(prefix="homedeck-")
    print(f"Writing to {out}")
    return out


def shots(out=None):
    out = out or private_dir()
    ui = test_ui()
    for name, page in (("home", ("home", 0)), ("room", ("room", "living", 0)), ("light", ("detail", "floor", 0)),
                       ("lights2", ("detail", "floor", 1)), ("thermostat", ("detail", "thermo", 0)),
                       ("status", ("status", 0)), ("hall", ("room", "hall", 0))):
        ui.stack = [("home", 0), page] if name != "home" else [page]
        keys = ui.keys(0)
        sheet(keys).save(f"{out}/homedeck-{name}.png")  # the selected theme
        print(f"{out}/homedeck-{name}.png")
        if name in ("home", "room"):
            for theme in THEMES:
                sheet(keys, theme=theme).save(f"{out}/homedeck-{theme}-{name}.png")
                print(f"{out}/homedeck-{theme}-{name}.png")


def shots_live(out=None):
    """Render the real home (home, every room, scenes, status) to PNGs without opening the deck.
    tado is left out: its refresh token rotates, so a second client would break a running `serve`."""
    out = out or private_dir()
    events = queue.Queue()
    hub = paired_hub(events, skip=("tado",))
    if not hub.sources:
        sys.exit("No Hue or DIRIGERA hub is paired.")
    hub.start()
    deadline = time.time() + 20
    while time.time() < deadline and not all(s.online for s in hub.sources):
        time.sleep(0.2)
    if not any(s.online for s in hub.sources):
        sys.exit("No hub answered within 20 s.")
    ui = UI(hub)
    ui.on_msg(hub.snapshot(), 0.0)
    pages = [("home", ("home", 0)), ("scenes", ("scenes", 0)), ("status", ("status", 0))]
    pages[1:1] = [("room-" + re.sub(r"\W+", "-", r["name"].lower()).strip("-"), ("room", r["id"], 0)) for r in ui.home()["rooms"]]
    for name, page in pages:
        n = count = 0
        while n <= count:  # a page with more keys than fit pages on key 14
            ui.stack = [("home", 0), page[:-1] + (n,)] if page[0] != "home" else [page]
            keys = ui.keys(0)
            count = keys[14].tap[1] - 1 if keys[14].tap and keys[14].tap[0] == "flip" else 0
            path = f"{out}/homedeck-live-{name}{'' if n == 0 else f'-{n + 1}'}.png"
            sheet(keys).save(path)
            print(path)
            n += 1


# --- pairing and status ---

def hostname():
    return socket.gethostname().split(".")[0] or "mac"


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def wait_reachable(host, port):
    """Retry the TCP connect until PAIR_SECONDS pass. macOS blocks a new program from the local
    network ("No route to host") until the user allows it in the Local Network dialog."""
    deadline, told = time.time() + PAIR_SECONDS, False
    while True:
        try:
            socket.create_connection((host, port), 5).close()
            return
        except OSError as e:
            if time.time() >= deadline:
                sys.exit(f"Cannot reach {host}:{port} ({e}). Allow this program in System Settings > "
                         "Privacy & Security > Local Network, then run the command again.")
            if not told:
                print(f"Cannot reach {host}:{port} yet ({e}). If macOS asks for Local Network access, allow it.")
                told = True
            time.sleep(PAIR_STEP)


def pair_hue(host=None):
    found = None
    if host is None:
        print("Looking for a Hue Bridge on the network...")
        found = next(iter(find_hue()), None)
        if not found:
            sys.exit("No Hue Bridge found. Run the command again with --host <ip>.")
        host = found["host"]
        print(f"Found the Hue Bridge at {host}.")
    wait_reachable(host, 443)
    print(f"Press the round link button on the Hue Bridge now. Waiting up to {PAIR_SECONDS} seconds...")
    pin, key, deadline = None, None, time.time() + PAIR_SECONDS
    while key is None:
        conn = https(host, 443, pin, first_use=pin is None)  # later attempts must see the same certificate
        pin = conn.pin
        try:
            st, obj, _ = request(conn, "POST", "/api", body={
                "devicetype": f"stream-deck-home#{hostname()[:19]}", "generateclientkey": True})
        finally:
            conn.close()
        item = obj[0] if isinstance(obj, list) and obj and isinstance(obj[0], dict) else {}
        if "success" in item:
            key = item["success"]["username"]
        elif dig(item, "error", "type") != 101:  # 101 = link button not pressed yet
            sys.exit(f"The Hue Bridge refused the pairing: {dig(item, 'error', 'description', default=f'HTTP {st}')}")
        elif time.time() >= deadline:
            sys.exit("The link button was not pressed in time. Run the command again.")
        else:
            time.sleep(PAIR_STEP)
    _, cfg, _ = hub_call(host, 443, pin, "GET", "/api/config")
    bridge_id = str(dig(cfg, "bridgeid", default="")).upper()
    if found and found["bridge_id"] and bridge_id and found["bridge_id"] != bridge_id:
        sys.exit("The bridge id differs from the one announced on the network. Nothing was saved.")
    Creds().put("hue", {"host": host, "bridge_id": bridge_id, "pin": pin, "key": key})
    print(f"Paired with the Hue Bridge {bridge_id or host}. Its certificate is pinned.")


def pair_ikea(host=None):
    if host is None:
        print("Looking for a DIRIGERA hub on the network...")
        host = next(iter(find_ikea()), None)
        if not host:
            sys.exit("No DIRIGERA hub found. Run the command again with --host <ip>.")
        print(f"Found the DIRIGERA hub at {host}.")
    wait_reachable(host, 8443)
    verifier = secrets.token_urlsafe(96)  # 128 characters
    query = urllib.parse.urlencode({"audience": "homesmart.local", "response_type": "code",
                                    "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256"})
    conn = https(host, 8443, None, first_use=True)
    pin = conn.pin
    try:
        _, obj, _ = request(conn, "GET", f"/v1/oauth/authorize?{query}")
    finally:
        conn.close()
    code = dig(obj, "code")
    if not code:
        sys.exit("The DIRIGERA hub gave no pairing code.")
    print(f"Press the action button on the DIRIGERA hub now. Waiting up to {PAIR_SECONDS} seconds...")
    deadline = time.time() + PAIR_SECONDS
    while True:
        st, obj, _ = hub_call(host, 8443, pin, "POST", "/v1/oauth/token", form={
            "code": code, "name": hostname(), "grant_type": "authorization_code", "code_verifier": verifier})
        if dig(obj, "access_token"):
            break
        if os.environ.get("PAIR_DEBUG"):
            print(f"  hub answered HTTP {st}: {obj}")
        if time.time() >= deadline:
            sys.exit(f"The action button was not pressed in time (last answer: HTTP {st} {obj}). "
                     "Run the command again.")
        time.sleep(PAIR_STEP)
    Creds().put("ikea", {"host": host, "pin": pin, "token": obj["access_token"]})
    print(f"Paired with the DIRIGERA hub at {host}. Its certificate is pinned.")


def pair_tado(host=None):
    st, obj, _ = cloud_call("login.tado.com", "POST", "/oauth2/device_authorize",
                            form={"client_id": TADO_CLIENT, "scope": "offline_access"})
    if st != 200 or not dig(obj, "device_code"):
        sys.exit(f"tado refused the login (HTTP {st}).")
    url = obj.get("verification_uri_complete") or obj["verification_uri"]
    print(f"Open this address, log in to tado and confirm the code {obj.get('user_code', '')}:\n  {url}")
    if url.startswith("https://login.tado.com/"):
        subprocess.run(["open", url], capture_output=True)
    wait, deadline = obj.get("interval", 5), time.time() + obj.get("expires_in", 300)
    while True:
        time.sleep(wait)
        st, tok, _ = cloud_call("login.tado.com", "POST", "/oauth2/token", form={
            "client_id": TADO_CLIENT, "device_code": obj["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if st == 200 and dig(tok, "refresh_token"):
            break
        error = dig(tok, "error")
        if error == "slow_down":
            wait += 5
        elif error != "authorization_pending" or time.time() >= deadline:
            sys.exit("The tado login was not confirmed in time. Run the command again.")
    _, me, _ = cloud_call("my.tado.com", "GET", "/api/v2/me", headers={"Authorization": "Bearer " + tok["access_token"]})
    homes = dig(me, "homes", default=[])
    if not homes:
        sys.exit("This tado account has no home.")
    if len(homes) > 1:
        print(f"The account has {len(homes)} homes. Using the first: {homes[0].get('name', homes[0]['id'])}.")
    Creds().put("tado", {"refresh_token": tok["refresh_token"], "home_id": homes[0]["id"]})
    print("Paired with tado.")


PAIR = {"hue": pair_hue, "ikea": pair_ikea, "tado": pair_tado}


def status():
    store = Creds()
    for name, cls in SOURCES.items():
        cfg = store.get(name)
        if not cfg:
            print(f"{name:5} not paired")
            continue
        if name == "tado":  # no network call: a refresh would rotate the token under a running `serve`
            print(f"{name:5} paired (not checked: the refresh token rotates)")
            continue
        try:
            src = cls(cfg, store)
            src.refresh()
            t = src.translate()
            rooms = {a["room"] for a in t["accessories"] if a["room"]}
            print(f"{name:5} paired, reachable: {len(rooms)} rooms, {len(t['accessories'])} devices, {len(t['scenes'])} scenes")
        except Exception as error:
            print(f"{name:5} paired, not reachable ({describe(error)})")


def names_cmd():
    """List every room, device and scene: kind, source, id, hub name, and "-> alias" or "(hidden)" from names.json.
    Copy the hub name or the id into names.json. tado is left out, like in shots --live (rotating token)."""
    store, names = Creds(), Names()
    rows = []
    for name, cls in SOURCES.items():
        cfg = None if name == "tado" else store.get(name)
        if not cfg:
            continue
        try:
            src = cls(cfg, store)
            src.refresh()
            t = src.translate()
        except Exception as error:
            print(f"{name:5} not reachable ({describe(error)})")
            continue
        shown = lambda kind, id, hub, now, hide=False: rows.append((kind, name, id, hub, now, hide or names.hidden(hub, now)))
        room_hidden = lambda r: names.hidden(r, names.room(r or "Other"))
        for r in dict.fromkeys(t["rooms"] + [a["room"] for a in t["accessories"] if a["room"]]):
            shown("room", "-", r, names.room(r))
        for a in sorted(t["accessories"], key=lambda a: (a["name"].lower(), a["id"])):
            shown("device", a["id"], a["name"], names.device(a["id"], a["name"]), room_hidden(a["room"]))
        for s in t["scenes"]:
            shown("scene", s["id"], s["name"], names.scene(s["id"], s["name"]), bool(s.get("room")) and room_hidden(s["room"]))
    print(f"names.json: {names.path} ({'found' if names.stamp else 'not found'})")
    for kind, source, id, hub, now, hide in sorted(rows, key=lambda r: ("room device scene".split().index(r[0]), r[1])):
        print(f"{kind:6} {source:5} {id:40} {hub}" + (f" -> {now}" if now != hub else "") + (" (hidden)" if hide else ""))


def unpair(name):
    store = Creds()
    print(f"Forgot {name}." if store.get(name) else f"{name} was not paired.")
    store.put(name, None)
    print("The access key stays on the hub until you remove it there (see the README).")


def main(argv=None):
    p = argparse.ArgumentParser(prog="stream-deck-home", description="Stream Deck control deck for Hue, DIRIGERA and tado.")
    p.add_argument("--theme", choices=THEMES, help="key look (default graphite, or STREAM_DECK_HOME_THEME)")
    sub = p.add_subparsers(dest="cmd")
    for name, text in (("serve", "run the deck (default)"), ("demo", "run the deck on a fake home"),
                       ("test", "self-check without hardware"), ("status", "paired hubs and counts"),
                       ("names", "list rooms, devices and scenes with source, id and current name (not tado)")):
        sub.add_parser(name, help=text)
    sh = sub.add_parser("shots", help="write PNG renders to a folder (default: a new private folder in the temp directory)")
    sh.add_argument("folder", nargs="?")
    sh.add_argument("--live", action="store_true", help="render the real home from the paired hubs (not tado)")
    pr = sub.add_parser("pair", help="pair a hub")
    pr.add_argument("source", choices=SOURCES)
    pr.add_argument("--host", help="IP address of the hub, when discovery fails")
    up = sub.add_parser("unpair", help="forget the credentials of a hub")
    up.add_argument("source", choices=SOURCES)
    args = p.parse_args(argv)
    global THEME
    THEME = args.theme or os.environ.get("STREAM_DECK_HOME_THEME") or THEME
    if THEME not in THEMES:
        sys.exit(f"Unknown theme {THEME!r}. Use one of: {', '.join(THEMES)}.")
    {"serve": serve, "demo": lambda: serve(True), "test": test, "status": status, "names": names_cmd, None: serve,
     "shots": lambda: (shots_live if args.live else shots)(args.folder), "pair": lambda: PAIR[args.source](args.host),
     "unpair": lambda: unpair(args.source)}[args.cmd]()


if __name__ == "__main__":
    main()
