# Stream Deck HomeKit control deck

A tryout built with Claude on 2026-11-07. It controls your HomeKit home from an original 15-key
Stream Deck: lights, outlets, fans, thermostats, blinds, locks, garage doors, the security system,
scenes and automations. It also shows sensors and problems.

## Parts

macOS gives HomeKit only to a signed Mac Catalyst app that has the HomeKit entitlement. So there are
two parts:

- `bridge/`: the app **HomeDeckBridge** (Swift). It has no window and no Dock icon. It talks to
  HomeKit and listens on `127.0.0.1`.
- `deck.py`: the program that owns the Stream Deck. It talks to the bridge.

```
Stream Deck  <--USB-->  deck.py  <--127.0.0.1, JSON lines-->  HomeDeckBridge  <-->  HomeKit
```

If the bridge is not running, `deck.py` starts it with `open -g -j -a ~/Applications/HomeDeckBridge.app`
and tries again every 2 s. Key 0 shows "Bridge offline" until the bridge answers.

## Layout

**Home page**

```
+---------+---------+---------+-------------+------------+
| Home    | Scenes  | Status  | Automations | All lights |
| 4 on    |         |         |             | off        |
+---------+---------+---------+-------------+------------+
| room    | room    | room    | room        | room       |
+---------+---------+---------+-------------+------------+
| room    | room    | room    | room        | room       |
+---------+---------+---------+-------------+------------+
```

| Key | Action |
| --- | --- |
| Home (0) | Shows the home name and the number of lights that are on. With more than one home, a press switches to the next home. |
| Scenes (1) | Opens the scenes page. |
| Status (2) | Opens the status page. The key is red and shows a count when something needs attention (see Status). |
| Automations (3) | Opens the automations page. |
| All lights off (4) | Switches off every light in the home. |
| Rooms (5-14) | One key per room, in HomeKit order. It shows the room name and `on/total` of the controllable devices. It is warm orange when a light is on. With more than 10 rooms, the last key pages. |

**Room page:** key 0 is Back. Key 1 switches all devices of the room off (or on, when all are off).
Keys 2-13 are the accessories, sorted by name. Key 14 pages when there are more than 12.

**Device keys**

- Short press: the main action. Lights, outlets, switches, fans, air purifiers and valves toggle.
  Thermostats and blinds open their detail page. Sensors do nothing.
- Long press (0.5 s): opens the detail page of every controllable device.
- Locks, garage doors and the security system need a **1 s hold**. The key shows "hold". A press
  between 0.5 s and 1 s opens the detail page.
- A light key has the real color of the light, scaled by brightness. It is dark grey when off. A thin
  bar at the bottom shows the brightness.
- A grey key with a red "!" is an unreachable accessory. A small yellow battery marks a low battery
  (below 20 % or reported by the accessory).
- A key that turns red for a moment means the write failed. The key is back in its old state.

**Detail pages** (key 0 is always Back; long lists page on key 14)

