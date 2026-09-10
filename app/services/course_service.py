"""
Course Service — Shared course operations used by both admin and instructor routers.

Eliminates duplication of:
- parse_course_file()
- create_course_from_parsed_data()
- update_course_curriculum_from_parsed_data()
- review_submission()
"""

import re
import os
from datetime import datetime
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from .. import models, completion_engine
from ..utils import UPLOAD_DIR


# ── Course File Parser (Single Source of Truth) ──────────────────────────────

def parse_course_file(content: str):
    """
    Parse a structured plain text course file into course data and modules.
    
    Supports section delimiters:
    ---COURSE---, ---MODULE---, ---LESSON---, ---END_LESSON---,
    ---ASSIGNMENT---, ---QUESTION---, ---END_ASSIGNMENT---, ---QUIZ---
    
    Returns: (course_data: dict, modules: list[dict])
    """
    lines = content.splitlines()
    course_data = {}
    modules = []
    current_module = None
    current_lesson = None
    current_assignment = None
    current_question = None
    current_state = None  # 'course', 'module', 'lesson_meta', 'lesson_content', 'assignment', 'question'

    for line in lines:
        stripped = line.strip()

        # Check delimiter lines (must start and end with ---)
        if stripped.startswith('---') and stripped.endswith('---') and len(stripped) >= 6:
            tag = stripped.strip('-').strip().upper()

            if tag == 'COURSE':
                current_state = 'course'
                continue
            elif tag == 'MODULE':
                current_module = {'lessons': []}
                modules.append(current_module)
                current_lesson = None
                current_assignment = None
                current_question = None
                current_state = 'module'
                continue
            elif tag == 'LESSON':
                if current_module is None:
                    current_module = {'title': 'General Module', 'lessons': []}
                    modules.append(current_module)
                current_lesson = {'lesson_type': 'text', 'content': ''}
                current_module['lessons'].append(current_lesson)
                current_assignment = None
                current_question = None
                current_state = 'lesson_meta'
                continue
            elif tag in ('ASSIGNMENT', 'QUIZ'):
                if current_lesson is None:
                    if current_module is None:
                        current_module = {'title': 'General Module', 'lessons': []}
                        modules.append(current_module)
                    current_lesson = {'title': 'Assessment', 'lesson_type': 'assignment', 'content': ''}
                    current_module['lessons'].append(current_lesson)
                else:
                    current_lesson['lesson_type'] = 'assignment'
                current_assignment = {'questions': []}
                current_lesson['assignment_data'] = current_assignment
                current_question = None
                current_state = 'assignment'
                continue
            elif tag == 'QUESTION':
                if current_assignment is None:
                    if current_lesson is None:
                        if current_module is None:
                            current_module = {'title': 'General Module', 'lessons': []}
                            modules.append(current_module)
                        current_lesson = {'title': 'Assessment', 'lesson_type': 'assignment', 'content': ''}
                        current_module['lessons'].append(current_lesson)
                    current_assignment = {'questions': []}
                    current_lesson['assignment_data'] = current_assignment
                current_question = {'options': []}
                current_assignment['questions'].append(current_question)
                current_state = 'question'
                continue
            elif tag == 'END_ASSIGNMENT':
                current_question = None
                current_state = 'lesson_meta' if current_lesson else None
                continue
            elif tag == 'END_LESSON':
                current_assignment = None
                current_question = None
                current_state = 'module' if current_module else None
                continue

        # Processing contents based on state
        if current_state == 'course':
            if ':' in line:
                key, val = line.split(':', 1)
                course_data[key.strip().lower().replace(' ', '_')] = val.strip()

        elif current_state == 'module':
            if ':' in line and current_module is not None:
                key, val = line.split(':', 1)
                current_module[key.strip().lower().replace(' ', '_')] = val.strip()

        elif current_state == 'lesson_meta':
            if ':' in line and current_lesson is not None:
                key, val = line.split(':', 1)
                k = key.strip().lower().replace(' ', '_')
                if k == 'content':
                    current_lesson['content'] = val.strip()
                    current_state = 'lesson_content'
                else:
                    current_lesson[k] = val.strip()

        elif current_state == 'lesson_content':
            if current_lesson is not None:
                if current_lesson['content']:
                    current_lesson['content'] += '\n' + line
                else:
                    current_lesson['content'] = line

        elif current_state == 'assignment':
            if ':' in line and current_assignment is not None:
                key, val = line.split(':', 1)
                current_assignment[key.strip().lower().replace(' ', '_')] = val.strip()

        elif current_state == 'question':
            if current_question is not None:
                if line.strip().startswith('Option:'):
                    opt_body = line.strip()[len('Option:'):].strip()
                    is_correct = 'is_correct=true' in opt_body.lower()
                    opt_text = re.sub(
                        r'\(is_correct=(true|false)\)', '', opt_body, flags=re.IGNORECASE
                    ).strip()
                    current_question['options'].append({'text': opt_text, 'is_correct': is_correct})
                elif ':' in line:
                    key, val = line.split(':', 1)
                    current_question[key.strip().lower().replace(' ', '_')] = val.strip()

    return course_data, modules


