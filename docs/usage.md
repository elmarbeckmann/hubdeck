# Usage

[Back to the README](../README.md)

## Home page

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
| Status (2) | Opens the status page. The key is red and shows a count when something needs attention. |
| Automations (3) | Opens the automations page. |
| All lights off (4) | Switches off every light of the home. |
| Rooms (5-14) | One key per room. It shows the room name and `on/total` of the controllable devices. It glows when a light is on, in the average color of the lights that are on. With more than 10 rooms, the last key pages. |

Rooms with the same name (any case) from different hubs are one room. There is one home.

## Room page

Key 0 is Back. Key 1 switches all devices of the room off (or on, when all are off). Keys 2-14 are the
accessories. When there are more than 13, key 14 pages and keys 2-13 hold 12.

For Hue lights, "all off" of a room is one command to the Hue room. "All lights off" on the home page is
one command to the whole Hue home. Lights of other hubs get one command each.

## Gestures

- Short press: the main action. Lights, outlets, switches, fans, air purifiers and valves toggle.
  Thermostats and blinds open their detail page. Sensors do nothing.
- Long press (0.5 s): opens the detail page of every controllable device.
- Locks, garage doors and the security system need a 1 s hold. The key shows "hold". A press between
  0.5 s and 1 s opens the detail page. (The supported hubs have no such device today.)
- The + and - keys repeat while you hold them.

## Key marks

- A light key has the real color of the light. A thin bar at the bottom shows the brightness.
- A key with a "!" at the top right is an unreachable accessory.
- A small yellow battery marks a low battery (below 20 % or reported by the accessory).
- A key that turns red for a moment means the write failed. The key returns to its old state.
- A word that is too wide for the key makes the label font smaller (down to 9 px). The deck never breaks
  a word. A word that does not fit at 9 px ends with "…".

## Detail pages

Key 0 is always Back. Long lists page on key 14.

| Device | Keys |
| --- | --- |
| Light | On/off, brightness -10 and +10, presets 10/30/60/100 %, color temperature warm/neutral/cool (370/250/154 mireds, limited to the light's range), 8 hue presets at saturation 100. Only the controls that the light supports. |
| Thermostat | Current temperature, target -0.5 and +0.5, target, the modes that the device supports (tado: Off, Heat, Auto), humidity when present. |
| Blinds | Position, open, close, 25/50/75 %, -10 and +10. |
| Air purifier | On/off, speed 25/50/75/100 %. |
| Outlet, switch | On/off. |

## Scenes page

Back and one key per scene (Hue scenes and smart scenes, DIRIGERA scenes). A press runs the scene. The
key flashes white when it worked and red when it failed. When two keys show the same name, the room name
is a second small line. Scenes that match the `hide` list do not show (see [names.md](names.md)). By
default, the automatic DIRIGERA integration scenes are hidden.

## Automations page

Back and one key per Hue automation (behavior instance). Green is enabled, grey is disabled. A press
toggles it. DIRIGERA and tado have no automations here.

## Status page

The page shows all sensors with their values (temperature in °C, humidity in %, light in lux, PM2.5,
contact open in red, motion highlighted, leak in red). Then it shows the unreachable accessories, then
the accessories with a low battery. A press on an unreachable or low-battery accessory opens its room.

The alert count on the home page is the number of accessories that are unreachable or low on battery,
plus the sensors that report a leak, smoke or CO. An open contact is not counted. When two accessories
show the same name, the room name is a second small line.

## Dimming

After 5 minutes without a key press, the deck dims. The first press only wakes the deck. A new alert also
wakes it. See [configuration.md](configuration.md) for the values.

## Themes

The keys have three color themes. The mockup is in [themes.html](themes.html). Open it in a browser.

| Theme | Look |
| --- | --- |
| `graphite` (default) | Graphite keys. A device that is on glows from the bottom in its light color. |
| `vivid` | Saturated two-tone tiles. Each room keeps its own hue. |
| `neon` | Near-black keys with a colored ring. |

Choose a theme with `--theme graphite|vivid|neon` before the command (`hubdeck --theme neon serve`), or
with the environment variable `HUBDECK_THEME`. The option wins over the variable. See
[configuration.md](configuration.md) for the full order.
