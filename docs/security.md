# Security

[Back to the README](../README.md)

The access keys control your house. Treat `~/.config/hubdeck/credentials.json` like a password.

## Credentials file

- The folder has mode 0700 and the file has mode 0600. The deck fixes a wider mode when it reads the
  file.
- The deck replaces the file in one step, so a crash does not leave half a file. It never overwrites a
  file that it cannot read.
- Everyone who can read the file (your user, and root) can control your home. Do not copy it, do not
  commit it and do not put it in a backup that you share.
- The file holds the access keys, the hub certificate pins and, for tado, the refresh token with the
  short-lived access token.

## What the app never logs

The deck never prints or logs tokens. `status`, `names` and the debug output of `PAIR_DEBUG=1` do not
show them.

## Certificate pinning

Hue and DIRIGERA use self-signed certificates. The deck does not switch the check off. It pins the
certificate by its SHA-256 fingerprint. For each connection it compares the certificate with the pin
before it sends the key. When it differs, the deck refuses the connection (see
[troubleshooting.md](troubleshooting.md)). A hub without a pin is refused.

tado uses the normal certificate check of the system.

When the IP address of a hub changes, the deck saves the new address only when the certificate there
matches the pin.

## Trust on first use

At pairing, the deck saves the first certificate that the hub shows. Nobody can verify the hub at that
moment. Pair on a network that you trust.

Discovery is not verified at pairing. Anyone on the network can answer the mDNS or cloud discovery. Use
`hubdeck pair hue --host <ip>` and `hubdeck pair ikea --host <ip>` with an address that you know. Hue
pairing without `--host` compares the bridge id with the announced one. That check does not prove the
identity of the hub. With `--host` there is no such check. DIRIGERA has no announced identity to compare.

## Unpair limit

`hubdeck unpair` deletes only the local credentials. It does not revoke the access key. The key stays
valid on the hub until you remove it there:

- Hue: the developer page of the bridge, or the Hue app.
- DIRIGERA: the IKEA Home smart app.
- tado: your tado account.

## Pairing changes on the hubs

Pairing adds one access key on each Hue and DIRIGERA hub. It changes nothing else on the hubs.