# ── Bulk Course Creation from Parsed Data ────────────────────────────────────

def create_course_from_parsed_data(
    db: Session,
    course_data: dict,
    modules_data: list,
    instructor_id: str = None
):
    """
    Create a new course with modules, lessons, assignments, and questions
    from parsed course file data. Uses flush() for efficient batched inserts.
    
    Returns the created Course ORM object.
    """
    db_course = models.Course(
        title=course_data.get('title', 'Imported Course'),
        description=course_data.get('description', ''),
        price=float(course_data.get('price', 0)),
        is_paid=course_data.get('is_paid', 'false').lower() == 'true',
        status=course_data.get('status', models.CourseStatus.DRAFT),
        passing_score=int(course_data.get('passing_score', 70)),
        instructor_id=instructor_id,
        is_deleted=False,
    )
    db.add(db_course)
    db.flush()

    _build_curriculum(db, db_course.id, modules_data)

    db.commit()
    db.refresh(db_course)
    return db_course


def update_course_curriculum_from_parsed_data(
    db: Session,
    course_id: int,
    course_data: dict,
    modules_data: list,
):
    """
    Update an existing course's metadata and replace its entire curriculum
    (modules, lessons, assignments, questions) from parsed file data.
    """
    db_course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()

    if not db_course:
        return None

    # Update metadata if present in file
    if course_data.get('title'):
        db_course.title = course_data['title']
    if course_data.get('description'):
        db_course.description = course_data['description']
    if 'price' in course_data:
        try:
            db_course.price = float(course_data['price'])
        except (ValueError, TypeError):
            pass
    if 'is_paid' in course_data:
        db_course.is_paid = str(course_data['is_paid']).lower() == 'true'
    if 'passing_score' in course_data:
        try:
            db_course.passing_score = int(course_data['passing_score'])
        except (ValueError, TypeError):
            pass

    # Delete existing curriculum (preserving course wishlists)
    _purge_course_curriculum_and_media(db, course_id, purge_wishlists=False)

    # Rebuild curriculum from file
    _build_curriculum(db, course_id, modules_data)

    db.commit()
    db.refresh(db_course)
    return db_course


