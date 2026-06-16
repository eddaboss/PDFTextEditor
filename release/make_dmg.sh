#!/usr/bin/env bash
# Wrap a built .app into a compressed DMG with a drag-to-Applications symlink.
# Usage: make_dmg.sh <app_path> <out_dmg> [volname]
set -euo pipefail
APP="$1"
OUT="$2"
VOLNAME="${3:-PDF Text Editor}"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$OUT"
hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null
rm -rf "$STAGE"
echo "wrote $OUT"
