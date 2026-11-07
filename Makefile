UID := $(shell id -u)
PREFIX ?= $(HOME)/.local
BIN := $(PREFIX)/bin/stream-deck-home
SHARE := $(PREFIX)/share/stream-deck-home
UV := $(shell command -v uv)
PLIST := $(HOME)/Library/LaunchAgents/dev.elmar.stream-deck-home.plist
LOG := $(HOME)/Library/Logs/stream-deck-home.log
APP := $(HOME)/Applications/HomeDeckBridge.app
TEAM ?= $(shell cat .team 2>/dev/null)

.PHONY: install uninstall bridge renew

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

uninstall:
	-launchctl bootout gui/$(UID)/dev.elmar.stream-deck-home 2>/dev/null || true
	rm -f $(PLIST) $(BIN)
	rm -rf $(SHARE)

# Usage: make bridge TEAM=<10-character team id>. The id is saved in .team for `make renew`.
bridge:
	@test -n "$(TEAM)" || { echo "usage: make bridge TEAM=<team id>"; exit 1; }
	xcodebuild -project bridge/HomeDeckBridge.xcodeproj -scheme HomeDeckBridge \
		-destination 'platform=macOS,variant=Mac Catalyst' -configuration Release \
		-derivedDataPath bridge/build DEVELOPMENT_TEAM=$(TEAM) -allowProvisioningUpdates build
	-pkill -x HomeDeckBridge
	install -d $(HOME)/Applications
	rm -rf $(APP)
	ditto bridge/build/Build/Products/Release-maccatalyst/HomeDeckBridge.app $(APP)
	echo $(TEAM) > .team

# Free Personal Team profiles expire after 7 days: rebuild and reinstall.
renew: bridge
