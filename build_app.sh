#!/usr/bin/env bash
# Build "PDF Text Editor.app" and install it to /Applications.
# Run this again after code changes to repackage the app.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Generating app icon"
.venv/bin/python build_icon.py

echo "==> Building .app with PyInstaller (takes a few minutes)"
rm -rf build dist
# Built from PDFTextEditor.spec so the bundle carries the macOS folder-access
# usage strings (without them the Open dialog shows empty folders).
.venv/bin/pyinstaller --noconfirm PDFTextEditor.spec >/dev/null

echo "==> Installing to /Applications"
APP="PDF Text Editor.app"
if [ -w /Applications ]; then
  TARGET="/Applications"
else
  mkdir -p "$HOME/Applications"
  TARGET="$HOME/Applications"
fi
# Update IN PLACE: there is only ever ONE installed copy. The same bundle id
# (com.eddaboss.pdftexteditor) means macOS treats every rebuild as the SAME
# app -- removing the old bundle first and copying the new one over the exact
# same path guarantees we never accumulate "PDF Text Editor 2.app" etc.
# Also sweep up any copy that an earlier build left in the OTHER target dir
# (e.g. a run that fell back to ~/Applications) so the two never coexist.
OTHER="/Applications"
[ "$TARGET" = "/Applications" ] && OTHER="$HOME/Applications"
rm -rf "$OTHER/$APP" 2>/dev/null || true
rm -rf "$TARGET/$APP"
cp -R "dist/$APP" "$TARGET/"
# Locally built, but strip any quarantine so it opens on a double-click.
xattr -dr com.apple.quarantine "$TARGET/$APP" 2>/dev/null || true

# Clean up the build intermediates. The freshly built app is now installed, so
# the copy PyInstaller left in dist/ is a redundant SECOND bundle that Spotlight
# and Launchpad would index as a duplicate app. Removing build/ and dist/ leaves
# exactly one "PDF Text Editor" on the machine: the installed one.
rm -rf build dist

# Refresh LaunchServices so any stale duplicate (an old dist/ copy, a previous
# path) is dropped from Spotlight / Launchpad / the "Open With" menu, and the
# installed bundle is re-registered cleanly.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
  "$LSREGISTER" -f "$TARGET/$APP" >/dev/null 2>&1 || true
fi
echo "==> Installed: $TARGET/$APP  (one copy; build/ and dist/ cleaned up)"
