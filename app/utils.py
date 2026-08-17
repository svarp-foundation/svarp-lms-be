import os
import time
import requests
from typing import Optional

# Base Path Constants
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(APP_DIR)
STATIC_DIR = os.path.join(BACKEND_DIR, "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")

SVARP_ADMIN_API_KEY = os.getenv("SVARP_ADMIN_API_KEY")
SVARP_ADMIN_BASE_URL = (os.getenv("SVARP_ADMIN_BASE_URL") or "").rstrip("/")
SVARP_VERIFY_URL = f"{SVARP_ADMIN_BASE_URL}/admin/verify-user"


# ── Membership Cache ─────────────────────────────────────────────────────────
# Caches fetch_user_membership results to avoid repeated HTTP calls
# for the same user within a short window.
# Key: email (str)
# Value: (membership_data_or_None, expires_at_monotonic)

_MEMBERSHIP_CACHE_TTL = 300  # 5 minutes
_membership_cache: dict[str, tuple[Optional[dict], float]] = {}
_MEMBERSHIP_CACHE_MAX_SIZE = 200


def _membership_cache_get(email: str) -> tuple[bool, Optional[dict]]:
    """Return (hit, data). hit=True means cache entry exists and is fresh."""
    entry = _membership_cache.get(email)
    if entry is None:
        return (False, None)
    data, expires_at = entry
    if time.monotonic() > expires_at:
        _membership_cache.pop(email, None)
        return (False, None)
    return (True, data)


def _membership_cache_set(email: str, data: Optional[dict]):
    """Store membership data in cache."""
    if len(_membership_cache) >= _MEMBERSHIP_CACHE_MAX_SIZE:
        now = time.monotonic()
        expired = [k for k, (_, exp) in _membership_cache.items() if now > exp]
        for k in expired:
            del _membership_cache[k]
        if len(_membership_cache) >= _MEMBERSHIP_CACHE_MAX_SIZE:
            sorted_keys = sorted(_membership_cache, key=lambda k: _membership_cache[k][1])
            for k in sorted_keys[:len(sorted_keys) // 2]:
                del _membership_cache[k]

    _membership_cache[email] = (data, time.monotonic() + _MEMBERSHIP_CACHE_TTL)


def fetch_user_membership(email: str) -> Optional[dict]:
    """
    Fetch user membership and profile data from the external SVARP Admin API.
    Results are cached for 5 minutes to reduce external API overhead.
    """
    # Check cache first
    hit, cached_data = _membership_cache_get(email)
    if hit:
        return cached_data

    # Cache miss — call external API
    try:
        response = requests.get(
            f"{SVARP_VERIFY_URL}?email={email}",
            headers={"X-API-Key": SVARP_ADMIN_API_KEY},
            timeout=5
        )
        if response.status_code == 200:
            user_data = response.json()
            if user_data:
                _membership_cache_set(email, user_data)
                return user_data
    except Exception as e:
        print(f"Error fetching membership for {email}: {e}")

    # Cache negative result too (avoid hammering on repeated failures)
    _membership_cache_set(email, None)
    return None

def is_active_member(membership_data: Optional[dict]) -> bool:
    """
    Check if the membership data indicates an active membership.
    """
    if not membership_data:
        return False
    # Based on the user's request, I'll assume if it's not None, it's an active membership or we check a 'membership' field.
    # The user's JSON had "membership": null. 
    # If the response itself is the user data, we check user_data.get("membership")
    return membership_data.get("membership") is not None
