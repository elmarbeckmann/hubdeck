# Names

[Back to the README](../README.md)

## Where the names come from

The deck shows the names that the hubs report:

| Hub | Names come from |
| --- | --- |
| Hue Bridge | The Philips Hue app |
| DIRIGERA | The IKEA Home smart app |
| tado | The tado app (the zone name is the room and the device name is "<zone> heating") |

The names do not come from Apple Home. Apple keeps its own names in iCloud. A rename in Apple Home does
not reach the hubs, so it does not show on the deck.

Rooms with the same name (any case) from different hubs are one room. "Lounge" in Hue and "lounge" in
DIRIGERA are one key on the deck. A room alias applies before the rooms merge. Two rooms that you give
the same alias become one room.

## Change a name

There are two ways:

1. Rename the item in the brand app. The new name shows on the deck within seconds (see
   [configuration.md](configuration.md)).
2. Use the optional file `~/.config/hubdeck/names.json`. It is a plain user file with no secrets. The
   deck reads it again when its modification time changes, so an edit shows within about 2 seconds
   (`NAMES_POLL`). A restart is not necessary. A broken file is logged and the last good names stay.
   When the file is broken at start-up, the deck uses no aliases and hides nothing.

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
- A device without a room belongs to the room "Other". Use `"Other"` under `rooms` to rename that room,
  or in `hide` to hide it.
- Run `hubdeck names` to see every room, device and scene of Hue and DIRIGERA. It does not contact tado
  and does not print tokens. The first line shows the path of `names.json` and whether the file exists.
  Each other line shows the kind, the source, the id and the hub name. A line has `-> alias` when
  `names.json` renames it, and `(hidden)` when it hides it. Rooms have the id `-`. Copy the name or the
  id from this list.

## Hide scenes and devices

`hide` is a list of glob patterns (`*` and `?`, case does not matter). A pattern matches the hub name or
the alias of a scene, a device or a room. Hidden scenes and devices do not show and do not count. A
hidden room hides all its devices and all its scenes.

- Without `names.json`, the deck hides `dirigera_integration_*`. These are automatic DIRIGERA
  integration scenes.
- When `names.json` exists, only its `hide` list counts. A file without `hide` hides nothing. Add
  `"dirigera_integration_*"` to your list to keep these scenes hidden.
