"""
HTTP client for communicating with Central User Portal service from svarp-lms.
Handles authentication, registration, token validation, user profile lookup, and updates via /api/v1/external.
"""
import os
import logging
from typing import Optional, Any
import httpx
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logger = logging.getLogger("svarp-lms-user-portal")

USER_PORTAL_URL = os.getenv("USER_PORTAL_URL", "http://localhost:8000").rstrip("/")
USER_PORTAL_API_KEY = os.getenv("USER_PORTAL_API_KEY", "")
USER_PORTAL_API_SECRET = os.getenv("USER_PORTAL_API_SECRET", "")

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"UserPortal {status_code}: {detail}")


class UserPortalClient:
    @property
    def base_url(self) -> str:
        url = os.getenv("USER_PORTAL_URL", "http://localhost:8000").rstrip("/")
        if url.endswith("/api/v1"):
            url = url[:-7].rstrip("/")
        return url

    @property
    def api_key(self) -> str:
        return os.getenv("USER_PORTAL_API_KEY", "")

    @property
    def api_secret(self) -> str:
        return os.getenv("USER_PORTAL_API_SECRET", "")

    def _auth_headers(self, bearer_token: Optional[str] = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        if self.api_secret:
            headers["X-API-SECRET"] = self.api_secret
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        return headers

    def _form_auth_headers(self) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        if self.api_secret:
            headers["X-API-SECRET"] = self.api_secret
        return headers

    async def _request(
        self,
        method: str,
        endpoint: str,
        bearer_token: Optional[str] = None,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> dict | list:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        req_headers = headers or self._auth_headers(bearer_token)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=req_headers,
                    json=json,
                    data=data,
                    params=params,
                )
            except httpx.ConnectError:
                raise ServiceError(503, "User Portal service is unavailable")
            except httpx.ReadTimeout:
                raise ServiceError(504, "User Portal service timed out")

            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail", response.text)
                except Exception:
                    detail = response.text
                raise ServiceError(response.status_code, str(detail))

            if response.status_code == 204 or not response.content:
                return {}

            return response.json()

    async def login(self, email: str, password: str) -> dict:
        """Authenticate user via OAuth2 password flow on portal-user."""
        return await self._request(
            "POST",
            "/api/v1/external/login",
            data={"username": email, "password": password},
            headers=self._form_auth_headers(),
        )

    async def create_user(self, email: str, password: str, full_name: str) -> dict:
        """Register a new user on portal-user."""
        return await self._request(
            "POST",
            "/api/v1/external/create-user",
            json={
                "email": email,
                "password": password,
                "full_name": full_name,
            },
        )

    async def get_user(self, user_id: Optional[str] = None, email: Optional[str] = None) -> dict:
        """Retrieve user details by user_id or email."""
        params = {}
        if user_id:
            params["user_id"] = user_id
        if email:
            params["email"] = email
        return await self._request("GET", "/api/v1/external/get-user", params=params)

    async def validate_token(self, token: str) -> dict:
        """Validate token with portal-user."""
        return await self._request(
            "GET",
            "/api/v1/external/validate-token",
            bearer_token=token,
        )

    async def update_user(self, user_id: str, data: dict) -> dict:
        """Update user profile on central portal."""
        return await self._request(
            "PUT",
            "/api/v1/external/update-user",
            json=data,
            params={"user_id": user_id},
        )

    async def list_users(self, skip: int = 0, limit: int = 100, search: Optional[str] = None) -> list:
        """List all users from central portal."""
        params = {"skip": skip, "limit": limit}
        if search:
            params["search"] = search
        return await self._request("GET", "/api/v1/external/list-users", params=params)


user_portal_client = UserPortalClient()
