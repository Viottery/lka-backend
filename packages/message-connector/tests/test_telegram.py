"""Offline adapter tests: no account credentials or network access."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest
from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityMention,
    MessageEntityMentionName,
    PeerChannel,
    PeerChat,
    PeerUser,
)

from lka_message_connector.adapters.telegram import TelegramAdapter
from lka_message_connector.config import TelegramConfig
from lka_message_connector.models import CaptureBackpressure, CapturePolicy, SourceEvent


class Client:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.session = NS(save_entities=True)
        self.authorized = True
        self.me = NS(id=42, bot=False)
        self.calls = []
        self.remote = None
        self.chunks = [b"abcd", b"ef"]
        self.closed = False
        self.event = None

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.me

    def add_event_handler(self, handler, builder):
        self.handler = handler
        self.calls.append("handler")
        assert builder.incoming is True

    def remove_event_handler(self, handler):
        self.calls.append("remove")

    async def set_receive_updates(self, value):
        self.calls.append("receive_updates" if value else "pause_updates")

    async def catch_up(self):
        self.calls.append("catch_up")
        if self.event is not None:
            await self.handler(self.event)

    async def run_until_disconnected(self):
        await asyncio.Future()

    async def get_messages(self, chat, ids):
        self.calls.append(("get_messages", chat, ids))
        return self.remote

    def iter_download(self, media, request_size):
        client = self

        class Download:
            def __aiter__(self):
                self.items = iter(client.chunks)
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration:
                    raise StopAsyncIteration from None

            async def close(self):
                client.closed = True

        return Download()

    async def iter_messages(self, peer, limit):
        self.calls.append(("history", peer, limit))
        yield self.remote


@pytest.fixture
def adapter(tmp_path):
    instance = TelegramAdapter(
        TelegramConfig(
            1, "a" * 32, tmp_path / "session" / "user.session", expected_account_id="42"
        ),
        Client,
    )
    asyncio.run(instance.connect())
    return instance


def message(**updates):
    values = {
        "peer_id": PeerChat(7),
        "id": 8,
        "message": "hello",
        "entities": [],
        "sender_id": 4,
        "sender": NS(first_name="A", last_name="B"),
        "chat": NS(),
        "date": datetime(2026, 1, 1, tzinfo=UTC),
        "reply_to": None,
        "media": None,
        "photo": None,
        "document": None,
        "sticker": None,
    }
    values.update(updates)
    return NS(**values)


def normalize(adapter, raw):
    conversation = adapter._conversation(raw, raw.chat)
    policy = CapturePolicy(
        **conversation.model_dump(), record_enabled=True, revision=1, capture_epoch=2
    )
    event = SourceEvent(conversation, raw, 100)
    return adapter.normalize(event, policy)


def test_connect_checks_user_identity_and_session_permissions(adapter):
    assert adapter.account_id == "42"
    assert adapter._client.session.save_entities is False
    assert adapter._client.kwargs["sequential_updates"] is True
    assert adapter._client.kwargs["auto_reconnect"] is False
    assert adapter._client.kwargs["receive_updates"] is False
    assert adapter.config.session_path.stat().st_mode & 0o777 == 0o600
    assert adapter.config.session_path.parent.stat().st_mode & 0o777 == 0o700
    assert adapter._client.calls == ["connect"]  # no history/contact scans


@pytest.mark.parametrize(
    "authorized,account,bot,error",
    [
        (False, 42, False, "telegram_login_required"),
        (True, 99, False, "telegram_account_mismatch"),
        (True, 42, True, "telegram_user_account_required"),
    ],
)
def test_connect_never_prompts(adapter, authorized, account, bot, error):
    adapter._client.authorized = authorized
    adapter._client.me = NS(id=account, bot=bot)
    with pytest.raises(PermissionError, match=error):
        asyncio.run(adapter.connect())
    assert adapter._client.calls[-1] == "disconnect"


def test_connect_requires_explicit_expected_account(tmp_path):
    adapter = TelegramAdapter(TelegramConfig(1, "a" * 32, tmp_path / "x.session"), Client)
    with pytest.raises(PermissionError, match="telegram_account_id_required"):
        asyncio.run(adapter.connect())
    assert adapter._client is None


def test_login_requires_terminal_before_client_creation(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    adapter = TelegramAdapter(TelegramConfig(1, "a" * 32, tmp_path / "x.session"), Client)
    with pytest.raises(ValueError, match="interactive_terminal"):
        asyncio.run(adapter.login())
    assert adapter._client is None


def test_mentions_use_utf16_and_ignore_overlapping_style(adapter):
    raw = message(
        message="😀 hi Ada!",
        entities=[MessageEntityBold(0, 9), MessageEntityMentionName(6, 3, 123)],
    )
    result = normalize(adapter, raw)
    assert result.message_id == "-7:8"
    assert result.sender_name == "A B"
    assert result.text == "😀 hi Ada!"
    assert result.mentions[0].user_id == "123"
    assert [p.kind for p in result.content_parts] == ["text", "mention", "text"]
    assert result.content_parts[0].text == "😀 hi "
    assert result.content_parts[2].text == "!"


def test_plain_username_mentions_are_not_guessed(adapter):
    raw = message(
        message="Ada @bob",
        entities=[MessageEntityMentionName(0, 3, 123), MessageEntityMention(4, 4)],
    )
    result = normalize(adapter, raw)
    assert result.mentions == []
    assert result.metadata_capabilities.mentions == "not_provided"
    assert result.content_parts[0].text == raw.message


def test_cross_chat_reply_and_forum_thread(adapter):
    raw = message(
        peer_id=PeerChannel(17),
        chat=NS(megagroup=True),
        reply_to=NS(reply_to_msg_id=20, reply_to_peer_id=PeerChat(2), reply_to_top_id=3),
    )
    result = normalize(adapter, raw)
    assert result.conversation_type == "group"
    assert result.message_id == "-1000000000017:8"
    assert result.reply_to_message_id == "-2:20"
    assert result.thread_id == "-1000000000017:3"


@pytest.mark.parametrize(
    "peer,chat,expected",
    [
        (PeerUser(3), NS(), "private"),
        (PeerChat(3), NS(), "group"),
        (PeerChannel(3), NS(megagroup=False), "channel"),
    ],
)
def test_conversation_kinds(adapter, peer, chat, expected):
    assert normalize(adapter, message(peer_id=peer, chat=chat)).conversation_type == expected


def test_policy_and_remote_peer_mismatch_rejected(adapter):
    raw = message()
    conversation = adapter._conversation(raw, raw.chat)
    policy = CapturePolicy(**conversation.model_dump(), revision=1, capture_epoch=1)
    with pytest.raises(ValueError, match="policy_mismatch"):
        adapter.normalize(SourceEvent(conversation, raw, 1), policy)
    policy = policy.model_copy(update={"record_enabled": True})
    with pytest.raises(ValueError, match="conversation_mismatch"):
        adapter.normalize(SourceEvent(conversation, message(peer_id=PeerChat(99)), 1), policy)


def test_oversize_rejected_and_stickers_not_downloadable(adapter):
    with pytest.raises(ValueError, match="oversize"):
        normalize(adapter, message(message="x" * 16385))
    result = normalize(
        adapter, message(message="", sticker=True, document=NS(mime_type="image/webp"), media=NS())
    )
    assert result.attachments == []
    assert result.content_kind == "unsupported"


def test_caption_and_safe_attachment_ref(adapter):
    result = normalize(adapter, message(message="caption", photo=NS(), media=NS()))
    assert result.text == "caption"
    assert result.attachments[0].model_dump() == {"ordinal": 0, "kind": "image", "file_name": None}


def test_link_preview_and_unsupported_media_do_not_erase_authored_text(adapter):
    result = normalize(adapter, message(message="meeting notes https://example.com", media=NS()))
    assert result.content_kind == "text" and result.text == "meeting notes https://example.com"
    assert result.attachments == []
    assert result.content_parts[-1].kind == "unsupported"


def test_backfill_recent_window_is_imported_oldest_first(adapter):
    async def recent(peer, limit):
        for number in (3, 2, 1):
            yield message(id=number)

    adapter._client.iter_messages = recent
    conversation = adapter._conversation(message(), NS())

    async def run():
        return [event.payload.id async for event in adapter.backfill(conversation, 3)]

    assert asyncio.run(run()) == [1, 2, 3]


def test_capture_handler_precedes_catchup_and_propagates_failure(adapter):
    adapter._client.event = NS(message=message(), chat=NS())

    async def consume(event):
        assert event.conversation.conversation_id == "-7"
        raise ValueError("private provider content")

    with pytest.raises(RuntimeError, match="^telegram_capture_consumer_failed$"):
        asyncio.run(adapter.capture(consume))
    assert adapter._client.calls.index("handler") < adapter._client.calls.index("catch_up")
    assert adapter._client.calls[-3:] == ["remove", "pause_updates", "disconnect"]


def test_download_bounded_and_partial_cleanup(adapter, tmp_path):
    raw = message(photo=NS(), media=NS())
    envelope = normalize(adapter, raw)
    adapter._client.remote = raw
    target = tmp_path / "media.bin"
    with pytest.raises(ValueError, match="media_oversize"):
        asyncio.run(adapter.download_attachment(envelope, 0, target, 5))
    assert not target.exists()
    assert adapter._client.closed
    assert ("get_messages", -7, 8) in adapter._client.calls
    result = asyncio.run(adapter.download_attachment(envelope, 0, target, 6))
    assert result.mime_type == "image/jpeg" and result.size_bytes == 6
    assert target.read_bytes() == b"abcdef"


def test_download_does_not_overwrite_existing_target(adapter, tmp_path):
    raw = message(photo=NS(), media=NS())
    adapter._client.remote = raw
    target = tmp_path / "existing.bin"
    target.write_bytes(b"user data")
    with pytest.raises(FileExistsError):
        asyncio.run(adapter.download_attachment(normalize(adapter, raw), 0, target, 6))
    assert target.read_bytes() == b"user data"


def test_backfill_explicit_and_bounded(adapter):
    raw = message()
    adapter._client.remote = raw
    conversation = adapter._conversation(raw, raw.chat)

    async def run(limit):
        return [event async for event in adapter.backfill(conversation, limit)]

    with pytest.raises(ValueError, match="backfill_limit_invalid"):
        asyncio.run(run(1001))
    assert len(asyncio.run(run(1))) == 1
    assert ("history", -7, 1) in adapter._client.calls


def test_login_phone_rejects_bot_token(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda _: "1234:secret-token")
    with pytest.raises(ValueError, match="telegram_phone_number_required"):
        TelegramAdapter._phone()


def test_forum_first_reply_retains_thread_root(adapter):
    result = normalize(
        adapter,
        message(
            reply_to=NS(
                reply_to_msg_id=88, reply_to_peer_id=None, reply_to_top_id=None, forum_topic=True
            )
        ),
    )
    assert result.thread_id == "-7:88"


def test_interactive_login_uses_hidden_prompts_and_prints_no_identity(
    tmp_path, monkeypatch, capsys
):
    from telethon.errors import SessionPasswordNeededError

    class LoginClient(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.authorized = False

        async def send_code_request(self, phone):
            assert phone == "+123456789"
            self.calls.append("code_request")

        async def sign_in(self, **kwargs):
            if "code" in kwargs:
                assert kwargs == {"phone": "+123456789", "code": "12345"}
                raise SessionPasswordNeededError(request=None)
            assert kwargs == {"password": "secret"}

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    values = iter(["+123456789", "12345", "secret"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(values))
    adapter = TelegramAdapter(TelegramConfig(1, "a" * 32, tmp_path / "user.session"), LoginClient)
    assert asyncio.run(adapter.login()) == "42"
    assert adapter._client.kwargs["receive_updates"] is False
    assert adapter._client.calls == ["connect", "code_request", "pause_updates", "disconnect"]
    assert capsys.readouterr().out == ""


def test_capture_preserves_backpressure_without_logging_content(adapter, caplog):
    adapter._client.event = NS(message=message(), chat=NS())

    class OutboxFull(CaptureBackpressure):
        pass

    async def consume(event):
        raise OutboxFull("private consumer details")

    with pytest.raises(CaptureBackpressure, match="^telegram_capture_backpressure$") as exc:
        asyncio.run(adapter.capture(consume))
    assert exc.value.__context__ is None
    assert "private consumer details" not in caplog.text
    assert adapter._client.calls[-3:] == ["remove", "pause_updates", "disconnect"]
