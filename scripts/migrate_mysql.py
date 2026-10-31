"""Install versioned MySQL schema using a separate migration credential file.

No application data is copied. Keep migration credentials out of the backend
service environment; the running account does not need SUPER or global trust.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.storage.mysql import MySQLConfig, connect_mysql
    from app.storage.mysql_bootstrap import bootstrap_mysql
    from app.storage.mysql_owner import coordinator_lock_name

    config = MySQLConfig(**json.loads(args.connection_file.expanduser().resolve(strict=True).read_text()))
    with connect_mysql(config) as connection:
        lock = connection.execute_native("SELECT GET_LOCK(%s,0)",
                                         (coordinator_lock_name(config.database),)).fetchone()
        if not lock or lock[0] != 1:
            raise SystemExit("Stop the active MySQL coordinator before applying schema migrations")
        try:
            bootstrap_mysql(connection, install=True)
            assert not connection.in_transaction
            from app.core.agent_checkpoints import create_mysql_checkpoint_runtime

            checkpoints = create_mysql_checkpoint_runtime(config)
            try:
                checkpoints.setup()
            finally:
                checkpoints.close()
        finally:
            connection.execute_native("SELECT RELEASE_LOCK(%s)",
                                      (coordinator_lock_name(config.database),))
    print("MySQL business and checkpoint schema migration completed; application rows unchanged.")


if __name__ == "__main__":
    main()
