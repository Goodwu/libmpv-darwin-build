# Path to the Xcode.app bundle. If not set, the overlay will fall back to
# fetching Xcode from the Nix store (requires manual download, see
# nix/overlays/xcode.nix for instructions).
# Example: make XCODE_PATH=/Applications/Xcode_16.1.0.app
XCODE_PATH ?=

# Version string to build. If not set, falls back to .nix/config/version.txt.
# Example: make VERSION=0.0.1
VERSION ?=

# Flake output attribute to build. If not set, the default package is built.
# Example: make TARGET=mk-out-archive-libs-macos-universal-video-default
TARGET ?=

all: build

# Build using Nix flakes.
# After the build, .nix/config/xcode.path and .nix/config/version.txt are
# restored to their committed values.
.PHONY: build
build:
	trap 'git checkout -- .nix/config/xcode.path .nix/config/version.txt' EXIT; \
	$(if $(XCODE_PATH),echo '$(XCODE_PATH)' > .nix/config/xcode.path;,) \
	$(if $(VERSION),echo '$(VERSION)' > .nix/config/version.txt;,) \
	nix build -v -L \
		--no-update-lock-file \
		--option sandbox true \
		--option sandbox-fallback false \
		$(if $(XCODE_PATH),--option extra-sandbox-paths $(XCODE_PATH),) \
		$(if $(TARGET),.#$(TARGET),)
