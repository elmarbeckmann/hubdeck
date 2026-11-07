# hubdeck

A tryout built with Claude on 2026-11-07. It controls your smart home from an original 15-key
Stream Deck: lights, outlets, fans, thermostats, blinds, locks, the security system, scenes and
automations. It also shows sensors and problems.

It talks to the hubs directly, like Home Assistant does. It does not use Apple HomeKit.

| Hub | How | Live state | Notes |
| --- | --- | --- | --- |
| Philips Hue Bridge | CLIP API v2, HTTPS on the local network | Event stream (SSE) | Lights, plugs, sensors, scenes, automations |
| IKEA DIRIGERA | Local API on port 8443 | WebSocket | Lights, outlets, blinds, air purifiers, sensors, scenes |
| tado | Cloud API (`my.tado.com`) | Polling every 15 minutes | Heating zones. See "tado rate limit" |
| Matter devices | Not supported | | See "Known limits" |

The devices can stay in Apple Home. Pairing adds one access key on each Hue and DIRIGERA hub. It
changes nothing else on the hubs.

```
Stream Deck <--USB--> hubdeck.py --+--HTTPS + SSE-->        Hue Bridge
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
brew install elmarbeckmann/tap/hubdeck
brew services start hubdeck
```

The formula will copy `hubdeck.py` to `libexec` and write a `hubdeck` command that runs
`uv run --script`, like `stream-deck-eq`.

From source:

```sh
git clone https://github.com/elmarbeckmann/hubdeck
cd hubdeck
make install
```

`make install` does these steps:

1. It copies `hubdeck.py` and `hubdeck.py.lock` to `~/.local/share/hubdeck/`.
2. It writes the command `~/.local/bin/hubdeck`. The command runs `uv run --script` on the
   installed `hubdeck.py`.
3. It writes the launch agent `~/Library/LaunchAgents/dev.elmar.hubdeck.plist` and starts it.

The launch agent `dev.elmar.hubdeck` runs `hubdeck serve`. It starts at login and
restarts after a crash. It writes to the log `~/Library/Logs/hubdeck.log`. To use another
prefix than `~/.local`, run `make install PREFIX=<folder>`. Run `make install` again after you change
`hubdeck.py`. Without installation, use `uv run hubdeck.py <command>` in the project folder.

`make install` and `brew services` are alternatives. Use one of them, not both.

The config folder was `~/.config/stream-deck-home` before the rename. The first command moves it to `~/.config/hubdeck` when the new folder does not exist yet.

**Lock file.** `hubdeck.py.lock` pins the exact versions of the dependencies (`pillow`, `streamdeck`,
`websocket-client`). `uv run` uses the lock file that stands next to the script. To update the pinned
versions, run:

```sh
uv lock --script hubdeck.py --upgrade
```

To check the lock file without a change, run `uv lock --script hubdeck.py --check`. After you change the
dependency list at the top of `hubdeck.py`, run `uv lock --script hubdeck.py`. Then run `make install` again.

Before the service shows anything, pair at least one hub (next section). Without a paired hub, key 0
shows "No hub paired".

## Pairing

Pairing needs you at the hub. Run these commands in a terminal. Each one finds the hub on the network
by itself. When that fails, add `--host <ip address of the hub>`.

**Use `--host`.** The pairing does not verify the identity of the hub. A device on your network can
answer the discovery and receive the pairing. Run `hubdeck pair hue --host <ip>` or
`hubdeck pair ikea --host <ip>` with the address that you know (see "Security").

**Local Network (macOS).** On the first run, macOS asks whether the program may find devices on the
local network. Until you allow it, connections fail with "No route to host". Allow it in the dialog, or
in System Settings › Privacy & Security › Local Network. Pairing waits and retries while the dialog is
open. `PAIR_DEBUG=1 hubdeck pair ikea` prints what the hub answers (never the tokens).

**Philips Hue Bridge**

```sh
hubdeck pair hue
```

1. Without `--host`, the command finds the bridge (mDNS `_hue._tcp`, then `discovery.meethue.com`).
2. It prints "Press the round link button on the Hue Bridge now". Press the button on top of the bridge.
3. The command asks the bridge every 2 s for up to 3 minutes. It saves the address, the application key,
   the bridge id and the SHA-256 fingerprint of the bridge certificate. The first certificate that the
   bridge shows is the one that is pinned (trust on first use).
4. When the bridge was found by discovery, the command compares the bridge id with the one that was
   announced. When they differ, it saves nothing. With `--host` there is no such check.

**IKEA DIRIGERA**

```sh
hubdeck pair ikea
```

