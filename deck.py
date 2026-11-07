#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["streamdeck>=0.9.6", "pillow>=10.1"]
# ///
"""Stream Deck control deck for HomeKit.

macOS gives HomeKit only to a signed Mac Catalyst app, so the app HomeDeckBridge (folder bridge/)
talks to HomeKit and this script talks to the bridge over a local socket: newline-delimited JSON on
127.0.0.1, token from ~/Library/Application Support/HomeDeck/bridge.json.

Home page: home, scenes, status, automations, all lights off, rooms. Room page: devices. A short
press does the main action, a long press (0.5 s) opens the detail page. Locks, garage doors and the
security system need a 1 s hold. Writes show at once and revert (key flashes red) when they fail.

  uv run deck.py serve   # real bridge, owns the Stream Deck (Elgato app must be closed)
  uv run deck.py demo    # same deck against a fake home, no bridge needed
  uv run deck.py test    # self-check, no hardware needed
  uv run deck.py shots   # write PNG renders of some pages to /tmp/homedeck-*.png
"""
import colorsys
import copy
import functools
import json
import os
import queue
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections import namedtuple

BRIDGE_FILE = os.path.expanduser("~/Library/Application Support/HomeDeck/bridge.json")
BRIDGE_APP = os.path.expanduser("~/Applications/HomeDeckBridge.app")
BRIGHTNESS = 60
DIM = 10  # brightness while idle
IDLE_MINUTES = 5  # no key press for this long: dim the deck. The first press only wakes it.
LONG = 0.5  # seconds: a long press opens the detail page
HOLD = 1.0  # seconds: locks, garage doors and the security system act after this hold
REPEAT_DELAY, REPEAT = 0.5, 0.2  # auto-repeat of the +/- keys while held
FLASH = 0.6  # seconds a key flashes after a scene or a failed write
RETRY = 2.0  # seconds between bridge connection attempts

# HAP short UUIDs of the services and characteristics (Apple's base UUID is added by HomeKit).
KINDS = {"43": "light", "47": "outlet", "49": "switch", "40": "fan", "B7": "fan", "4A": "thermostat",
         "BC": "heater", "45": "lock", "41": "garage", "8C": "cover", "81": "cover", "8B": "cover",
         "7E": "security", "BB": "purifier", "D0": "valve", "8A": "temp", "82": "humidity",
         "80": "contact", "85": "motion", "86": "occupancy", "83": "leak", "87": "smoke", "7F": "co",
         "84": "lux"}  # 96 (battery) is no device: it only sets the low battery badge
ON, BRI, HUE, SAT, CT, ACTIVE, SPEED = "25", "8", "13", "2F", "CE", "B0", "29"
CURT, TARGT, CURMODE, TARMODE, HUM = "11", "35", "F", "33", "10"
LOCKC, LOCKT, DOORC, DOORT, OBST, POSC, POST, SECC, SECT = "1D", "1E", "E", "32", "24", "6D", "7C", "66", "67"
HMODE, HEATTH, COOLTH = "B2", "12", "D"
CONTACT, MOTION, OCC, LEAK, SMOKE, CO, LUX, LOWBAT, BATLVL, CHARGING = "6A", "22", "71", "70", "76", "69", "6B", "79", "68", "8F"
BASE_UUID = "-0000-1000-8000-0026BB765291"
# Optimistic mirror: writing the target char also moves the current-state char at once.
MIRROR = {LOCKT: (LOCKC, lambda v: v), POST: (POSC, lambda v: v), SECT: (SECC, lambda v: v),
          DOORT: (DOORC, lambda v: 2 if v == 0 else 3)}  # garage: opening / closing
INT_FORMATS = {"int", "uint8", "uint16", "uint32", "uint64"}
TOGGLE = {"light", "outlet", "switch", "fan", "purifier", "valve"}  # short press toggles
HOLD_KINDS = {"lock", "garage", "security"}  # short press does nothing, 1 s hold acts
OPENS_DETAIL = {"thermostat", "heater", "cover"}  # short press opens the detail page too
SENSORS = {"temp", "humidity", "contact", "motion", "occupancy", "leak", "smoke", "co", "lux"}
HAZARDS = {"leak": LEAK, "smoke": SMOKE, "co": CO}
LOW_BATTERY = 20  # percent

BLACK, DARK, OFF = (0, 0, 0), (38, 38, 38), (45, 45, 45)
GREY, BLUE, WARM, GREEN, RED, ORANGE = (75, 75, 75), (0, 95, 170), (190, 110, 20), (20, 130, 55), (200, 30, 30), (210, 120, 0)
WHITE, PURPLE = (255, 255, 255), (110, 55, 160)
HUES = [("Red", 0), ("Orange", 30), ("Yellow", 55), ("Green", 120), ("Cyan", 180), ("Blue", 240),
        ("Purple", 280), ("Pink", 330)]
