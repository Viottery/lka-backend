"""Build a private Windows x64 portable bundle from installed native applications.

Run with the backend's Windows Python. No downloads or usage databases are copied.
Secrets are deliberately retained; keep the generated archive private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
import zipfile

SKIP = {'__pycache__', '.pytest_cache', '.ruff_cache', '.git', 'tests', 'test', 'evals'}


def copy_tree(source: Path, target: Path, extra_skip=()):
    if not source.is_dir():
        raise FileNotFoundError(source)
    ignored = SKIP | set(extra_skip)
    def ignore(_directory, names):
        return [name for name in names if name in ignored or name.endswith(('.pyc', '.pyo'))]
    shutil.copytree(source, target, ignore=ignore, dirs_exist_ok=True, copy_function=shutil.copyfile)


def copy_file(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def json_write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def toml_value(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    raise TypeError(type(value).__name__)


def toml_dump(data):
    lines = []
    def emit(table, prefix=()):
        if prefix:
            lines.extend(['', '[' + '.'.join(prefix) + ']'])
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(key + ' = ' + toml_value(value))
        for key, value in table.items():
            if isinstance(value, dict):
                emit(value, (*prefix, key))
    emit(data)
    return '\n'.join(lines).lstrip() + '\n'


def set_env(text, key, value):
    line = key + '=' + value
    pattern = re.compile(r'^\s*(?:export\s+)?' + re.escape(key) + r'\s*=.*$', re.M)
    return pattern.sub(lambda _: line, text) if pattern.search(text) else text.rstrip() + '\n' + line + '\n'


def package_python_env(source: Path, destination: Path, frontend=False):
    site = source / '.venv/Lib/site-packages'
    skip = [p.name for p in site.iterdir() if p.name.lower().startswith(
        ('pytest', '_pytest', 'ruff', 'pyqt6') if frontend else ('pytest', '_pytest', 'ruff'))]
    copy_tree(site, destination / '.venv/Lib/site-packages', skip)
    for name in ('python.exe', 'pythonw.exe'):
        exe = source / '.venv/Scripts' / name
        if exe.exists():
            copy_file(exe, destination / '.venv/Scripts' / name)
    # Recreated from the actual extraction path by bootstrap.py, never the source path.
    (destination / '.venv/pyvenv.cfg').write_text('include-system-site-packages = false\n', encoding='utf-8')
    target_site = destination / '.venv/Lib/site-packages'
    for pth in target_site.glob('*lka_backend*.pth'):
        pth.write_text('../../../\n', encoding='utf-8')
    for direct in target_site.glob('*.dist-info/direct_url.json'):
        direct.unlink()
    packages = []
    for metadata in target_site.glob('*.dist-info/METADATA'):
        content = metadata.read_text(encoding='utf-8', errors='replace')
        name = re.search(r'^Name: (.+)$', content, re.M)
        version = re.search(r'^Version: (.+)$', content, re.M)
        if name and version:
            packages.append({'name': name[1], 'version': version[1]})
    return sorted(packages, key=lambda item: item['name'].lower())


def read_global_settings(backend: Path):
    result = {'ui_defaults': None, 'memory_learning_enabled': None, 'background_overrides': {}}
    db = backend / 'data/runtime/lka.sqlite3'
    if not db.exists():
        return result
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
        if 'agent_ui_preferences' in tables:
            row = conn.execute('select preferences from agent_ui_preferences where id=1').fetchone()
            if row:
                result['ui_defaults'] = json.loads(row[0])
                result['ui_defaults']['workspace_parent'] = ''
        if 'memory_background_settings' in tables:
            row = conn.execute('select overrides from memory_background_settings where id=1').fetchone()
            if row:
                result['background_overrides'] = json.loads(row[0])
        if 'memory_learning_policies' in tables:
            row = conn.execute("select enabled from memory_learning_policies where scope='global' and scope_id='global'").fetchone()
            if row:
                result['memory_learning_enabled'] = bool(row[0])
    return result


def browser_preferences():
    cache = Path(os.environ['APPDATA']) / 'java/webview/localstorage/http_127.0.0.1_8780.localstorage'
    keys = {'agentic-rag-pet-agent-settings', 'agentic-rag-pet-run-settings-v1', 'agentic-rag-workbench-panel-sizes-v1'}
    result = {}
    if cache.exists():
        with sqlite3.connect(cache.as_uri() + '?mode=ro', uri=True) as conn:
            for key in keys:
                row = conn.execute('select value from ItemTable where key=?', (key,)).fetchone()
                if not row:
                    continue
                value = bytes(row[0]).decode('utf-16le') if isinstance(row[0], bytes) else row[0]
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    for field in ('workspace', 'workspaceParent', 'workspace_parent', 'cwd'):
                        if field in parsed:
                            parsed[field] = ''
                    result[key] = json.dumps(parsed, ensure_ascii=False)
    return result


def main():
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise SystemExit('Build using native Windows x64 Python.')
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('backend-root', 'frontend-root', 'python-home', 'java-home', 'output-directory'):
        parser.add_argument('--' + key, required=True, type=Path)
    parser.add_argument('--mail-config', type=Path)
    parser.add_argument('--mail-env', type=Path)
    parser.add_argument('--mail-source-root', type=Path)
    parser.add_argument('--model-cache', type=Path)
    parser.add_argument('--name', default='LKA-Windows-x64-20261004')
    parser.add_argument('--skip-zip', action='store_true')
    args = parser.parse_args()
    backend, frontend = args.backend_root.resolve(), args.frontend_root.resolve()
    output = args.output_directory.resolve()
    bundle = output / args.name
    if bundle.exists() or (output / (args.name + '.zip')).exists():
        raise SystemExit('Output already exists; choose a new name instead of overwriting it.')
    bundle.mkdir(parents=True)
    backend_out, frontend_out = bundle / 'backend', bundle / 'frontend'
    templates = Path(__file__).resolve().parent / 'windows_portable'
    print('Copying application and configuration (excluding usage data)...', flush=True)
    for source, target in ((backend, backend_out), (frontend, frontend_out)):
        copy_tree(source / 'app', target / 'app')
        copy_file(source / '.env', target / '.env')
    for name in ('AGENTS.md', 'README.md', 'pyproject.toml', 'uv.lock'):
        copy_file(backend / name, backend_out / name)
    copy_file(backend / 'scripts/start_backend.py', backend_out / 'scripts/start_backend.py')
    copy_tree(backend / 'config', backend_out / 'config', ('cache', 'secrets'))
    # Read only global preferences, never copy SQLite files or project-specific policies.
    preferences = read_global_settings(backend)
    config = tomllib.loads((backend / 'config/local.toml').read_text(encoding='utf-8'))
    for section, overrides in preferences.pop('background_overrides').items():
        config.setdefault(section, {}).update(overrides)
    mail_source = args.mail_source_root or (args.mail_config.parent.parent if args.mail_config else backend)
    if args.mail_config:
        config['mail'] = tomllib.loads(args.mail_config.read_text(encoding='utf-8'))['mail']
    outlook = config.get('mail', {}).get('outlook', {})
    if outlook.get('token_store_path'):
        token = Path(outlook['token_store_path']).expanduser()
        if not token.is_absolute():
            token = mail_source / token
        if not token.exists() and outlook.get('enabled'):
            raise FileNotFoundError('Enabled Outlook account has no stored authorization token; authenticate before building.')
        if token.exists():
            copy_file(token, backend_out / 'config/secrets/outlook_token.json')
        outlook['token_store_path'] = './config/secrets/outlook_token.json'
    env = (backend_out / '.env').read_text(encoding='utf-8')
    if args.mail_env:
        from dotenv import dotenv_values
        mail_env = dotenv_values(args.mail_env)
        for section in config.get('mail', {}).values():
            if isinstance(section, dict):
                for key, value in section.items():
                    if key.endswith('_env') and value in mail_env and mail_env[value] is not None:
                        # JSON string quoting is supported by python-dotenv, including escapes.
                        env = set_env(env, value, json.dumps(mail_env[value], ensure_ascii=False))
    for key, value in {'LKA_DATA_DIR': './data/runtime', 'LKA_LOCAL_CONFIG': './config/local.toml',
                       'LKA_WORKSPACE_ROOTS': '', 'LKA_PLATFORM': 'windows', 'LKA_DEFAULT_SHELL': 'auto'}.items():
        env = set_env(env, key, value)
    (backend_out / '.env').write_text(env, encoding='utf-8')
    if args.model_cache:
        print('Copying configured local models (materializing snapshot links, excluding duplicate blobs)...', flush=True)
        copy_tree(args.model_cache, bundle / 'models', ('blobs', '.locks', 'tmp', 'CACHEDIR.TAG'))
        for section in ('embedding', 'reranker'):
            if section in config:
                config[section]['cache_dir'] = '../models'
    (backend_out / 'config/local.toml').write_text(toml_dump(config), encoding='utf-8')
    # Instructions are application configuration, not learned memory or project history.
    for relative in ('instructions/AGENTS.md', 'instructions/watches/AGENTS.md'):
        source = backend / 'data/runtime' / relative
        if source.exists():
            copy_file(source, backend_out / 'data/runtime' / relative)
    json_write(bundle / 'settings/global-preferences.json', preferences)
    prefs = browser_preferences()
    json_write(bundle / 'settings/browser-preferences.json', prefs)
    # Bootstrap only application preference keys; never import session/history storage.
    js = '(function(){var prefs=' + json.dumps(prefs, ensure_ascii=False) + ';Object.keys(prefs).forEach(function(k){try{if(localStorage.getItem(k)===null)localStorage.setItem(k,prefs[k]);}catch(e){}});})();\n'
    (frontend_out / 'app/web/pet/portable-preferences.js').write_text(js, encoding='utf-8')
    html = frontend_out / 'app/web/pet/chat.html'
    html.write_text(html.read_text(encoding='utf-8').replace('<script src="./chat.js', '<script src="./portable-preferences.js"></script>\n  <script src="./chat.js'), encoding='utf-8')
    panel_html = frontend_out / 'app/web/pet/index.html'
    panel_html.write_text(panel_html.read_text(encoding='utf-8').replace('<head>', '<head>\n  <script src="./portable-preferences.js"></script>'), encoding='utf-8')
    copy_file(frontend / 'data/pet/profiles.json', frontend_out / 'data/pet/profiles.json')
    state = json.loads((frontend / 'data/pet/state.json').read_text(encoding='utf-8'))
    json_write(frontend_out / 'data/pet/state.json', {key: state[key] for key in ('active_profile_id', 'manual_mode', 'transparent_mode') if key in state})
    copy_file(frontend / 'run-lka-native-windows.ps1', frontend_out / 'run-lka-native-windows.ps1')
    for name in ('Start-LKA.ps1', 'Stop-LKA.ps1', 'README.txt'):
        copy_file(templates / name, bundle / name)
    copy_file(templates / 'bootstrap.py', bundle / 'scripts/bootstrap.py')
    copy_file(templates / 'start-desktop-pet-java.ps1', frontend_out / 'scripts/start-desktop-pet-java.ps1')
    for action in ('Start', 'Stop'):
        (bundle / (action + '-LKA.cmd')).write_bytes(('@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0' + action + '-LKA.ps1" %*\r\nset "LKA_EXIT=%ERRORLEVEL%"\r\nif not "%LKA_EXIT%"=="0" pause\r\nexit /b %LKA_EXIT%\r\n').encode('ascii'))
    (bundle / 'Verify-Package.cmd').write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\n"%~dp0runtime\\python\\python.exe" -I "%~dp0scripts\\bootstrap.py" verify\r\npause\r\n')
    # Optional external QQ integration configuration only; no QQ account/client history.
    for config_dir in (frontend / '.runtime/napcat/v4.18.28/config', frontend / '.runtime/snowluma/config'):
        if config_dir.is_dir():
            for file in config_dir.glob('*.json'):
                copy_file(file, bundle / 'settings/external-qq' / config_dir.parent.name / file.name)
    print('Copying native Python and isolated dependency environments...', flush=True)
    py = bundle / 'runtime/python'
    copy_tree(args.python_home, py, ('site-packages', 'Scripts', 'Doc', 'include', 'libs', 'tcl'))
    # ONNX Runtime imports the MSVC C++ runtime, which is not guaranteed on a clean PC.
    system_dlls = Path(os.environ['SystemRoot']) / 'System32'
    for pattern in ('msvcp140*.dll', 'vcruntime140*.dll', 'concrt140.dll', 'vcomp140.dll'):
        for dll in system_dlls.glob(pattern):
            copy_file(dll, py / dll.name)
    for required in ('msvcp140.dll', 'msvcp140_1.dll'):
        if not (py / required).exists():
            raise FileNotFoundError('Required redistributable DLL missing: ' + required)
    packages = {'backend': package_python_env(backend, backend_out),
                'frontend': package_python_env(frontend, frontend_out, frontend=True)}
    json_write(bundle / 'settings/python-packages.json', packages)
    print('Copying Java runtime, compiled desktop pet and native jars...', flush=True)
    copy_tree(args.java_home, bundle / 'runtime/java', ('jmods', 'include', 'src.zip', 'demo', 'sample'))
    # JavaFX WebKit also imports MSVCP140_2; ship the C++ runtime beside java.exe.
    for pattern in ('msvcp140*.dll', 'vcruntime140*.dll', 'concrt140.dll', 'vcomp140.dll'):
        for dll in system_dlls.glob(pattern):
            copy_file(dll, bundle / 'runtime/java/bin' / dll.name)
    if not (bundle / 'runtime/java/bin/msvcp140_2.dll').exists():
        raise FileNotFoundError('JavaFX requires msvcp140_2.dll')
    for relative in ('build/classes/java/main', 'build/resources/main'):
        copy_tree(frontend / 'desktop-pet-java' / relative, frontend_out / 'desktop-pet-java' / relative)
    gradle = Path.home() / '.gradle/caches/modules-2/files-2.1'
    jars = ['gdx-1.11.0.jar', 'gdx-backend-lwjgl3-1.11.0.jar', 'gdx-jnigen-loader-2.3.1.jar',
            'gdx-platform-1.11.0-natives-desktop.jar', 'jlayer-1.0.1-gdx.jar', 'spine-libgdx-3.5.51.1.jar',
            'jsr305-3.0.2.jar', 'gson-2.11.0.jar', 'error_prone_annotations-2.27.0.jar',
            'jna-5.14.0.jar', 'jna-platform-5.14.0.jar', 'jorbis-0.0.17.jar']
    for suffix in ('', '-glfw', '-jemalloc', '-openal', '-opengl', '-stb'):
        jars += [f'lwjgl{suffix}-3.3.1.jar', f'lwjgl{suffix}-3.3.1-natives-windows.jar']
    jars += [f'javafx-{module}-21.0.5-win.jar' for module in ('base', 'controls', 'graphics', 'media', 'swing', 'web')]
    for name in jars:
        matches = list(gradle.glob('*/*/*/*/' + name))
        if not matches:
            raise FileNotFoundError('Missing required Java runtime jar: ' + name)
        copy_file(matches[0], frontend_out / 'desktop-pet-java/lib' / name)
    print('Writing manifest and checking excluded data...', flush=True)
    for base in (backend_out / 'data', frontend_out / 'data'):
        for path in base.rglob('*'):
            if path.is_file() and not any(part in {'instructions', 'pet'} for part in path.relative_to(base).parts):
                raise ValueError('Unexpected usage data in package: ' + str(path.relative_to(bundle)))
    files = {}
    for file in sorted(bundle.rglob('*')):
        if file.is_file():
            with file.open('rb') as stream:
                files[file.relative_to(bundle).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    json_write(bundle / 'manifest.json', {'format': 1, 'built_at': datetime.now(timezone.utc).isoformat(),
        'platform': 'Windows x64', 'usage_data_included': False, 'contains_private_credentials': True, 'files': files})
    if not args.skip_zip:
        archive = output / (args.name + '.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for file in sorted(bundle.rglob('*')):
                if file.is_file():
                    zf.write(file, Path(args.name) / file.relative_to(bundle))
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        (output / (archive.name + '.sha256')).write_text(digest + '  ' + archive.name + '\n', encoding='ascii')
        print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': digest}), flush=True)
    print(json.dumps({'bundle': str(bundle), 'files': len(files), 'status': 'built'}), flush=True)


if __name__ == '__main__':
    main()
