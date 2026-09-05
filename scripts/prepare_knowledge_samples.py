"""Create ignored local Markdown samples from public pages and import them.

The knowledge store treats every generated file as a local document.  Original
URLs are retained only as provenance metadata, not as a knowledge source type.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.domains.knowledge import KnowledgeService  # noqa: E402
from app.integrations.public_document_snapshot import (  # noqa: E402
    extract_article_snapshot,
    fetch_public_html,
)
from app.storage.db import connect, get_db_path, init_db  # noqa: E402


@dataclass(frozen=True)
class SampleSpec:
    url: str
    collection: str
    section: str
    slug: str


DEFAULT_SAMPLES = [
    SampleSpec(
        url="https://zh.minecraft.wiki/w/Minecraft_Wiki",
        collection="minecraft",
        section="overview",
        slug="minecraft_wiki",
    ),
    SampleSpec(
        url="https://zh.minecraft.wiki/w/%E8%8B%A6%E5%8A%9B%E6%80%95",
        collection="minecraft",
        section="mobs",
        slug="creeper",
    ),
    SampleSpec(
        url="https://zh.minecraft.wiki/w/%E5%83%B5%E5%B0%B8",
        collection="minecraft",
        section="mobs",
        slug="zombie",
    ),
    SampleSpec(
        url="https://zh.minecraft.wiki/w/%E6%9C%AB%E5%BD%B1%E4%BA%BA",
        collection="minecraft",
        section="mobs",
        slug="enderman",
    ),
    SampleSpec(
        url="https://prts.wiki",
        collection="arknights",
        section="overview",
        slug="prts_wiki",
    ),
    SampleSpec(
        url="https://prts.wiki/w/%E9%98%BF%E7%B1%B3%E5%A8%85",
        collection="arknights",
        section="operators",
        slug="amiya",
    ),
    SampleSpec(
        url="https://prts.wiki/w/%E8%83%BD%E5%A4%A9%E4%BD%BF",
        collection="arknights",
        section="operators",
        slug="exusiai",
    ),
    SampleSpec(
        url="https://prts.wiki/w/%E9%93%B6%E7%81%B0",
        collection="arknights",
        section="operators",
        slug="silverash",
    ),
]


def _safe_filename(url: str) -> str:
    parsed = urlparse(url)
    raw = f"{parsed.netloc}{parsed.path}".strip("/") or parsed.netloc
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_") or "document"


def _write_snapshot(
    *,
    output_dir: Path,
    sample: SampleSpec,
    title: str,
    text: str,
) -> Path:
    document_dir = output_dir / sample.collection / sample.section
    document_dir.mkdir(parents=True, exist_ok=True)
    document_path = document_dir / f"{sample.slug}.md"
    metadata_path = document_dir / f"{sample.slug}.metadata.json"
    fetched_at = datetime.now(timezone.utc).isoformat()
    document_path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "original_url": sample.url,
                "fetched_at": fetched_at,
                "content_sha256": sha256(text.encode("utf-8")).hexdigest(),
                "ingestion_kind": "public_page_snapshot",
                "collection": sample.collection,
                "section": sample.section,
                "untrusted_data": True,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return document_path


def _import_snapshot(*, service: KnowledgeService, document_path: Path) -> dict[str, object]:
    metadata_path = document_path.with_suffix(".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    result = service.import_text_file(
        path=document_path,
        source_type="local_document",
        sensitivity="public",
        remote_policy="allow",
        metadata=metadata,
    )
    return result.model_dump(mode="json")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create local knowledge samples from public pages and import them into SQLite.",
    )
    parser.add_argument("--url", action="append", dest="urls", help="Public page URL; repeatable.")
    parser.add_argument(
        "--collection",
        default="custom",
        help="Collection for custom --url values (default: custom).",
    )
    parser.add_argument(
        "--section",
        default="inbox",
        help="Section for custom --url values (default: inbox).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "data" / "knowledge_samples",
        help="Ignored directory for Markdown snapshots and metadata.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "data",
        help="Directory containing lka.sqlite3.",
    )
    parser.add_argument(
        "--skip-import",
        action="store_true",
        help="Only create local snapshot files; do not write to SQLite.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    samples = (
        [
            SampleSpec(
                url=url,
                collection=args.collection,
                section=args.section,
                slug=_safe_filename(url),
            )
            for url in args.urls
        ]
        if args.urls
        else DEFAULT_SAMPLES
    )
    db_path = get_db_path(args.data_dir)
    init_db(db_path)
    service = KnowledgeService(lambda: connect(db_path))

    for sample in samples:
        html = fetch_public_html(sample.url)
        title, text = extract_article_snapshot(html)
        document_path = _write_snapshot(
            output_dir=args.output_dir,
            sample=sample,
            title=title,
            text=text,
        )
        print(f"snapshot: {document_path} ({len(text)} chars)")
        if not args.skip_import:
            result = _import_snapshot(service=service, document_path=document_path)
            print(json.dumps({"import": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