GLYPH = {"security": "shield", "leak": "drop", "humidity": "drop", "smoke": "cloud", "occupancy": "person",
         "lux": "sun", "heater": "flame", "thermostat": "thermo", "cover": "blinds", "light": "bulb"}  # kind -> glyph
MODES = [("Off", None), ("Heat", 1), ("Cool", 2), ("Auto", 0)]  # name, HeaterCooler target state

Key = namedtuple("Key", "label glyph fill bar badge low sub hint ident tap hold long repeat sel",
                 defaults=("", None, DARK, None, "", False, "", "", "", None, None, None, False, False))
EMPTY = Key(fill=BLACK)
Dev = namedtuple("Dev", "sid name acc room kind")


def short(uuid):
    """HAP short UUID ('4A') of a full UUID; other UUIDs stay as they are."""
    head, _, rest = uuid.upper().partition("-")
    return f"{int(head, 16):X}" if "-" + rest == BASE_UUID else uuid


def full(short_id):
    return f"{int(short_id, 16):08X}{BASE_UUID}"


def parse(line):
    """One protocol line to a dict, or None when it is not a JSON object."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


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
        """Drop pages whose room or device vanished from the snapshot."""
        rooms = {r["id"] for r in (self.home() or {"rooms": []})["rooms"]}
        sids = {d.sid for d in self.devs()}
        self.stack = [p for p in self.stack if (p[0] != "room" or p[1] in rooms) and (p[0] != "detail" or p[1] in sids)]
        self.stack = self.stack or [("home", 0)]

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
            out[0] = Key("Bridge\noffline" if not self.connected else "Loading", "alert", RED if not self.connected else GREY, ident="conn")
            return out
        p = self.page
        out = {"home": p_home, "room": p_room, "detail": p_detail, "scenes": p_scenes, "autos": p_autos,
               "status": p_status}[p[0]](self)
        for i, k in enumerate(out):
            if k.ident in self.flash and self.flash[k.ident][0] > now:
                out[i] = k._replace(fill=self.flash[k.ident][1], sel=False)
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
            trig = next(t for h in m.homes for t in h["triggers"] if t["id"] == a[1])
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
    return Key("Back", "back", tap=("back",))


def p_home(ui):
    m, home = ui.model, ui.home()
    lights = [d for d in ui.devs() if d.kind == "light"]
    on = sum(ui.is_on(d) for d in lights)
    alerts = ui.alerts()
    out = [EMPTY] * 15
    out[0] = Key(f"{home['name']}\n{on} on", "home", WARM if on else DARK, ident="home",
                 tap=("home_next",) if len(m.homes) > 1 else None)
    out[1] = Key("Scenes", "play", tap=("goto", ("scenes", 0)))
    out[2] = Key("Status", "info", RED if alerts else DARK, badge=str(alerts) if alerts else "", tap=("goto", ("status", 0)))
    out[3] = Key("Automations", "clock", tap=("goto", ("autos", 0)))
    out[4] = Key("All\nlights off", "power", tap=("power", tuple(d.sid for d in lights), False))
    rooms = []
    for r in home["rooms"]:
        ds = [d for d in ui.devs() if d.room == r["id"] and d.kind not in SENSORS]
        lit = any(ui.is_on(d) for d in ds if d.kind == "light")
        rooms.append(Key(r["name"], None, WARM if lit else DARK, sub=f"{sum(ui.is_on(d) for d in ds)}/{len(ds)}",
                         tap=("goto", ("room", r["id"], 0))))
    return place(out, rooms, 5, ui.page[1])


def p_room(ui):
    _, rid, n = ui.page
    ds = [d for d in ui.devs() if d.room == rid]
    switchable = [d for d in ds if d.kind in TOGGLE and ui.model.reachable(d.acc)]
    any_on = any(ui.is_on(d) for d in switchable)
    out = [EMPTY] * 15
    out[0] = back_key()
    if switchable:
        out[1] = Key("All off" if any_on else "All on", "power", tap=("power", tuple(d.sid for d in switchable), not any_on))
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
    if not reach:
        k.update(fill=GREY, badge="!", bar=None, tap=None, hold=None, long=None, hint="")
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
    if kind == "contact":
        return dict(glyph="door_open", fill=RED, sub="open") if v(CONTACT) == 1 else dict(glyph="door", sub="shut")
    if kind in ("motion", "occupancy"):
        on = bool(v(MOTION if kind == "motion" else OCC))
        return dict(fill=ORANGE if on else DARK, sub="yes" if on else "no")
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
        for name, mode in MODES:
            if d.kind == "thermostat":
                act, sel = w(TARMODE, {"Off": 0, "Heat": 1, "Cool": 2, "Auto": 3}[name]), v(TARMODE) == {"Off": 0, "Heat": 1, "Cool": 2, "Auto": 3}[name]
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


def p_scenes(ui):
    out = [EMPTY] * 15
    out[0] = back_key()
    return place(out, [Key(s["name"], "play", BLUE, ident=f"scene:{s['id']}", tap=("scene", s["id"]))
                       for s in ui.home()["scenes"]], 1, ui.page[1])


def p_autos(ui):
    out = [EMPTY] * 15
    out[0] = back_key()
    return place(out, [Key(t["name"], "clock", GREEN if t["enabled"] else GREY, ident=f"trigger:{t['id']}",
                           tap=("trigger", t["id"], not t["enabled"])) for t in ui.home()["triggers"]], 1, ui.page[1])


def p_status(ui):
    m = ui.model
    out = [EMPTY] * 15
    out[0] = back_key()
    items = [dev_key(ui, d)._replace(tap=None, hold=None, long=None, hint="") for d in ui.devs() if d.kind in SENSORS]
    accs = {d.acc for d in ui.devs()}
    names = lambda ids: sorted(ids, key=lambda a: (m.accs[a]["name"].lower(), a))
    items += [Key(m.accs[a]["name"], "alert", GREY, badge="!", ident=f"acc:{a}", tap=("room_of", a)) for a in names(a for a in accs if not m.reachable(a))]
    items += [Key(m.accs[a]["name"], "battery", DARK, low=True, ident=f"acc:{a}", tap=("room_of", a)) for a in names(a for a in accs if m.low(a))]
    return place(out, items, 1, ui.page[1])


# --- drawing ---

@functools.lru_cache(maxsize=None)
def font(size):
    from PIL import ImageFont
    return ImageFont.load_default(size=size)


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
        d.text((cx, cy), "CO", font=font(max(8, round(size * 1.2))), fill=fg, anchor="mm")
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
        d.text((cx, cy), "i", font=font(max(8, round(size * 1.3))), fill=fg, anchor="mm")
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
    elif name == "auto":
        d.text((cx, cy), "A", font=font(max(8, round(size * 1.6))), fill=fg, anchor="mm")


def face(key, size=72):
    """Draw one key image (PIL, RGB) from a Key."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (size, size), key.fill)
    d = ImageDraw.Draw(img)
    fg = (0, 0, 0) if sum(key.fill) / 3 > 150 else WHITE
    lines = [w for part in key.label.split("\n") for w in (textwrap.wrap(part, 11) or [""])]
    bottom = size - 7 if key.bar is not None else size - 3
    if key.glyph is None and not key.sub:  # text only: centered
        f = font(15 if len(lines) <= 2 else 12)
        d.multiline_text((size / 2, size / 2), "\n".join(lines[:4]), font=f, fill=fg, anchor="mm", align="center")
    else:
        lines = lines[:2]
        d.multiline_text((size / 2, bottom), "\n".join(lines), font=font(11), fill=fg, anchor="md", align="center")
        if key.sub and key.glyph:
            glyph(d, key.glyph, 12, 13, 7, fg)
            d.text((size / 2 + 4, 27), key.sub, font=font(19 if len(key.sub) <= 4 else 13), fill=fg, anchor="mm")
        elif key.sub:
            d.text((size / 2, 25), key.sub, font=font(19 if len(key.sub) <= 4 else 13), fill=fg, anchor="mm")
        else:
            glyph(d, key.glyph, size / 2, 24, 13, fg)
    if key.hint:
        d.text((3, 2), key.hint, font=font(10), fill=(255, 210, 0) if fg == WHITE else (120, 0, 0))
    if key.bar is not None:
        d.rectangle((4, size - 5, size - 5, size - 3), fill=(0, 0, 0))
        d.rectangle((4, size - 5, 4 + (size - 9) * max(0.0, min(1.0, key.bar)), size - 3), fill=fg)
    if key.low:
        glyph(d, "battery", size - 28 if key.badge else size - 11, 10, 6, (255, 210, 0))
    if key.badge:
        d.ellipse((size - 20, 2, size - 2, 20), fill=RED if key.badge != "!" else (230, 80, 0))
        d.text((size - 11, 11), key.badge, font=font(13), fill=WHITE, anchor="mm")
    if key.sel:
        d.rectangle((0, 0, size - 1, size - 1), outline=WHITE, width=3)
    return img


