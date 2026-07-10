#!/usr/bin/env bash
# Package a signed Ba-Ge.app into a drag-and-drop .dmg (mount → drag to
# Applications). RUN ON macOS, after build-macos.sh has produced+signed the app.
#
# What this does NOT do: make the app trusted on OTHER people's Macs. That needs a
# **Developer ID Application** cert (Apple Development / Apple Distribution won't do)
# plus notarization. See the "DISTRIBUTION" note printed at the end.
set -euo pipefail
cd "$(dirname "$0")"

APP="${APP:-$HOME/Applications/Ba-Ge.app}"
[ -d "$APP" ] || APP="dist/Ba-Ge.app"
[ -d "$APP" ] || { echo "!! No Ba-Ge.app found. Run ./build-macos.sh first."; exit 1; }

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
    "$APP/Contents/Info.plist" 2>/dev/null || echo 0.0.0)"
VOL="Ba-Ge"
OUT="dist/Ba-Ge-${VERSION}.dmg"
mkdir -p dist
rm -f "$OUT"

# Stage the bundle + an /Applications alias in a NON-iCloud temp dir (iCloud xattrs
# on ~/Documents corrupt the app tree; hdiutil would bake them in).
STAGE="$(mktemp -d)/dmg"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Ba-Ge.app"
ln -s /Applications "$STAGE/Applications"

echo "==> Building $OUT from $APP (v$VERSION)"
hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDZO -quiet "$OUT"
rm -rf "$(dirname "$STAGE")"

# Sign the DMG itself if a Developer ID / Apple Development identity is around
# (a signed DMG is required before it can be notarized).
SIGN_ID="${SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
    | grep -m1 -E 'Developer ID Application|Apple Development' | sed -E 's/.*"(.*)"$/\1/')}"
if [ -n "$SIGN_ID" ]; then
    codesign --force -s "$SIGN_ID" "$OUT" && echo "==> Signed DMG with: $SIGN_ID"
fi

SIZE="$(du -h "$OUT" | cut -f1)"
cat <<EOF

Built $OUT ($SIZE).
Test locally: open "$OUT"  → drag Ba-Ge into Applications.

DISTRIBUTION to other Macs (so it opens without "unidentified developer"):
  Needs a **Developer ID Application** cert (you have Apple Development + Apple
  Distribution — neither works for direct download). Create one:
    Xcode › Settings › Accounts › Manage Certificates › + › "Developer ID Application"
  Then rebuild signed with it and notarize:
    SIGN_ID="Developer ID Application: YOUR NAME (TEAMID)" ./build-macos.sh
    SIGN_ID="Developer ID Application: YOUR NAME (TEAMID)" ./build-dmg.sh
    xcrun notarytool submit "$OUT" --apple-id <id> --team-id <TEAMID> \\
        --password <app-specific-pw> --wait
    xcrun stapler staple "$OUT"
  (app-specific password: appleid.apple.com › Sign-In & Security › App-Specific
  Passwords. Or store creds once with: xcrun notarytool store-credentials.)
EOF
