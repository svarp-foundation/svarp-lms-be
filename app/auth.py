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

AUTH_CACHE_TTL_SECONDS = 60  # Cache valid for 60 seconds
_auth_cache: dict[str, tuple[str, dict, float]] = {}
_CACHE_MAX_SIZE = 500  # Prevent unbounded growth


def _cache_key(token: str) -> str:
    """Hash the token so we don't store raw tokens in memory."""
    return hashlib.sha256(token.encode()).hexdigest()


def _cache_get(token: str) -> Optional[tuple[str, dict]]:
    """Return (user_id, user_info) if token is cached and not expired."""
    key = _cache_key(token)
    entry = _auth_cache.get(key)
    if entry is None:
        return None
    user_id, user_info, expires_at = entry
    if time.monotonic() > expires_at:
        # Expired — remove and return miss
        _auth_cache.pop(key, None)
        return None
    return (user_id, user_info)


def _cache_set(token: str, user_id: str, user_info: dict):
    """Store validated auth result in cache."""
    # Evict expired entries if cache is getting large
    if len(_auth_cache) >= _CACHE_MAX_SIZE:
        now = time.monotonic()
        expired_keys = [k for k, (_, _, exp) in _auth_cache.items() if now > exp]
        for k in expired_keys:
            del _auth_cache[k]
        # If still too large, clear oldest half
        if len(_auth_cache) >= _CACHE_MAX_SIZE:
            sorted_keys = sorted(_auth_cache, key=lambda k: _auth_cache[k][2])
            for k in sorted_keys[:len(sorted_keys) // 2]:
                del _auth_cache[k]

    key = _cache_key(token)
    _auth_cache[key] = (user_id, user_info, time.monotonic() + AUTH_CACHE_TTL_SECONDS)


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
    try:
        # ── Check cache first ────────────────────────────────────────
        cached = _cache_get(token)
        if cached:
            user_id, user_info = cached
        else:
            try:
              # Cache miss — call external User Portal
              validation = await user_portal_client.validate_token(token)
              if not validation.get("is_valid"):
                  raise credentials_exception

              user_id = str(validation.get("user_id"))
              user_info = await user_portal_client.get_user(user_id=user_id)

              # Store in cache for subsequent requests
              _cache_set(token, user_id, user_info)
            except ServiceError as se:
              logger.warning(f"User Portal ServiceError ({se.detail}). Attempting JWT + local DB fallback.")
              try:
                  payload = jwt.get_unverified_claims(token)
                  exp = payload.get("exp")
                  if exp and time.time() > exp:
                      raise credentials_exception

                  sub = payload.get("sub")
                  email = payload.get("email")
                  
                  db_user = None
                  if sub:
                      db_user = db.query(models.User).filter(models.User.id == str(sub)).first()
                  if not db_user and email:
                      db_user = db.query(models.User).filter(models.User.email == email).first()

                  if db_user:
                      user_id = str(db_user.id)
                      user_info = {
                          "id": db_user.id,
                          "email": db_user.email,
                          "full_name": db_user.full_name,
                          "roles": [db_user.role],
                          "is_active": db_user.is_active,
                      }
                      _cache_set(token, user_id, user_info)
                  else:
                      raise credentials_exception
              except (JWTError, Exception):
                  raise credentials_exception

        roles = user_info.get("roles", [])
        primary_role = "admin" if "admin" in roles else "learner"

        if not user_info.get("is_active", True):
            _cache_invalidate(token)
            raise HTTPException(status_code=400, detail="Inactive user")

        email = user_info.get("email")
        full_name = user_info.get("full_name") or (email.split("@")[0].title() if email else "")

        # Auto-sync with local DB for FK integrity
        db_user = db.query(models.User).filter(models.User.id == user_id).first()
        if not db_user and email:
            db_user = db.query(models.User).filter(models.User.email == email).first()

        if db_user:
            db_user.id = user_id
            db_user.email = email
            db_user.full_name = full_name
            db_user.role = primary_role
            db_user.is_active = True
            db.commit()
            db.refresh(db_user)
        else:
            db_user = models.User(
                id=user_id,
                email=email,
                full_name=full_name,
                role=primary_role,
                is_active=True,
                is_suspended=False,
                hashed_password=""
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)

        return schemas.User(
            id=user_id,
            email=email,
            full_name=full_name,
            role=primary_role,
            is_suspended=db_user.is_suspended if db_user else False,
            created_at=db_user.created_at if (db_user and db_user.created_at) else datetime.utcnow(),
        )
    except ServiceError as se:
        raise HTTPException(status_code=se.status_code, detail="Authentication service temporarily unavailable")
    except HTTPException:
        raise
    except Exception:
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


def require_learner(current_user: schemas.User = Depends(get_current_user)) -> schemas.User:
    return current_user
