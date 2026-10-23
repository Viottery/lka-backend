"""Bootstrap the first trusted administrator through a local terminal."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.domains.accounts import AccountError, AccountService
from app.storage.db import connect, get_db_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin", help="Create the first administrator; all accounts share personal data")
    create.add_argument("username")
    args = parser.parse_args(argv)
    settings = Settings()
    config = settings.load_local_config().auth
    if not config.enabled:
        parser.error("Enable [auth] before bootstrapping an administrator")
    service = AccountService(lambda: connect(get_db_path(settings.data_dir)), config)
    password = getpass.getpass("Password (12..256 characters): ")
    repeated = getpass.getpass("Repeat password: ")
    if password != repeated:
        parser.error("Passwords do not match")
    try:
        service.create_admin(args.username, password)
    except AccountError as exc:
        parser.error(str(exc))
    print("Administrator created. Additional trusted administrators can be managed after sign-in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
