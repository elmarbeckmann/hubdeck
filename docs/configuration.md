# Configuration

[Back to the README](../README.md)

## Settings

The settings are constants at the top of `hubdeck.py`. To change one, edit `hubdeck.py` in a clone of the
repository and install from it (see [development.md](development.md)). A Homebrew upgrade replaces the
installed file.

| Constant | Value | Meaning |
| --- | --- | --- |
| `THEME` | `graphite` | Key look (see [usage.md](usage.md)). |
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
| `NAMES_POLL` | 2 s | Time between two checks of `names.json`. |
| `DEFAULT_HIDE` | `dirigera_integration_*` | Hidden items when `names.json` does not exist. |
| `TADO_POLL` | 900 s | Time between tado polls (see below). |
| `TADO_COALESCE` | 3 s | tado target presses within this time become one request. |
| `TADO_RATE_WAIT` | 3600 s | Wait after HTTP 429 when tado sends no `Retry-After`. |
| `TADO_TERMINATION` | `TADO_MODE` | End of a manual overlay (see below). |
| `TADO_DEFAULT_TARGET` | 21 | Target in degrees when you choose "Heat" and tado has no target. |
| `IKEA_BLINDS_INVERTED` | `True` | Direction of DIRIGERA blinds (see [troubleshooting.md](troubleshooting.md)). |

Hue gets about 10 light commands per second and 1 room command per second. Rapid repeats (the +/- keys
while held) are collected and only the latest value is sent.

## Theme order

`--theme` wins over the environment variable `HUBDECK_THEME`. The variable wins over the constant
`THEME`. An unknown theme stops the program with a message.

## Refresh behavior

The deck updates itself. Do not restart it and do not pair again.

- **Live events.** Hue (event stream) and DIRIGERA (WebSocket) send an event when a device or scene is
  added, deleted or renamed, or when a device moves to another room. A new, deleted or renamed room, zone
  or scene does the same. The deck then fetches everything again.
- **Events during a fetch.** The deck keeps the events that arrive while a fetch runs. It applies them
  again to the new data, so a change is not lost.
- **Full refresh.** Every 10 minutes (`FULL_REFRESH`) the deck fetches everything from Hue and DIRIGERA
  again, in case it missed an event. It does the same after each reconnect. When the Hue event stream is
  quiet for 5 minutes (`SSE_TIMEOUT`), the deck only connects again.
- **Unknown device id.** A DIRIGERA event for a device id that the deck does not know changes nothing.
  The device shows after the next full refresh, at most 10 minutes later.
- **tado.** Changes show after the next poll (every 15 minutes, `TADO_POLL`).
- **Hub data that cannot be read.** When the data of one hub cannot be translated, the deck keeps the last
  good data of that hub. It logs the error once. The other hubs stay online and keep working.
- **IP address change.** After a failure the deck looks for the hub again (Hue: by its bridge id). It
  saves a new address only when the certificate at that address has the pinned fingerprint. It checks at
  most 5 addresses (`RELOCATE_TRIES`).
- **Open pages.** When the room or device that you look at disappears, the deck goes back one level.

Pair again only after a hub reset or when you get a new hub.

## tado rate limit

tado limits the requests of accounts without the paid subscription to about 100 per day. So:

- The deck asks tado every 15 minutes (`TADO_POLL`, 96 requests per day with one zone request each).
  Make the value larger when your account has more zones or you use the tado app a lot.
- It asks once more after each write. Target presses within 3 s (`TADO_COALESCE`) become one request.
- After HTTP 429 the deck sends nothing to tado for the time of the `Retry-After` header (one hour when
  there is none) and shows the last known values.
- The refresh token of tado rotates. The deck saves the new one at each renewal. The credentials file
  also holds the short-lived access token and its expiry time. Before the deck renews the token, it reads
  the file again. When another process saved a newer token, the deck uses that token. Do not run two
  copies of the deck with the same credentials. Two processes that renew the token at the same moment can
  still collide.
- `status`, `names` and `shots --live` do not contact tado, for the same reason.

A manual target in tado ends at the next time block of the schedule, like the tado app does by default
(`TADO_TERMINATION`). When tado answers HTTP 422 to that, the deck tries once again with a manual overlay
that stays until you change it.
