from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
import os
import requests
from .. import models, schemas, database

router = APIRouter(
    prefix="/public",
    tags=["public"],
)

SVARP_ADMIN_API_KEY = os.getenv("SVARP_ADMIN_API_KEY")
SVARP_ADMIN_BASE_URL = os.getenv("SVARP_ADMIN_BASE_URL", "https://svarp-website-be.svarp.cloud")
SVARP_VERIFY_URL = f"{SVARP_ADMIN_BASE_URL}/admin/verify-user"

@router.get("/courses", response_model=List[schemas.Course])
def read_public_courses(
    search: Optional[str] = None,
    skip: int = 0, 
    limit: int = 100, 
    db: Session = Depends(database.get_db)
):
    query = db.query(models.Course).filter(
        models.Course.status == models.CourseStatus.PUBLISHED,
        models.Course.is_deleted == False
    )
    
    if search:
        query = query.filter(
            (models.Course.title.ilike(f"%{search}%")) | 
            (models.Course.description.ilike(f"%{search}%"))
        )
        
    courses = query.offset(skip).limit(limit).all()
    return courses

@router.get("/courses/{course_id}", response_model=schemas.PublicCourseDetail)
def read_public_course(course_id: int, db: Session = Depends(database.get_db)):
    course = db.query(models.Course).filter(
        models.Course.id == course_id, 
        models.Course.status == models.CourseStatus.PUBLISHED,
        models.Course.is_deleted == False
    ).first()
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    return course

@router.get("/certificates/verify/{certificate_code}", response_model=schemas.PublicCertificateVerification)
def verify_certificate(certificate_code: str, db: Session = Depends(database.get_db)):
    cert = db.query(models.Certificate).filter(models.Certificate.certificate_code == certificate_code).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    
    status_text = "Valid"
    if cert.revoked_at:
        status_text = "Revoked"
        
    # Fetch profile picture from SVARP Admin API
    profile_picture_url = None
    try:
        verify_response = requests.get(
            f"{SVARP_VERIFY_URL}?email={cert.user.email}",
            headers={"X-API-Key": SVARP_ADMIN_API_KEY},
            timeout=5
        )
        if verify_response.status_code == 200:
            user_data = verify_response.json()
            if user_data:
                profile_path = user_data.get("profile_picture_path")
                if profile_path:
                    profile_picture_url = f"{SVARP_ADMIN_BASE_URL}{profile_path}"
    except Exception as e:
        print(f"Error fetching profile picture for public verification: {e}")

    return schemas.PublicCertificateVerification(
        student_name=cert.user.full_name,
        course_title=cert.course.title,
        issue_date=cert.issued_at,
        status=status_text,
        certificate_code=cert.certificate_code,
        profile_picture_url=profile_picture_url
    )
