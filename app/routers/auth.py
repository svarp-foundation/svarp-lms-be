from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from app.clients.user_portal_client import user_portal_client, ServiceError
from .. import schemas, auth, utils

router = APIRouter(
    tags=["auth"],
)


@router.post("/token", response_model=schemas.Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    try:
        auth_res = await user_portal_client.login(
            email=form_data.username,
            password=form_data.password,
        )
        token_str = auth_res.get("access_token")
        return {
            "access_token": token_str,
            "refresh_token": token_str,
            "token_type": auth_res.get("token_type", "bearer"),
        }
    except ServiceError as se:
        raise HTTPException(status_code=se.status_code, detail=se.detail)


@router.post("/refresh", response_model=schemas.Token)
async def refresh_access_token(token_data: schemas.TokenRefresh):
    try:
        val = await user_portal_client.validate_token(token_data.refresh_token)
        if not val.get("is_valid"):
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        return {
            "access_token": token_data.refresh_token,
            "refresh_token": token_data.refresh_token,
            "token_type": "bearer",
        }
    except ServiceError as se:
        raise HTTPException(status_code=se.status_code, detail=se.detail)


@router.post("/register", response_model=schemas.User)
async def register_user(user: schemas.UserCreate):
    try:
        full_name = user.full_name or user.email.split("@")[0].title()
        portal_user = await user_portal_client.create_user(
            email=user.email,
            password=user.password,
            full_name=full_name,
        )
        roles = portal_user.get("roles", [])
        primary_role = "admin" if "admin" in roles else "learner"

        return schemas.User(
            id=str(portal_user.get("user_id")),
            email=portal_user.get("email"),
            full_name=portal_user.get("full_name") or full_name,
            role=primary_role,
            is_suspended=False,
            created_at=datetime.utcnow(),
        )
    except ServiceError as se:
        raise HTTPException(status_code=se.status_code, detail=se.detail)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Registration error: {type(e).__name__}: {str(e)}")


@router.get("/users/me", response_model=schemas.User)
async def read_users_me(current_user: schemas.User = Depends(auth.get_current_user)):
    user_data = utils.fetch_user_membership(current_user.email)
    current_user.membership = user_data.get("membership") if user_data else None
    current_user.profile_picture_url = "/media/profile-picture" if (user_data and user_data.get("profile_picture_path")) else None
    return current_user