1. Without `--host`, the command finds the hub (mDNS `_ihsp._tcp`). It asks the hub for a pairing code
   (OAuth with PKCE).
2. It prints "Press the action button on the DIRIGERA hub now". Press the button on the bottom of the hub.
3. The command asks the hub every 2 s for up to 3 minutes. It saves the address, the access token and the
   SHA-256 fingerprint of the hub certificate. The first certificate that the hub shows is the one that is
   pinned (trust on first use). The command cannot compare the hub with an announced identity.

**tado**

```sh
hubdeck pair tado
```

1. The command prints a web address and a code. It opens the address in your browser when the address
   is on `login.tado.com`.
2. Log in to tado in the browser and confirm the code.
3. The command saves the refresh token and the first home of the account. When the account has more than
   one home, the command prints which home it uses.

`--host` has no effect for tado, because tado has no local access.

**Other commands**

- `hubdeck status` prints one line for each hub. For Hue and DIRIGERA it shows whether the hub
  answers and how many rooms, devices and scenes it has. For tado it prints only that tado is paired. It
  does not contact tado, because a token refresh would rotate the refresh token under a running deck.
  It does not print tokens.
- `hubdeck names` lists every room, device and scene of Hue and DIRIGERA (see "Names"). It does
  not contact tado. It does not print tokens.
- `hubdeck unpair hue|ikea|tado` deletes the local credentials of one hub from this Mac. It does
  not revoke the access key. The key stays valid on the hub until you remove it there (Hue: developer
  page of the bridge or the Hue app; DIRIGERA: IKEA Home smart app; tado: your tado account).
- `hubdeck --help` lists all commands. `serve` is the default command.

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
Keys 2-14 are the accessories. When there are more than 13, key 14 pages and keys 2-13 hold 12. For Hue lights,
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
the scene. The key flashes white when it worked and red when it failed. When two keys show the same name,
the room name is a second small line. Scenes that match the `hide` list (see "Names") do not show. By
default, these are the automatic DIRIGERA integration scenes.

**Automations page:** Back and one key per Hue automation (behavior instance). Green is enabled, grey
is disabled. A press toggles it. DIRIGERA and tado have none here.

**Status page:** all sensors with their values (temperature in °C, humidity in %, light in lux, PM2.5,
contact open in red, motion highlighted, leak in red), then the unreachable accessories, then the
accessories with a low battery. A press on an unreachable or low-battery accessory opens its room. The
alert count on the home page is the number of accessories that are unreachable or low on battery plus
the sensors that report a leak, smoke or CO. An open contact is not counted. When two accessories show the
same name, the room name is a second small line.

**Long names:** a word that is too wide for the key makes the label font smaller (down to 9 px). The
deck never breaks a word. A word that does not fit at 9 px ends with "…".

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

Choose one with the constant `THEME` at the top of `hubdeck.py`, with `--theme graphite|vivid|neon`
before the command (`hubdeck --theme neon serve`), or with the environment variable
`HUBDECK_THEME`. The option wins over the variable, and the variable wins over the constant.
`uv run hubdeck.py shots` renders sample pages of the demo home (see "Development").

## Names

**Where the names come from.** The deck shows the names that the hubs report:

| Hub | Names come from |
| --- | --- |
| Hue Bridge | The Philips Hue app |
| DIRIGERA | The IKEA Home smart app |
| tado | The tado app (the zone name is the room and the device name is "<zone> heating") |

The names do not come from Apple Home. Apple keeps its own names in iCloud. A rename in Apple Home does
not reach the hubs, so it does not show on the deck.

**How rooms merge.** Rooms with the same name (any case) from different hubs are one room. "Lounge" in
Hue and "lounge" in DIRIGERA are one key on the deck. A room alias (see below) applies before the
rooms merge. Two rooms that you give the same alias become one room.

**Change a name.** There are two ways:

