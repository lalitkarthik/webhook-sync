"""The Razorpay source adapter (sources.md, "Razorpay: the differences").

Signature: `x-razorpay-signature`, hex HMAC-SHA256 of the raw body, no timestamp, compared through
the SDK (constant time). Event id: the `x-razorpay-event-id` header, which the signature doesn't
cover. Razorpay events carry no mode; the configured mode is used, and the key prefix is checked at
start. Every exception becomes a rejection reason, never a 500.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from razorpay import Utility  # pyright: ignore[reportMissingTypeStubs]
from razorpay.errors import SignatureVerificationError  # pyright: ignore[reportMissingTypeStubs]

from webhook_sync.config import Mode
from webhook_sync.sources.base import EventDescription, Rejection, VerifiedDelivery

SIGNATURE_HEADER = "x-razorpay-signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

# Q109: the types that make a ledger job. Every other type is stored and ignored with a reason.
HANDLED_TYPES = frozenset(
    {
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "refund.created",
        "refund.processed",
        "refund.failed",
        "refund.speed_changed",
    }
)

# The primary entity, by event-type prefix, most specific first (Q117).
_PRIMARY_ENTITY = (
    ("payment.dispute.", "dispute"),
    ("payment_link.", "payment_link"),
    ("payment.", "payment"),
    ("refund.", "refund"),
    ("order.", "order"),
)

# Where an event names its payment, in order (A16): stored for ignored events too.
_PAYMENT_KEY_PATHS = (("payment", "id"), ("refund", "payment_id"), ("dispute", "payment_id"))


class RazorpayAdapter:
    name = "razorpay"

    def __init__(self, *, mode: Mode, webhook_secrets: Sequence[str]) -> None:
        self._mode = mode
        self._secrets = tuple(webhook_secrets)

    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> VerifiedDelivery | Rejection:
        try:
            return self._verify(raw_body, headers)
        except Exception:
            return Rejection("bad_signature")

    def describe(self, delivery: VerifiedDelivery) -> EventDescription | Rejection:
        try:
            return self._describe(delivery.body)
        except Exception:
            return Rejection("malformed")

    def _verify(self, raw_body: bytes, headers: Mapping[str, str]) -> VerifiedDelivery | Rejection:
        signature = headers.get(SIGNATURE_HEADER)
        if not signature:
            return Rejection("bad_signature")
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return Rejection("malformed")
        if not any(self._signed_with(text, signature, secret) for secret in self._secrets):
            return Rejection("bad_signature")
        try:
            body: object = json.loads(text)
        except ValueError:
            return Rejection("malformed")
        if not _encodes_as_utf8(body) or not isinstance(body, dict):
            return Rejection("malformed")
        event_id = headers.get(EVENT_ID_HEADER, "").strip()
        if not event_id:
            return Rejection("missing_id")
        return VerifiedDelivery(
            event_id=event_id,
            body=cast(dict[str, Any], body),
            body_sha256=hashlib.sha256(raw_body).hexdigest(),
        )

    @staticmethod
    def _signed_with(text: str, signature: str, secret: str) -> bool:
        try:
            Utility().verify_webhook_signature(text, signature, secret)  # pyright: ignore[reportUnknownMemberType]
        except SignatureVerificationError, TypeError, ValueError:
            return False
        return True

    def _describe(self, body: Mapping[str, Any]) -> EventDescription | Rejection:
        event_type = body.get("event")
        if not isinstance(event_type, str) or not event_type:
            return Rejection("malformed")
        handled = event_type in HANDLED_TYPES

        entity_name = next(
            (name for prefix, name in _PRIMARY_ENTITY if event_type.startswith(prefix)), None
        )
        entity_id = _entity_field(body, entity_name, "id") if entity_name else None
        payment_key = next(
            (key for name, field in _PAYMENT_KEY_PATHS if (key := _entity_field(body, name, field))),
            None,
        )
        # A handled event's job is keyed by its payment, so it must name one.
        if handled and (entity_id is None or payment_key is None):
            return Rejection("malformed")

        created_at = body.get("created_at")
        created_seconds = (
            created_at if isinstance(created_at, int) and not isinstance(created_at, bool) else None
        )
        fingerprint_parts = (
            event_type,
            entity_id or "",
            "" if created_seconds is None else str(created_seconds),
            self._mode,
        )
        account_id = body.get("account_id")
        return EventDescription(
            type=event_type,
            account_id=account_id if isinstance(account_id, str) and account_id else None,
            payment_key=payment_key,
            fingerprint=hashlib.sha256("|".join(fingerprint_parts).encode()).hexdigest(),
            source_created_at=(
                None if created_seconds is None else datetime.fromtimestamp(created_seconds, UTC)
            ),
            ignore_reason=None if handled else "unhandled_type",
        )


def _encodes_as_utf8(body: object) -> bool:
    """False when a `\\ud800`-style escape left a lone surrogate, which SQLite can't store."""
    try:
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _entity_field(body: Mapping[str, Any], entity_name: str, field: str) -> str | None:
    """`payload.<entity_name>.entity.<field>` when it is a non-empty string, else None."""
    node: object = body.get("payload")
    for key in (entity_name, "entity", field):
        if not isinstance(node, dict):
            return None
        node = cast(dict[str, object], node).get(key)
    return node if isinstance(node, str) and node else None