| Device | Keys |
| --- | --- |
| Light | On/off, brightness -10 and +10 (repeat while held), presets 10/30/60/100 %, color temperature warm/neutral/cool (370/250/154 mireds, limited to the light's range), 8 hue presets at saturation 100. Only the controls that the light supports. |
| Thermostat, heater-cooler | Current temperature, target -0.5 and +0.5 (repeat while held), target, mode Off/Heat/Cool/Auto (active mode has a white frame), humidity when present. |
| Blinds, doors, windows | Position, open, close, 25/50/75 %, -10 and +10. |
| Fan, air purifier | On/off, speed 25/50/75/100 %. |
| Lock | State, Lock (press), Unlock (hold 1 s). |
| Garage door | State (open, closed, opening, closing, stuck), Open and Close (hold 1 s). |
| Security system | State, Stay, Away, Night, Disarm (hold 1 s). |
| Outlet, switch, valve | On/off. |

**Scenes page:** Back and one key per scene. A press runs the scene. The key flashes white when it
worked and red when it failed.

**Automations page:** Back and one key per automation. Green is enabled, grey is disabled. A press
toggles it.

**Status page:** all sensors with their values (temperature in °C, humidity in %, light in lux,
contact open in red, motion and occupancy highlighted, leak, smoke and CO in red), then the
unreachable accessories, then the accessories with a low battery. A press on an unreachable or
low-battery accessory opens its room. The alert count on the home page is the number of accessories
that are unreachable or low on battery plus the sensors that report a leak, smoke or CO. An open
contact is not counted.

**Dimming:** after 5 minutes without a key press the deck goes to the brightness `DIM`. The first
press only wakes the deck. A new alert also wakes it.

## Setup

1. Install the tools once: `brew install uv hidapi`.
2. Open Xcode, then Settings > Accounts, and sign in with your Apple ID. This creates the free
   Personal Team. Note its team id (10 characters). It is in the signing settings of any project
   in Xcode, below the team name.
3. Run `make bridge TEAM=<team id>` in this folder. It builds the app, signs it with your team and
   copies it to `~/Applications/HomeDeckBridge.app`.
4. Start the app once with `open ~/Applications/HomeDeckBridge.app`. Allow the Home access dialog
   when macOS asks. The app has no window.
5. Quit the Elgato Stream Deck app, because only one program can control the device.
6. Run `make install`. It copies `deck.py` to `~/.local/share/stream-deck-home/`, writes the command
   `~/.local/bin/stream-deck-home` and starts the launchd service. The service starts at login and
   restarts after a crash. The log is `~/Library/Logs/stream-deck-home.log`.
7. Run `make uninstall` to remove the service and the installed files. Delete
   `~/Applications/HomeDeckBridge.app` by hand to remove the bridge.

Run `make install` again after you change `deck.py`.

Without a bridge, `uv run deck.py demo` shows the same deck on a fake home (one home, five rooms,
a failing Desk lamp, a failing Party scene). `uv run deck.py test` is the self-check without hardware.
`uv run deck.py shots` writes PNG renders to `/tmp/homedeck-*.png`.

## Renewal every 7 days

Apps that are signed with a free Personal Team expire after 7 days. Then the bridge does not start,
and the deck shows "Bridge offline". Run `make renew` to rebuild and sign the app again. It uses
the team id that `make bridge` saved in `.team`. You can use a cron job or a calendar reminder.
A paid Apple Developer account removes this limit (profiles last one year).

**Known limit of the free team:** Apple may refuse the HomeKit entitlement for a free team. Then
`make bridge` fails with the message `Entitlement com.apple.developer.homekit not found and could
not be included in profile`. The HomeKit capability needs a paid Apple Developer Program account.

## Security

The bridge can operate your locks, garage door and alarm. So:

- The bridge listens on `127.0.0.1` only. It picks a random port at each start.
- At each start it writes a random token (32 bytes, hex) with the port to
  `~/Library/Application Support/HomeDeck/bridge.json`. The file has mode 0600, and the folder has
  mode 0700.
- A connection must send `{"op":"hello","token":...}` as the first line within 5 s. With a wrong token
  the bridge closes the connection.
- Everyone who can read `bridge.json` (your user, and root) can control your home through the
  bridge. Do not copy this file, do not commit it and do not put it in a backup you share.

## Protocol

Newline-delimited JSON in both directions. A client message may carry `"req": n`. The bridge
answers it with `{"type":"result","req":n,"ok":true|false,"error":"..."}`.

| Client | Meaning |
| --- | --- |
| `{"op":"hello","token":"..."}` | First message. |
| `{"op":"snapshot"}` | Answer: `{"type":"snapshot","homes":[...]}` with rooms, accessories, services, characteristics (value, format, min, max, step, units), scenes and automations. |
| `{"op":"write","service":id,"char":uuid,"value":v}` | Writes one characteristic. |
| `{"op":"scene","id":id}` | Runs a scene. |
| `{"op":"trigger","id":id,"enabled":bool}` | Enables or disables an automation. |

The bridge pushes `{"type":"char",service,char,value}`, `{"type":"reach",accessory,reachable}` and a
full `snapshot` when the home structure changes.

## Settings

Constants at the top of `deck.py`: `BRIGHTNESS` (60), `DIM` (10), `IDLE_MINUTES` (5), `LONG` (0.5 s),
`HOLD` (1 s), `REPEAT_DELAY` and `REPEAT` (auto-repeat), `FLASH` (0.6 s), `RETRY` (2 s).

## Files

- `deck.py`: the whole deck program, with `serve`, `demo`, `test` and `shots`.
- `bridge/`: the Xcode project and the Swift sources of HomeDeckBridge.
- `Makefile`, `dev.elmar.stream-deck-home.plist`: bridge build and local install as a launchd service.

## Known limits

- Only the first Stream Deck is used.
- The detail page of a thermostat shows all four modes, also when the device supports fewer.
- Optimistic updates assume that a write works. A device that accepts a write and later reverts it
  shows the right state only when it reports the change.
