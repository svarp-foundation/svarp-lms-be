from sqlalchemy import func
from sqlalchemy.orm import Session
from . import models, schemas
from .certificate_generator import generate_certificate_code
from fastapi import HTTPException
from typing import Dict, List, Set


def calculate_dynamic_progress(db: Session, user_id: str, course_id: int) -> int:
    """Calculates completion progress on the fly in minimal queries."""
    total_lessons = (
        db.query(func.count(models.Lesson.id))
        .join(models.Module, models.Module.id == models.Lesson.module_id)
        .filter(models.Module.course_id == course_id)
        .scalar()
        or 0
    )

    if total_lessons == 0:
        return 0

    completed_ids: Set[int] = set(
        x[0] for x in (
            db.query(models.LessonCompletion.lesson_id)
            .join(models.Lesson, models.Lesson.id == models.LessonCompletion.lesson_id)
            .join(models.Module, models.Module.id == models.Lesson.module_id)
            .filter(
                models.LessonCompletion.user_id == str(user_id),
                models.Module.course_id == course_id
            )
            .all()
        )
    )

    submitted_ids: Set[int] = set(
        x[0] for x in (
            db.query(models.Assignment.lesson_id)
            .join(models.Submission, models.Submission.assignment_id == models.Assignment.id)
            .join(models.Lesson, models.Lesson.id == models.Assignment.lesson_id)
            .join(models.Module, models.Module.id == models.Lesson.module_id)
            .filter(
                models.Submission.user_id == str(user_id),
                models.Module.course_id == course_id,
                models.Submission.status.in_([models.SubmissionStatus.SUBMITTED, models.SubmissionStatus.UNDER_REVIEW]),
                models.Assignment.lesson_id != None
            )
            .all()
        )
    )

    done_count = len(completed_ids | submitted_ids)
    return min(100, int((done_count / total_lessons) * 100))


def calculate_dynamic_progress_batch(db: Session, user_id: str, course_ids: List[int]) -> Dict[int, int]:
    """Calculates completion progress for multiple courses simultaneously in 3 bulk queries."""
    if not course_ids:
        return {}

    # 1. Total lessons per course
    total_counts = dict(
        db.query(models.Module.course_id, func.count(models.Lesson.id))
        .join(models.Lesson, models.Lesson.module_id == models.Module.id)
        .filter(models.Module.course_id.in_(course_ids))
        .group_by(models.Module.course_id)
        .all()
    )

    # 2. Completed lessons per course
    completed_rows = (
        db.query(models.Module.course_id, models.LessonCompletion.lesson_id)
        .join(models.Lesson, models.Lesson.id == models.LessonCompletion.lesson_id)
        .join(models.Module, models.Module.id == models.Lesson.module_id)
        .filter(
            models.LessonCompletion.user_id == str(user_id),
            models.Module.course_id.in_(course_ids)
        )
        .all()
    )

    # 3. Submitted assignment lessons per course
    submitted_rows = (
        db.query(models.Module.course_id, models.Assignment.lesson_id)
        .join(models.Submission, models.Submission.assignment_id == models.Assignment.id)
        .join(models.Lesson, models.Lesson.id == models.Assignment.lesson_id)
        .join(models.Module, models.Module.id == models.Lesson.module_id)
        .filter(
            models.Submission.user_id == str(user_id),
            models.Module.course_id.in_(course_ids),
            models.Submission.status.in_([models.SubmissionStatus.SUBMITTED, models.SubmissionStatus.UNDER_REVIEW]),
            models.Assignment.lesson_id != None
        )
        .all()
    )

    done_per_course: Dict[int, Set[int]] = {cid: set() for cid in course_ids}
    for cid, lid in completed_rows:
        done_per_course[cid].add(lid)
    for cid, lid in submitted_rows:
        done_per_course[cid].add(lid)

    result: Dict[int, int] = {}
    for cid in course_ids:
        tot = total_counts.get(cid, 0)
        done = len(done_per_course.get(cid, set()))
        result[cid] = min(100, int((done / tot) * 100)) if tot > 0 else 0

    return result

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

    # 3. Check Rule: All Assignments Completed / Submitted (Non-rejected)
    if course.require_assignment_approval:
        assignments = db.query(models.Assignment).filter(
            models.Assignment.course_id == course_id
        ).all()
        
        for assignment in assignments:
            submission = db.query(models.Submission).filter(
                models.Submission.assignment_id == assignment.id,
                models.Submission.user_id == user_id,
                models.Submission.status.in_([
                    models.SubmissionStatus.APPROVED,
                    models.SubmissionStatus.SUBMITTED,
                    models.SubmissionStatus.UNDER_REVIEW
                ])
            ).first()
            if not submission:
                return # Missing a valid submission

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
                models.Submission.status.in_([
                    models.SubmissionStatus.APPROVED,
                    models.SubmissionStatus.SUBMITTED,
                    models.SubmissionStatus.UNDER_REVIEW
                ])
            ).first()
            if not submission:
                return # Final Assignment not submitted yet

    # --- SUCCESS! TRIGGER CERTIFICATE ---
    
    user = db.query(models.User).filter(models.User.id == str(user_id)).first()
    if not user:
        user = db.query(models.User).filter(models.User.email == str(user_id)).first()
    if not user:
        return None


    # Check profile verification if SVARP membership is connected
    from . import utils

    user_data = utils.fetch_user_membership(user.email)
    if user_data:
        readiness = user_data.get("payment_readiness")
        if isinstance(readiness, dict) and readiness.get("ready") is False:
            print(f"[completion_engine] Profile explicitly not ready for certificate: {user.email}")
            return None
    
    from .certificate_generator import generate_certificate_code
    cert_code = generate_certificate_code()
    
    new_cert = models.Certificate(
        user_id=user.id,
        course_id=course_id,
        certificate_code=cert_code,
        pdf_url=f"/media/{cert_code}.pdf"
    )
    db.add(new_cert)
    db.commit()
    db.refresh(new_cert)
    return new_cert
