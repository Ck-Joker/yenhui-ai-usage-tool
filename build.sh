#!/bin/bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SOURCE_DIR/build/Subscription Pin.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
/usr/bin/swiftc -O -target arm64-apple-macos13.0 -framework AppKit "$SOURCE_DIR/main.swift" "$SOURCE_DIR/SetupWindow.swift" -o "$APP_DIR/Contents/MacOS/SubscriptionPin"
cp "$SOURCE_DIR/usage.py" "$APP_DIR/Contents/Resources/usage.py"
cp "$SOURCE_DIR/assets/codex.png" "$SOURCE_DIR/assets/claude.png" "$APP_DIR/Contents/Resources/"
cp "$SOURCE_DIR/assets/yenhui-mark.svg" "$APP_DIR/Contents/Resources/"
/usr/bin/python3 - "$APP_DIR/Contents/Info.plist" <<'PY'
import plistlib, sys
with open(sys.argv[1], 'wb') as output:
    plistlib.dump({
        'CFBundleIdentifier': 'tw.ckc.subscription-pin',
        'CFBundleName': 'Subscription Pin',
        'CFBundleDisplayName': 'Subscription Pin',
        'CFBundleExecutable': 'SubscriptionPin',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': '1.1.2',
        'CFBundleVersion': '4',
        'LSMinimumSystemVersion': '13.0',
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
    }, output)
PY
/usr/bin/codesign --force --sign - "$APP_DIR"
printf '%s\n' "$APP_DIR"
