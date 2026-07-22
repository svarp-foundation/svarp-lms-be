from datetime import datetime, timedelta
from typing import Optional, Union
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from app.clients.user_portal_client import user_portal_client, ServiceError
from . import schemas, models

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/token", auto_error=False)


async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> schemas.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
    try:
        validation = await user_portal_client.validate_token(token)
        if not validation.get("is_valid"):
            raise credentials_exception

        user_id = validation.get("user_id")
        user_info = await user_portal_client.get_user(user_id=user_id)

        roles = user_info.get("roles", [])
        primary_role = "admin" if "admin" in roles else "learner"

        if not user_info.get("is_active", True):
            raise HTTPException(status_code=400, detail="Inactive user")

        return schemas.User(
            id=str(user_info.get("user_id")),
            email=user_info.get("email"),
            full_name=user_info.get("full_name") or user_info.get("email", "").split("@")[0].title(),
            role=primary_role,
            is_suspended=False,
            created_at=datetime.utcnow(),
        )
    except ServiceError as se:
        raise HTTPException(status_code=se.status_code, detail=se.detail)
    except HTTPException:
        raise
    except Exception:
        raise credentials_exception


async def get_current_user_optional(token: Optional[str] = Depends(oauth2_scheme)) -> Optional[schemas.User]:
    if not token:
        return None
    try:
        return await get_current_user(token)
    except Exception:
        return None


def require_admin(current_user: schemas.User = Depends(get_current_user)) -> schemas.User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def require_learner(current_user: schemas.User = Depends(get_current_user)) -> schemas.User:
    return current_user
