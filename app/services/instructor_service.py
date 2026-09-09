"""
Instructor Service — Optimized instructor business logic.

Replaces per-course N+1 queries with batch grouped aggregations.
"""

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from typing import Optional

from .. import models


# ── Optimized Instructor Course Listing ──────────────────────────────────────

def list_instructor_courses_with_counts(
    db: Session,
    instructor_id: str = None,
    is_admin: bool = False,
) -> list:
    """
    List instructor courses with module/lesson/student counts.
    
    Previous: 3 COUNT queries per course (N+1).
    Now: 3 grouped queries + dictionary lookup (constant regardless of course count).
    """
    query = db.query(models.Course).filter(models.Course.is_deleted == False)
    if not is_admin:
        query = query.filter(models.Course.instructor_id == str(instructor_id))

    courses = query.order_by(models.Course.created_at.desc()).all()

    if not courses:
        return []

    course_ids = [c.id for c in courses]

    # Batch: module counts per course (1 query)
    module_counts = dict(
        db.query(models.Module.course_id, func.count(models.Module.id))
        .filter(models.Module.course_id.in_(course_ids))
        .group_by(models.Module.course_id)
        .all()
    )

    # Batch: lesson counts per course (1 query)
    lesson_counts = dict(
        db.query(models.Module.course_id, func.count(models.Lesson.id))
        .join(models.Lesson, models.Lesson.module_id == models.Module.id)
        .filter(models.Module.course_id.in_(course_ids))
        .group_by(models.Module.course_id)
        .all()
    )

    # Batch: student counts per course (1 query)
    student_counts = dict(
        db.query(models.Enrollment.course_id, func.count(models.Enrollment.id))
        .filter(models.Enrollment.course_id.in_(course_ids))
        .group_by(models.Enrollment.course_id)
        .all()
    )

    # Batch: instructor names (1 query)
    instructor_ids = {c.instructor_id for c in courses if c.instructor_id}
    instructor_map = {}
    if instructor_ids:
        instructors = db.query(models.User).filter(models.User.id.in_(instructor_ids)).all()
        instructor_map = {u.id: u.full_name for u in instructors}

    results = []
    for c in courses:
        results.append({
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "thumbnail_url": c.thumbnail_url,
            "status": c.status,
            "is_paid": c.is_paid,
            "price": c.price,
            "passing_score": c.passing_score,
            "require_all_lessons_completed": c.require_all_lessons_completed,
            "require_assignment_approval": c.require_assignment_approval,
            "require_final_assignment": c.require_final_assignment,
            "instructor_id": c.instructor_id,
            "instructor_name": instructor_map.get(c.instructor_id) or "SVARP GLOBAL ACADEMY",
            "created_at": c.created_at,
            "module_count": module_counts.get(c.id, 0),
            "lesson_count": lesson_counts.get(c.id, 0),
            "student_count": student_counts.get(c.id, 0),
        })

    return results


# ── Submission Detail with Eager Loading ─────────────────────────────────────

def get_submission_detail(db: Session, submission_id: int) -> Optional[dict]:
    """
    Get full submission detail with answers, questions, student, and course info.
    Uses eager loading — 1 query instead of N lazy loads.
    """
    sub = db.query(models.Submission).options(
        joinedload(models.Submission.student),
        joinedload(models.Submission.assignment).joinedload(models.Assignment.course),
        joinedload(models.Submission.assignment)
            .joinedload(models.Assignment.lesson)
            .joinedload(models.Lesson.module)
            .joinedload(models.Module.course),
        joinedload(models.Submission.answers)
            .joinedload(models.AnswerSubmission.question),
        joinedload(models.Submission.answers)
            .joinedload(models.AnswerSubmission.selected_option),
    ).filter(models.Submission.id == submission_id).first()

    if not sub:
        return None

    course = None
    if sub.assignment:
        if sub.assignment.course:
            course = sub.assignment.course
        elif sub.assignment.lesson and sub.assignment.lesson.module:
            course = sub.assignment.lesson.module.course

    answers_data = []
    for ans in sub.answers:
        q = ans.question
        selected_opt = ans.selected_option
        answers_data.append({
            "question_id": q.id if q else None,
            "question_text": q.question_text if q else None,
            "question_type": q.question_type if q else None,
            "answer_text": ans.answer_text,
            "selected_option_id": ans.selected_option_id,
            "selected_option_text": selected_opt.option_text if selected_opt else None,
            "is_correct": ans.is_correct
        })

    return {
        "id": sub.id,
        "student_id": sub.user_id,
        "student_name": sub.student.full_name if sub.student else "Learner",
        "student_email": sub.student.email if sub.student else "N/A",
        "course_id": course.id if course else None,
        "course_title": course.title if course else "Unknown Course",
        "assignment_id": sub.assignment_id,
        "assignment_title": sub.assignment.title if sub.assignment else "Assignment",
        "assignment_description": sub.assignment.description if sub.assignment else "",
        "status": sub.status,
        "submitted_at": sub.submitted_at,
        "grade": sub.grade,
        "feedback": sub.feedback,
        "mcq_score": sub.mcq_score,
        "mcq_total": sub.mcq_total,
        "content": sub.content,
        "file_url": sub.file_url,
        "answers": answers_data
    }