def _purge_course_curriculum_and_media(db: Session, course_id: int, purge_wishlists: bool = False):
    """
    Safely purges all child curriculum, assignments, submissions, questions,
    options, completions, comments, and unlinks media files in strict
    foreign key dependency order.
    """
    # 1. Collect all assignments (both course-level and lesson-level)
    direct_assignment_ids = [
        a.id for a in db.query(models.Assignment.id).filter(models.Assignment.course_id == course_id).all()
    ]
    lesson_assignment_ids = [
        a.id for a in db.query(models.Assignment.id).join(models.Lesson).join(models.Module).filter(
            models.Module.course_id == course_id
        ).all()
    ]
    all_assignment_ids = list(set(direct_assignment_ids + lesson_assignment_ids))

    if all_assignment_ids:
        # Submissions & AnswerSubmissions
        submissions = db.query(models.Submission).filter(
            models.Submission.assignment_id.in_(all_assignment_ids)
        ).all()
        for s in submissions:
            if s.file_url:
                delete_physical_file(s.file_url)

        submission_ids = [s.id for s in submissions]
        if submission_ids:
            db.query(models.AnswerSubmission).filter(
                models.AnswerSubmission.submission_id.in_(submission_ids)
            ).delete(synchronize_session=False)

            db.query(models.Submission).filter(
                models.Submission.id.in_(submission_ids)
            ).delete(synchronize_session=False)

        # Questions & Options & Question-linked AnswerSubmissions
        question_ids = [
            q.id for q in db.query(models.Question.id).filter(
                models.Question.assignment_id.in_(all_assignment_ids)
            ).all()
        ]
        if question_ids:
            db.query(models.AnswerSubmission).filter(
                models.AnswerSubmission.question_id.in_(question_ids)
            ).delete(synchronize_session=False)

            db.query(models.QuestionOption).filter(
                models.QuestionOption.question_id.in_(question_ids)
            ).delete(synchronize_session=False)

            db.query(models.Question).filter(
                models.Question.id.in_(question_ids)
            ).delete(synchronize_session=False)

        # Delete all assignments
        db.query(models.Assignment).filter(
            models.Assignment.id.in_(all_assignment_ids)
        ).delete(synchronize_session=False)

    # 2. Collect all lessons & modules
    lesson_ids = [
        l.id for l in db.query(models.Lesson.id).join(models.Module).filter(
            models.Module.course_id == course_id
        ).all()
    ]

    if lesson_ids:
        # Completions and comments
        db.query(models.LessonCompletion).filter(
            models.LessonCompletion.lesson_id.in_(lesson_ids)
        ).delete(synchronize_session=False)

        db.query(models.LessonComment).filter(
            models.LessonComment.lesson_id.in_(lesson_ids)
        ).delete(synchronize_session=False)

        # Lesson video files
        lessons = db.query(models.Lesson).filter(models.Lesson.id.in_(lesson_ids)).all()
        for l in lessons:
            if l.video_url:
                delete_physical_file(l.video_url)

        # Delete lessons
        db.query(models.Lesson).filter(
            models.Lesson.id.in_(lesson_ids)
        ).delete(synchronize_session=False)

    # Delete modules
    db.query(models.Module).filter(
        models.Module.course_id == course_id
    ).delete(synchronize_session=False)

    # Delete wishlists if explicitly requested (e.g. on hard delete)
    if purge_wishlists:
        db.query(models.Wishlist).filter(
            models.Wishlist.course_id == course_id
        ).delete(synchronize_session=False)

    db.flush()


def _build_curriculum(db: Session, course_id: int, modules_data: list):
    """Build modules, lessons, assignments, and questions from parsed data."""
    for m_idx, m_data in enumerate(modules_data):
        db_module = models.Module(
            course_id=course_id,
            title=m_data.get('title', f"Module {m_idx + 1}"),
            description=m_data.get('description', ''),
            order=m_idx + 1
        )
        db.add(db_module)
        db.flush()

        for l_idx, l_data in enumerate(m_data.get('lessons', [])):
            db_lesson = models.Lesson(
                module_id=db_module.id,
                title=l_data.get('title', f"Lesson {l_idx + 1}"),
                content=l_data.get('content', ''),
                video_url=l_data.get('video_url', None),
                lesson_type=l_data.get('lesson_type', 'text'),
                order=l_idx + 1
            )
            db.add(db_lesson)
            db.flush()

            assignment_data = l_data.get('assignment')
            if assignment_data:
                db_assignment = models.Assignment(
                    lesson_id=db_lesson.id,
                    title=assignment_data.get('title', f"Assignment for {db_lesson.title}"),
                    description=assignment_data.get('description', '')
                )
                db.add(db_assignment)
                db.flush()

                for q_idx, q_data in enumerate(assignment_data.get('questions', [])):
                    db_question = models.Question(
                        assignment_id=db_assignment.id,
                        question_text=q_data.get('question_text', ''),
                        question_type=q_data.get('question_type', 'subjective'),
                        order=q_idx + 1
                    )
                    db.add(db_question)
                    db.flush()

                    for opt_data in q_data.get('options', []):
                        db_option = models.QuestionOption(
                            question_id=db_question.id,
                            option_text=opt_data.get('option_text', ''),
                            is_correct=opt_data.get('is_correct', False)
                        )
                        db.add(db_option)


# ── Submission Review Logic (Single Source of Truth) ──────────────────────────

