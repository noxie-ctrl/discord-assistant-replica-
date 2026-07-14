"""
utils/rate_limiter.py

Pure in-memory rate limiting for Lucy. No new dependencies — reuses
cachetools (already used in cogs/ai_chat.py for TTL/LRU caching).

Two limits:
  - Chat messages: 7 messages / minute / user
  - Slash commands: 1 command / 5 seconds / user

Bot owner bypasses both entirely (reuses _is_owner from permissions.py).

This module is deliberately stateless across restarts — a deploy resets all
buckets, which is fine for a Discord bot (the alternative is a Redis dependency
or DB writes on every message, neither of which is worth it for this use case).
"""

import time
import logging

import cachetools

from utils.permissions import _is_owner

logger = logging.getLogger("lucy.rate_limiter")

# (user_id) -> list of timestamps (Unix float). Window = 60s.
_chat_buckets: cachetools.TTLCache = cachetools.TTLCache(maxsize=5000, ttl=60)
# (user_id) -> last slash-command timestamp (Unix float).
_slash_buckets: cachetools.TTLCache = cachetools.TTLCache(maxsize=5000, ttl=5)

CHAT_LIMIT = 7       # messages per 60-second window
SLASH_COOLDOWN = 5.0  # seconds between slash commands


def is_chat_rate_limited(user_id: int) -> bool:
    """Returns True if this user has hit the 7 msg/min cap. Owner always passes."""
    if _is_owner(user_id):
        return False
    now = time.time()
    bucket = _chat_buckets.get(user_id)
    if bucket is None:
        bucket = []
        _chat_buckets[user_id] = bucket
    # Prune entries older than 60s
    cutoff = now - 60
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= CHAT_LIMIT:
        return True
    bucket.append(now)
    return False


def is_slash_rate_limited(user_id: int) -> bool:
    """Returns True if this user's last slash command was < 5s ago. Owner always passes."""
    if _is_owner(user_id):
        return False
    now = time.time()
    last = _slash_buckets.get(user_id)
    if last is not None and (now - last) < SLASH_COOLDOWN:
        return True
    _slash_buckets[user_id] = now
    return False