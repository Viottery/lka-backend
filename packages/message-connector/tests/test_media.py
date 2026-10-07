"""Media jobs, permissions, TTL and bounded byte access with synthetic files."""

import asyncio
import json
from dataclasses import replace

import pytest
from test_service_mcp import connector
from test_store import ack, message, policy

from lka_message_connector.media import MediaCache, attachment_key
from lka_message_connector.models import DownloadedMedia


def setup(tmp_path):
    service, _ = connector(tmp_path, media_enabled=True)
    approved = policy(media_enabled=True)
    service.store.replace_policies([approved])
    item = message(attachments=[{"ordinal": 0, "kind": "image"}])
    service.store.enqueue(item)
    cache = MediaCache(service.config, service.store)
    cache.enqueue(item, approved)
    return service, cache, item, approved


class Adapter:
    platform = "telegram"
    account_id = "account"

    def __init__(self, callback=None, mime="image/jpeg"):
        self.downloads, self.callback, self.mime = 0, callback, mime

    async def download_attachment(self, message, ordinal, target, max_bytes):
        self.downloads += 1
        assert not target.exists()
        target.write_bytes(b"fake-image")
        if self.callback:
            self.callback()
        return DownloadedMedia(self.mime, 10)


class Backend:
    def __init__(self):
        self.rows = []

    async def import_media(self, rows):
        self.rows.extend(rows)
        return {
            "acknowledged": [
                {k: r[k] for k in ("platform", "account_id", "message_id", "ordinal")} for r in rows
            ],
            "rejected": [],
        }


def acknowledge(service, item):
    service.store.apply_ack(
        service.store.pending(), {"acknowledged": [ack(item.model_dump())], "rejected": []}
    )


def test_download_only_after_parent_ack_and_chunked_read(tmp_path):
    async def run():
        service, cache, item, _ = setup(tmp_path)
        adapter, backend = Adapter(), Backend()
        await cache.tick([adapter], backend)
        assert not adapter.downloads and not backend.rows
        acknowledge(service, item)
        abandoned = cache.directory / (attachment_key(item, 0) + ".part")
        abandoned.write_bytes(b"crashed-partial")
        await cache.tick([adapter], backend)
        assert adapter.downloads == 1 and backend.rows[0]["state"] == "cached"
        attachment = cache.attachments()["items"][0]
        chunk = cache.read_chunk(attachment["attachment_id"], length=4)
        assert chunk["data_base64"] == "ZmFrZQ==" and chunk["next_offset"] == 4
        assert chunk["sha256"] == backend.rows[0]["sha256"]
        assert cache.attachments(allowed_keys=set())["total"] == 0
        with pytest.raises(PermissionError):
            cache.read_chunk(attachment["attachment_id"], allowed_keys=set())
        with pytest.raises(ValueError):
            cache.read_chunk(attachment["attachment_id"], length=65537)
        await service.backend.close()

    asyncio.run(run())


def test_media_disable_observed_before_regrant_permanently_fences_bytes(tmp_path):
    async def run():
        service, cache, item, approved = setup(tmp_path)
        acknowledge(service, item)
        await cache.tick([Adapter()], Backend())
        attachment = cache.attachments()["items"][0]
        disabled = approved.model_copy(update={"media_enabled": False, "revision": 2})
        cache.reconcile([disabled])
        service.store.replace_policies([disabled])
        regranted = approved.model_copy(update={"revision": 3})
        cache.reconcile([regranted])
        service.store.replace_policies([regranted])
        assert cache.attachments()["total"] == 0
        with pytest.raises(PermissionError):
            cache.read_chunk(attachment["attachment_id"])
        assert not list(cache.directory.glob("*.bin"))
        await service.backend.close()

    asyncio.run(run())


def test_revocation_during_download_never_publishes_cached_bytes(tmp_path):
    async def run():
        service, cache, item, approved = setup(tmp_path)
        acknowledge(service, item)
        disabled = approved.model_copy(update={"record_enabled": False, "capture_epoch": 2})
        adapter = Adapter(callback=lambda: service.store.replace_policies([disabled]))
        backend = Backend()
        await cache.tick([adapter], backend)
        assert not backend.rows and not list(cache.directory.glob("*.bin"))
        assert cache.attachments()["total"] == 0
        await service.backend.close()

    asyncio.run(run())


def test_ttl_budget_symlink_and_spoofed_metadata_ack(tmp_path, monkeypatch):
    async def run():
        service, cache, item, _ = setup(tmp_path)
        acknowledge(service, item)
        await cache.tick([Adapter()], Backend())
        row = cache._rows()[0]
        attachment_id = "message_attachment_" + row["key"]
        update = json.loads(row["update_json"])
        with pytest.raises(ValueError):
            cache._apply_ack(
                [row],
                {
                    "acknowledged": [
                        {
                            "platform": "telegram",
                            "account_id": "other",
                            "message_id": item.message_id,
                            "ordinal": 0,
                        }
                    ],
                    "rejected": [],
                },
            )
        path = cache.directory / (row["key"] + ".bin")
        path.unlink()
        elsewhere = tmp_path / "private"
        elsewhere.write_bytes(b"fake-image")
        path.symlink_to(elsewhere)
        with pytest.raises(PermissionError):
            cache.read_chunk(attachment_id)
        path.unlink()
        path.write_bytes(b"fake-image")
        monkeypatch.setattr("lka_message_connector.media.time.time", lambda: update["expires_at"])
        with pytest.raises(PermissionError):
            cache.read_chunk(attachment_id)
        cache.cleanup()
        assert not path.exists()
        assert cache.attachments()["items"][0]["state"] == "expired"
        await service.backend.close()

    asyncio.run(run())


def test_budget_eviction_and_invalid_mime(tmp_path):
    async def run():
        service, cache, item, _ = setup(tmp_path)
        acknowledge(service, item)
        await cache.tick([Adapter()], Backend())
        cache.config = replace(cache.config, media_max_bytes=1)
        cache.cleanup()
        assert not list(cache.directory.glob("*.bin"))
        service2, cache2, item2, _ = setup(tmp_path / "mime")
        acknowledge(service2, item2)
        remote = Backend()
        await cache2.tick([Adapter(mime="text/html")], remote)
        assert remote.rows[0]["state"] == "failed"
        assert not list(cache2.directory.glob("*.bin"))
        await service.backend.close()
        await service2.backend.close()

    asyncio.run(run())
