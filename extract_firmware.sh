#!/usr/bin/env bash
# Extract an Epson XP-442 firmware update into this repo's layout.
#
# Given a downloaded .efu (or .rcx already unzipped), produces:
#   rcx_output/                  IPL blocks + decompressed Block 1 streams
#   squashfs_rootfs1/            Linux rootfs
#   squashfs_rootfs2/            network module (nwsoc)
#
# Usage:
#   ./extract_firmware.sh                        # uses ./FWF729TL.efu
#   ./extract_firmware.sh path/to/other.efu
#   ./extract_firmware.sh path/to/other.rcx
#
# Requires: python3, unsquashfs
#   brew install squashfs                # macOS
#   apt install squashfs-tools           # Debian/Ubuntu
# and the abrasive/epson-reversing toolkit, a git submodule at ./epson-reversing/
# (run `git submodule update --init` after cloning). Set EPSON_REVERSING=/path
# to use another location.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
FIRMWARE="${1:-$REPO_ROOT/FWF729TL.efu}"

if [ ! -f "$FIRMWARE" ]; then
  cat >&2 <<EOF
error: firmware file not found: $FIRMWARE

Download the .efu for this printer from Epson's support site, drop it in the
repo root as FWF729TL.efu, or pass its path as the first argument.
EOF
  exit 1
fi

TOOLKIT_ARG=()
if [ -n "${EPSON_REVERSING:-}" ]; then
  TOOLKIT_ARG=(--toolkit "$EPSON_REVERSING")
fi

exec python3 "$REPO_ROOT/extract_firmware.py" \
  --out "$REPO_ROOT" \
  ${TOOLKIT_ARG[@]+"${TOOLKIT_ARG[@]}"} \
  "$FIRMWARE"
