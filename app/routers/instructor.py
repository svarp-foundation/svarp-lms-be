import os
import re
import shutil
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from .. import models, schemas, auth, database, completion_engine
from ..utils import UPLOAD_DIR

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


def parse_course_file(content: str):
    sections = re.split(r'---(COURSE|MODULE|LESSON|END_LESSON|ASSIGNMENT|QUESTION|END_ASSIGNMENT)---', content)
    
    course_data = {}
    modules = []
    current_module = None
    current_lesson = None
    current_assignment = None
    
    i = 1
    while i < len(sections):
        tag = sections[i]
        body = sections[i+1].strip()
        i += 2
        
        if tag == 'COURSE':
            for line in body.split('\n'):
                if ':' in line:
                    key, val = line.split(':', 1)
                    course_data[key.strip().lower().replace(' ', '_')] = val.strip()
        elif tag == 'MODULE':
            current_module = {'lessons': []}
            for line in body.split('\n'):
                if ':' in line:
                    key, val = line.split(':', 1)
                    current_module[key.strip().lower().replace(' ', '_')] = val.strip()
            modules.append(current_module)
        elif tag == 'LESSON':
            if not current_module:
                continue
            current_lesson = {}
            lines = body.split('\n')
            for idx, line in enumerate(lines):
                if ':' in line:
                    key, val = line.split(':', 1)
                    k = key.strip().lower().replace(' ', '_')
                    if k == 'content':
                        current_lesson['content'] = val.strip() + ("\n" + "\n".join(lines[idx+1:]) if idx+1 < len(lines) else "")
                        break
                    else:
                        current_lesson[k] = val.strip()
            current_module['lessons'].append(current_lesson)
        elif tag == 'ASSIGNMENT':
            if not current_lesson:
                continue
            current_assignment = {'questions': []}
            for line in body.split('\n'):
                if ':' in line:
                    key, val = line.split(':', 1)
                    current_assignment[key.strip().lower().replace(' ', '_')] = val.strip()
            current_lesson['assignment_data'] = current_assignment
        elif tag == 'QUESTION':
            if not current_assignment:
                continue
            current_question = {'options': []}
            for line in body.split('\n'):
                if line.startswith('Option:'):
                    opt_body = line[len('Option:'):].strip()
                    is_correct = 'is_correct=true' in opt_body.lower()
                    opt_text = re.sub(r'\(is_correct=(true|false)\)', '', opt_body, flags=re.IGNORECASE).strip()
                    current_question['options'].append({'text': opt_text, 'is_correct': is_correct})
                elif ':' in line:
                    key, val = line.split(':', 1)
                    current_question[key.strip().lower().replace(' ', '_')] = val.strip()
            current_assignment['questions'].append(current_question)
            
    return course_data, modules


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

    # Total distinct enrolled students in instructor's courses
    total_students = (
        db.query(func.count(distinct(models.Enrollment.user_id)))
        .filter(models.Enrollment.course_id.in_(course_ids))
        .scalar()
        or 0
    )

    # Pending submissions
    pending_reviews = (
        db.query(func.count(models.Submission.id))
        .join(models.Assignment, models.Assignment.id == models.Submission.assignment_id)
        .join(models.Course, models.Course.id == models.Assignment.course_id)
        .filter(
            models.Course.id.in_(course_ids),
            models.Submission.status.in_([models.SubmissionStatus.SUBMITTED, models.SubmissionStatus.UNDER_REVIEW])
        )
        .scalar()
        or 0
    )

    # Certificates issued
    certificates_issued = (
        db.query(func.count(models.Certificate.id))
        .filter(
            models.Certificate.course_id.in_(course_ids),
            models.Certificate.revoked_at == None
        )
        .scalar()
        or 0
    )

    # Total revenue for instructor's courses
    total_revenue = (
        db.query(func.sum(models.CoursePayment.amount))
        .filter(
            models.CoursePayment.course_id.in_(course_ids),
            models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS
        )
        .scalar()
        or 0.0
    )

    # Completion rate
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
    query = db.query(models.Course).filter(models.Course.is_deleted == False)
    if current_user.role != "admin":
        query = query.filter(models.Course.instructor_id == str(current_user.id))

    courses = query.order_by(models.Course.created_at.desc()).all()
    results = []

    for c in courses:
        module_count = db.query(func.count(models.Module.id)).filter(models.Module.course_id == c.id).scalar() or 0
        lesson_count = (
            db.query(func.count(models.Lesson.id))
            .join(models.Module, models.Module.id == models.Lesson.module_id)
            .filter(models.Module.course_id == c.id)
            .scalar()
            or 0
        )
        student_count = db.query(func.count(models.Enrollment.id)).filter(models.Enrollment.course_id == c.id).scalar() or 0

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
            "instructor_name": c.instructor.full_name if c.instructor else None,
            "created_at": c.created_at,
            "module_count": module_count,
            "lesson_count": lesson_count,
            "student_count": student_count,
        })

    return results


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
    
    db_course = models.Course(
        **course_dict,
        is_deleted=False
    )
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

    modules = db.query(models.Module).filter(models.Module.course_id == course_id).order_by(models.Module.order).all()
    modules_data = []

    for m in modules:
        lessons = db.query(models.Lesson).filter(models.Lesson.module_id == m.id).order_by(models.Lesson.order).all()
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
                        "options": [{"id": o.id, "option_text": o.option_text, "is_correct": o.is_correct} for o in q.options]
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
        "instructor_name": course.instructor.full_name if course.instructor else None,
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
    course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    _check_course_ownership(course, current_user)

    course.is_deleted = True
    db.commit()
    return {"message": "Course deleted successfully"}


