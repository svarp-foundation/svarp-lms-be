from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

# Load .env from the root of the backend folder
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lms.db")

# Ensure directory exists for SQLite
if SQLALCHEMY_DATABASE_URL.startswith("sqlite:///"):
    sqlite_path = SQLALCHEMY_DATABASE_URL.split(":///")[1]
    db_dir = os.path.dirname(sqlite_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)

# Build engine with appropriate args for each backend
engine_kwargs = {"pool_pre_ping": True}

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL / MySQL connection pooling to remote database
    engine_kwargs["pool_size"] = 20
    engine_kwargs["max_overflow"] = 10
    engine_kwargs["pool_recycle"] = 1800
    engine_kwargs["pool_timeout"] = 30

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    **engine_kwargs
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
