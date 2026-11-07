# Stream Deck smart home control deck

A tryout built with Claude on 2026-11-07. It controls your smart home from an original 15-key
Stream Deck: lights, outlets, fans, thermostats, blinds, locks, the security system, scenes and
automations. It also shows sensors and problems.

It talks to the hubs directly, like Home Assistant does. It does not use Apple HomeKit.

| Hub | How | Live state | Notes |
| --- | --- | --- | --- |
| Philips Hue Bridge | CLIP API v2, HTTPS on the local network | Event stream (SSE) | Lights, plugs, sensors, scenes, automations |
| IKEA DIRIGERA | Local API on port 8443 | WebSocket | Lights, outlets, blinds, air purifiers, sensors, scenes |
| tado | Cloud API (`my.tado.com`) | Polling every 15 minutes | Heating zones. See "tado rate limit" |
| Matter devices | Not supported now | | See "Later options" |

All devices stay in Apple Home. Nothing is unpaired and the hubs do not change.

```
Stream Deck <--USB--> deck.py --+--HTTPS + SSE-->        Hue Bridge
                                +--HTTPS + WebSocket-->  DIRIGERA
                                +--HTTPS, cloud-->       tado
```

## Requirements

- macOS, with `dns-sd` (it is part of macOS).
- An original or compatible Stream Deck (15 keys) on USB.
- `uv` (it installs Python 3.12 or 3.13 and the libraries for you) and `hidapi` for the USB access:
  `brew install uv hidapi`.
- The Elgato Stream Deck app must be closed, or the deck must be released from it. Only one program
  can control the device.
- The hubs and your Mac are on the same network. tado needs the internet.

## Installation

Planned, with the tap pattern of `~/Code/tinker-lab/homebrew-tap` (not published yet):

```sh
brew install elmarbeckmann/tap/stream-deck-home
brew services start stream-deck-home
```

The formula will copy `deck.py` to `libexec` and write a `stream-deck-home` command that runs
`uv run --script`, like `stream-deck-eq`.

From source:

```sh
git clone https://github.com/elmarbeckmann/stream-deck-home
cd stream-deck-home
make install
```

`make install` copies `deck.py` to `~/.local/share/stream-deck-home/`, writes the command
`~/.local/bin/stream-deck-home` and starts a launchd service. The service starts at login and restarts
after a crash. The log is `~/Library/Logs/stream-deck-home.log`. Run `make install` again after you
change `deck.py`. Without installation, use `uv run deck.py <command>` in the project folder.

Before the service shows anything, pair at least one hub (next section). Without a paired hub, key 0
shows "No hub paired".

## Pairing

Pairing needs you at the hub. Run these commands in a terminal. Each one finds the hub on the network
by itself. When that fails, add `--host <ip address of the hub>`.

**Local Network (macOS).** On the first run, macOS asks whether the program may find devices on the
local network. Until you allow it, connections fail with "No route to host". Allow it in the dialog, or
in System Settings › Privacy & Security › Local Network. Pairing waits and retries while the dialog is
open. `PAIR_DEBUG=1 stream-deck-home pair ikea` prints what the hub answers (never the tokens).

**Philips Hue Bridge**

```sh
stream-deck-home pair hue
```

1. The command finds the bridge (mDNS `_hue._tcp`, then `discovery.meethue.com`).
2. It prints "Press the round link button on the Hue Bridge now". Press the button on top of the bridge.
3. The command asks the bridge every 2 s for up to 3 minutes. It saves the application key, the bridge id
   and the fingerprint of the bridge certificate.

**IKEA DIRIGERA**

```sh
stream-deck-home pair ikea
```

1. The command finds the hub (mDNS `_ihsp._tcp`) and asks for a pairing code (OAuth with PKCE).
2. It prints "Press the action button on the DIRIGERA hub now". Press the button on the bottom of the hub.
3. The command asks the hub every 2 s for up to 3 minutes. It saves the access token and the fingerprint of
   the hub certificate.

**tado**

```sh
stream-deck-home pair tado
```

