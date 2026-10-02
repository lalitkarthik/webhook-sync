"""The receiver's checks 3-5 (receiver.md, "The checks, in order"), each answering a `Rejection`.

The source's own checks (signature, a JSON object with an event id) live in its adapter.
"""

from fastapi import Request

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
