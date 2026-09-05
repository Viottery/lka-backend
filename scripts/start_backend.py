"""Start the API with an explicit personal or isolated test profile."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Literal


REPO_ROOT = Path(__file__).resolve().parents[1]
ProfileName = Literal["personal", "test"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start Local Knowledge Agent OS with an explicit data profile.",
    )
    parser.add_argument("profile", choices=("personal", "test"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--test-data-dir",
        type=Path,
        default=None,
        help="Persistent isolated data directory for test mode; omitted means a temporary directory.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable Uvicorn reload for personal mode only.",
    )
    return parser


def configure_profile(
    profile: ProfileName,
    *,
    test_data_dir: Path | None = None,
) -> Path:
    """Set process-local environment required by the requested startup profile."""

    if profile == "personal":
        os.environ.setdefault("LKA_DATA_DIR", str(REPO_ROOT / "data" / "runtime"))
        os.environ.setdefault("LKA_LOCAL_CONFIG", str(REPO_ROOT / "config" / "local.toml"))
        return Path(os.environ["LKA_DATA_DIR"])

    data_dir = test_data_dir or Path(tempfile.mkdtemp(prefix="lka_test_backend_"))
    os.environ["LKA_DATA_DIR"] = str(data_dir)
    # Do not allow a real local provider/mail config to be read in test mode.
    os.environ["LKA_LOCAL_CONFIG"] = str(data_dir / "missing-local.toml")
    return data_dir


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    profile: ProfileName = args.profile
    if profile == "test" and args.reload:
        raise SystemExit("--reload is unavailable in test mode; use a fresh isolated server instead.")

    temporary_context = (
        tempfile.TemporaryDirectory(prefix="lka_test_backend_")
        if profile == "test" and args.test_data_dir is None
        else nullcontext(None)
    )
    with temporary_context as temp_dir:
        data_dir = configure_profile(
            profile,
            test_data_dir=Path(temp_dir) if temp_dir is not None else args.test_data_dir,
        )
        port = args.port if args.port is not None else (8765 if profile == "personal" else 8766)
        print(f"Starting {profile} backend on http://{args.host}:{port}", flush=True)
        print(f"LKA_DATA_DIR={data_dir}", flush=True)
        if profile == "test":
            print("LKA_LOCAL_CONFIG=<isolated mock configuration>", flush=True)

        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import uvicorn

        uvicorn.run(
            "app.api.main:app",
            host=args.host,
            port=port,
            reload=args.reload,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
