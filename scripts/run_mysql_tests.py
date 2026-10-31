"""Run opt-in MySQL tests with isolated import-time configuration and mock LLMs."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True,
                        help="Private JSON with host/port/user/password/database; never printed")
    parser.add_argument("--migration-connection-file", type=Path,
                        help="Optional separate DDL credential for synthetic trigger fixtures")
    parser.add_argument("tests", nargs="*")
    args, extra = parser.parse_known_args()
    connection_file = args.connection_file.expanduser().resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="lka-mysql-tests-") as temporary:
        temporary = Path(temporary)
        for name in list(os.environ):
            if name.startswith(("LKA_", "OPENAI_", "ANTHROPIC_", "MS_GRAPH_", "MAIL_", "IMAP_", "SMTP_")):
                os.environ.pop(name, None)
        config = temporary / "mock.toml"
        config.write_text('[llm]\nprovider="mock"\n', encoding="utf-8")
        os.environ.update(
            LKA_DATA_DIR=str(temporary / "module-import-data"),
            LKA_LOCAL_CONFIG=str(config),
            LKA_MYSQL_TEST_CONFIG=str(connection_file),
        )
        if args.migration_connection_file:
            os.environ["LKA_MYSQL_TEST_MIGRATION_CONFIG"] = str(args.migration_connection_file.expanduser().resolve(strict=True))
        from app.core.config import Settings, get_settings

        Settings.model_config["env_file"] = None
        get_settings.cache_clear()
        assert Settings().data_dir == temporary / "module-import-data", "Import data isolation failed"
        import pytest

        targets = args.tests or [str(path.relative_to(root)) for path in sorted((root / "tests").glob("test_mysql*.py"))]
        return pytest.main(["-q", "-o", "faulthandler_timeout=60", *targets, *extra])


if __name__ == "__main__":
    raise SystemExit(main())
