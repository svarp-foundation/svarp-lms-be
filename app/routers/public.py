from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
import os
import requests
from .. import models, schemas, database, auth, utils

router = APIRouter(
    prefix="/public",
    tags=["public"],
)

# Note: SVARP_ADMIN_BASE_URL etc. are now in utils.py

@router.get("/courses", response_model=List[schemas.Course])
def read_public_courses(
    search: Optional[str] = None,
    skip: int = 0, 
    limit: int = 100, 
    db: Session = Depends(database.get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
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
    
    # Apply membership discount
    if current_user:
        user_data = utils.fetch_user_membership(current_user.email)
        if utils.is_active_member(user_data):
            for course in courses:
                if course.is_paid:
                    course.discounted_price = 0.0
    
    return courses

@router.get("/courses/{course_id}", response_model=schemas.PublicCourseDetail)
def read_public_course(
    course_id: int, 
    db: Session = Depends(database.get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    course = db.query(models.Course).filter(
        models.Course.id == course_id, 
        models.Course.status == models.CourseStatus.PUBLISHED,
        models.Course.is_deleted == False
    ).first()
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
        
    # Apply membership discount
    if current_user:
        user_data = utils.fetch_user_membership(current_user.email)
        if utils.is_active_member(user_data):
            if course.is_paid:
                course.discounted_price = 0.0
                
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
    user_data = utils.fetch_user_membership(cert.user.email)
    if user_data:
        profile_path = user_data.get("profile_picture_path")
        if profile_path:
            profile_picture_url = f"{utils.SVARP_ADMIN_BASE_URL}{profile_path}"

    return schemas.PublicCertificateVerification(
        student_name=cert.user.full_name,
        course_title=cert.course.title,
        issue_date=cert.issued_at,
        status=status_text,
        certificate_code=cert.certificate_code,
        profile_picture_url=profile_picture_url
    )
