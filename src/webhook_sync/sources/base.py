"""The source adapter interface: what the receiver needs from each source.

`verify` checks a delivery's raw bytes and headers and finds its event id; `describe` reads the
verified body. Neither raises: every failure is a `Rejection` with its reason (receiver.md).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Rejection:
    """A delivery refused before storing, with the reason it is answered and counted under."""

    reason: str
    status_code: int = 400


@dataclass(frozen=True)
class VerifiedDelivery:
    """A delivery whose signature matched: its event id, the parsed body and the body's hash."""

    event_id: str
    body: Mapping[str, Any]
    body_sha256: str


@dataclass(frozen=True)
class EventDescription:
    """What the receiver stores about an event.

    `account_id` is the account the body names, or None when it names none. `ignore_reason` is
    None for a handled type. `payment_key` is set whenever the body names a payment (A16).
    `payload` is the body with every field outside the source's allow-list dropped (A5).
    """

    type: str
    account_id: str | None
    payment_key: str | None
    fingerprint: str
    source_created_at: datetime | None
    ignore_reason: str | None
    payload: Mapping[str, Any]

    @property
    def handled(self) -> bool:
        return self.ignore_reason is None


class SourceAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def verify(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> VerifiedDelivery | Rejection: ...

    def describe(self, delivery: VerifiedDelivery) -> EventDescription | Rejection: ...
