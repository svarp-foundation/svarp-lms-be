from datetime import datetime, timedelta
from typing import Optional, Union
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from app.clients.user_portal_client import user_portal_client, ServiceError
from . import schemas, models, database

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/token", auto_error=False)


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
        validation = await user_portal_client.validate_token(token)
        if not validation.get("is_valid"):
            raise credentials_exception

        user_id = str(validation.get("user_id"))
        user_info = await user_portal_client.get_user(user_id=user_id)

        roles = user_info.get("roles", [])
        primary_role = "admin" if "admin" in roles else "learner"

        if not user_info.get("is_active", True):
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
        raise HTTPException(status_code=se.status_code, detail=se.detail)
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

