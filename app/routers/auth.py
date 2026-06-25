from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import timedelta
from jose import jwt, JWTError
from .. import database, models, schemas, auth, utils

router = APIRouter(
    tags=["auth"],
)

@router.post("/token", response_model=schemas.Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(database.get_db)):
    user = db.query(models.User).filter(models.User.email == form_data.username).first()
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
         raise HTTPException(status_code=400, detail="Inactive user")
    if user.is_suspended:
         raise HTTPException(status_code=403, detail="Account suspended")
         
    status_str = "active"
    if user.is_suspended:
        status_str = "suspended"
    elif not user.is_active:
        status_str = "inactive"

    access_token_expires = timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={
            "sub": str(user.id),
            "role": user.role,
            "status": status_str,
            "email": user.email,
            "full_name": user.full_name
        },
        expires_delta=access_token_expires
    )
    refresh_token = auth.create_refresh_token(
         data={"sub": str(user.id)}
    )
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}

@router.post("/refresh", response_model=schemas.Token)
async def refresh_access_token(token_data: schemas.TokenRefresh, db: Session = Depends(database.get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token_data.refresh_token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if not user or not user.is_active or user.is_suspended:
         raise HTTPException(status_code=401, detail="User inactive or suspended")
         
    status_str = "active"

    new_access_token = auth.create_access_token(
        data={
            "sub": str(user.id),
            "role": user.role,
            "status": status_str,
            "email": user.email,
            "full_name": user.full_name
        },
        expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    new_refresh_token = auth.create_refresh_token(
         data={"sub": str(user.id)}
    )
    return {"access_token": new_access_token, "refresh_token": new_refresh_token, "token_type": "bearer"}

@router.post("/register", response_model=schemas.User)
def register_user(user: schemas.UserCreate, db: Session = Depends(database.get_db)):
    db_user = db.query(models.User).filter(models.User.email == user.email).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    hashed_password = auth.get_password_hash(user.password)
    # Default role is LEARNER
    db_user = models.User(email=user.email, full_name=user.full_name, hashed_password=hashed_password, role=models.UserRole.LEARNER)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

@router.get("/users/me", response_model=schemas.User)
async def read_users_me(current_user: models.User = Depends(auth.get_current_user)):
    user_data = utils.fetch_user_membership(current_user.email)
    current_user.membership = user_data.get("membership") if user_data else None
    current_user.profile_picture_url = "/media/profile-picture" if (user_data and user_data.get("profile_picture_path")) else None
    return current_user
