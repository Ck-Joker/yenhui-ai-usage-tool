"""檢查分享包是否夾帶個人資料，包含壓縮的 Python 程式內容。"""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import re
import zipfile


def check_bytes(data, label):
    for needle in [b'ck-joker', b'/Users/', b'BEGIN PRIVATE KEY', b'BEGIN RSA PRIVATE KEY']:
        if needle in data:
            raise ValueError('禁止的個人路徑或私密內容：' + label)
    if re.search(rb'sk-(?:ant-|proj-)?[A-Za-z0-9_-]{30,}', data):
        raise ValueError('疑似 API 憑證：' + label)


def audit(app):
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    forbidden = {'auth.json', '.credentials.json', '.env', '.claude', '.codex', '.ssh',
                 'cookies', 'cookies.sqlite', 'claude.json', 'codex.json', '.DS_Store'}
    allowed = {'Info.plist', '_CodeSignature', 'MacOS', 'Resources'}
    if {p.name for p in (app / 'Contents').iterdir()} - allowed:
        raise ValueError('App 頂層不在白名單內')
    resources = app / 'Contents/Resources'
    if {p.name for p in resources.iterdir()} != {'usage-helper', 'codex.png', 'claude.png',
                                                'yenhui-mark.svg', 'GettingStarted.html', 'Licenses'}:
        raise ValueError('資源白名單不符')
    manifest = []
    for path in sorted(app.rglob('*')):
        relative = path.relative_to(app).as_posix()
        if any(part in forbidden or part.endswith(('.keychain', '.keychain-db')) for part in path.relative_to(app).parts):
            raise ValueError('禁止的檔案：' + relative)
        if path.is_symlink() and not path.resolve().is_relative_to(app.resolve()):
            raise ValueError('App 內有指向外部的連結：' + relative)
        if not path.is_file():
            continue
        data = path.read_bytes()
        check_bytes(data, relative)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    check_bytes(archive.read(name), relative + ':' + name)
        manifest.append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    executable = resources / 'usage-helper/usage-helper'
    archive = CArchiveReader(str(executable))
    checked_code = []
    for name in archive.toc:
        data = archive.extract(name)
        if data:
            check_bytes(data, 'embedded:' + name)
            checked_code.append(name)
        if name.endswith('.pyz'):
            python_archive = archive.open_embedded_archive(name)
            for module in python_archive.toc:
                code = python_archive.extract(module)
                if code is not None:
                    check_bytes(marshal.dumps(code), 'embedded-python:' + module)
    for path in resources.rglob('*.pyz'):
        archive = ZlibArchiveReader(str(path))
        for name in archive.toc:
            code = archive.extract(name)
            if code is not None:
                check_bytes(marshal.dumps(code), 'python:' + name)
    return {'status': 'passed', 'files': manifest, 'embedded_entries': checked_code,
            'credentials_read_for_audit': False, 'personal_paths_found': 0,
            'private_login_files_found': 0, 'notarized': False, 'architecture': 'arm64'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.app)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print('Package privacy audit passed:', len(result['files']), 'files; no user credential source read')
