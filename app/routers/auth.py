from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.clients.user_portal_client import user_portal_client, ServiceError
from .. import schemas, auth, utils, models, database

router = APIRouter(
    tags=["auth"],
)


@router.post("/token", response_model=schemas.Token)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(database.get_db)
):
    try:
        auth_res = await user_portal_client.login(
            email=form_data.username,
            password=form_data.password,
        )
        token_str = auth_res.get("access_token")

        # Query local LMS user for accurate database role (instructor/admin/learner)
        db_user = db.query(models.User).filter(models.User.email == form_data.username).first()
        user_schema = None
        if db_user:
            user_schema = schemas.User(
                id=db_user.id,
                email=db_user.email,
                full_name=db_user.full_name or form_data.username.split("@")[0].title(),
                role=db_user.role or "learner",
                is_suspended=db_user.is_suspended,
                created_at=db_user.created_at,
            )

        return {
            "access_token": token_str,
            "refresh_token": token_str,
            "token_type": auth_res.get("token_type", "bearer"),
            "user": user_schema,
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
async def register_user(user: schemas.UserCreate, db: Session = Depends(database.get_db)):
    try:
        full_name = user.full_name or user.email.split("@")[0].title()
        is_instructor_app = user.role in ["instructor", "instructor_pending"] or bool(user.specialty) or bool(user.bio)
        assigned_role = "instructor_pending" if is_instructor_app else "learner"

        portal_user = await user_portal_client.create_user(
            email=user.email,
            password=user.password,
            full_name=full_name,
            roles=["learner"],
        )
        user_id = str(portal_user.get("user_id"))
        roles = portal_user.get("roles", [])
        primary_role = "admin" if "admin" in roles else assigned_role

        # Sync user to local LMS DB
        db_user = db.query(models.User).filter(models.User.id == user_id).first()
        if not db_user:
            db_user = models.User(
                id=user_id,
                email=user.email,
                full_name=full_name,
                role=primary_role,
                is_active=True,
                is_suspended=False,
                hashed_password=""
            )
            db.add(db_user)
        else:
            db_user.role = primary_role
        
        # If instructor application, create application record
        if is_instructor_app:
            existing_app = db.query(models.InstructorApplication).filter(models.InstructorApplication.user_id == user_id).first()
            if not existing_app:
                app_record = models.InstructorApplication(
                    user_id=user_id,
                    specialty=user.specialty,
                    bio=user.bio,
                    status=models.InstructorApplicationStatus.PENDING,
                )
                db.add(app_record)
        
        db.commit()

        return schemas.User(
            id=user_id,
            email=portal_user.get("email") or user.email,
            full_name=portal_user.get("full_name") or full_name,
            role=primary_role,
            is_suspended=False,
            created_at=datetime.utcnow(),
        )
    except ServiceError as se:
        # Check if the user already exists on the portal
        try:
            existing_user = await user_portal_client.get_user(email=user.email)
            if existing_user and existing_user.get("user_id"):
                user_id = str(existing_user.get("user_id"))
                roles = existing_user.get("roles", [])
                primary_role = "admin" if "admin" in roles else ("instructor_pending" if user.role == "instructor" else "learner")
                
                db_user = db.query(models.User).filter(models.User.id == user_id).first()
                if not db_user:
                    db_user = models.User(
                        id=user_id,
                        email=user.email,
                        full_name=existing_user.get("full_name") or full_name,
                        role=primary_role,
                        is_active=True,
                        is_suspended=False,
                        hashed_password=""
                    )
                    db.add(db_user)
                    db.commit()

                return schemas.User(
                    id=user_id,
                    email=existing_user.get("email"),
                    full_name=existing_user.get("full_name") or full_name,
                    role=db_user.role if db_user else primary_role,
                    is_suspended=False,
                    created_at=datetime.utcnow(),
                )
        except Exception:
            pass

        if se.status_code == 400:
            raise HTTPException(status_code=400, detail="This email is already registered. Please log in.")
        raise HTTPException(status_code=se.status_code, detail=se.detail)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Registration could not be completed: {str(e)}")


@router.get("/users/me", response_model=schemas.User)
async def read_users_me(current_user: schemas.User = Depends(auth.get_current_user)):
    user_data = utils.fetch_user_membership(current_user.email)
    current_user.membership = user_data.get("membership") if user_data else None
    current_user.profile_picture_url = "/media/profile-picture" if (user_data and user_data.get("profile_picture_path")) else None
    return current_user
