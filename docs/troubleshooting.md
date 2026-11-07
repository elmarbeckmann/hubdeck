# Troubleshooting

[Back to the README](../README.md)

The log of `brew services` is `$(brew --prefix)/var/log/hubdeck.log`. After `make install`, it is
`~/Library/Logs/hubdeck.log`.

- **A hub is not found.** The Mac and the hub must be on the same network and subnet. Check with
  `dns-sd -B _hue._tcp` (Hue) or `dns-sd -B _ihsp._tcp` (DIRIGERA). Use `--host <ip>` as a workaround.
- **"No route to host" although the hub is on the network.** macOS blocks programs from the local network
  until you allow it. Open System Settings › Privacy & Security › Local Network and switch on your
  terminal (or the program that runs `hubdeck`). Run `PAIR_DEBUG=1 hubdeck pair ikea` to see the answers
  of the hub during pairing. It never prints tokens.
- **"the certificate of the hub changed (hub reset?): pair it again".** The hub has a new certificate,
  for example after a factory reset, or somebody is between your Mac and the hub. Check the hub, then run
  `hubdeck unpair <hub>` and `hubdeck pair <hub>`.
- **The deck stays black or shows "Waiting for a Stream Deck".** Another program owns the device. Quit the
  Elgato Stream Deck app and any other app that uses the deck. Check that `hidapi` is installed.
- **Key 0 shows "No hub paired".** Pair a hub (see the README).
- **Key 0 shows "Offline:" and a list.** No hub answers. The deck tries again after 2 s, 4 s and so on up
  to 60 s. Run `hubdeck status`. Read the log.
- **tado shows old values.** The deck polls every 15 minutes, and it waits after HTTP 429 (see
  [configuration.md](configuration.md)).
- **tado: "tado refused the login ... pair tado again".** The refresh token is not valid any more. Run
  `hubdeck unpair tado` and `hubdeck pair tado`.
- **A hub shows old values and stays online.** The deck cannot read the new data of that hub. It keeps
  the last good data and logs the error. Read the log.
- **A new DIRIGERA device does not show.** Wait for the next full refresh (at most 10 minutes).
- **Blinds open when you close them (DIRIGERA).** The direction of `blindsTargetLevel` is a guess
  (0 = open). Change `IKEA_BLINDS_INVERTED` to `False` in `hubdeck.py` (see
  [development.md](development.md)).
- **A DIRIGERA water sensor or air purifier does not work.** The attribute names (`waterLeakDetected`,
  `fanMode`) are not verified against a real device. Unknown attributes are ignored. Report what
  `GET /v1/devices` shows.

## Known limits

- Only the first Stream Deck is used.
- One home only. Zones of Hue that are not rooms are not shown as rooms. For tado, only the first home
  of the account is used.
- A room with Hue and IKEA lights: the Hue lights switch one by one, not with one room command.
- Matter devices are not supported.
- `unpair` deletes the local credentials only. The key stays valid on the hub.
- Two processes that renew the tado token at the same moment can still collide.
- Pairing does not verify the identity of the hub. Use `--host` (see [security.md](security.md)).
- Optimistic updates assume that a write works. A device that accepts a write and later reverts it shows
  the right state only when it reports the change.
- Not verified on real devices: the tado thermostat mode list, the DIRIGERA field names, the blinds
  direction, leak sensors and air purifiers.
