"""Read-only Telegram user-account capture using Telethon 1.x.

Only inbound updates are captured. Telethon's internal update queue is not a
bounded durable queue: callers must stop capture on saturation and report gaps.
No implicit history scan, contact scan, read receipts or sending occurs here.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from ..config import TelegramConfig
from ..models import (
    AttachmentRef,
    CaptureBackpressure,
    CapturePolicy,
    ContentPart,
    ConversationRef,
    DownloadedMedia,
    Mention,
    MessageEnvelope,
    MetadataCapabilities,
    SourceEvent,
)


class TelegramAdapterError(ValueError):
    """Credential-free adapter validation error."""


class TelegramAuthenticationError(PermissionError):
    """Credential-free account authorization failure; daemon must fail fast."""


class TelegramAdapter:
    platform = "telegram"

    def __init__(self, config: TelegramConfig, client_factory=None):
        self.config = config
        self.account_id = ""
        self._factory = client_factory
        self._client = None

    def _secure_session(self):
        path = self.config.session_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            raise TelegramAdapterError("unsafe_telegram_session_path")
        if os.name == "posix":
            path.parent.chmod(0o700)
        # Precreate before SQLite opens it, so initial creation is private too.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        if os.name == "posix":
            for candidate in (
                path,
                Path(str(path) + "-journal"),
                Path(str(path) + "-wal"),
                Path(str(path) + "-shm"),
            ):
                if candidate.is_symlink():
                    raise TelegramAdapterError("unsafe_telegram_session_path")
                if candidate.exists():
                    candidate.chmod(0o600)

    def _get_client(self):
        if self._client is None:
            self._secure_session()
            factory = self._factory
            if factory is None:
                from telethon import TelegramClient

                factory = TelegramClient
            self._client = factory(
                str(self.config.session_path),
                self.config.api_id,
                self.config.api_hash,
                proxy=self.config.proxy,
                sequential_updates=True,
                auto_reconnect=False,
                receive_updates=False,
            )
            # Session storage contains authorization/update state, not message
            # bodies or a persistent address book. Entity cache remains in RAM.
            self._client.session.save_entities = False
        return self._client

    async def _verify(self, *, require_expected: bool):
        me = await self._client.get_me()
        if me is None or getattr(me, "bot", False):
            raise TelegramAuthenticationError("telegram_user_account_required")
        account_id = str(me.id)
        expected = self.config.expected_account_id
        if (require_expected and not expected) or (expected and expected != account_id):
            raise TelegramAuthenticationError("telegram_account_mismatch")
        self.account_id = account_id
        return account_id

    @staticmethod
    def _phone():
        phone = getpass.getpass("Telegram phone: ")
        # Telethon start() accepts bot tokens in the phone callback too.
        if ":" in phone or not phone.lstrip("+").isdigit():
            raise TelegramAdapterError("telegram_phone_number_required")
        return phone

    async def login(self) -> str:
        """Explicit CLI-only authentication; secrets never enter model tools."""
        if not sys.stdin.isatty():
            raise TelegramAdapterError("telegram_login_requires_interactive_terminal")
        from telethon.errors import SessionPasswordNeededError

        client = self._get_client()
        try:
            await client.connect()
            if not await client.is_user_authorized():
                phone = self._phone()
                await client.send_code_request(phone)
                code = getpass.getpass("Telegram code: ")
                if not code:
                    raise TelegramAdapterError("telegram_code_required")
                try:
                    await client.sign_in(phone=phone, code=code)
                except SessionPasswordNeededError:
                    password = getpass.getpass("Telegram 2FA password: ")
                    await client.sign_in(password=password)
            return await self._verify(require_expected=False)
        except (TelegramAdapterError, TelegramAuthenticationError):
            raise
        except Exception:  # noqa: BLE001 - provider failures may contain credentials
            raise RuntimeError("telegram_login_failed") from None
        finally:
            await self.disconnect()

    async def connect(self) -> None:
        if not self.config.expected_account_id:
            raise TelegramAuthenticationError("telegram_account_id_required")
        client = self._get_client()
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise TelegramAuthenticationError("telegram_login_required")
            await self._verify(require_expected=True)
        except Exception as exc:
            await self.disconnect()
            if isinstance(exc, (TelegramAdapterError, TelegramAuthenticationError)):
                raise
            raise RuntimeError("telegram_connect_failed") from None

    async def disconnect(self) -> None:
        if self._client is not None:
            # Reset before reuse: reconnect must not receive updates until a new
            # policy-aware capture handler has been registered.
            await self._client.set_receive_updates(False)
            await self._client.disconnect()
            self._secure_session()

    def _conversation(self, message, chat=None) -> ConversationRef:
        from telethon.utils import get_peer_id

        peer = message.peer_id
        kind = type(peer).__name__
        if kind == "PeerUser":
            conversation_type = "private"
        elif kind == "PeerChat":
            conversation_type = "group"
        elif kind == "PeerChannel":
            # Never misclassify an uncached channel as a broadcast channel.
            if chat is None or not hasattr(chat, "megagroup"):
                raise TelegramAdapterError("telegram_chat_metadata_unavailable")
            conversation_type = "group" if chat.megagroup else "channel"
        else:
            raise TelegramAdapterError("telegram_peer_unsupported")
        return ConversationRef(
            platform=self.platform,
            account_id=self.account_id,
            conversation_type=conversation_type,
            conversation_id=str(get_peer_id(peer)),
        )

    async def capture(self, consume: Callable[[SourceEvent], Awaitable[None]]) -> None:
        from telethon import events

        if not self.account_id:
            raise TelegramAdapterError("telegram_not_connected")
        client = self._client
        failed = asyncio.get_running_loop().create_future()

        async def handler(event):
            if failed.done():
                return
            try:
                source = SourceEvent(
                    self._conversation(event.message, event.chat), event, int(time.time())
                )
                await consume(source)
            except CaptureBackpressure:
                # Preserve durable saturation as a distinct failure without
                # retaining content/tracebacks from the consumer exception.
                failed.set_result(CaptureBackpressure("telegram_capture_backpressure"))
            except Exception:  # noqa: BLE001 - arbitrary consumer failure stops capture
                # Telethon logs/swallow handler exceptions; surface a safe error
                # to the daemon and stop accepting new updates.
                failed.set_result(RuntimeError("telegram_capture_consumer_failed"))

        client.add_event_handler(handler, events.NewMessage(incoming=True))
        disconnected = None
        try:
            await client.set_receive_updates(True)
            await client.catch_up()  # handler must be installed first
            disconnected = asyncio.ensure_future(client.run_until_disconnected())
            done, _ = await asyncio.wait(
                (disconnected, failed), return_when=asyncio.FIRST_COMPLETED
            )
            if failed in done:
                raise failed.result() from None
            await disconnected
            raise RuntimeError("telegram_capture_disconnected")
        except (asyncio.CancelledError, CaptureBackpressure):
            raise
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc) in {
                "telegram_capture_consumer_failed",
                "telegram_capture_disconnected",
            }:
                raise
            raise RuntimeError("telegram_capture_failed") from None
        finally:
            client.remove_event_handler(handler)
            if disconnected is not None and not disconnected.done():
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)
            failed.cancel()
            await self.disconnect()

    def _check_conversation(self, conversation: ConversationRef):
        if (
            not self.account_id
            or conversation.platform != self.platform
            or conversation.account_id != self.account_id
        ):
            raise TelegramAdapterError("telegram_account_mismatch")

    @staticmethod
    def _media(message):
        if getattr(message, "sticker", None):
            return None
        if getattr(message, "photo", None):
            return "image", "image/jpeg", None
        document = getattr(message, "document", None)
        if document is None:
            return None
        attrs = getattr(document, "attributes", ())
        if any(
            type(attr).__name__ in {"DocumentAttributeSticker", "DocumentAttributeCustomEmoji"}
            for attr in attrs
        ):
            return None
        mime = getattr(document, "mime_type", "")
        if mime in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            return "image", mime, getattr(document, "size", None)
        if mime in {"video/mp4", "video/webm", "video/quicktime"}:
            return "video", mime, getattr(document, "size", None)
        return None

    def normalize(self, event: SourceEvent, policy: CapturePolicy) -> MessageEnvelope:
        from telethon.utils import get_peer_id

        self._check_conversation(event.conversation)
        if event.conversation != policy.conversation or not policy.record_enabled:
            raise TelegramAdapterError("telegram_capture_policy_mismatch")
        raw = event.payload
        message = getattr(raw, "message", raw)
        # Telethon Message.message is text, while NewMessage.Event.message is Message.
        if isinstance(message, str) or message is None:
            message = raw
        chat = getattr(raw, "chat", None) or getattr(message, "chat", None)
        if self._conversation(message, chat) != event.conversation:
            raise TelegramAdapterError("telegram_event_conversation_mismatch")
        chat_id = event.conversation.conversation_id
        text = getattr(message, "message", "") or ""
        if len(text) > 16384:
            raise TelegramAdapterError("telegram_message_oversize")
        entities = getattr(message, "entities", None) or []
        incomplete_mentions = any(type(e).__name__ == "MessageEntityMention" for e in entities)
        mentions = []
        parts = []
        encoded = text.encode("utf-16-le")
        cursor = 0
        for entity in sorted(entities, key=lambda e: e.offset):
            if type(entity).__name__ != "MessageEntityMentionName" or incomplete_mentions:
                continue
            start, end = entity.offset * 2, (entity.offset + entity.length) * 2
            if start < cursor or end > len(encoded):
                raise TelegramAdapterError("telegram_invalid_mention_offsets")
            if start > cursor:
                parts.append(
                    ContentPart(kind="text", text=encoded[cursor:start].decode("utf-16-le"))
                )
            mention = Mention(kind="user", user_id=str(entity.user_id))
            mentions.append(mention)
            parts.append(ContentPart(kind="mention", mention=mention))
            cursor = end
        if cursor < len(encoded):
            parts.append(ContentPart(kind="text", text=encoded[cursor:].decode("utf-16-le")))
        reply = getattr(message, "reply_to", None)
        reply_id = None
        reply_capability = "supported"
        if reply and getattr(reply, "reply_to_msg_id", None):
            peer = getattr(reply, "reply_to_peer_id", None)
            try:
                reply_chat = str(get_peer_id(peer)) if peer is not None else chat_id
                reply_id = f"{reply_chat}:{reply.reply_to_msg_id}"
            except (TypeError, ValueError):
                reply_capability = "not_provided"
        top_id = getattr(reply, "reply_to_top_id", None) if reply else None
        if not top_id and reply and getattr(reply, "forum_topic", False):
            top_id = getattr(reply, "reply_to_msg_id", None)
        thread_id = f"{chat_id}:{top_id}" if top_id else None
        sender = getattr(message, "sender", None)
        name = None
        if sender is not None:
            name = " ".join(
                filter(
                    None, (getattr(sender, "first_name", None), getattr(sender, "last_name", None))
                )
            ) or getattr(sender, "title", None)
        media = self._media(message)
        attachments = [AttachmentRef(ordinal=0, kind=media[0])] if media else []
        unsupported = getattr(message, "media", None) is not None and media is None
        if unsupported:
            parts.append(ContentPart(kind="unsupported"))
        date = getattr(message, "date", None)
        return MessageEnvelope(
            **event.conversation.model_dump(),
            message_id=f"{chat_id}:{message.id}",
            sender_id=str(message.sender_id)
            if getattr(message, "sender_id", None) is not None
            else None,
            sender_name=name,
            text=text,
            sent_at=int(date.timestamp()) if date else None,
            received_at=event.received_at,
            # A link preview/unsupported attachment must not erase authored text
            # when the backend stores only text for content_kind='text'.
            content_kind="text" if text.strip() else "unsupported",
            attachments=attachments,
            capture_epoch=policy.capture_epoch,
            adapter_id="telegram.telethon",
            adapter_version="1",
            metadata_capabilities=MetadataCapabilities(
                mentions="not_provided" if incomplete_mentions else "supported",
                reply=reply_capability,
                thread="supported",
                content_parts="supported",
            ),
            mentions=mentions,
            reply_to_message_id=reply_id,
            thread_id=thread_id,
            content_parts=parts,
        )

    async def backfill(
        self, conversation: ConversationRef, limit: int
    ) -> AsyncIterator[SourceEvent]:
        self._check_conversation(conversation)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise TelegramAdapterError("telegram_backfill_limit_invalid")
        try:
            recent = []
            async for message in self._client.iter_messages(
                int(conversation.conversation_id), limit=limit
            ):
                if len(recent) >= limit:
                    break
                if self._conversation(message, message.chat) != conversation:
                    raise TelegramAdapterError("telegram_event_conversation_mismatch")
                recent.append(message)
            # iter_messages defaults to newest-first. Import this bounded window
            # oldest-first so analysis and local context preserve event order.
            for message in reversed(recent):
                yield SourceEvent(conversation, message, int(time.time()))
        except TelegramAdapterError:
            raise
        except Exception:  # noqa: BLE001 - sanitize provider exception details
            raise RuntimeError("telegram_backfill_failed") from None

    async def download_attachment(
        self, message: MessageEnvelope, ordinal: int, target: Path, max_bytes: int
    ) -> DownloadedMedia:
        from telethon.utils import get_peer_id

        self._check_conversation(message.conversation)
        if ordinal != 0 or not any(a.ordinal == ordinal for a in message.attachments):
            raise TelegramAdapterError("telegram_attachment_not_found")
        if type(max_bytes) is not int or max_bytes < 1:
            raise TelegramAdapterError("telegram_media_limit_invalid")
        prefix = message.conversation_id + ":"
        if not message.message_id.startswith(prefix):
            raise TelegramAdapterError("telegram_message_reference_invalid")
        iterator = None
        created = False
        try:
            native_id = int(message.message_id[len(prefix) :])
            remote = await self._client.get_messages(int(message.conversation_id), ids=native_id)
            if (
                remote is None
                or remote.id != native_id
                or str(get_peer_id(remote.peer_id)) != message.conversation_id
            ):
                raise TelegramAdapterError("telegram_attachment_not_found")
            media = self._media(remote)
            ref = next(a for a in message.attachments if a.ordinal == ordinal)
            if media is None or media[0] != ref.kind:
                raise TelegramAdapterError("telegram_media_unsupported")
            if media[2] is not None and media[2] > max_bytes:
                raise TelegramAdapterError("telegram_media_oversize")
            size = 0
            iterator = self._client.iter_download(remote.media, request_size=64 * 1024)
            with target.open("xb") as handle:
                created = True
                async for chunk in iterator:
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramAdapterError("telegram_media_oversize")
                    handle.write(chunk)
            if size == 0:
                raise TelegramAdapterError("telegram_media_empty")
            return DownloadedMedia(mime_type=media[1], size_bytes=size)
        except BaseException as exc:
            if created:
                target.unlink(missing_ok=True)
            if isinstance(exc, (TelegramAdapterError, asyncio.CancelledError, FileExistsError)):
                raise
            raise RuntimeError("telegram_media_download_failed") from None
        finally:
            if iterator is not None:
                closer = getattr(iterator, "close", None) or getattr(iterator, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except BaseException as exc:
                        if created:
                            target.unlink(missing_ok=True)
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        raise RuntimeError("telegram_media_download_failed") from None
