# Development

[Back to the README](../README.md)

## Run from the clone

```sh
git clone https://github.com/elmarbeckmann/hubdeck
cd hubdeck
brew services stop hubdeck    # only one program can hold the deck
uv run hubdeck.py <command>
```

`uv run hubdeck.py` uses the same `~/.config/hubdeck` as the installed app. That folder is outside the
repository, so no secrets are in the working tree. Never commit files from `~/.config/hubdeck`.

`uv` installs Python 3.12 or 3.13 and the libraries. The deck needs `hidapi` (`brew install hidapi`).

## Self-check

`uv run hubdeck.py test` needs no hardware and no network. It does not touch the real config. It checks
the color math, the translation of sample Hue, DIRIGERA and tado data into the model, the translation of
writes back to requests, room merging, name aliases and hiding, structure events, the credentials file
mode, the coalescing of writes, the fit of labels and the rendering of every page in every theme.

`uv run hubdeck.py demo` shows the deck on a fake home (five rooms, a failing Desk lamp, a failing Party
scene) without a hub.

## Screenshots

- `uv run hubdeck.py shots [folder]` renders pages of the demo home to `hubdeck-<page>.png` (home, room,
  light, lights2, thermostat, status, hall). It also writes the home page and a room page in every theme
  to `hubdeck-<theme>-home.png` and `hubdeck-<theme>-room.png`. With a folder, the files go there.
  Without a folder, the command makes a new private folder (mode 0700) in the temp directory and prints
  its path. It prints the path of each file.
- `uv run hubdeck.py shots [folder] --live` connects to the paired Hue and DIRIGERA hubs (not tado, whose
  rotating token would break a running deck). It waits up to 20 s for the hubs. It writes the home, every
  room, the scenes and the status page of your real home to `hubdeck-live-<page>.png`.

The `--live` images show your real room and device names. Keep them private. Do not commit them.

## Lock file

`hubdeck.py.lock` pins the exact versions of the dependencies (`pillow`, `streamdeck`,
`websocket-client`). `uv run` uses the lock file that stands next to the script.

```sh
uv lock --script hubdeck.py --upgrade   # update the pinned versions
uv lock --script hubdeck.py --check     # check the lock file without a change
uv lock --script hubdeck.py             # after you change the dependency list in hubdeck.py
```

## Install from the clone

`make install` is an alternative to Homebrew. Use one of them, never both. Two launch agents would fight
for the deck.

`make install` does these steps:

1. It copies `hubdeck.py` and `hubdeck.py.lock` to `~/.local/share/hubdeck/`.
2. It writes the command `~/.local/bin/hubdeck`. The command runs `uv run --script` on the installed
   `hubdeck.py`.
3. It writes the launch agent `~/Library/LaunchAgents/dev.elmar.hubdeck.plist` and starts it.

The launch agent runs `hubdeck serve`. It starts at login and restarts after a crash. It writes to
`~/Library/Logs/hubdeck.log`. Use `make install PREFIX=<folder>` for another prefix than `~/.local`. Run
`make install` again after you change `hubdeck.py`.

`make uninstall` stops the launch agent and removes its plist, the command and the installed folder. It
keeps the credentials and the log. Run `hubdeck unpair <hub>` first to remove the credentials.

## Release checklist

1. Commit all changes. The tag is the version: `hubdeck.py` has no version constant, and Homebrew reads the version from the tag URL.
2. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
3. Push the tag: `git push origin vX.Y.Z`.
4. Compute the checksum:
   `curl -L https://github.com/elmarbeckmann/hubdeck/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256`
5. In `Formula/hubdeck.rb` of `elmarbeckmann/homebrew-tap`, update `url` and `sha256`. Commit and push.
6. Run `brew style elmarbeckmann/tap/hubdeck`.
7. Run `brew upgrade hubdeck`.
8. Run `brew test hubdeck`.
