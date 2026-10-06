"""Relocate bundled Python and import configuration without importing usage data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise RuntimeError('This package requires Windows x64.')
    home = ROOT / 'runtime/python'
    if not (home / 'python312.dll').is_file():
        raise RuntimeError('Bundled Python is incomplete. Extract the entire archive first.')
    for name in ('backend', 'frontend'):
        venv = ROOT / name / '.venv'
        if not (venv / 'Scripts/python.exe').is_file():
            raise RuntimeError('Missing bundled interpreter: ' + name)
        (venv / 'pyvenv.cfg').write_text(
            'home = ' + str(home) + '\ninclude-system-site-packages = false\nversion = 3.12.0\n',
            encoding='utf-8',
        )
        for pth in (venv / 'Lib/site-packages').glob('*lka_backend*.pth'):
            pth.write_text('../../../\n', encoding='utf-8')
    (ROOT / 'workspaces').mkdir(exist_ok=True)
    print('Portable Python paths prepared.')


def seed_config(base_url):
    marker = ROOT / 'frontend/.runtime/portable-config-imported.json'
    if marker.exists():
        return
    saved = json.loads((ROOT / 'settings/global-preferences.json').read_text(encoding='utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(path, method='GET', body=None):
        raw = json.dumps(body).encode('utf-8') if body is not None else None
        request = urllib.request.Request(base_url.rstrip('/') + path, data=raw, method=method,
                                         headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=15) as response:
            return json.load(response)
    defaults = saved.get('ui_defaults')
    if defaults is not None and not call('/agent/ui-defaults').get('configured'):
        defaults['workspace_parent'] = str(ROOT / 'workspaces')
        call('/agent/ui-defaults', 'PUT', defaults)
    enabled = saved.get('memory_learning_enabled')
    if enabled is not None:
        call('/memories/learning', 'PUT', {'scope': 'global', 'enabled': enabled})
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"configuration_imported":true}\n', encoding='utf-8')
    print('Global preferences imported; no history, messages or learned memories imported.')


def verify():
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    failed = []
    for relative, expected in manifest['files'].items():
        path = ROOT / relative
        if not path.is_file():
            failed.append(relative)
            continue
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                failed.append(relative)
    if failed:
        print('Files missing or changed (configuration may change after first launch):')
        for relative in failed:
            print(relative)
        raise RuntimeError(str(len(failed)) + ' files differ from the original package.')
    print('Verified ' + str(len(manifest['files'])) + ' packaged files. All SHA256 hashes match.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'seed-config', 'verify'])
    parser.add_argument('--backend-url', default='http://127.0.0.1:8765')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'seed-config':
        seed_config(args.backend_url)
    else:
        verify()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