def review_submission(
    db: Session,
    submission_id: int,
    status_str: str,
    grade: int | None = None,
    feedback: str | None = None,
    current_user_role: str = "admin",
    current_user_id: str | int = None
):
    """
    Review a submission with role-based permissions, automated status handling,
    and completion engine triggering.
    """
    submission = db.query(models.Submission).filter(
        models.Submission.id == submission_id
    ).first()

    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    assignment = db.query(models.Assignment).filter(
        models.Assignment.id == submission.assignment_id
    ).first()

    # Determine course_id for ownership checks and completion triggering
    course_id = None
    if assignment:
        if assignment.course_id:
            course_id = assignment.course_id
        elif assignment.lesson_id:
            lesson = db.query(models.Lesson).filter(models.Lesson.id == assignment.lesson_id).first()
            if lesson:
                module = db.query(models.Module).filter(models.Module.id == lesson.module_id).first()
                if module:
                    course_id = module.course_id

    # Instructor ownership verification
    if current_user_role != "admin" and course_id:
        course = db.query(models.Course).filter(models.Course.id == course_id).first()
        if not course or str(course.instructor_id) != str(current_user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only grade submissions for your own courses."
            )

    # Validate status enum
    try:
        status_enum = models.SubmissionStatus(status_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status: '{status_str}'. Allowed: {[s.value for s in models.SubmissionStatus]}"
        )

    # Apply updates
    submission.status = status_enum.value
    if grade is not None:
        submission.grade = grade
    if feedback is not None:
        submission.feedback = feedback

    # Handle status-specific side effects
    if status_enum == models.SubmissionStatus.APPROVED:
        _handle_submission_approved(db, submission, assignment, course_id)
    elif status_enum in (models.SubmissionStatus.REJECTED, models.SubmissionStatus.RESUBMISSION_REQUIRED):
        _handle_submission_rejected(db, submission, assignment, course_id)

    db.commit()
    db.refresh(submission)
    return submission


def _handle_submission_approved(db: Session, submission, assignment, course_id):
    """On approval: mark lesson complete and check course completion."""
    if assignment and assignment.lesson_id:
        existing_completion = db.query(models.LessonCompletion).filter(
            models.LessonCompletion.user_id == submission.user_id,
            models.LessonCompletion.lesson_id == assignment.lesson_id
        ).first()

        if not existing_completion:
            completion = models.LessonCompletion(
                user_id=submission.user_id,
                lesson_id=assignment.lesson_id
            )
            db.add(completion)

    if course_id:
        completion_engine.check_course_completion(db, submission.user_id, course_id)


def _handle_submission_rejected(db: Session, submission, assignment, course_id):
    """On rejection: remove lesson completion and revoke certificates."""
    if assignment and assignment.lesson_id:
        db.query(models.LessonCompletion).filter(
            models.LessonCompletion.user_id == submission.user_id,
            models.LessonCompletion.lesson_id == assignment.lesson_id
        ).delete()

    if course_id:
        certs = db.query(models.Certificate).filter(
            models.Certificate.user_id == submission.user_id,
            models.Certificate.course_id == course_id,
            models.Certificate.revoked_at == None
        ).all()
        for cert in certs:
            cert.revoked_at = datetime.utcnow()
            cert.revoked_reason = submission.feedback or "Submission rejected during review"
            db.add(cert)


# ── File Utilities ───────────────────────────────────────────────────────────

def delete_physical_file(file_url: str):
    """Safely delete a physical file from the uploads directory."""
    if not file_url or not isinstance(file_url, str):
        return
    try:
        clean_url = file_url.split("?")[0].strip()
        filename = os.path.basename(clean_url)
        if not filename or filename in (".", ".."):
            return
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
    except Exception as e:
        print(f"Failed to delete physical file {file_url}: {e}")


# ── Course Deletion Engine (Safe Conditional Hard Delete & Admin Force Purge) ─

