"""Web tool acquisition: current authority, scoped artifacts, and local continuation."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from typing import Any

from app.core.tools import ToolContext
from app.domains.web_cache import WebCacheError, WebCacheService
from app.domains.web_views import build_page_view, build_search_view
from app.integrations.web_search import (
    BraveSearchAdapter,
    PublicPageFetcher,
    _safe_public_https_url,
)


class WebResources:
    def __init__(self, cache: WebCacheService, *, page_max_age_seconds: int = 300,
                 preview_chars: int = 2400, snippet_chars: int = 360) -> None:
        self.cache = cache
        self.page_max_age_seconds = page_max_age_seconds
        self.preview_chars = preview_chars
        self.snippet_chars = snippet_chars

    def _authority(self, context: ToolContext, tool_name: str):
        if not context.run_id:
            raise WebCacheError("A current run is required for web snapshots.")

        def check() -> None:
            view = context.tool_view
            if view is not None:
                denial = view.denial_reason(tool_name=tool_name, package="web", read_only=True)
                if denial or (view.child_run_id is not None and view.child_run_id != context.run_id):
                    raise WebCacheError("Current ToolView does not authorize this web operation.")
            conn = self.cache.conn_factory()
            try:
                row = conn.execute("""SELECT r.status, r.record_payload, s.status session_status
                    FROM agent_runs r JOIN agent_sessions s ON s.session_id=r.session_id
                    WHERE r.run_id=? AND r.session_id=?""", (context.run_id, context.session_id)).fetchone()
            finally:
                conn.close()
            if row is None or row["session_status"] == "deleted":
                raise WebCacheError("Web run/session is unavailable.")
            record = json.loads(row["record_payload"])
            if (row["status"] in {"cancelled", "timed_out", "failed", "completed"}
                    or record.get("metadata", {}).get("cancel_requested") is True):
                raise WebCacheError("Web operation cancelled or run already finished.")
            expires = record.get("deadline_at")
            if expires and datetime.fromisoformat(expires) <= datetime.now(UTC):
                raise WebCacheError("Web run deadline expired.")
            is_child = record.get("parent_run_id") is not None
            if is_child and (view is None or view.child_run_id != context.run_id):
                raise WebCacheError("Child web cache requires its current ToolView.")

        check()
        # A restricted root cannot borrow the unrestricted root's cached references.
        view = context.tool_view
        owner = f"run:{context.run_id}" if view and view.child_run_id else f"session:{context.session_id}"
        permission = view.model_dump(mode="json") if view else None
        scope = hashlib.sha256(json.dumps([owner, permission], sort_keys=True).encode()).hexdigest()
        return scope, check

    @staticmethod
    def _max_age(inputs: dict, default: int | None) -> int | None:
        value = inputs.get("max_age_seconds", default)
        if value is not None and (type(value) is not int or not 0 <= value <= 86400):
            raise ValueError("max_age_seconds must be an integer from 0 through 86400.")
        return value

    @staticmethod
    def _check_deadline(check, deadline):
        def bounded_check():
            check()
            if time.monotonic() >= deadline:
                raise WebCacheError("Web acquisition deadline exceeded; no snapshot was published.")
        return bounded_check

    def search(self, adapter: BraveSearchAdapter, inputs: dict, context: ToolContext) -> dict:
        scope, check = self._authority(context, "web.search")
        view = inputs.get("view", "compact")
        if view not in {"compact", "full"}:
            raise ValueError("search view must be compact or full.")
        identity = inputs.get("search_id")
        if identity:
            if any(name in inputs for name in ("query", "mode", "freshness", "limit", "country", "search_lang")):
                raise ValueError("search_id reads an existing result; omit search query/filter parameters.")
            raw = self.cache.load(identity, scope_key=scope, kind="search",
                                  max_age_seconds=self._max_age(inputs, None))
        else:
            if not isinstance(inputs.get("query"), str) or not inputs["query"].strip():
                raise ValueError("query or search_id is required.")
            args = {name: inputs[name] for name in ("query", "mode", "limit", "freshness", "country", "search_lang") if name in inputs}
            key = json.dumps(args, sort_keys=True, ensure_ascii=False)

            def fetch(deadline):
                bounded = self._check_deadline(check, deadline)
                bounded()
                raw = adapter.search(**args, deadline=deadline)
                bounded()
                raw.update(queried_at=datetime.now(UTC).isoformat(), cache_hit=False,
                           evidence_scope="search_candidates_not_verified_page_content")
                return self.cache.put(scope_key=scope, run_id=context.run_id, kind="search",
                                      source_key=key, payload=raw, check=bounded)

            raw = self.cache.acquire(scope_key=scope, key=f"search:{key}", operation=fetch, check=check)
        check()
        result = dict(raw)
        result["results"] = [{**row, "ref_id": f"{raw['search_id']}:{index}"}
                             for index, row in enumerate(raw["results"])]
        return build_search_view(result, snippet_chars=self.snippet_chars) if view == "compact" else result

    def page(self, fetcher: PublicPageFetcher, inputs: dict, context: ToolContext,
             tool_name: str) -> dict:
        scope, check = self._authority(context, tool_name)
        provided = [name for name in ("url", "ref_id", "snapshot_id") if inputs.get(name)]
        if len(provided) != 1:
            raise ValueError("Provide exactly one of url, ref_id, or snapshot_id.")
        if type(inputs.get("refresh", False)) is not bool:
            raise ValueError("refresh must be boolean.")
        refresh = inputs.get("refresh", False)
        expected = inputs.get("expected_text_sha256")
        if expected is not None and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ValueError("expected_text_sha256 must be a lowercase SHA-256 hex digest.")
        max_age = self._max_age(inputs, None if inputs.get("snapshot_id") else self.page_max_age_seconds)
        if inputs.get("snapshot_id"):
            if refresh:
                raise ValueError("snapshot_id fixes a version; refresh its URL instead.")
            page = self.cache.load(inputs["snapshot_id"], scope_key=scope, kind="page",
                                   max_age_seconds=max_age)
        else:
            if inputs.get("ref_id"):
                match = re.fullmatch(r"(web_search_[0-9a-f]{32}):(\d{1,2})", inputs["ref_id"])
                if match is None:
                    raise ValueError("ref_id must be a returned search result reference.")
                search = self.cache.load(match[1], scope_key=scope, kind="search")
                index = int(match[2])
                if index >= len(search["results"]):
                    raise WebCacheError("Search result reference is unavailable.")
                url = search["results"][index]["url"]
            else:
                url = inputs["url"]
            url = _safe_public_https_url(url)
            expected = inputs.get("expected_text_sha256")
            # A continuation with an expected version must never refetch on a miss.
            page = None if refresh or max_age == 0 else self.cache.latest(
                scope_key=scope, kind="page", source_key=url,
                max_age_seconds=max_age,
            )
            if page is None and expected and not refresh:
                raise WebCacheError("Expected page version is unavailable; reopen URL at offset 0 or use snapshot_id.")
            if page is None:
                def fetch(deadline):
                    bounded = self._check_deadline(check, deadline)
                    if not refresh and max_age != 0:
                        found = self.cache.latest(scope_key=scope, kind="page", source_key=url,
                                                  max_age_seconds=max_age)
                        if found is not None:
                            return found
                    full = fetcher.read(url, deadline=deadline, check=bounded)
                    bounded()
                    full.update(snapshot_stable=True, cache_hit=False,
                                cache_scope="child_run" if context.tool_view and context.tool_view.child_run_id else "session",
                                extraction_complete=True,
                                source_scope="bounded_readable_extraction_not_full_webpage")
                    return self.cache.put(scope_key=scope, run_id=context.run_id, kind="page",
                                          source_key=url, payload=full, check=bounded)

                # Two concurrent refreshes share only that refresh, not an existing snapshot.
                page = self.cache.acquire(scope_key=scope,
                                          key=f"page:{url}:refresh={refresh}:max_age={max_age}",
                                          operation=fetch, check=check)
        expected = inputs.get("expected_text_sha256")
        if expected and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ValueError("expected_text_sha256 must be a lowercase SHA-256 hex digest.")
        if expected and expected != page["text_sha256"]:
            raise WebCacheError("Page extraction changed; do not mix versions. Reopen at offset 0.")
        check()
        return page

    def open(self, fetcher: PublicPageFetcher, inputs: dict, context: ToolContext) -> dict[str, Any]:
        # Validate before acquisition; malformed input cannot consume network/cache quota.
        offset = inputs.get("offset", 0)
        cap = inputs.get("max_chars", self.preview_chars)
        query = inputs.get("query")
        if type(offset) is not int or offset < 0 or type(cap) is not int or not 1 <= cap <= 20000:
            raise ValueError("Invalid offset or max_chars.")
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 400):
            raise ValueError("query must contain 1 through 400 characters.")
        # A size budget must not silently disable a requested local query.
        # Explicit offsets/pages still win and preserve contiguous paging.
        default_view = "page" if "offset" in inputs or ("max_chars" in inputs and query is None) else "auto"
        view = inputs.get("view", default_view)
        if view not in {"auto", "overview", "page"}:
            raise ValueError("view must be auto, overview, or page.")
        page = self.page(fetcher, inputs, context, "web.open")
        result = build_page_view(page, view=view, query=query, offset=offset, max_chars=cap)
        result.update(total_chars=len(page["text"]), truncated=result["returned_chars"] < len(page["text"]),
                      untrusted_data=True)
        return self._reading_order(result)

    def find(self, fetcher: PublicPageFetcher, inputs: dict, context: ToolContext) -> dict:
        query = inputs.get("query")
        offset, limit = inputs.get("offset", 0), inputs.get("limit", 3)
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("query must contain 1 through 200 characters.")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("Invalid match offset or limit.")
        page = self.page(fetcher, inputs, context, "web.find")
        return self._reading_order({**fetcher.find_in_page(page, query, offset=offset, limit=limit), "untrusted_data": True})

    @staticmethod
    def _reading_order(result: dict) -> dict:
        # Evidence and continuation precede optional diagnostics under generic mapping caps.
        first = ("text", "excerpts", "matches", "recovery_preview", "snapshot_id", "url", "fetched_at",
                 "text_sha256", "offset", "has_more", "next_offset", "context_complete", "match_status",
                 "coverage_scope", "returned_chars", "total_chars", "context_start", "context_end", "omitted_context",
                 "outline", "outline_omitted_count", "truncated", "snapshot_stable", "cache_hit",
                 "cache_age_seconds")
        return {**{key: result[key] for key in first if key in result},
                **{key: value for key, value in result.items() if key not in first}}
