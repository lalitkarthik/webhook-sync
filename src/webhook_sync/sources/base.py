"""The source adapter interface: what the receiver and the ledger worker need from each source.

`verify` checks a delivery's raw bytes and headers and finds its event id; `describe` reads the
verified body. Neither raises: every failure is a `Rejection` with its reason (receiver.md).
`fetch` asks the source for a payment's current state; `to_row` maps that state to its business
row, or to no row (sources.md, "The interface").
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from webhook_sync.queue import Failure


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


type PaymentStatus = Literal[
    "processing",
    "authorized",
    "failed",
    "succeeded",
    "partially_refunded",
    "refunded",
    "canceled",
]
"""Our status vocabulary, the same for every source (sources.md)."""


@dataclass(frozen=True)
class BusinessRow:
    """A payment's business columns as its source reports them now (data-model.md, `payments`).

    Amounts are integers in minor units. A null fee, fee tax or net is pending, not zero.
    `metadata` holds the configured metadata keys found, each value as the source gave it. The ledger adds the key's source and
    account and the bookkeeping columns (`row_hash`, `row_version`, `via`, ...).
    """

    payment_id: str
    status: PaymentStatus
    source_status: str
    amount_minor: int
    currency: str
    source_created_at: datetime | None
    authorized_minor: int | None = None
    refunded_minor: int = 0
    fee_minor: int | None = None
    fee_tax_minor: int | None = None
    net_minor: int | None = None
    settlement_minor: int | None = None
    settlement_currency: str | None = None
    method: str | None = None
    metadata: Mapping[str, Any] | None = None
    customer_email: str | None = None
    customer_name: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Fetched:
    """A payment's current state, as the source answered it."""

    state: Mapping[str, Any]


type FetchResult = Fetched | Failure
"""What `fetch` returns: the current state, or a failure with its error class (queue.md).
(P2's later tickets add `not_found`.)"""


class SourceAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def verify(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> VerifiedDelivery | Rejection: ...

    def describe(self, delivery: VerifiedDelivery) -> EventDescription | Rejection: ...

    async def fetch(self, payment_key: str) -> FetchResult: ...

    def to_row(self, state: Mapping[str, Any]) -> BusinessRow | None: ...
