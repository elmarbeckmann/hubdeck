UID := $(shell id -u)
PREFIX ?= $(HOME)/.local
BIN := $(PREFIX)/bin/hubdeck
SHARE := $(PREFIX)/share/hubdeck
UV := $(shell command -v uv)
PLIST := $(HOME)/Library/LaunchAgents/dev.elmar.hubdeck.plist
LOG := $(HOME)/Library/Logs/hubdeck.log

# `make install` and `brew services` are alternatives: use one, not both (two launch agents would fight for the deck).
.PHONY: install uninstall

install:
	@test -n "$(UV)" || { echo "uv not found: brew install uv"; exit 1; }
	install -d $(PREFIX)/bin $(SHARE)
	install -m 644 hubdeck.py hubdeck.py.lock $(SHARE)
	printf '#!/bin/sh\nexec "$(UV)" run --script "$(SHARE)/hubdeck.py" "$$@"\n' > $(BIN)
	chmod 755 $(BIN)
	sed -e 's#__BIN__#$(BIN)#' -e 's#__LOG__#$(LOG)#' dev.elmar.hubdeck.plist > $(PLIST)
	-launchctl bootout gui/$(UID)/dev.elmar.hubdeck 2>/dev/null || true
	launchctl enable gui/$(UID)/dev.elmar.hubdeck
	launchctl bootstrap gui/$(UID) $(PLIST)

# The credentials in ~/.config/hubdeck stay: run `hubdeck unpair <hub>` first to remove them.
uninstall:
	-launchctl bootout gui/$(UID)/dev.elmar.hubdeck 2>/dev/null || true
	rm -f $(PLIST) $(BIN)
	rm -rf $(SHARE)