@router.post("/courses/import-bundle")
def import_course_bundle(
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    content = file.file.read().decode('utf-8')
    course_data, modules_data = parse_course_file(content)

    if not course_data or not course_data.get('title'):
        raise HTTPException(status_code=400, detail="Invalid course file format. Missing COURSE title.")

    db_course = models.Course(
        title=course_data.get('title', 'Imported Course'),
        description=course_data.get('description', ''),
        status=course_data.get('status', models.CourseStatus.DRAFT),
        passing_score=int(course_data.get('passing_score', 70)),
        is_paid=course_data.get('is_paid', 'false').lower() == 'true',
        price=float(course_data.get('price', 0.0)),
        instructor_id=str(current_user.id),
        is_deleted=False
    )
    db.add(db_course)
    db.commit()
    db.refresh(db_course)

    for mod_order, mod in enumerate(modules_data, 1):
        db_module = models.Module(
            course_id=db_course.id,
            title=mod.get('title', f'Module {mod_order}'),
            description=mod.get('description', ''),
            order=mod_order
        )
        db.add(db_module)
        db.commit()
        db.refresh(db_module)

        for les_order, les in enumerate(mod.get('lessons', []), 1):
            db_lesson = models.Lesson(
                module_id=db_module.id,
                title=les.get('title', f'Lesson {les_order}'),
                content=les.get('content', ''),
                video_url=les.get('video_url', ''),
                lesson_type=les.get('lesson_type', models.LessonType.TEXT),
                order=les_order
            )
            db.add(db_lesson)
            db.commit()
            db.refresh(db_lesson)

            if les.get('assignment_data'):
                asgn = les['assignment_data']
                db_assignment = models.Assignment(
                    course_id=db_course.id,
                    lesson_id=db_lesson.id,
                    title=asgn.get('title', f'Assignment {les_order}'),
                    description=asgn.get('description', '')
                )
                db.add(db_assignment)
                db.commit()
                db.refresh(db_assignment)

                for q_order, q in enumerate(asgn.get('questions', []), 1):
                    db_question = models.Question(
                        assignment_id=db_assignment.id,
                        question_text=q.get('question_text', ''),
                        question_type=q.get('question_type', models.QuestionType.SUBJECTIVE),
                        order=q_order
                    )
                    db.add(db_question)
                    db.commit()
                    db.refresh(db_question)

                    for opt in q.get('options', []):
                        db_option = models.QuestionOption(
                            question_id=db_question.id,
                            option_text=opt.get('text', ''),
                            is_correct=opt.get('is_correct', False)
                        )
                        db.add(db_option)
                db.commit()

    return {"message": "Course bundle imported successfully", "course_id": db_course.id}


@router.post("/courses/{course_id}/update-from-file", status_code=status.HTTP_200_OK)
async def update_instructor_course_from_file(
    course_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="File must be a .txt file")

    db_course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if not db_course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")
    _check_course_ownership(db_course, current_user)

    content = await file.read()
    decoded_content = content.decode('utf-8')

    try:
        course_data, modules_data = parse_course_file(decoded_content)
        if not course_data:
            raise HTTPException(status_code=400, detail="Course data missing in file")

        if 'title' in course_data:
            db_course.title = course_data['title']
        if 'description' in course_data:
            db_course.description = course_data['description']
        if 'price' in course_data:
            db_course.price = float(course_data['price'])
        if 'is_paid' in course_data:
            db_course.is_paid = course_data['is_paid'].lower() == 'true'

        existing_modules = db.query(models.Module).filter(models.Module.course_id == course_id).all()
        for m in existing_modules:
            lessons = db.query(models.Lesson).filter(models.Lesson.module_id == m.id).all()
            for l in lessons:
                assignments = db.query(models.Assignment).filter(models.Assignment.lesson_id == l.id).all()
                for a in assignments:
                    questions = db.query(models.Question).filter(models.Question.assignment_id == a.id).all()
                    for q in questions:
                        db.query(models.QuestionOption).filter(models.QuestionOption.question_id == q.id).delete(synchronize_session=False)
                        db.delete(q)
                    db.delete(a)
                db.delete(l)
            db.delete(m)
        db.flush()

        for mod_order, mod in enumerate(modules_data, 1):
            db_module = models.Module(
                course_id=db_course.id,
                title=mod.get('title', f'Module {mod_order}'),
                description=mod.get('description', ''),
                order=mod_order
            )
            db.add(db_module)
            db.flush()

            for les_order, les in enumerate(mod.get('lessons', []), 1):
                db_lesson = models.Lesson(
                    module_id=db_module.id,
                    title=les.get('title', f'Lesson {les_order}'),
                    content=les.get('content', ''),
                    video_url=les.get('video_url', ''),
                    lesson_type=les.get('lesson_type', models.LessonType.TEXT),
                    order=les_order
                )
                db.add(db_lesson)
                db.flush()

                if les.get('assignment_data'):
                    asgn = les['assignment_data']
                    db_assignment = models.Assignment(
                        course_id=db_course.id,
                        lesson_id=db_lesson.id,
                        title=asgn.get('title', f'Assignment {les_order}'),
                        description=asgn.get('description', '')
                    )
                    db.add(db_assignment)
                    db.flush()

                    for q_order, q in enumerate(asgn.get('questions', []), 1):
                        db_question = models.Question(
                            assignment_id=db_assignment.id,
                            question_text=q.get('question_text', ''),
                            question_type=q.get('question_type', models.QuestionType.SUBJECTIVE),
                            order=q_order
                        )
                        db.add(db_question)
                        db.flush()

                        for opt in q.get('options', []):
                            db_option = models.QuestionOption(
                                question_id=db_question.id,
                                option_text=opt.get('text', ''),
                                is_correct=opt.get('is_correct', False)
                            )
                            db.add(db_option)
        db.commit()
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

    max_order = db.query(func.max(models.Module.order)).filter(models.Module.course_id == course_id).scalar() or 0

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

    max_order = db.query(func.max(models.Lesson.order)).filter(models.Lesson.module_id == module_id).scalar() or 0

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

        # If questions passed, update questions
        if lesson_update.questions is not None:
            # Delete old questions
            db.query(models.Question).filter(models.Question.assignment_id == db_assignment.id).delete()
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
    query = (
        db.query(models.Submission)
        .join(models.Assignment, models.Assignment.id == models.Submission.assignment_id)
        .join(models.Course, models.Course.id == models.Assignment.course_id)
        .filter(models.Course.is_deleted == False)
    )

    if current_user.role != "admin":
        query = query.filter(models.Course.instructor_id == str(current_user.id))

    if course_id:
        query = query.filter(models.Course.id == course_id)

    if status_filter:
        query = query.filter(models.Submission.status == status_filter)

    submissions = query.order_by(models.Submission.submitted_at.desc()).all()
    results = []

    for sub in submissions:
        student = sub.student
        assignment = sub.assignment
        course = assignment.course if assignment else None

        results.append({
            "id": sub.id,
            "student_id": sub.user_id,
            "student_name": student.full_name if student else "Learner",
            "student_email": student.email if student else "N/A",
            "course_id": course.id if course else None,
            "course_title": course.title if course else "Unknown Course",
            "assignment_id": assignment.id if assignment else None,
            "assignment_title": assignment.title if assignment else "Assignment",
            "status": sub.status,
            "submitted_at": sub.submitted_at,
            "grade": sub.grade,
            "feedback": sub.feedback,
            "mcq_score": sub.mcq_score,
            "mcq_total": sub.mcq_total,
            "content": sub.content,
            "file_url": sub.file_url,
        })

    return results


@router.get("/submissions/{submission_id}")
def get_instructor_submission_detail(
    submission_id: int,
    db: Session = Depends(database.get_db),
    current_user: schemas.User = Depends(auth.require_instructor)
):
    sub = db.query(models.Submission).filter(models.Submission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    course = sub.assignment.course if sub.assignment else None
    if course:
        _check_course_ownership(course, current_user)

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
            completion_engine.check_and_issue_certificate(db, sub.user_id, course.id)
        except Exception as e:
            print(f"[instructor_review] Certificate check warning: {e}")

    return {
        "message": "Submission reviewed successfully",
        "submission_id": sub.id,
        "status": sub.status,
        "grade": sub.grade
    }
