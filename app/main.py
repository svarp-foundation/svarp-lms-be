import logging
import sys

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from fastapi.security import OAuth2PasswordRequestForm
from contextlib import asynccontextmanager
from datetime import timedelta
import os

from . import models, schemas, auth, database
from .utils import STATIC_DIR, UPLOAD_DIR
from .routers import admin, public, learner, media, auth as auth_router, course_payments, webhooks, instructor
from .clients.user_portal_client import user_portal_client
from .middleware.timing_middleware import TimingMiddleware

# ── Structured Logging Configuration ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Create tables if not existing
models.Base.metadata.create_all(bind=database.engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown — close the persistent HTTP client
    await user_portal_client.close()


app = FastAPI(title="SVARP GLOBAL ACADEMY API", lifespan=lifespan)

# ── Middleware (order matters: last added = first executed) ───────────────────

# Ensure static/uploads directory exists
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Mount static files handler
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# CORS Setup
origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Timing Middleware — logs every request with response time
app.add_middleware(TimingMiddleware)

app.include_router(public.router)
app.include_router(auth_router.router)
app.include_router(learner.router)
app.include_router(instructor.router)
app.include_router(admin.router)
app.include_router(media.router)
app.include_router(course_payments.router)
app.include_router(webhooks.router)


@app.get("/")
def read_root():
    return {"message": "Welcome to SVARP LMS API"}
