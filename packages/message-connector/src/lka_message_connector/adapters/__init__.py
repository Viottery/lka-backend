"""Opt-in provider adapters; importing this package does not connect accounts."""

from .telegram import TelegramAdapter

__all__ = ["TelegramAdapter"]
