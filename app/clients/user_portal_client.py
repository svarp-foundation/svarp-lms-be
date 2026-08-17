"""
HTTP client for communicating with Central User Portal service from svarp-lms.
Handles authentication, registration, token validation, user profile lookup, and updates via /api/v1/external.
"""
import os
import logging
from typing import Optional, Any
import httpx
import asyncio
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logger = logging.getLogger("svarp-lms-user-portal")

USER_PORTAL_URL = os.getenv("USER_PORTAL_URL", "http://localhost:8000").rstrip("/")
USER_PORTAL_API_KEY = os.getenv("USER_PORTAL_API_KEY", "")
USER_PORTAL_API_SECRET = os.getenv("USER_PORTAL_API_SECRET", "")

# Reduced timeouts: 10s read (was 20s), 5s connect (was 10s)
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Max retries reduced from 3 to 2
MAX_RETRIES = 2


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"UserPortal {status_code}: {detail}")


class UserPortalClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

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

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a persistent AsyncClient with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30,
                ),
            )
        return self._client

    async def close(self):
        """Close the persistent client. Call during app shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

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

        client = await self._get_client()
        response = None

        for attempt in range(MAX_RETRIES):
            try:
                response = await client.request(
                    method,
                    url,
                    headers=req_headers,
                    json=json,
                    data=data,
                    params=params,
                )
                break
            except httpx.ConnectTimeout:
                if attempt == MAX_RETRIES - 1:
                    raise ServiceError(503, "Connection to User Portal timed out")
                await asyncio.sleep(0.3)
            except httpx.ConnectError:
                if attempt == MAX_RETRIES - 1:
                    raise ServiceError(503, "User Portal service is unavailable")
                await asyncio.sleep(0.3)
            except httpx.ReadTimeout:
                if attempt == MAX_RETRIES - 1:
                    raise ServiceError(504, "User Portal service timed out")
                await asyncio.sleep(0.3)
            except httpx.RequestError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise ServiceError(502, f"Failed to communicate with User Portal: {exc}")
                await asyncio.sleep(0.3)

        if response is None:
            raise ServiceError(502, "Failed to get response from User Portal")

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

    async def create_user(
        self,
        email: str,
        password: str,
        full_name: str,
        roles: Optional[Any] = None,
    ) -> dict:
        """Register a new user on portal-user."""
        return await self._request(
            "POST",
            "/api/v1/external/create-user",
            json={
                "email": email,
                "password": password,
                "full_name": full_name,
                "roles": roles if roles is not None else ["learner"],
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

    async def delete_user(self, user_id: str) -> dict:
        """Soft delete user on central portal."""
        return await self._request(
            "DELETE",
            "/api/v1/external/delete-user",
            params={"user_id": user_id},
        )


user_portal_client = UserPortalClient()