def sheet(keys, gap=8, size=72):
    """All 15 keys in a 5x3 grid, like the deck."""
    from PIL import Image

    img = Image.new("RGB", (5 * size + 4 * gap, 3 * size + 2 * gap), (10, 10, 10))
    for i, k in enumerate(keys):
        img.paste(face(k, size), ((i % 5) * (size + gap), (i // 5) * (size + gap)))
    return img


# --- bridge links ---

class Link:
    """Real bridge: socket thread that connects, authenticates and reconnects."""

    def __init__(self, events):
        self.events, self.sock, self.lock, self.connected = events, None, threading.Lock(), False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, msg):
        with self.lock:
            try:
                self.sock.sendall(json.dumps(msg).encode() + b"\n")
                return True
            except (OSError, AttributeError):
                return False

    def _run(self):
        while True:
            try:
                with open(BRIDGE_FILE) as f:
                    info = json.load(f)
                sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=3)
            except (OSError, ValueError, KeyError):  # no file, stale file or bridge not running
                subprocess.run(["open", "-g", "-j", "-a", BRIDGE_APP], capture_output=True)
                time.sleep(RETRY)
                continue
            try:
                self.sock = sock
                sock.sendall(json.dumps({"op": "hello", "token": info["token"]}).encode() + b"\n")
                reader = sock.makefile("rb")
                hello = parse(reader.readline())
                if not hello or not hello.get("ok"):
                    raise OSError("bridge refused the token")
                sock.settimeout(None)
                self.connected = True
                self.events.put(("conn", True))
                for line in reader:
                    if (msg := parse(line)) is not None:
                        self.events.put(("msg", msg))
            except OSError as error:
                print(f"Bridge connection lost: {error}", flush=True)
            finally:
                self.connected = False
                self.events.put(("conn", False))
                sock.close()
            time.sleep(RETRY)


def fake_snapshot():
    def ch(s, value, fmt="bool", lo=None, hi=None, step=None, units=None, w=True):
        return {"type": full(s), "value": value, "format": fmt, "min": lo, "max": hi, "step": step, "units": units,
                "readable": True, "writable": w}

    def sv(id, name, s, chars):
        return {"id": id, "name": name, "type": full(s), "primary": True, "chars": chars}

    def ac(id, name, room, services, reachable=True):
        return {"id": id, "name": name, "room": room, "reachable": reachable, "services": services}

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


def serve(demo=False):
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.Transport.Transport import TransportError

    events = queue.Queue()
    link = (FakeBridge if demo else Link)(events)
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


def test():
    # protocol and UUIDs
    assert parse(b'{"type":"char","value":1}') == {"type": "char", "value": 1}
    assert parse(b"nope") is None and parse(b"[1]") is None
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
    assert len(k) == 15 and k[0].label == "Home\n4 on" and k[0].tap is None  # floor, ceiling, counter, desk
    assert label(k[1:5]) == ["Scenes", "Status", "Automations", "All lights off"]
    assert k[2].badge == "2" and k[2].fill == RED  # unreachable lamp + low battery contact
    assert label(k[5:10]) == ["Living Room", "Kitchen", "Bedroom", "Office", "Hallway"] and k[10] == EMPTY
    assert k[5].sub == "5/5", k[5].sub  # floor, ceiling, TV, blinds, thermostat; the motion sensor is not counted
    assert k[5].fill == WARM and k[7].fill == DARK  # bedroom: no light on
    assert k[4].tap == ("power", tuple(d.sid for d in ui.devs() if d.kind == "light"), False)
    assert UI(Stub(), 0).keys(0)[1:] == [EMPTY] * 14 and ui.keys(0)[0].glyph == "home"
    off = UI(Stub(), 0)
    off.connected = False
    assert off.keys(0)[0].label == "Bridge\noffline"
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
    assert r[5].tap is None and r[5].long is None and r[5].sub == "no"  # sensor does nothing
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
    assert any(x.label == "Window contact" and x.sub == "open" and x.fill == RED for x in s)
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
    assert not ui3.pending and ui3.keys(10)[0].label == "Bridge\noffline"
    # snapshot push keeps the page, drops vanished rooms
    ui3.handle(("conn", True), 11)
    ui3.stack = [("home", 0), ("room", "office", 0)]
    gone = fake_snapshot()
    gone["homes"][0]["rooms"] = [r for r in gone["homes"][0]["rooms"] if r["id"] != "office"]
    ui3.on_msg(gone, 12)
    assert ui3.stack == [("home", 0)]
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
        img = sheet(keys)
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


def shots(out="/tmp"):
    ui = test_ui()
    for name, page in (("home", ("home", 0)), ("room", ("room", "living", 0)), ("light", ("detail", "floor", 0)),
                       ("lights2", ("detail", "floor", 1)), ("thermostat", ("detail", "thermo", 0)),
                       ("status", ("status", 0)), ("hall", ("room", "hall", 0))):
        ui.stack = [("home", 0), page] if name != "home" else [page]
        sheet(ui.keys(0)).save(f"{out}/homedeck-{name}.png")
        print(f"{out}/homedeck-{name}.png")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    {"serve": serve, "demo": lambda: serve(True), "test": test, "shots": lambda: shots(*sys.argv[2:3])}[cmd]()