1. Rename the item in the brand app. The new name shows on the deck within seconds (see "New, removed
   and renamed devices").
2. Use the optional file `~/.config/hubdeck/names.json`. It is a plain user file with no
   secrets. The deck reads it again when its modification time changes, so an edit shows within about
   2 seconds (`NAMES_POLL`). A restart is not necessary. A broken file is logged and the last good names
  stay. When the file is broken at start-up, the deck uses no aliases and hides nothing.

```json
{
  "devices": {"Pendant": "Kitchen pendant", "ikea:3c5f0a1e-0000-0000-0000-000000000000": "Desk (garage)"},
  "rooms": {"Lounge": "Living room"},
  "scenes": {"Dinner": "Supper"},
  "hide": ["dirigera_integration_*", "Floor spot *"]
}
```

- A key is the hub name or the prefixed id (`hue:...`, `ikea:...`, `tado:...`). Keys match without
  regard to case and without spaces at the start and the end. The id wins over the name. Use the id
  when two devices have the same name.
- An alias that is `null`, empty or only spaces is ignored. The item keeps its hub name.
- A device alias also renames the sensors of that device ("Hall sensor temperature" becomes "Hallway
  sensor temperature").
- A device without a room belongs to the room "Other". Use `"Other"` under `rooms` to rename
  that room, or in `hide` to hide it.
- Run `hubdeck names` to see every room, device and scene of Hue and DIRIGERA. The first line
  shows the path of names.json and whether the file exists. Each other line shows the kind, the source,
  the id and the hub name. A line has `-> alias` when names.json renames it, and `(hidden)` when it
  hides it. Rooms have the id `-`. Copy the name or the id from this list.

**Hide scenes and devices.** `hide` is a list of glob patterns (`*` and `?`, case does not matter). A
pattern matches the hub name or the alias of a scene, a device or a room. Hidden scenes and devices do not
show and do not count. A hidden room hides all its devices and all its scenes.

- Without names.json, the deck hides `dirigera_integration_*`. These are automatic DIRIGERA integration
  scenes.
- When names.json exists, only its `hide` list counts. A file without `hide` hides nothing. Add
  `"dirigera_integration_*"` to your list to keep these scenes hidden.

## New, removed and renamed devices

The deck updates itself. Do not restart it and do not pair again.

- **Live events.** Hue (event stream) and DIRIGERA (WebSocket) send an event when a device or scene is
  added, deleted or renamed, or when a device moves to another room. A new, deleted or renamed room, zone
  or scene does the same. The deck then fetches everything again.
- **Events during a fetch.** The deck keeps the events that arrive while a fetch runs. It applies them
  again to the new data, so a change is not lost.
- **Full refresh.** Every 10 minutes (`FULL_REFRESH`) the deck fetches everything from Hue and DIRIGERA
  again, in case it missed an event. It does the same after each reconnect. It does not fetch
  everything when the Hue event stream is quiet for 5 minutes (`SSE_TIMEOUT`). It only connects again.
- **Unknown device id.** A DIRIGERA event for a device id that the deck does not know changes nothing.
  The device shows after the next full refresh, at most 10 minutes later.
- **tado.** Changes show after the next poll (every 15 minutes, `TADO_POLL`).
- **Hub data that cannot be read.** When the data of one hub cannot be translated, the deck keeps the last
  good data of that hub. It logs the error once. The other hubs stay online and keep working.
- **IP address change.** After a failure the deck looks for the hub again (Hue: by its bridge id).
  It saves a new address only when the certificate at that address has the pinned fingerprint. It
  checks at most 5 addresses (`RELOCATE_TRIES`).
- **Open pages.** When the room or device that you look at disappears, the deck goes back one level.

Pair again only after a hub reset or when you get a new hub.

## tado rate limit

tado limits the requests of accounts without the paid subscription to about 100 per day. So:

- The deck asks tado every 15 minutes (`TADO_POLL` in `hubdeck.py`, 96 requests per day with one zone
  request each). Make the value larger when your account has more zones or you use the tado app a lot.
- It asks once more after each write. Target presses within 3 s (`TADO_COALESCE`) become one request.
- After HTTP 429 the deck sends nothing to tado for the time of the `Retry-After` header (one hour
  when there is none) and shows the last known values.
- The refresh token of tado rotates. The deck saves the new one at each renewal. The credentials file
  also holds the short-lived access token and its expiry time. Before the deck renews the token, it
  reads the file again. When another process saved a newer token, the deck uses that token. Do not run
  two copies of the deck with the same credentials. Two processes that renew the token at the same
  moment can still collide (see "Known limits").
- `status`, `names` and `shots --live` do not contact tado, for the same reason.

A manual target in tado ends at the next time block of the schedule, like the tado app does by default
(`TADO_TERMINATION`). When tado answers HTTP 422 to that, the deck tries once again with a manual
overlay that stays until you change it.

## Security

The access keys control your house. Treat `~/.config/hubdeck/credentials.json` like a
password:

- The folder has mode 0700 and the file has mode 0600. The deck fixes a wider mode when it reads the
  file. The file is replaced in one step, so a crash does not leave half a file. The deck never
  overwrites a file that it cannot read.
- The deck never prints or logs tokens.
- Hue and DIRIGERA use self-signed certificates. The deck does not switch the check off. It pins the
  certificate by its SHA-256 fingerprint. For each connection it compares the certificate with the pin
  **before** it sends the key. When it differs, the deck refuses the connection (see Troubleshooting).
  A hub without a pin is refused.
- **Trust on first use.** At pairing, the deck saves the first certificate that the hub shows. Nobody can
  verify the hub at that moment. Pair on a network that you trust.
- **Discovery is not verified at pairing.** Anyone on the network can answer the mDNS or cloud
  discovery. Use `pair hue --host <ip>` and `pair ikea --host <ip>` with an address that you know. Hue
  pairing without `--host` compares the bridge id with the announced one. That check does not prove
  the identity of the hub.
- When the IP address of a hub changes, the deck saves the new address only when the certificate there
  matches the pin.
- Everyone who can read the credentials file (your user, and root) can control your home. Do not copy
  it, do not commit it and do not put it in a backup that you share.
- tado uses the normal certificate check of the system.
- `unpair` deletes only the local token. The key stays valid on the hub (see "Other commands").

## Settings

Constants at the top of `hubdeck.py`. Change one, then run `make install` again.

| Constant | Value | Meaning |
| --- | --- | --- |
| `THEME` | `graphite` | Key look (see "Themes"). |
| `BRIGHTNESS`, `DIM` | 60, 10 | Deck brightness, and brightness while idle. |
| `IDLE_MINUTES` | 5 | Minutes without a key press before the deck dims. |
| `LONG`, `HOLD` | 0.5 s, 1 s | Long press, and hold for locks, garage doors and the security system. |
| `REPEAT_DELAY`, `REPEAT` | 0.5 s, 0.2 s | Auto-repeat of the +/- keys. |
| `FLASH` | 0.6 s | Flash of a key after a scene or a failed write. |
| `RETRY`, `RETRY_MAX` | 2 s, 60 s | Wait before a hub is tried again. It doubles up to the maximum. |
| `HTTP_TIMEOUT` | 10 s | Time for one request to a hub. |
| `SSE_TIMEOUT` | 300 s | Quiet time on the Hue event stream before the deck connects again. |
| `WS_PING` | 30 s | Quiet time on the DIRIGERA WebSocket before a ping. |
| `COALESCE` | 0.05 s | Writes to one device within this time become one request. |
| `HUE_INTERVAL`, `HUE_GROUP_INTERVAL`, `IKEA_INTERVAL` | 0.1 s, 1 s, 0.1 s | Time between commands to a hub. |
| `FULL_REFRESH` | 600 s | Time between two full fetches from Hue and DIRIGERA. |
| `RELOCATE_TRIES` | 5 | Most addresses that the deck checks after an IP change. |
| `NAMES_POLL` | 2 s | Time between two checks of names.json. |
| `DEFAULT_HIDE` | `dirigera_integration_*` | Hidden items when names.json does not exist. |
| `TADO_POLL` | 900 s | Time between tado polls (see "tado rate limit"). |
| `TADO_COALESCE` | 3 s | tado target presses within this time become one request. |
| `TADO_RATE_WAIT` | 3600 s | Wait after HTTP 429 when tado sends no `Retry-After`. |
| `TADO_TERMINATION` | `TADO_MODE` | End of a manual overlay (see "tado rate limit"). |
| `TADO_DEFAULT_TARGET` | 21 | Target in degrees when you choose "Heat" and tado has no target. |
| `IKEA_BLINDS_INVERTED` | `True` | Direction of DIRIGERA blinds (see Troubleshooting). |

Hue gets about 10 light commands per second and 1 room command per second. Rapid repeats (the +/-
keys while held) are collected and only the latest value is sent.

## Troubleshooting

- **A hub is not found.** The Mac and the hub must be on the same network and subnet. Check with
  `dns-sd -B _hue._tcp` (Hue) or `dns-sd -B _ihsp._tcp` (DIRIGERA). Use `--host <ip>` as a workaround.
- **"No route to host" although the hub is on the network.** macOS blocks programs from the local network
  until you allow it. Open System Settings › Privacy & Security › Local Network and switch on your
  terminal (or the program that runs `hubdeck.py`). Run `PAIR_DEBUG=1 hubdeck pair ikea` to see
  the answers of the hub during pairing.
- **"the certificate of the hub changed (hub reset?): pair it again".** The hub has a new certificate,
  for example after a factory reset, or somebody is between your Mac and the hub. Check the hub, then
  run `hubdeck unpair <hub>` and `hubdeck pair <hub>`.
- **The deck stays black or "Waiting for a Stream Deck".** Another program owns the device. Quit the
  Elgato Stream Deck app and `stream-deck-t3` or `stream-deck-eq`. Check that `hidapi` is installed.
- **Key 0 shows "Offline:" and a list.** No hub answers. The deck tries again after 2 s, 4 s and so on
  up to 60 s. Run `hubdeck status`. Read the log `~/Library/Logs/hubdeck.log`.
- **tado shows old values.** The deck polls every 15 minutes, and it waits after HTTP 429.
- **tado: "tado refused the login ... pair tado again".** The refresh token is not valid any more. Run
  `hubdeck unpair tado` and `hubdeck pair tado`.
- **A hub shows old values and stays online.** The deck cannot read the new data of that hub. It keeps
  the last good data and logs the error. Read the log.
- **Blinds open when you close them (DIRIGERA).** The direction of `blindsTargetLevel` is a guess
  (0 = open). Change `IKEA_BLINDS_INVERTED` to `False` in `hubdeck.py`.
- **A DIRIGERA water sensor or air purifier does not work.** The attribute names (`waterLeakDetected`,
  `fanMode`) are not verified against a real device. Unknown attributes are ignored. Report what
  `GET /v1/devices` shows.
- **A new DIRIGERA device does not show.** Wait for the next full refresh (at most 10 minutes).

## Development

- `uv run hubdeck.py demo` shows the same deck on a fake home (five rooms, a failing Desk lamp, a failing
  Party scene) without a hub.
- `uv run hubdeck.py test` is the self-check. It needs no hardware and no network. It checks the color
  math, the translation of sample Hue, DIRIGERA and tado data into the model, the translation of writes
  back to requests, room merging, name aliases and hiding, structure events, the credentials file mode,
  the coalescing of writes, the fit of labels and the rendering of every page in every theme.
- `uv run hubdeck.py shots [folder]` renders pages of the demo home to `hubdeck-<page>.png` (home, room,
  light, lights2, thermostat, status, hall). It also writes the home page and a room page in every theme
  to `hubdeck-<theme>-home.png` and `hubdeck-<theme>-room.png`. With a folder, the files go there.
  Without a folder, the command makes a new private folder (mode 0700) in the temp directory and
  prints its path. It prints the path of each file.
- `uv run hubdeck.py shots [folder] --live` connects to the paired Hue and DIRIGERA hubs (not tado, whose
  rotating token would break a running deck). It waits up to 20 s for the hubs. It writes the home,
  every room, the scenes and the status page of your real home to `hubdeck-live-<page>.png`. The
  folder rules are the same. The images show your room and device names. Keep them private.

API names that the code follows from the public descriptions and that no real hub has confirmed are
marked in the code (DIRIGERA leak sensor, purifier fan mode, blinds direction).

## Uninstall

```sh
hubdeck unpair hue
hubdeck unpair ikea
hubdeck unpair tado
make uninstall
```

`make uninstall` stops the launch agent `dev.elmar.hubdeck` and removes its plist, the command
`~/.local/bin/hubdeck` and the folder `~/.local/share/hubdeck`. It does not remove
the credentials or the log. With Homebrew (not published yet): `brew services stop hubdeck`
and `brew uninstall hubdeck`. Remove the folder `~/.config/hubdeck` when you do not
need the credentials any more. Remove the log `~/Library/Logs/hubdeck.log` too. `unpair`
deletes only the local token, so remove the access keys on the hubs too (see "Other commands").

## Files

- `hubdeck.py`: the whole program, with the source adapters and the commands `serve`, `demo`, `test`,
  `shots`, `pair`, `status`, `names` and `unpair`.
- `hubdeck.py.lock`: the pinned versions of the dependencies.
- `docs/themes.html`: mockup of the three themes.
- `Makefile`, `dev.elmar.hubdeck.plist`: local install as a launch agent.
- `~/.config/hubdeck/credentials.json`: tokens and hub certificate pins (mode 0600).
- `~/.config/hubdeck/names.json`: your optional aliases and hide list.
- `~/Library/Logs/hubdeck.log`: the log of the launch agent.

## Known limits

- Only the first Stream Deck is used.
- One home only. Zones of Hue that are not rooms are not shown as rooms.
- tado: only the first home of the account is used.
- `unpair` deletes the local token only. The key stays valid on the hub.
- A room with Hue and IKEA lights: the Hue lights switch one by one, not with one room command.
- A DIRIGERA event for an unknown device id waits for the next full refresh (at most 10 minutes).
- Matter devices are not supported.
- Two processes that renew the tado token at the same moment can still collide.
- Pairing does not verify the identity of the hub. Use `--host` (see "Security").
- Optimistic updates assume that a write works. A device that accepts a write and later reverts it
  shows the right state only when it reports the change.
- Not verified on real devices: the tado thermostat mode list, the DIRIGERA field names, the blinds
  direction, leak sensors and air purifiers.
