from sqlalchemy.orm import Session
from . import models, schemas
from .certificate_generator import generate_certificate_code
from fastapi import HTTPException

def calculate_dynamic_progress(db: Session, user_id: int, course_id: int) -> int:
    """Calculates completion progress on the fly."""
    total_lessons = db.query(models.Lesson).join(models.Module).filter(
        models.Module.course_id == course_id
    ).count()

    if total_lessons == 0:
        return 0

    completed_lessons_count = db.query(models.LessonCompletion).join(models.Lesson).join(models.Module).filter(
        models.LessonCompletion.user_id == user_id,
        models.Module.course_id == course_id
    ).count()

    return int((completed_lessons_count / total_lessons) * 100)

def check_course_completion(db: Session, user_id: int, course_id: int):
    """
    Evaluates completion rules.
    If conditions met, generates a Certificate if one doesn't exist.
    """
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        return

    # 1. Check if certificate already exists
    existing_cert = db.query(models.Certificate).filter(
        models.Certificate.user_id == user_id,
        models.Certificate.course_id == course_id
    ).first()
    
    if existing_cert:
        return  # Already completed

    # 2. Check Rule: All Lessons Completed
    if course.require_all_lessons_completed:
        progress = calculate_dynamic_progress(db, user_id, course_id)
        if progress < 100:
            return # Missing lessons

    # 3. Check Rule: All Assignments Approved
    if course.require_assignment_approval:
        # Get all assignments for the course
        assignments = db.query(models.Assignment).filter(
            models.Assignment.course_id == course_id
        ).all()
        
        for assignment in assignments:
            # Check if this user has an APPROVED submission for this assignment
            submission = db.query(models.Submission).filter(
                models.Submission.assignment_id == assignment.id,
                models.Submission.user_id == user_id,
                models.Submission.status == models.SubmissionStatus.APPROVED
            ).first()
            if not submission:
                return # Missing an approved assignment

    # --- SUCCESS! TRIGGER CERTIFICATE ---
    
    user = db.query(models.User).filter(models.User.id == user_id).first()
    
    from .certificate_generator import generate_certificate_code
    cert_code = generate_certificate_code()
    
    new_cert = models.Certificate(
        user_id=user_id,
        course_id=course_id,
        certificate_code=cert_code,
        pdf_url=f"/media/{cert_code}.pdf"
    )
    db.add(new_cert)
    db.commit()
    return new_cert
