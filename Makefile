UID := $(shell id -u)
PREFIX ?= $(HOME)/.local
BIN := $(PREFIX)/bin/stream-deck-home
SHARE := $(PREFIX)/share/stream-deck-home
UV := $(shell command -v uv)
PLIST := $(HOME)/Library/LaunchAgents/dev.elmar.stream-deck-home.plist
LOG := $(HOME)/Library/Logs/stream-deck-home.log

.PHONY: install uninstall

install:
	@test -n "$(UV)" || { echo "uv not found: brew install uv"; exit 1; }
	install -d $(PREFIX)/bin $(SHARE)
	install -m 644 deck.py $(SHARE)
	printf '#!/bin/sh\nexec "$(UV)" run --script "$(SHARE)/deck.py" "$$@"\n' > $(BIN)
	chmod 755 $(BIN)
	sed -e 's#__BIN__#$(BIN)#' -e 's#__LOG__#$(LOG)#' dev.elmar.stream-deck-home.plist > $(PLIST)
	-launchctl bootout gui/$(UID)/dev.elmar.stream-deck-home 2>/dev/null || true
	launchctl enable gui/$(UID)/dev.elmar.stream-deck-home
	launchctl bootstrap gui/$(UID) $(PLIST)

# The credentials in ~/.config/stream-deck-home stay: run `stream-deck-home unpair <hub>` first to remove them.
uninstall:
	-launchctl bootout gui/$(UID)/dev.elmar.stream-deck-home 2>/dev/null || true
	rm -f $(PLIST) $(BIN)
	rm -rf $(SHARE)
