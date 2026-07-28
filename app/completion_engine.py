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

    print(f"[DEBUG] completed_lessons_count: {completed_lessons_count}")

    # Add assignments that have been submitted but not yet approved (Submitted or Under Review)
    submitted_assignment_lessons_count = db.query(models.Submission).join(models.Assignment).join(models.Lesson).join(models.Module).filter(
        models.Submission.user_id == user_id,
        models.Module.course_id == course_id,
        models.Submission.status.in_([models.SubmissionStatus.SUBMITTED, models.SubmissionStatus.UNDER_REVIEW])
    ).filter(
        # Avoid double counting if already in LessonCompletion
        ~models.Lesson.id.in_(
            db.query(models.LessonCompletion.lesson_id).filter(models.LessonCompletion.user_id == user_id)
        )
    ).count()

    print(f"[DEBUG] submitted_assignment_lessons_count: {submitted_assignment_lessons_count}")
    print(f"[DEBUG] total_lessons: {total_lessons}")

    return int(((completed_lessons_count + submitted_assignment_lessons_count) / total_lessons) * 100)

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

    # 4. Check Final Assignment (Course-Level Assignment)
    if course.require_final_assignment:
        db_final = db.query(models.Assignment).filter(
            models.Assignment.course_id == course_id,
            models.Assignment.lesson_id == None
        ).first()
        if db_final:
            submission = db.query(models.Submission).filter(
                models.Submission.assignment_id == db_final.id,
                models.Submission.user_id == user_id,
                models.Submission.status == models.SubmissionStatus.APPROVED
            ).first()
            if not submission:
                return # Final Assignment not approved yet

    # --- SUCCESS! TRIGGER CERTIFICATE ---
    
    user = db.query(models.User).filter(models.User.id == str(user_id)).first()
    if not user:
        user = db.query(models.User).filter(models.User.email == str(user_id)).first()
    if not user:
        return None


    # Check profile verification before generating certificate
    import os
    import requests
    SVARP_ADMIN_API_KEY = os.getenv("SVARP_ADMIN_API_KEY")
    SVARP_ADMIN_BASE_URL = (os.getenv("SVARP_ADMIN_BASE_URL") or "").rstrip("/")
    SVARP_VERIFY_URL = f"{SVARP_ADMIN_BASE_URL}/admin/verify-user"
    
    try:
        verify_response = requests.get(
            f"{SVARP_VERIFY_URL}?email={user.email}",
            headers={"X-API-Key": SVARP_ADMIN_API_KEY},
            timeout=5
        )
        if verify_response.status_code == 200:
            verification_data = verify_response.json()
            readiness = verification_data.get("payment_readiness") or {}
            if not readiness.get("ready"):
                print(f"[completion_engine] Profile not ready for certificate: {user.email}")
                return None
        else:
            print(f"[completion_engine] Verification system returned status {verify_response.status_code} for {user.email}")
            return None
    except Exception as e:
        print(f"[completion_engine] Verification system error: {e}")
        return None
    
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
