from datetime import datetime, timedelta
from typing import Optional, Union
import hashlib
import time
import logging
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from app.clients.user_portal_client import user_portal_client, ServiceError
from . import schemas, models, database

logger = logging.getLogger("svarp-lms-auth")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/token", auto_error=False)


# ── In-Memory Auth Cache ─────────────────────────────────────────────────────
# Caches validated token results so repeated requests with the same token
# skip the expensive validate_token + get_user round-trips.
# Key: SHA-256 hash of the bearer token
# Value: (user_id, user_info_dict, expires_at_monotonic)

AUTH_CACHE_TTL_SECONDS = 300  # Cache valid for 5 minutes
_auth_cache: dict[str, tuple[schemas.User, float]] = {}
_CACHE_MAX_SIZE = 500  # Prevent unbounded growth


def _cache_key(token: str) -> str:
    """Hash the token so we don't store raw tokens in memory."""
    return hashlib.sha256(token.encode()).hexdigest()


def _cache_get(token: str) -> Optional[schemas.User]:
    """Return schemas.User if token is cached and not expired."""
    key = _cache_key(token)
    entry = _auth_cache.get(key)
    if entry is None:
        return None
    user_schema, expires_at = entry
    if time.monotonic() > expires_at:
        # Expired — remove and return miss
        _auth_cache.pop(key, None)
        return None
    return user_schema


def _cache_set(token: str, user_schema: schemas.User):
    """Store validated auth User schema in cache."""
    # Evict expired entries if cache is getting large
    if len(_auth_cache) >= _CACHE_MAX_SIZE:
        now = time.monotonic()
        expired_keys = [k for k, (_, exp) in _auth_cache.items() if now > exp]
        for k in expired_keys:
            del _auth_cache[k]
        # If still too large, clear oldest half
        if len(_auth_cache) >= _CACHE_MAX_SIZE:
            sorted_keys = sorted(_auth_cache, key=lambda k: _auth_cache[k][1])
            for k in sorted_keys[:len(sorted_keys) // 2]:
                del _auth_cache[k]

    key = _cache_key(token)
    _auth_cache[key] = (user_schema, time.monotonic() + AUTH_CACHE_TTL_SECONDS)


def _cache_invalidate(token: str):
    """Remove a token from the cache (e.g. on logout)."""
    _auth_cache.pop(_cache_key(token), None)


# ── Auth Dependencies ────────────────────────────────────────────────────────

async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(database.get_db)
) -> schemas.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception

    # ── 1. Fast path: Memory Cache Hit (0ms, 0 DB queries, 0 HTTP calls) ─────
    cached_user = _cache_get(token)
    if cached_user is not None:
        return cached_user

    # ── 2. Cache Miss: Validate token & resolve user ─────────────────────────
    try:
        # Quick JWT claims inspection
        claims = {}
        try:
            claims = jwt.get_unverified_claims(token)
            exp = claims.get("exp")
            if exp and time.time() > exp:
                raise credentials_exception
        except JWTError:
            pass

        sub = claims.get("sub")
        email = claims.get("email")
        roles = claims.get("roles", [])
        if not roles and claims.get("role"):
            roles = [claims.get("role")]
        
        def _resolve_primary_role(r_list: list[str]) -> str:
            if "admin" in r_list:
                return "admin"
            if "instructor" in r_list or "teacher" in r_list:
                return "instructor"
            if "instructor_pending" in r_list:
                return "instructor_pending"
            return "learner"

        primary_role = _resolve_primary_role(roles)

        db_user = None
        if sub:
            db_user = db.query(models.User).filter(models.User.id == str(sub)).first()
        if not db_user and email:
            db_user = db.query(models.User).filter(models.User.email == email).first()

        # If user already exists in local DB and is valid, use it directly
        if db_user:
            if not db_user.is_active:
                raise HTTPException(status_code=400, detail="Inactive user")

            user_schema = schemas.User(
                id=db_user.id,
                email=db_user.email,
                full_name=db_user.full_name or (email.split("@")[0].title() if email else "Learner"),
                role=db_user.role or primary_role,
                is_suspended=db_user.is_suspended,
                created_at=db_user.created_at if db_user.created_at else datetime.utcnow(),
            )
            _cache_set(token, user_schema)
            return user_schema

        # If user is not yet in local DB, fetch details from Central User Portal
        user_info = None
        try:
            validation = await user_portal_client.validate_token(token)
            if validation and validation.get("is_valid"):
                user_id = str(validation.get("user_id"))
                user_info = await user_portal_client.get_user(user_id=user_id)
        except Exception as ex:
            logger.warning(f"User Portal validation call failed: {ex}. Using JWT fallback.")

        if user_info:
            user_id = str(user_info.get("user_id") or user_info.get("id") or sub)
            email = user_info.get("email") or email
            full_name = user_info.get("full_name") or (email.split("@")[0].title() if email else "Learner")
            roles = user_info.get("roles", roles)
            primary_role = _resolve_primary_role(roles)
            is_active = user_info.get("is_active", True)
        else:
            if not sub:
                raise credentials_exception
            user_id = str(sub)
            full_name = claims.get("name") or claims.get("full_name") or (email.split("@")[0].title() if email else "Learner")
            is_active = True

        if not is_active:
            raise HTTPException(status_code=400, detail="Inactive user")

        # Sync user to local DB once
        db_user = models.User(
            id=user_id,
            email=email,
            full_name=full_name,
            role=primary_role,
            is_active=True,
            is_suspended=False,
            hashed_password=""
        )
        db.merge(db_user)
        db.commit()

        user_schema = schemas.User(
            id=user_id,
            email=email,
            full_name=full_name,
            role=primary_role,
            is_suspended=False,
            created_at=datetime.utcnow(),
        )
        _cache_set(token, user_schema)
        return user_schema

    except ServiceError as se:
        raise HTTPException(status_code=se.status_code, detail="Authentication service temporarily unavailable")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication exception: {e}")
        raise credentials_exception


async def get_current_user_optional(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(database.get_db)
) -> Optional[schemas.User]:
    if not token:
        return None
    try:
        return await get_current_user(token, db)
    except Exception:
        return None


def require_admin(current_user: schemas.User = Depends(get_current_user)) -> schemas.User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def require_instructor(current_user: schemas.User = Depends(get_current_user)) -> schemas.User:
    if current_user.role not in ["instructor", "admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Instructor or Admin access required")
    return current_user


def require_learner(current_user: schemas.User = Depends(get_current_user)) -> schemas.User:
    return current_user

