"""
Instructor Router — Thin HTTP handlers delegating to service layer.

Business logic extracted to:
- app.services.instructor_service (course listing, submission detail)
- app.services.course_service (file parsing, curriculum CRUD, submission review)
"""

import os
import shutil
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from .. import models, schemas, auth, database, completion_engine
from ..utils import UPLOAD_DIR
from ..services import instructor_service, course_service

router = APIRouter(
    prefix="/instructor",
    tags=["instructor"],
)


def _check_course_ownership(course: models.Course, current_user: schemas.User):
    if current_user.role == "admin":
        return True
    if str(course.instructor_id) != str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage this course."
        )
    return True


# ── Dashboard & Analytics ───────────────────────────────────────────────────

@router.get("/stats", response_model=schemas.InstructorStats)
def get_instructor_stats(
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    course_filter = [models.Course.is_deleted == False]
    if current_user.role != "admin":
        course_filter.append(models.Course.instructor_id == str(current_user.id))

    instructor_courses = db.query(models.Course).filter(*course_filter).all()
    course_ids = [c.id for c in instructor_courses]

    total_courses = len(instructor_courses)
    published_courses = sum(1 for c in instructor_courses if c.status == models.CourseStatus.PUBLISHED)

    if not course_ids:
        return schemas.InstructorStats(
            total_courses=0,
            published_courses=0,
            total_students=0,
            pending_reviews=0,
            certificates_issued=0,
            total_revenue=0.0,
            completion_rate=0.0
        )

    total_students = (
        db.query(func.count(distinct(models.Enrollment.user_id)))
        .filter(models.Enrollment.course_id.in_(course_ids))
        .scalar()
        or 0
    )

    pending_reviews = (
        db.query(func.count(models.Submission.id))
        .join(models.Assignment, models.Assignment.id == models.Submission.assignment_id)
        .join(models.Course, models.Course.id == models.Assignment.course_id)
        .filter(
            models.Course.id.in_(course_ids),
            models.Submission.status.in_([
                models.SubmissionStatus.SUBMITTED,
                models.SubmissionStatus.UNDER_REVIEW
            ])
        )
        .scalar()
        or 0
    )

    certificates_issued = (
        db.query(func.count(models.Certificate.id))
        .filter(
            models.Certificate.course_id.in_(course_ids),
            models.Certificate.revoked_at == None
        )
        .scalar()
        or 0
    )

    total_revenue = (
        db.query(func.sum(models.CoursePayment.amount))
        .filter(
            models.CoursePayment.course_id.in_(course_ids),
            models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS
        )
        .scalar()
        or 0.0
    )

    total_enrollments = (
        db.query(func.count(models.Enrollment.id))
        .filter(models.Enrollment.course_id.in_(course_ids))
        .scalar()
        or 0
    )
    completion_rate = round((certificates_issued / total_enrollments * 100), 1) if total_enrollments > 0 else 0.0

    return schemas.InstructorStats(
        total_courses=total_courses,
        published_courses=published_courses,
        total_students=total_students,
        pending_reviews=pending_reviews,
        certificates_issued=certificates_issued,
        total_revenue=float(total_revenue),
        completion_rate=float(completion_rate)
    )


# ── Course Management ────────────────────────────────────────────────────────

@router.get("/courses")
def list_instructor_courses(
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    return instructor_service.list_instructor_courses_with_counts(
        db,
        instructor_id=str(current_user.id),
        is_admin=(current_user.role == "admin"),
    )


@router.post("/courses", response_model=schemas.Course)
def create_instructor_course(
    course: schemas.CourseCreate,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    course_dict = course.dict(exclude={"discounted_price", "instructor_name"})
    course_dict["instructor_id"] = str(current_user.id)
    if "status" not in course_dict or not course_dict["status"]:
        course_dict["status"] = models.CourseStatus.DRAFT

    db_course = models.Course(**course_dict, is_deleted=False)
    db.add(db_course)
    db.commit()
    db.refresh(db_course)
    return db_course


@router.get("/courses/{course_id}")
def get_instructor_course_detail(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    _check_course_ownership(course, current_user)

    modules = db.query(models.Module).filter(
        models.Module.course_id == course_id
    ).order_by(models.Module.order).all()
    modules_data = []

    for m in modules:
        lessons = db.query(models.Lesson).filter(
            models.Lesson.module_id == m.id
        ).order_by(models.Lesson.order).all()
        lessons_data = []
        for l in lessons:
            lesson_dict = {
                "id": l.id,
                "title": l.title,
                "lesson_type": l.lesson_type,
                "content": l.content,
                "video_url": l.video_url,
                "order": l.order,
                "module_id": l.module_id,
                "assignment": None
            }
            if l.assignment:
                questions_data = []
                for q in l.assignment.questions:
                    questions_data.append({
                        "id": q.id,
                        "question_text": q.question_text,
                        "question_type": q.question_type,
                        "order": q.order,
                        "options": [
                            {"id": o.id, "option_text": o.option_text, "is_correct": o.is_correct}
                            for o in q.options
                        ]
                    })
                lesson_dict["assignment"] = {
                    "id": l.assignment.id,
                    "title": l.assignment.title,
                    "description": l.assignment.description,
                    "due_date": l.assignment.due_date,
                    "questions": questions_data
                }
            lessons_data.append(lesson_dict)

        modules_data.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "order": m.order,
            "lessons": lessons_data
        })

    return {
        "id": course.id,
        "title": course.title,
        "description": course.description,
        "thumbnail_url": course.thumbnail_url,
        "status": course.status,
        "passing_score": course.passing_score,
        "require_all_lessons_completed": course.require_all_lessons_completed,
        "require_assignment_approval": course.require_assignment_approval,
        "require_final_assignment": course.require_final_assignment,
        "is_paid": course.is_paid,
        "price": course.price,
        "instructor_id": course.instructor_id,
        "instructor_name": (
            course.instructor.full_name
            if (course.instructor and course.instructor.full_name)
            else "SVARP GLOBAL ACADEMY"
        ),
        "created_at": course.created_at,
        "modules": modules_data
    }


@router.put("/courses/{course_id}", response_model=schemas.Course)
def update_instructor_course(
    course_id: int,
    course_update: schemas.CourseUpdate,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    _check_course_ownership(course, current_user)

    update_data = course_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(course, key, value)

    db.commit()
    db.refresh(course)
    return course


@router.delete("/courses/{course_id}")
def delete_instructor_course(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    return course_service.delete_course_service(
        db=db,
        course_id=course_id,
        user_id=current_user.id,
        user_role=current_user.role,
        force=False
    )


@router.post("/courses/import-bundle", status_code=status.HTTP_201_CREATED)
@router.post("/courses/bulk-create", status_code=status.HTTP_201_CREATED)
async def import_course_bundle(
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="File must be a .txt file")

    content = await file.read()
    decoded_content = content.decode('utf-8')

    try:
        parsed_course, modules_data = course_service.parse_course_file(decoded_content)

        if not parsed_course or not parsed_course.get('title'):
            raise HTTPException(status_code=400, detail="Invalid course file format. Missing COURSE title.")

        db_course = course_service.create_course_from_parsed_data(
            db, parsed_course, modules_data, instructor_id=str(current_user.id)
        )

        return {
            "message": "Course created successfully",
            "course_id": db_course.id,
            "title": db_course.title
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to parse or create course: {str(e)}")


@router.post("/courses/{course_id}/update-from-file", status_code=status.HTTP_200_OK)
async def update_instructor_course_from_file(
    course_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="File must be a .txt file")

    db_course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()
    if not db_course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")
    _check_course_ownership(db_course, current_user)

    content = await file.read()
    decoded_content = content.decode('utf-8')

    try:
        parsed_course, modules_data = course_service.parse_course_file(decoded_content)
        if not parsed_course:
            raise HTTPException(status_code=400, detail="Course data missing in file")

        course_service.update_course_curriculum_from_parsed_data(
            db, course_id, parsed_course, modules_data
        )

        return {"message": "Course curriculum updated from file successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to update course from file: {str(e)}")


# ── Module Management ────────────────────────────────────────────────────────

@router.post("/courses/{course_id}/modules")
def add_instructor_module(
    course_id: int,
    module: schemas.ModuleCreate,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    _check_course_ownership(course, current_user)

    max_order = db.query(func.max(models.Module.order)).filter(
        models.Module.course_id == course_id
    ).scalar() or 0

    db_module = models.Module(
        course_id=course_id,
        title=module.title,
        description=module.description,
        order=max_order + 1
    )
    db.add(db_module)
    db.commit()
    db.refresh(db_module)
    return db_module


@router.put("/modules/{module_id}")
def update_instructor_module(
    module_id: int,
    module_update: schemas.ModuleCreate,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    db_module = db.query(models.Module).filter(models.Module.id == module_id).first()
    if not db_module:
        raise HTTPException(status_code=404, detail="Module not found")

    _check_course_ownership(db_module.course, current_user)

    db_module.title = module_update.title
    db_module.description = module_update.description
    db.commit()
    db.refresh(db_module)
    return db_module


@router.delete("/modules/{module_id}")
def delete_instructor_module(
    module_id: int,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    db_module = db.query(models.Module).filter(models.Module.id == module_id).first()
    if not db_module:
        raise HTTPException(status_code=404, detail="Module not found")

    _check_course_ownership(db_module.course, current_user)

    db.delete(db_module)
    db.commit()
    return {"message": "Module deleted successfully"}


# ── Lesson Management ────────────────────────────────────────────────────────

@router.post("/modules/{module_id}/lessons")
def add_instructor_lesson(
    module_id: int,
    lesson: schemas.LessonCreate,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    db_module = db.query(models.Module).filter(models.Module.id == module_id).first()
    if not db_module:
        raise HTTPException(status_code=404, detail="Module not found")

    _check_course_ownership(db_module.course, current_user)

    max_order = db.query(func.max(models.Lesson.order)).filter(
        models.Lesson.module_id == module_id
    ).scalar() or 0

    db_lesson = models.Lesson(
        module_id=module_id,
        title=lesson.title,
        content=lesson.content,
        video_url=lesson.video_url,
        lesson_type=lesson.lesson_type,
        order=max_order + 1
    )
    db.add(db_lesson)
    db.commit()
    db.refresh(db_lesson)

    # If assignment type, create assignment record
    if lesson.lesson_type == models.LessonType.ASSIGNMENT:
        db_assignment = models.Assignment(
            course_id=db_module.course_id,
            lesson_id=db_lesson.id,
            title=lesson.assignment_title or lesson.title,
            description=lesson.assignment_description or lesson.content or "",
            due_date=lesson.due_date
        )
        db.add(db_assignment)
        db.commit()
        db.refresh(db_assignment)

        if lesson.questions:
            for q_order, q in enumerate(lesson.questions, 1):
                db_question = models.Question(
                    assignment_id=db_assignment.id,
                    question_text=q.question_text,
                    question_type=q.question_type,
                    order=q_order
                )
                db.add(db_question)
                db.commit()
                db.refresh(db_question)

                if q.options:
                    for opt in q.options:
                        db_option = models.QuestionOption(
                            question_id=db_question.id,
                            option_text=opt.option_text,
                            is_correct=opt.is_correct
                        )
                        db.add(db_option)
            db.commit()

    db.refresh(db_lesson)
    return db_lesson


@router.put("/lessons/{lesson_id}")
def update_instructor_lesson(
    lesson_id: int,
    lesson_update: schemas.LessonCreate,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    db_lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_id).first()
    if not db_lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    _check_course_ownership(db_lesson.module.course, current_user)

    db_lesson.title = lesson_update.title
    db_lesson.content = lesson_update.content
    db_lesson.video_url = lesson_update.video_url
    db_lesson.lesson_type = lesson_update.lesson_type

    if lesson_update.lesson_type == models.LessonType.ASSIGNMENT:
        if not db_lesson.assignment:
            db_assignment = models.Assignment(
                course_id=db_lesson.module.course_id,
                lesson_id=db_lesson.id,
                title=lesson_update.assignment_title or lesson_update.title,
                description=lesson_update.assignment_description or lesson_update.content or "",
                due_date=lesson_update.due_date
            )
            db.add(db_assignment)
            db.commit()
            db.refresh(db_assignment)
        else:
            db_assignment = db_lesson.assignment
            db_assignment.title = lesson_update.assignment_title or lesson_update.title
            db_assignment.description = lesson_update.assignment_description or lesson_update.content or ""
            db_assignment.due_date = lesson_update.due_date

        if lesson_update.questions is not None:
            db.query(models.Question).filter(
                models.Question.assignment_id == db_assignment.id
            ).delete()
            for q_order, q in enumerate(lesson_update.questions, 1):
                db_question = models.Question(
                    assignment_id=db_assignment.id,
                    question_text=q.question_text,
                    question_type=q.question_type,
                    order=q_order
                )
                db.add(db_question)
                db.commit()
                db.refresh(db_question)

                if q.options:
                    for opt in q.options:
                        db_option = models.QuestionOption(
                            question_id=db_question.id,
                            option_text=opt.option_text,
                            is_correct=opt.is_correct
                        )
                        db.add(db_option)
            db.commit()

    db.commit()
    db.refresh(db_lesson)
    return db_lesson


@router.delete("/lessons/{lesson_id}")
def delete_instructor_lesson(
    lesson_id: int,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    db_lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_id).first()
    if not db_lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    _check_course_ownership(db_lesson.module.course, current_user)

    db.delete(db_lesson)
    db.commit()
    return {"message": "Lesson deleted successfully"}


# ── File & Media Upload ──────────────────────────────────────────────────────

@router.post("/upload")
def upload_instructor_media(
    file: UploadFile = File(...),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{file.filename.replace(' ', '_')}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {"url": f"/static/uploads/{filename}"}


# ── Submissions & Grading ────────────────────────────────────────────────────

@router.get("/submissions")
def list_instructor_submissions(
    course_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    from ..services.admin_service import list_submissions_with_details

    instructor_id = None if current_user.role == "admin" else str(current_user.id)
    course_ids = [course_id] if course_id else None

    return list_submissions_with_details(
        db,
        status_filter=status_filter,
        course_ids=course_ids,
        instructor_id=instructor_id,
    )


@router.get("/submissions/{submission_id}")
def get_instructor_submission_detail(
    submission_id: int,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    detail = instructor_service.get_submission_detail(db, submission_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Submission not found")

    # Ownership check
    if detail.get("course_id") and current_user.role != "admin":
        course = db.query(models.Course).filter(
            models.Course.id == detail["course_id"]
        ).first()
        if course:
            _check_course_ownership(course, current_user)

    return detail


@router.put("/submissions/{submission_id}/review")
def review_instructor_submission(
    submission_id: int,
    review_data: schemas.SubmissionReview,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    sub = db.query(models.Submission).filter(models.Submission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    course = sub.assignment.course if sub.assignment else None
    if course:
        _check_course_ownership(course, current_user)

    sub.status = review_data.status
    sub.grade = review_data.grade
    sub.feedback = review_data.feedback
    db.commit()
    db.refresh(sub)

    # If approved, check if learner completed all requirements to issue certificate
    if sub.status == models.SubmissionStatus.APPROVED and course:
        try:
            completion_engine.check_course_completion(db, sub.user_id, course.id)
        except Exception as e:
            print(f"[instructor_review] Certificate check warning: {e}")

    return {
        "message": "Submission reviewed successfully",
        "submission_id": sub.id,
        "status": sub.status,
        "grade": sub.grade
    }