1. The command prints a web address and a code, and opens the address in your browser.
2. Log in to tado in the browser and confirm the code.
3. The command saves the refresh token and the first home of the account.

`--host` is not used for tado, because tado has no local access.

**Other commands**

- `stream-deck-home status` lists the paired hubs, whether they answer, and how many rooms, devices
  and scenes each one has. It does not print tokens.
- `stream-deck-home unpair hue|ikea|tado` removes the credentials of one hub from this Mac. The access
  key stays on the hub until you remove it there (Hue: developer page of the bridge or the Hue app;
  DIRIGERA: IKEA Home smart app; tado: your tado account).

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
| Home (0) | Shows the home name and the number of lights that are on. When no hub answers, it shows "Offline:" and the names of the hubs. When some hubs answer, their devices work and the devices of the others show the unreachable badge. |
| Scenes (1) | Opens the scenes page. |
| Status (2) | Opens the status page. The key is red and shows a count when something needs attention (see Status). |
| Automations (3) | Opens the automations page. |
| All lights off (4) | Switches off every light of the home. |
| Rooms (5-14) | One key per room. It shows the room name and `on/total` of the controllable devices. It glows when a light is on, in the average color of the lights that are on. With more than 10 rooms, the last key pages. |

Rooms with the same name (any case) from different hubs are one room. There is one home.

**Room page:** key 0 is Back. Key 1 switches all devices of the room off (or on, when all are off).
Keys 2-13 are the accessories, sorted by name. Key 14 pages when there are more than 12. For Hue lights,
"all off" of a room is one command to the Hue room. "All lights off" on the home page is one command
to the whole Hue home. Lights of other hubs get one command each.

**Device keys**

- Short press: the main action. Lights, outlets, switches, fans, air purifiers and valves toggle.
  Thermostats and blinds open their detail page. Sensors do nothing.
- Long press (0.5 s): opens the detail page of every controllable device.
- Locks, garage doors and the security system need a **1 s hold**. The key shows "hold". A press
  between 0.5 s and 1 s opens the detail page. (The three hubs have none of these today. The code is
  there for a later source.)
- A light key has the real color of the light. A thin bar at the bottom shows the brightness.
- A key with a "!" at the top right is an unreachable accessory. A small yellow battery marks a low
  battery (below 20 % or reported by the accessory).
- A key that turns red for a moment means the write failed. The key is back in its old state.

**Detail pages** (key 0 is always Back; long lists page on key 14)

