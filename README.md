# hubdeck

hubdeck turns an Elgato Stream Deck Original (15 keys) into a control deck for your smart home. It
controls Philips Hue, IKEA DIRIGERA and tado. It talks to the Hue and DIRIGERA hubs directly on your
local network. It reaches tado through the tado cloud. It does not use Apple HomeKit, and your devices
can stay in Apple Home.

## Supported devices

- Philips Hue Bridge: lights, plugs, sensors, scenes, automations.
- IKEA DIRIGERA: lights, outlets, blinds, air purifiers, sensors, scenes.
- tado: heating zones.

Matter devices are not supported.

## Requirements

- macOS.
- A Stream Deck Original or a compatible deck with 15 keys, on USB.
- A Hue Bridge, a DIRIGERA hub, or a tado account. The hubs and your Mac must be on the same network.
- [Homebrew](https://brew.sh).

Only one program can hold the deck. Quit the Elgato Stream Deck app, or any other app that uses the
deck, before you start hubdeck.

## Install

```sh
brew install elmarbeckmann/tap/hubdeck
hubdeck pair hue --host <ip of the bridge>
brew services start hubdeck
```

Pair at least one hub before you start the service (see the next section). The service runs
`hubdeck serve` at login. Without a paired hub, key 0 shows "No hub paired".

## Pair

Pairing needs you at the hub.

1. Run `hubdeck pair hue --host <ip>` or `hubdeck pair ikea --host <ip>`. Use the IP address that you
   know. Without `--host`, the command looks for the hub on the network, but it cannot verify who
   answers (see [docs/security.md](docs/security.md)).
2. Press the button on the hub when the command asks. Hue: the round link button on top of the bridge.
   DIRIGERA: the action button on the bottom of the hub. You have 3 minutes.
3. For tado, run `hubdeck pair tado`. It prints a web address and a code. Log in to tado in the browser
   and confirm the code. `--host` has no effect for tado.

On the first run, macOS asks whether the program may use the local network. Allow it. See
[docs/troubleshooting.md](docs/troubleshooting.md) when pairing fails.

## Use

- Tap a key: the main action. Lights, outlets, switches and fans toggle. A room key opens the room.
- Long press (0.5 s): opens the detail page of a device. There you set brightness, color, target
  temperature or blind position.
- Hold (1 s): needed for locks, garage doors and the security system.
- Key 0 is Back on every page except the home page. The home page also has keys for scenes,
  automations, status and "all lights off".

See [docs/usage.md](docs/usage.md) for all pages and themes.

## Commands

| Command | What it does |
| --- | --- |
| `hubdeck serve` | Runs the deck. This is the default command. |
| `hubdeck demo` | Runs the deck on a fake home, without a hub. |
| `hubdeck status` | Prints one line for each paired hub. |
| `hubdeck names` | Lists rooms, devices and scenes with their ids (for `names.json`). |
| `hubdeck pair hue\|ikea\|tado` | Pairs a hub. Add `--host <ip>` for Hue or DIRIGERA. |
| `hubdeck unpair hue\|ikea\|tado` | Deletes the local credentials of one hub. |
| `hubdeck shots [folder]` | Renders sample pages to PNG files. |
| `hubdeck test` | Runs the self-check. It needs no deck and no network. |

Add `--theme graphite|vivid|neon` before the command to choose a theme.

## Files

- `~/.config/hubdeck/credentials.json`: access tokens and hub certificate pins. Keep it private.
- `~/.config/hubdeck/names.json`: your optional aliases and hide list (see [docs/names.md](docs/names.md)).
- `$(brew --prefix)/var/log/hubdeck.log`: the log of `brew services`.

## Uninstall

```sh
hubdeck unpair hue
hubdeck unpair ikea
hubdeck unpair tado
brew services stop hubdeck
brew uninstall hubdeck
```

`unpair` deletes only the local credentials. Remove the access key on the hub too (see
[docs/security.md](docs/security.md)). Then delete the folder `~/.config/hubdeck`.

## Known limits

- Only the first Stream Deck is used.
- One home only. For tado, only the first home of the account is used.
- A room with Hue and IKEA lights switches the Hue lights one by one, not with one room command.
- Some DIRIGERA features are not verified on real devices (see
  [docs/troubleshooting.md](docs/troubleshooting.md)).

## Documentation

- [Usage](docs/usage.md): layout, pages, gestures, themes.
- [Names](docs/names.md): aliases and hidden items.
- [Configuration](docs/configuration.md): settings, refresh behavior, tado rate limit.
- [Security](docs/security.md): certificate pinning, credentials, unpair.
- [Troubleshooting](docs/troubleshooting.md)
- [Development](docs/development.md): run from source, tests, release.

## License

See [LICENSE](LICENSE).
