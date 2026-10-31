"""Run explicit offline pytest targets without loading personal configuration.

Example: .venv/bin/python scripts/run_isolated_tests.py tests/test_event_dispatch.py
Use --source-root before the targets to validate a frozen source snapshot.
Opt-in provider/MySQL tests remain disabled unless their own fixtures configure
an isolated target. For real MySQL use run_mysql_tests.py instead.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.pytest_args:
        parser.error('explicit offline pytest targets are required')
    source = args.source_root.expanduser().resolve(strict=True)
    if not (source / 'app/core/config.py').is_file():
        parser.error('source root must be an LKA backend checkout')
    for key in list(os.environ):
        if key.startswith(('LKA_', 'OPENAI_', 'ANTHROPIC_', 'MS_GRAPH_', 'MAIL_', 'IMAP_', 'SMTP_')):
            os.environ.pop(key, None)
    sys.path.insert(0, str(source))
    os.chdir(source)
    with tempfile.TemporaryDirectory(prefix='lka-isolated-tests-') as temporary:
        temporary = Path(temporary)
        config = temporary / 'mock.toml'
        config.write_text('[llm]\nprovider="mock"\n', encoding='utf-8')
        os.environ.update(LKA_DATA_DIR=str(temporary / 'import-data'), LKA_LOCAL_CONFIG=str(config))
        from app.core.config import Settings, get_settings
        Settings.model_config['env_file'] = None
        get_settings.cache_clear()
        assert Settings().data_dir == temporary / 'import-data'
        import app
        assert Path(app.__file__).resolve().is_relative_to(source)
        import pytest
        targets = args.pytest_args
        if targets[0] == '--':
            targets = targets[1:]
        print('Isolated source:', source, flush=True)
        return pytest.main(['-q', '-o', 'faulthandler_timeout=30', *targets])


if __name__ == '__main__':
    raise SystemExit(main())
