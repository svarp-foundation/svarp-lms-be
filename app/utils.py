import os
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

def fetch_user_membership(email: str) -> Optional[dict]:
    """
    Fetch user membership and profile data from the external SVARP Admin API.
    """
    try:
        response = requests.get(
            f"{SVARP_VERIFY_URL}?email={email}",
            headers={"X-API-Key": SVARP_ADMIN_API_KEY},
            timeout=5
        )
        if response.status_code == 200:
            user_data = response.json()
            if user_data:
                return user_data
    except Exception as e:
        print(f"Error fetching membership for {email}: {e}")
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