| Device | Keys |
| --- | --- |
| Light | On/off, brightness -10 and +10 (repeat while held), presets 10/30/60/100 %, color temperature warm/neutral/cool (370/250/154 mireds, limited to the light's range), 8 hue presets at saturation 100. Only the controls that the light supports. |
| Thermostat | Current temperature, target -0.5 and +0.5 (repeat while held), target, the modes that the device supports (tado: Off, Heat, Auto), humidity when present. |
| Blinds | Position, open, close, 25/50/75 %, -10 and +10. |
| Air purifier | On/off, speed 25/50/75/100 %. |
| Outlet, switch | On/off. |

**Scenes page:** Back and one key per scene (Hue scenes and smart scenes, DIRIGERA scenes). A press runs
the scene. The key flashes white when it worked and red when it failed. When two Hue scenes have the same
name, the room name is added.

**Automations page:** Back and one key per Hue automation (behavior instance). Green is enabled, grey
is disabled. A press toggles it. DIRIGERA and tado have none here.

**Status page:** all sensors with their values (temperature in °C, humidity in %, light in lux, PM2.5,
contact open in red, motion highlighted, leak in red), then the unreachable accessories, then the
accessories with a low battery. A press on an unreachable or low-battery accessory opens its room. The
alert count on the home page is the number of accessories that are unreachable or low on battery plus
the sensors that report a leak, smoke or CO. An open contact is not counted.

**Dimming:** after 5 minutes without a key press the deck goes to the brightness `DIM`. The first
press only wakes the deck. A new alert also wakes it.

## Themes

The look of the keys has three color themes. The mockup is in [docs/themes.html](docs/themes.html).
Open it in a browser.

| Theme | Look |
| --- | --- |
| `graphite` (default) | Graphite keys. A device that is on glows from the bottom in its light color. |
| `vivid` | Saturated two-tone tiles. Each room keeps its own hue. |
| `neon` | Near-black keys with a colored ring. |

Choose one with the constant `THEME` at the top of `deck.py`, with `--theme graphite|vivid|neon`
before the command (`stream-deck-home --theme neon serve`), or with the environment variable
`STREAM_DECK_HOME_THEME`. The option wins over the variable, and the variable wins over the constant.
`uv run deck.py shots` writes the home page and a room page in every theme to
`/tmp/homedeck-<theme>-home.png` and `/tmp/homedeck-<theme>-room.png`.

## tado rate limit

tado limits the requests of accounts without the paid subscription to about 100 per day. So:

- The deck asks tado every 15 minutes (`TADO_POLL` in `deck.py`, 96 requests per day with one zone
  request each). Make the value larger when your account has more zones or you use the tado app a lot.
- It asks once more after each write. Target presses within 3 s (`TADO_COALESCE`) become one request.
- After HTTP 429 the deck sends nothing to tado for the time of the `Retry-After` header (one hour
  when there is none) and shows the last known values.
- The refresh token of tado rotates. The deck saves the new one at each refresh. Do not run two copies
  of the deck with the same credentials.

A manual target in tado ends at the next time block of the schedule, like the tado app does by default
(`TADO_TERMINATION`). When tado answers HTTP 422 to that, the deck tries once again with a manual
overlay that stays until you change it.

## Security

The access keys control your house. Treat `~/.config/stream-deck-home/credentials.json` like a
password:

- The folder has mode 0700 and the file has mode 0600. The deck fixes a wider mode when it reads the
  file. The file is replaced in one step, so a crash does not leave half a file.
- The deck never prints or logs tokens.
- Hue and DIRIGERA use self-signed certificates. The deck does not switch the check off. At pairing it
  saves the SHA-256 fingerprint of the certificate. For each later connection it compares the
  certificate with that fingerprint **before** it sends the key. When it differs, the deck refuses the
  connection (see Troubleshooting). A Hue Bridge is found again by its bridge id when its IP address
  changes.
- At the first contact in pairing, nobody can verify the hub yet. Pair on a network that you trust.
  Hue pairing also compares the bridge id with the one that the bridge announced on the network.
- Everyone who can read the credentials file (your user, and root) can control your home. Do not copy
  it, do not commit it and do not put it in a backup that you share.
- tado uses the normal certificate check of the system.

## Apple Home scenes and automations

The scenes and automations of Apple Home are not visible in this deck. Apple keeps them in iCloud.
Only an app with the HomeKit entitlement can read them, and Apple gives that entitlement to paid
developer accounts only. The deck shows the scenes and automations of the hubs themselves (Hue,
DIRIGERA). A later option is a paid developer account, or an app such as Itsyhome that already reads
HomeKit.

## Later options

- **Matter devices** (5 devices in the test home). They are not supported now. The way is multi-admin:
  in the Apple Home app, open the device, choose "Turn on pairing mode" to get a pairing code, and pair
  the device a second time with the deck. Nothing is unpaired.
- Apple Home scenes and automations (see above).

## Settings

Constants at the top of `deck.py`: `THEME`, `BRIGHTNESS` (60), `DIM` (10), `IDLE_MINUTES` (5),
`LONG` (0.5 s), `HOLD` (1 s), `REPEAT_DELAY` and `REPEAT` (auto-repeat), `FLASH` (0.6 s), `RETRY` and
`RETRY_MAX` (2 s to 60 s), `COALESCE`, `HUE_INTERVAL`, `HUE_GROUP_INTERVAL`, `IKEA_INTERVAL`,
`TADO_POLL`, `TADO_COALESCE`, `TADO_RATE_WAIT`, `TADO_TERMINATION`, `IKEA_BLINDS_INVERTED`.

Hue gets about 10 light commands per second and 1 room command per second. Rapid repeats (the +/-
keys while held) are collected and only the latest value is sent.

## Troubleshooting

- **A hub is not found.** The Mac and the hub must be on the same network and subnet. Check with
  `dns-sd -B _hue._tcp` (Hue) or `dns-sd -B _ihsp._tcp` (DIRIGERA). Use `--host <ip>` as a workaround.
- **"No route to host" although the hub is on the network.** macOS blocks programs from the local network
  until you allow it. Open System Settings › Privacy & Security › Local Network and switch on your
  terminal (or the program that runs `deck.py`). Run `PAIR_DEBUG=1 stream-deck-home pair ikea` to see
  the answers of the hub during pairing.
- **"the certificate of the hub changed (hub reset?): pair it again".** The hub has a new certificate,
  for example after a factory reset, or somebody is between your Mac and the hub. Check the hub, then
  run `stream-deck-home unpair <hub>` and `stream-deck-home pair <hub>`.
- **The deck stays black or "Waiting for a Stream Deck".** Another program owns the device. Quit the
  Elgato Stream Deck app and `stream-deck-t3` or `stream-deck-eq`. Check that `hidapi` is installed.
- **Key 0 shows "Offline:" and a list.** No hub answers. The deck tries again after 2 s, 4 s and so on
  up to 60 s. Run `stream-deck-home status`. Read the log `~/Library/Logs/stream-deck-home.log`.
- **tado shows old values.** The deck polls every 15 minutes, and it waits after HTTP 429.
- **Blinds open when you close them (DIRIGERA).** The direction of `blindsTargetLevel` is a guess
  (0 = open). Change `IKEA_BLINDS_INVERTED` to `False` in `deck.py`.
- **A DIRIGERA water sensor or air purifier does not work.** The attribute names (`waterLeakDetected`,
  `fanMode`) are not verified against a real device. Unknown attributes are ignored. Report what
  `GET /v1/devices` shows.

## Development

- `uv run deck.py demo` shows the same deck on a fake home (five rooms, a failing Desk lamp, a failing
  Party scene) without a hub.
- `uv run deck.py test` is the self-check. It needs no hardware and no network. It checks the color
  math, the translation of sample Hue, DIRIGERA and tado data into the model, the translation of writes
  back to requests, room merging, event handling, the credentials file mode, the coalescing of writes
  and the rendering of every page in every theme.
- `uv run deck.py shots [folder]` writes PNG renders to `/tmp/homedeck-*.png`.
- `uv run deck.py shots --live` connects to the paired Hue and DIRIGERA hubs (not tado, whose rotating
  token would break a running deck), waits up to 20 s for the first snapshot and writes the home, every
  room, the scenes and the status page to `/tmp/homedeck-live-<page>.png`.

API names that the code follows from the public descriptions and that no real hub has confirmed are
marked in the code (DIRIGERA leak sensor, purifier fan mode, blinds direction).

## Uninstall

```sh
stream-deck-home unpair hue
stream-deck-home unpair ikea
stream-deck-home unpair tado
make uninstall
```

`make uninstall` removes the service, the command and `~/.local/share/stream-deck-home`. With Homebrew:
`brew services stop stream-deck-home` and `brew uninstall stream-deck-home`. Remove the folder
`~/.config/stream-deck-home` when you do not need the credentials any more. Remove the access keys on the
hubs too (see "Other commands").

## Files

- `deck.py`: the whole program, with the source adapters and the commands `serve`, `demo`, `test`,
  `shots`, `pair`, `status` and `unpair`.
- `docs/themes.html`: mockup of the three themes.
- `Makefile`, `dev.elmar.stream-deck-home.plist`: local install as a launchd service.

## Known limits

- Only the first Stream Deck is used.
- One home only. Zones of Hue that are not rooms are not shown.
- Optimistic updates assume that a write works. A device that accepts a write and later reverts it
  shows the right state only when it reports the change.
- The old Apple HomeKit bridge was removed. The git history keeps it.
