from .database import SessionLocal, engine
from . import models, auth
import sys
import getpass

import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
# Load environment variables from .env file
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_super_user(db, email, password, full_name):
    """Core logic to create or update a super user."""
    hashed_password = auth.get_password_hash(password)
    existing_user = db.query(models.User).filter(models.User.email == email).first()
    
    if existing_user:
        logger.info(f"User with email {email} already exists. Updating details...")
        existing_user.full_name = full_name
        existing_user.hashed_password = hashed_password
        existing_user.role = models.UserRole.ADMIN
        db.commit()
        logger.info(f"Success! Admin user {email} updated.")
        return

    try:
        new_admin = models.User(
            email=email,
            full_name=full_name,
            hashed_password=hashed_password,
            role=models.UserRole.ADMIN
        )
        db.add(new_admin)
        db.commit()
        logger.info(f"Success! Admin user {email} created.")
    except Exception as e:
        logger.error(f"Error creating admin: {e}")
        db.rollback()

def create_admin_interactive():
    """Interactive CLI version."""
    models.Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    
    print("--- Create Admin User ---")

    email = os.getenv("SUPER_USER_EMAIL")
    if not email:
        email = input("Enter Admin Email: ").strip()
    
    if not email:
        print("Email is required.")
        return

    full_name = os.getenv("SUPER_USER_FULL_NAME")
    if not full_name:
        full_name = input("Enter Full Name: ").strip()
        
    password = os.getenv("SUPER_USER_PASSWORD")
    if not password:
        password = getpass.getpass("Enter Password: ").strip()
        confirm_password = getpass.getpass("Confirm Password: ").strip()
        
        if password != confirm_password:
            print("Passwords do not match.")
            return

    if not password:
        print("Password cannot be empty.")
        return

    try:
        create_super_user(db, email, password, full_name)
    finally:
        db.close()

def create_admin_auto():
    """Auto-creation for system startup (non-interactive)."""
    email = os.getenv("SUPER_USER_EMAIL")
    if email:
        logger.info(f"Central Admin User configured for LMS: {email} (Managed by Central User Portal)")


if __name__ == "__main__":
    create_admin_interactive()
