#!/usr/bin/env python3
"""從明確來源白名單建立 DMG，不讀取使用者登入或執行中的 App 資料。"""
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
VERSION = '1.1.2'


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def main():
    if sys.version_info[:2] != (3, 13):
        raise SystemExit('請使用 Python 3.13 的獨立建置環境，並安裝 PyInstaller 與 certifi。')
    root = Path(tempfile.mkdtemp(prefix='subscription-pin-release-', dir='/tmp'))
    inputs = root / 'source'
    inputs.mkdir()
    for name in ['main.swift', 'SetupWindow.swift', 'usage.py']:
        shutil.copyfile(SOURCE / name, inputs / name)
    volume = root / 'volume'
    app = volume / 'Subscription Pin.app'
    contents = app / 'Contents'
    resources = contents / 'Resources'
    (contents / 'MacOS').mkdir(parents=True)
    resources.mkdir()
    # 編譯從乾淨 /tmp 來源，避免編譯器寫入開發者的使用者路徑。
    run('/usr/bin/swiftc', '-O', '-target', 'arm64-apple-macos13.0', '-framework', 'AppKit',
        'main.swift', 'SetupWindow.swift', '-o', contents / 'MacOS/SubscriptionPin', cwd=inputs)
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'usage-helper',
        '--target-arch', 'arm64', '--collect-data', 'certifi', '--optimize', '1',
        '--distpath', root / 'runtime', '--workpath', root / 'work', '--specpath', root / 'spec',
        'usage.py', cwd=inputs)
    shutil.copytree(root / 'runtime/usage-helper', resources / 'usage-helper', symlinks=True)
    for name in ['codex.png', 'claude.png', 'yenhui-mark.svg']:
        shutil.copyfile(SOURCE / 'assets' / name, resources / name)
    guide = (SOURCE / 'GettingStarted.html').read_text().replace('BRAND_IMAGE',
        base64.b64encode((SOURCE / 'assets/yenhui-mark.svg').read_bytes()).decode())
    (resources / 'GettingStarted.html').write_text(guide)
    (volume / '安裝與登入指南.html').write_text(guide)
    (volume / 'Applications').symlink_to('/Applications')
    info = {'CFBundleIdentifier': 'tw.ckc.subscription-pin', 'CFBundleName': 'Subscription Pin',
            'CFBundleDisplayName': 'Subscription Pin', 'CFBundleExecutable': 'SubscriptionPin',
            'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': VERSION, 'CFBundleVersion': '4',
            'LSMinimumSystemVersion': '13.0', 'LSUIElement': True, 'NSHighResolutionCapable': True,
            'NSHumanReadableCopyright': '言回有限公司開發'}
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    licenses = resources / 'Licenses'
    licenses.mkdir()
    shutil.copyfile(Path(sys.base_prefix) / 'lib/python3.13/LICENSE.txt', licenses / 'Python.txt')
    shutil.copytree(SOURCE / 'vendor-licenses', licenses / 'Python-runtime', dirs_exist_ok=True)
    for name in ['certifi', 'pyinstaller']:
        distribution = importlib.metadata.distribution(name)
        for entry in distribution.files or []:
            if '.dist-info/' in str(entry) and Path(entry).name.upper().startswith(('LICENSE', 'COPYING')):
                shutil.copyfile(distribution.locate_file(entry), licenses / (name + '-' + Path(entry).name))
    (licenses / 'NOTICE.txt').write_text('Subscription Pin：言回有限公司開發。\nPython 與執行元件授權見本目錄。\n'
        'Codex／Claude 圖示與名稱屬各權利人，用於辨識對應服務；本工具並非 OpenAI 或 Anthropic 官方產品。\n'
        'Mozilla CA 憑證由 certifi 提供，MPL 2.0：https://www.mozilla.org/MPL/2.0/\n'
        'Python standalone runtime：https://github.com/astral-sh/python-build-standalone\n')
    run('/usr/bin/codesign', '--force', '--deep', '--sign', '-', app)
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', app)
    run(resources / 'usage-helper/usage-helper', '--self-check')
    # 稽核時只掃描封裝成品，絕不讀取真正憑證來作比較。
    run(sys.executable, SOURCE / 'audit_package.py', app, '--report', root / 'audit.json')
    destination = SOURCE / 'dist'
    destination.mkdir(exist_ok=True)
    dmg = destination / ('Subscription-Pin-' + VERSION + '-AppleSilicon.dmg')
    if dmg.exists():
        raise SystemExit('目的 DMG 已存在，請先保留舊版再重建：' + str(dmg))
    run('/usr/bin/hdiutil', 'create', '-volname', 'Subscription Pin', '-srcfolder', volume,
        '-format', 'UDZO', '-imagekey', 'zlib-level=9', dmg)
    checksum = hashlib.sha256(dmg.read_bytes()).hexdigest()
    (destination / (dmg.name + '.sha256')).write_text(checksum + '  ' + dmg.name + '\n')
    shutil.copyfile(root / 'audit.json', destination / 'package-audit.json')
    shutil.copyfile(volume / '安裝與登入指南.html', destination / '安裝與登入指南.html')
    (destination / 'build-location.txt').write_text(str(app) + '\n')
    print(json.dumps({'dmg': str(dmg), 'app': str(app), 'sha256': checksum}, ensure_ascii=False))


if __name__ == '__main__':
    main()
