"""The receiver's checks 3-5 (receiver.md, "The checks, in order"), each answering a `Rejection`.

The source's own checks (signature, a JSON object with an event id) live in its adapter.
"""

from fastapi import Request
from starlette.datastructures import Headers

from webhook_sync.sources.base import Rejection

MAX_BODY_BYTES = 1024 * 1024


async def read_body(request: Request) -> bytes | Rejection:
    """Check 3: the raw body, read once; over 1 MB is 413 `too_large`, found while streaming."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return Rejection("too_large", 413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return Rejection("too_large", 413)
    return bytes(body)


def check_content_type(content_type: str | None) -> Rejection | None:
    """Check 4: `application/json` (any parameters) or any `…+json` type, else 415 `not_json`."""
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    kind, _, subtype = media_type.partition("/")
    if media_type == "application/json" or (
        kind and len(subtype) > 5 and subtype.endswith("+json")
    ):
        return None
    return Rejection("not_json", 415)


# Every source's security headers: Standard Webhooks verifies the last copy while FastAPI reads
# the first, so a repeat is refused for all of them, whichever source the route is for.
SECURITY_HEADERS = (
    "stripe-signature",
    "x-razorpay-signature",
    "x-razorpay-event-id",
    "webhook-id",
    "webhook-timestamp",
    "webhook-signature",
)


def check_security_headers(headers: Headers) -> Rejection | None:
    """Check 5: a security header appearing twice is 400 `duplicate_header`."""
    if any(len(headers.getlist(name)) > 1 for name in SECURITY_HEADERS):
        return Rejection("duplicate_header")
    return None
