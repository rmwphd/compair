"""Tiny in-memory sliding-window rate limiter. IPs live only in process memory."""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Cloudflare Tunnel / reverse proxies put the real client here.
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "?")
    )


def limit(request: Request, bucket: str, per_minute: int, key: str | None = None) -> None:
    """Allow `per_minute` hits per (bucket, key); key defaults to the client IP."""
    now = time.monotonic()
    q = _hits[(bucket, key or client_ip(request))]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(status_code=429, detail="Too many requests, slow down a little.")
    q.append(now)
    if len(_hits) > 50_000:  # crude bound on memory
        _hits.clear()
