"""Explicit notifications after self-owned persistence operations return."""

from __future__ import annotations

import logging
from functools import wraps
from inspect import signature

_logger = logging.getLogger(__name__)
_callback_failures = 0


def notify_after_commit(method):
    """Call ``self.on_change`` only after a self-owned write method returns.

    Methods accepting a caller-owned ``conn`` are deliberately skipped: their
    outer transaction owner must signal after commit.
    """
    method_signature = signature(method)

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        global _callback_failures
        result = method(self, *args, **kwargs)
        callback = getattr(self, "on_change", None)
        bound = method_signature.bind_partial(self, *args, **kwargs)
        external_conn = bound.arguments.get("conn") is not None
        if callback is not None and not external_conn:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - signaling cannot undo a committed write
                _callback_failures += 1
                _logger.warning(
                    "Post-commit signal failed (%s; count=%d)",
                    type(exc).__name__,
                    _callback_failures,
                )
        return result

    return wrapped
