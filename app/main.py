from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from fastapi.security import OAuth2PasswordRequestForm
from datetime import timedelta
import os

from . import models, schemas, auth, database, create_admin
from .routers import admin, public, learner, media, auth as auth_router, course_payments

models.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="SVARP GLOBAL ACADEMY API")

# Create static directory if it doesn't exist
if not os.path.exists("backend/static/uploads"):
    os.makedirs("backend/static/uploads")

# Security Fix: We removed the global static mount to protect premium paid media.
# Media is served via the authenticated /media/ router.
# app.mount("/static", StaticFiles(directory="backend/static"), name="static")

@app.on_event("startup")
def startup_event():
    create_admin.create_admin_auto()

# CORS Setup
origins = [
    "http://localhost:3000",
    "http://localhost:5173", # Vite default 
    "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(public.router)
app.include_router(auth_router.router)
app.include_router(learner.router)
app.include_router(admin.router)
app.include_router(media.router)
app.include_router(course_payments.router)


@app.get("/")
def read_root():
    return {"message": "Welcome to SVARP LMS API"}