def delete_course_service(
    db: Session,
    course_id: int,
    user_id: str,
    user_role: str,
    force: bool = False
) -> dict:
    """
    Unified course deletion engine implementing Option A (Safe Conditional Deletion)
    with Admin Force Purge capability.

    - If a course has 0 activity (0 enrollments, 0 payments, 0 certificates):
      Hard deletes curriculum and removes physical media files for both Instructors & Admins.
    - If a course has active history:
      - For Instructors: Soft deletes / Archives the course.
      - For Admins with force=False: Soft deletes / Archives the course.
      - For Admins with force=True: Cascades and purges ALL child entities (enrollments,
        submissions, answer submissions, lesson completions, comments, certificates,
        payments, assignments, questions, options, modules, lessons), cleans up physical files,
        logs to audit_logs, and hard deletes the course.
    """
    is_admin = (user_role == models.UserRole.ADMIN.value or user_role == "admin")

    query = db.query(models.Course).filter(models.Course.id == course_id)
    if not is_admin:
        query = query.filter(models.Course.is_deleted == False)

    course = query.first()

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # Instructor ownership check
    if not is_admin:
        if str(course.instructor_id) != str(user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delete this course"
            )
        if force:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only administrators can force hard delete a course with active records"
            )

    # Calculate active activity
    enrollment_count = db.query(models.Enrollment).filter(models.Enrollment.course_id == course_id).count()
    payment_count = db.query(models.CoursePayment).filter(models.CoursePayment.course_id == course_id).count()
    certificate_count = db.query(models.Certificate).filter(models.Certificate.course_id == course_id).count()

    has_history = (enrollment_count > 0 or payment_count > 0 or certificate_count > 0)

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 1: No activity — Safe Hard Delete for both Instructor & Admin
    # ─────────────────────────────────────────────────────────────────────────
    if not has_history:
        # 1. Unlink course cover image
        if course.thumbnail_url:
            delete_physical_file(course.thumbnail_url)

        # 2. Clean up curriculum hierarchy, assignments, questions, and files in strict FK order
        _purge_course_curriculum_and_media(db, course_id, purge_wishlists=True)

        # 3. Remove course entity
        db.delete(course)
        db.commit()

        return {
            "success": True,
            "action": "hard_deleted",
            "message": f"Course '{course.title}' has been permanently deleted along with all curriculum modules."
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 2: Has activity & (force is False OR non-admin) -> Soft Delete / Archive
    # ─────────────────────────────────────────────────────────────────────────
    if not force or not is_admin:
        course.is_deleted = True
        course.status = models.CourseStatus.ARCHIVED.value

        if is_admin:
            log = models.AuditLog(
                admin_id=user_id,
                action_type="soft_delete_course",
                target_entity=f"Course {course_id} ({course.title}) [Archived due to {enrollment_count} learners, {payment_count} payments]"
            )
            db.add(log)

        db.commit()

        return {
            "success": True,
            "action": "soft_deleted",
            "message": f"Course '{course.title}' has {enrollment_count} active learner(s) / payment records. It has been safely archived and hidden from catalogs."
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 3: Has activity & force is True & is_admin -> Complete Hard Purge
    # ─────────────────────────────────────────────────────────────────────────
    # 1. Clean up certificates & files
    certs = db.query(models.Certificate).filter(models.Certificate.course_id == course_id).all()
    for cert in certs:
        if cert.pdf_url:
            delete_physical_file(cert.pdf_url)

    db.query(models.Certificate).filter(models.Certificate.course_id == course_id).delete(synchronize_session=False)

    # 2. Delete payments and enrollments
    db.query(models.CoursePayment).filter(models.CoursePayment.course_id == course_id).delete(synchronize_session=False)
    db.query(models.Enrollment).filter(models.Enrollment.course_id == course_id).delete(synchronize_session=False)

    # 3. Clean up thumbnail
    if course.thumbnail_url:
        delete_physical_file(course.thumbnail_url)

    # 4. Clean up all curriculum, assignments, submissions, questions, options, comments, completions, wishlists
    _purge_course_curriculum_and_media(db, course_id, purge_wishlists=True)

    # 5. Create Audit Log before deleting course entity
    log = models.AuditLog(
        admin_id=user_id,
        action_type="force_hard_delete_course",
        target_entity=f"Course {course_id}: {course.title} (Permanently purged {enrollment_count} enrollments, {payment_count} payments, {certificate_count} certificates)"
    )
    db.add(log)

    # 4. Remove Course (ORM automatically cascades modules, lessons, completions, comments, assignments, questions, options, submissions)
    db.delete(course)
    db.commit()

    return {
        "success": True,
        "action": "force_hard_deleted",
        "message": f"Course '{course.title}' and all associated enrollments, progress, payments, certificates, and media files have been permanently purged."
    }

