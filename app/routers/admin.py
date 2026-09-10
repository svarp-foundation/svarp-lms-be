"""
Admin Router — Thin HTTP handlers delegating to service layer.

All business logic has been extracted to:
- app.services.admin_service (stats, analytics, submissions)
- app.services.course_service (course CRUD, file parsing, submission review)
"""

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, distinct, desc
from typing import List, Optional
from datetime import datetime, timedelta
import shutil
import os
import uuid
import csv
import io

from app.clients.user_portal_client import user_portal_client, ServiceError
from .. import models, schemas, database, auth, completion_engine
from ..utils import UPLOAD_DIR
from ..services import admin_service, course_service

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)


# ── Dashboard & Analytics ────────────────────────────────────────────────────

@router.get("/stats")
async def get_admin_stats(
    refresh: bool = Query(False),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    return admin_service.get_admin_stats(db, force_refresh=refresh)


@router.get("/analytics")
async def get_admin_analytics(
    refresh: bool = Query(False),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    return admin_service.get_admin_analytics(db, force_refresh=refresh)


@router.get("/analytics/courses/{course_id}/learners")
async def get_course_learners_analytics(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    result = admin_service.get_course_learners_analytics(db, course_id=course_id)
    if not result:
        raise HTTPException(status_code=404, detail="Course not found or deleted")
    return result


@router.get("/analytics/learners/{user_id}/courses")
async def get_learner_courses_analytics(
    user_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    result = admin_service.get_learner_courses_analytics(db, user_id=user_id)
    if not result:
        raise HTTPException(status_code=404, detail="Learner not found")
    return result


@router.get("/analytics/learners")
async def get_all_learners_analytics(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    return admin_service.get_all_learners_analytics(db)



# ── Payments ─────────────────────────────────────────────────────────────────

@router.get("/payments", response_model=List[schemas.CoursePaymentAdmin])
def get_all_payments(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    payments = db.query(models.CoursePayment).order_by(
        models.CoursePayment.created_at.desc()
    ).all()
    if not payments:
        return []

    user_ids = {p.user_id for p in payments if p.user_id}
    course_ids = {p.course_id for p in payments if p.course_id}

    users_map = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()} if user_ids else {}
    courses_map = {c.id: c for c in db.query(models.Course).filter(models.Course.id.in_(course_ids)).all()} if course_ids else {}

    result = []
    for p in payments:
        user = users_map.get(p.user_id)
        course = courses_map.get(p.course_id)
        result.append({
            "id": p.id,
            "user_id": p.user_id,
            "user_email": user.email if user else "Unknown User",
            "course_id": p.course_id,
            "course_title": course.title if course else "Unknown Course",
            "payment_id": p.payment_id,
            "amount": p.amount,
            "currency": p.currency,
            "status": p.status,
            "created_at": p.created_at
        })
    return result


# ── File Upload ──────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: models.User = Depends(auth.require_admin)
):
    file_extension = os.path.splitext(file.filename)[1]
    unique_filename = f"{uuid.uuid4()}{file_extension}"

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, unique_filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {"url": f"/static/uploads/{unique_filename}"}


# ── Assignment Management ────────────────────────────────────────────────────

@router.post("/assignments", response_model=schemas.Assignment)
def create_assignment(
    assignment: schemas.AssignmentCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    db_assignment = models.Assignment(
        title=assignment.title,
        description=assignment.description,
        lesson_id=assignment.lesson_id,
        course_id=assignment.course_id,
    )
    db.add(db_assignment)
    db.flush()

    for q_data in assignment.questions:
        db_question = models.Question(
            assignment_id=db_assignment.id,
            question_text=q_data.question_text,
            question_type=q_data.question_type,
            order=q_data.order,
        )
        db.add(db_question)
        db.flush()

        if q_data.question_type == models.QuestionType.MCQ:
            for opt_data in q_data.options:
                db_option = models.QuestionOption(
                    question_id=db_question.id,
                    option_text=opt_data.option_text,
                    is_correct=opt_data.is_correct,
                )
                db.add(db_option)

    db.commit()
    db.refresh(db_assignment)
    return db_assignment


@router.get("/assignments/{assignment_id}", response_model=schemas.AssignmentAdminDetail)
def get_assignment_admin(
    assignment_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    assignment = db.query(models.Assignment).filter(
        models.Assignment.id == assignment_id
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return assignment


@router.get("/courses/{course_id}/assignments", response_model=List[schemas.Assignment])
def list_course_assignments(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    return db.query(models.Assignment).filter(
        models.Assignment.course_id == course_id
    ).all()


@router.post("/courses/{course_id}/generate-final-assignment", response_model=schemas.Assignment)
def generate_final_assignment(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    """Consolidate all lesson-based assignment questions into a single final assignment."""
    course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    lesson_assignments = db.query(models.Assignment).join(models.Lesson).join(models.Module).filter(
        models.Module.course_id == course_id
    ).all()

    if not lesson_assignments:
        raise HTTPException(status_code=400, detail="No assignments found in lessons to consolidate.")

    db_final = db.query(models.Assignment).filter(
        models.Assignment.course_id == course_id,
        models.Assignment.lesson_id == None
    ).first()

    if db_final:
        db.query(models.Question).filter(models.Question.assignment_id == db_final.id).delete()
    else:
        db_final = models.Assignment(
            course_id=course_id,
            lesson_id=None,
            title=f"Final Assignment: {course.title}",
            description="Complete this final assignment covering all course topics to receive your certificate."
        )
        db.add(db_final)
        db.flush()

    q_count = 0
    for l_asgn in lesson_assignments:
        for q in l_asgn.questions:
            q_count += 1
            new_q = models.Question(
                assignment_id=db_final.id,
                question_text=q.question_text,
                question_type=q.question_type,
                order=q_count
            )
            db.add(new_q)
            db.flush()

            if q.question_type == models.QuestionType.MCQ:
                for opt in q.options:
                    new_opt = models.QuestionOption(
                        question_id=new_q.id,
                        option_text=opt.option_text,
                        is_correct=opt.is_correct
                    )
                    db.add(new_opt)

    course.require_final_assignment = True
    db.commit()
    db.refresh(db_final)
    return db_final


# ── User Management ──────────────────────────────────────────────────────────

@router.post("/users/bulk", status_code=status.HTTP_201_CREATED)
async def bulk_create_users(
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    content = await file.read()
    decoded_content = content.decode('utf-8')
    csv_reader = csv.DictReader(io.StringIO(decoded_content))

    created_count = 0
    errors = []

    for row in csv_reader:
        email = row.get('email')
        full_name = row.get('full_name')
        password = row.get('password')

        if not email or not password:
            errors.append(f"Row missing email or password: {row}")
            continue

        try:
            portal_user = await user_portal_client.create_user(
                email=email,
                password=password,
                full_name=full_name or email.split('@')[0].title(),
                roles=["learner"]
            )
            user_id = str(portal_user.get("user_id"))

            db_user = db.query(models.User).filter(models.User.id == user_id).first()
            if not db_user:
                db_user = db.query(models.User).filter(models.User.email == email).first()

            if db_user:
                db_user.id = user_id
                db_user.email = email
                db_user.full_name = full_name or db_user.full_name
                db_user.role = models.UserRole.LEARNER
                db_user.is_active = True
            else:
                db_user = models.User(
                    id=user_id,
                    email=email,
                    full_name=full_name or email.split('@')[0].title(),
                    role=models.UserRole.LEARNER,
                    is_active=True,
                    is_suspended=False,
                    hashed_password=""
                )
                db.add(db_user)
            created_count += 1
        except ServiceError as se:
            errors.append(f"Portal error for {email}: {se.detail}")
        except Exception as e:
            errors.append(f"Failed to process {email}: {str(e)}")

    db.commit()
    return {"message": f"Successfully processed {created_count} users", "errors": errors}


@router.get("/users", response_model=List[schemas.User])
async def read_users(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    try:
        portal_users = await user_portal_client.list_users(skip=skip, limit=limit)
    except Exception:
        portal_users = []

    if not portal_users:
        return db.query(models.User).offset(skip).limit(limit).all()

    portal_user_ids = {str(pu.get("user_id")) for pu in portal_users if pu.get("user_id")}
    portal_emails = {pu.get("email") for pu in portal_users if pu.get("email")}

    existing_users = db.query(models.User).filter(
        (models.User.id.in_(portal_user_ids)) | (models.User.email.in_(portal_emails))
    ).all()

    users_by_id = {u.id: u for u in existing_users}
    users_by_email = {u.email: u for u in existing_users if u.email}

    result = []
    has_changes = False

    for pu in portal_users:
        user_id = str(pu.get("user_id"))
        email = pu.get("email")
        full_name = pu.get("full_name") or (email.split("@")[0].title() if email else "")
        roles = pu.get("roles", [])

        db_user = users_by_id.get(user_id) or users_by_email.get(email)

        if "admin" in roles:
            primary_role = "admin"
        elif "instructor" in roles or "teacher" in roles:
            primary_role = "instructor"
        elif "instructor_pending" in roles:
            primary_role = "instructor_pending"
        elif db_user and db_user.role in ["instructor", "instructor_pending", "admin"]:
            primary_role = db_user.role
        else:
            primary_role = "learner"

        if db_user:
            if (db_user.full_name != full_name or db_user.role != primary_role or not db_user.is_active):
                db_user.id = user_id
                db_user.email = email
                db_user.full_name = full_name
                db_user.role = primary_role
                db_user.is_active = True
                has_changes = True
        else:
            db_user = models.User(
                id=user_id,
                email=email,
                full_name=full_name,
                role=primary_role,
                is_active=True,
                is_suspended=False,
                hashed_password=""
            )
            db.add(db_user)
            has_changes = True

        result.append(db_user)

    if has_changes:
        db.commit()

    return result


@router.put("/users/{user_id}/role")
def update_user_role(
    user_id: str,
    role_data: schemas.UserRoleUpdate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = role_data.role
    db.commit()
    db.refresh(user)
    return {"message": "User role updated successfully", "user_id": user.id, "role": user.role}


# ── Instructor Applications ─────────────────────────────────────────────────

@router.get("/instructor-applications")
def get_instructor_applications(
    status_filter: Optional[str] = None,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    query = db.query(models.InstructorApplication)
    if status_filter:
        query = query.filter(models.InstructorApplication.status == status_filter)

    apps = query.order_by(models.InstructorApplication.applied_at.desc()).all()
    results = []
    for app in apps:
        u = app.user
        results.append({
            "id": app.id,
            "user_id": app.user_id,
            "user_name": u.full_name if u else "Applicant",
            "user_email": u.email if u else "N/A",
            "specialty": app.specialty,
            "bio": app.bio,
            "status": app.status,
            "applied_at": app.applied_at,
            "reviewed_at": app.reviewed_at,
            "admin_feedback": app.admin_feedback
        })
    return results


@router.put("/instructor-applications/{app_id}/review")
@router.post("/instructor-applications/{app_id}/review")
def review_instructor_application(
    app_id: int,
    review_data: schemas.InstructorApplicationReview,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    app = db.query(models.InstructorApplication).filter(
        models.InstructorApplication.id == app_id
    ).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    new_status = review_data.status or review_data.action
    feedback = review_data.admin_feedback or review_data.feedback

    if not new_status:
        raise HTTPException(status_code=400, detail="Review status/action is required ('approved' or 'rejected')")

    app.status = new_status
    app.admin_feedback = feedback
    app.reviewed_at = datetime.utcnow()
    app.reviewed_by = str(current_user.id)

    user = db.query(models.User).filter(models.User.id == app.user_id).first()
    if user:
        if new_status == "approved":
            user.role = models.UserRole.INSTRUCTOR
        elif new_status == "rejected" and user.role == models.UserRole.INSTRUCTOR_PENDING:
            user.role = models.UserRole.LEARNER

    db.commit()
    return {"message": f"Instructor application {new_status}", "application_id": app.id, "status": app.status}


# ── Course Management ────────────────────────────────────────────────────────

@router.post("/courses", response_model=schemas.Course)
def create_course(
    course: schemas.CourseCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    course_data = course.dict(exclude={"discounted_price", "instructor_name"})
    db_course = models.Course(**course_data, status=models.CourseStatus.DRAFT, is_deleted=False)
    db.add(db_course)
    db.commit()
    db.refresh(db_course)
    return db_course


@router.post("/courses/bulk-create", status_code=status.HTTP_201_CREATED)
@router.post("/courses/import-bundle", status_code=status.HTTP_201_CREATED)
async def bulk_create_course(
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="File must be a .txt file")

    content = await file.read()
    decoded_content = content.decode('utf-8')

    try:
        parsed_course, modules_data = course_service.parse_course_file(decoded_content)
        if not parsed_course:
            raise HTTPException(status_code=400, detail="Course data missing in file")

        db_course = course_service.create_course_from_parsed_data(db, parsed_course, modules_data)

        # Audit Log
        log = models.AuditLog(
            admin_id=current_user.id,
            action_type="bulk_create_course",
            target_entity=f"Course {db_course.id}"
        )
        db.add(log)
        db.commit()

        return {"message": "Course created successfully", "course_id": db_course.id, "title": db_course.title}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to parse or create course: {str(e)}")


@router.post("/courses/{course_id}/update-from-file", status_code=status.HTTP_200_OK)
async def update_course_from_file(
    course_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="File must be a .txt file")

    db_course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()
    if not db_course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")

    content = await file.read()
    decoded_content = content.decode('utf-8')

    try:
        parsed_course, modules_data = course_service.parse_course_file(decoded_content)
        if not parsed_course:
            raise HTTPException(status_code=400, detail="Course data missing in file")

        updated_course = course_service.update_course_curriculum_from_parsed_data(
            db, course_id, parsed_course, modules_data
        )

        # Audit Log
        log = models.AuditLog(
            admin_id=current_user.id,
            action_type="update_course_from_file",
            target_entity=f"Course {course_id}"
        )
        db.add(log)
        db.commit()

        return {
            "message": f"Course '{updated_course.title}' updated successfully from file",
            "course_id": updated_course.id,
            "title": updated_course.title
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to parse or update course: {str(e)}")


@router.put("/courses/{course_id}", response_model=schemas.Course)
def update_course(
    course_id: int,
    course_update: schemas.CourseUpdate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    db_course = db.query(models.Course).filter(
        models.Course.id == course_id,
        models.Course.is_deleted == False
    ).first()
    if db_course is None:
        raise HTTPException(status_code=404, detail="Course not found or deleted")

    update_data = course_update.dict(exclude_unset=True)

    # Validation for Publishing
    if update_data.get("status") == models.CourseStatus.PUBLISHED:
        has_content = db.query(models.Module).filter(models.Module.course_id == course_id).first()
        if not has_content:
            raise HTTPException(status_code=400, detail="Cannot publish a course without modules.")

        has_lesson = db.query(models.Lesson).join(models.Module).filter(
            models.Module.course_id == course_id
        ).first()
        if not has_lesson:
            raise HTTPException(status_code=400, detail="Cannot publish a course without lessons.")

    for key, value in update_data.items():
        setattr(db_course, key, value)

    db.commit()
    db.refresh(db_course)

    if update_data.get("status") == models.CourseStatus.PUBLISHED:
        log = models.AuditLog(
            admin_id=current_user.id,
            action_type="publish_course",
            target_entity=f"Course {db_course.id}"
        )
        db.add(log)
        db.commit()

    return db_course


@router.get("/courses", response_model=List[schemas.Course])
def read_all_courses(
    include_deleted: bool = Query(True, description="Include soft-deleted courses"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    query = db.query(models.Course).options(joinedload(models.Course.instructor))
    if not include_deleted:
        query = query.filter(models.Course.is_deleted == False)
    return query.order_by(models.Course.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/courses/{course_id}", response_model=schemas.CourseAdminDetail)
def read_course(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    course = db.query(models.Course).options(
        joinedload(models.Course.instructor)
    ).filter(
        models.Course.id == course_id
    ).first()
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    return course


@router.post("/courses/{course_id}/restore", response_model=schemas.Course)
def restore_course(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    course.is_deleted = False
    course.status = models.CourseStatus.DRAFT.value
    log = models.AuditLog(
        admin_id=current_user.id,
        action_type="restore_course",
        target_entity=f"Course {course.id}: {course.title}"
    )
    db.add(log)
    db.commit()
    db.refresh(course)
    return course


@router.delete("/courses/{course_id}", status_code=status.HTTP_200_OK)
def delete_course(
    course_id: int,
    force: bool = Query(False, description="Set to true to force hard delete all course data, enrollments, and payments"),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    return course_service.delete_course_service(
        db=db,
        course_id=course_id,
        user_id=current_user.id,
        user_role=models.UserRole.ADMIN.value,
        force=force
    )


# ── User Suspension & Deletion ───────────────────────────────────────────────

@router.post("/users/{user_id}/suspend", status_code=status.HTTP_200_OK)
async def suspend_user(
    user_id: str,
    suspend: bool = True,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    user = db.query(models.User).filter(models.User.id == str(user_id)).first()
    if not user:
        user = db.query(models.User).filter(models.User.email == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        await user_portal_client.update_user(user.id, {"is_active": not suspend})
    except Exception as e:
        print(f"Warning: Failed to update user_portal status: {e}")

    user.is_suspended = suspend
    user.is_active = not suspend

    action = "suspend_user" if suspend else "unsuspend_user"
    log = models.AuditLog(admin_id=current_user.id, action_type=action, target_entity=f"User {user_id}")
    db.add(log)
    db.commit()
    return {"message": f"User suspension status set to {suspend}"}


@router.delete("/users/{user_id}", status_code=status.HTTP_200_OK)
async def delete_user_lms(
    user_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    user = db.query(models.User).filter(models.User.id == str(user_id)).first()
    if not user:
        user = db.query(models.User).filter(models.User.email == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account from LMS")

    target_user_id = user.id

    try:
        await user_portal_client.delete_user(target_user_id)
    except Exception as e:
        print(f"Warning: Failed to soft delete user on Central User Portal: {e}")

    # Cascade delete all related LMS records
    db.query(models.Enrollment).filter(models.Enrollment.user_id == target_user_id).delete()
    db.query(models.LessonCompletion).filter(models.LessonCompletion.user_id == target_user_id).delete()

    submissions = db.query(models.Submission).filter(models.Submission.user_id == target_user_id).all()
    for sub in submissions:
        db.query(models.AnswerSubmission).filter(models.AnswerSubmission.submission_id == sub.id).delete()
        db.delete(sub)

    db.query(models.Wishlist).filter(models.Wishlist.user_id == target_user_id).delete()
    db.query(models.LessonComment).filter(models.LessonComment.user_id == target_user_id).delete()
    db.query(models.Certificate).filter(models.Certificate.user_id == target_user_id).delete()
    db.query(models.CoursePayment).filter(models.CoursePayment.user_id == target_user_id).delete()
    db.query(models.AuditLog).filter(models.AuditLog.admin_id == target_user_id).delete()

    db.delete(user)

    log = models.AuditLog(admin_id=current_user.id, action_type="delete_user", target_entity=f"User {user_id}")
    db.add(log)
    db.commit()
    return {"message": f"User {user_id} deleted locally from LMS and soft-deleted from Central User Portal"}


# ── Module & Lesson Management ───────────────────────────────────────────────

@router.post("/courses/{course_id}/modules")
def create_module(
    course_id: int,
    module: schemas.ModuleCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    module_data = module.dict(exclude={"lessons"})
    db_module = models.Module(**module_data, course_id=course_id)
    db.add(db_module)
    db.commit()
    db.refresh(db_module)
    return db_module


@router.post("/modules/{module_id}/lessons")
def create_lesson(
    module_id: int,
    lesson: schemas.LessonCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    db_lesson = models.Lesson(**lesson.dict(), module_id=module_id)
    db.add(db_lesson)
    db.commit()
    db.refresh(db_lesson)
    return db_lesson


@router.put("/lessons/{lesson_id}", response_model=schemas.LessonAdmin)
def update_lesson(
    lesson_id: int,
    lesson_update: schemas.LessonUpdate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    db_lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_id).first()
    if not db_lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    update_data = lesson_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_lesson, key, value)

    # If it's an assignment, keep the assignment title/description in sync if provided
    if db_lesson.lesson_type == models.LessonType.ASSIGNMENT:
        assignment = db.query(models.Assignment).filter(
            models.Assignment.lesson_id == lesson_id
        ).first()
        if assignment:
            if "title" in update_data:
                assignment.title = update_data["title"]
            if "content" in update_data:
                assignment.description = update_data["content"]

    db.commit()
    db.refresh(db_lesson)
    return db_lesson


@router.delete("/modules/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_module(
    module_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    module = db.query(models.Module).filter(models.Module.id == module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")

    if module.lessons:
        for lesson in module.lessons:
            if lesson.video_url:
                course_service.delete_physical_file(lesson.video_url)

    log = models.AuditLog(
        admin_id=current_user.id,
        action_type="delete_module",
        target_entity=f"Module {module_id}"
    )
    db.add(log)
    db.delete(module)
    db.commit()
    return None


@router.delete("/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lesson(
    lesson_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    if lesson.video_url:
        course_service.delete_physical_file(lesson.video_url)

    log = models.AuditLog(
        admin_id=current_user.id,
        action_type="delete_lesson",
        target_entity=f"Lesson {lesson_id}"
    )
    db.add(log)
    db.delete(lesson)
    db.commit()


# ── Submissions ──────────────────────────────────────────────────────────────

@router.get("/submissions")
def list_submissions(
    status: Optional[str] = Query(None),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    return admin_service.list_submissions_with_details(db, status_filter=status)


@router.post("/submissions/{submission_id}/review")
def review_submission(
    submission_id: int,
    review_data: schemas.SubmissionReview,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    result = course_service.review_submission(
        db, submission_id, review_data.status, review_data.grade, review_data.feedback
    )
    if not result:
        raise HTTPException(status_code=404, detail="Submission not found")
    return {"message": "Submission reviewed successfully"}


@router.post("/courses/{course_id}/users/{user_id}/review-all")
def review_all_course_submissions(
    course_id: int,
    user_id: int,
    review_data: schemas.SubmissionReview,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    course_id = int(course_id)
    user_id = int(user_id)

    assignments = db.query(models.Assignment).filter(
        models.Assignment.course_id == course_id
    ).all()
    assignment_ids = [a.id for a in assignments]

    lessons = db.query(models.Lesson).join(models.Module).filter(
        models.Module.course_id == course_id
    ).all()
    lesson_ids = [l.id for l in lessons]
    if lesson_ids:
        lesson_assignments = db.query(models.Assignment).filter(
            models.Assignment.lesson_id.in_(lesson_ids)
        ).all()
        assignment_ids.extend([la.id for la in lesson_assignments if la.id not in assignment_ids])

    query = db.query(models.Submission).filter(models.Submission.user_id == user_id)
    if assignment_ids:
        query = query.filter(models.Submission.assignment_id.in_(assignment_ids))
    submissions = query.all()

    if review_data.status == models.SubmissionStatus.APPROVED:
        for sub in submissions:
            sub.status = models.SubmissionStatus.APPROVED
            sub.grade = review_data.grade or 100
            sub.feedback = review_data.feedback or "Approved all course modules"
            db.add(sub)

        for l in lessons:
            existing = db.query(models.LessonCompletion).filter(
                models.LessonCompletion.user_id == user_id,
                models.LessonCompletion.lesson_id == l.id
            ).first()
            if not existing:
                db.add(models.LessonCompletion(user_id=user_id, lesson_id=l.id))

        completion_engine.check_course_completion(db, user_id, course_id)

    elif review_data.status == models.SubmissionStatus.REJECTED:
        for sub in submissions:
            sub.status = models.SubmissionStatus.REJECTED
            sub.feedback = review_data.feedback or "Course assignments rejected by instructor"
            db.add(sub)

        if lesson_ids:
            db.query(models.LessonCompletion).filter(
                models.LessonCompletion.user_id == user_id,
                models.LessonCompletion.lesson_id.in_(lesson_ids)
            ).delete(synchronize_session=False)

        certs = db.query(models.Certificate).filter(
            models.Certificate.user_id == user_id,
            models.Certificate.course_id == course_id,
            models.Certificate.revoked_at == None
        ).all()
        for cert in certs:
            cert.revoked_at = datetime.utcnow()
            cert.revoked_reason = review_data.feedback or "Course submissions rejected by instructor"
            db.add(cert)

    db.commit()
    return {"message": "All submissions for course reviewed successfully"}


@router.post("/courses/{course_id}/consolidate-to-final")
def consolidate_course_to_single_final_exam(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    lessons = db.query(models.Lesson).join(models.Module).filter(
        models.Module.course_id == course_id,
        models.Lesson.lesson_type == models.LessonType.ASSIGNMENT
    ).all()

    all_questions = []
    for l in lessons:
        asgns = db.query(models.Assignment).filter(models.Assignment.lesson_id == l.id).all()
        for a in asgns:
            for q in a.questions:
                all_questions.append(q)

    db_final = db.query(models.Assignment).filter(
        models.Assignment.course_id == course_id,
        models.Assignment.lesson_id == None
    ).first()

    if not db_final:
        db_final = models.Assignment(
            course_id=course_id,
            lesson_id=None,
            title=f"Final Certification Exam: {course.title}",
            description="Comprehensive exam covering all course topics."
        )
        db.add(db_final)
        db.flush()

    existing_q_texts = [q.question_text for q in db_final.questions]
    q_count = len(existing_q_texts)
    for q in all_questions:
        if q.question_text not in existing_q_texts:
            q_count += 1
            new_q = models.Question(
                assignment_id=db_final.id,
                question_text=q.question_text,
                question_type=q.question_type,
                order=q_count
            )
            db.add(new_q)
            db.flush()

            if q.question_type == models.QuestionType.MCQ:
                for opt in q.options:
                    new_opt = models.QuestionOption(
                        question_id=new_q.id,
                        option_text=opt.option_text,
                        is_correct=opt.is_correct
                    )
                    db.add(new_opt)

    for l in lessons:
        db.query(models.LessonCompletion).filter(
            models.LessonCompletion.lesson_id == l.id
        ).delete(synchronize_session=False)
        asgns = db.query(models.Assignment).filter(models.Assignment.lesson_id == l.id).all()
        for a in asgns:
            db.query(models.Submission).filter(
                models.Submission.assignment_id == a.id
            ).delete(synchronize_session=False)
            db.delete(a)
        db.delete(l)

    course.require_final_assignment = True
    course.require_assignment_approval = False
    db.commit()
    return {"message": f"Successfully consolidated course '{course.title}' to a single final exam!"}


# ── Bulk Enrollment ──────────────────────────────────────────────────────────

async def process_bulk_enrollment(course_id: int, file_content: bytes):
    db: Session = database.SessionLocal()
    try:
        decoded_content = file_content.decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(decoded_content))

        for row in csv_reader:
            email = row.get('email')
            if not email:
                continue

            user = db.query(models.User).filter(models.User.email == email).first()
            if not user:
                try:
                    portal_user = await user_portal_client.get_user(email=email)
                except Exception:
                    portal_user = None

                if not portal_user:
                    try:
                        portal_user = await user_portal_client.create_user(
                            email=email,
                            password="Changeme@123",
                            full_name=email.split('@')[0].title(),
                            roles=["learner"]
                        )
                    except Exception as e:
                        print(f"Bulk enrollment portal creation error for {email}: {e}")
                        continue

                user_id = str(portal_user.get("user_id"))
                user = models.User(
                    id=user_id,
                    email=email,
                    full_name=portal_user.get("full_name") or email.split('@')[0].title(),
                    role=models.UserRole.LEARNER,
                    is_active=True,
                    is_suspended=False,
                    hashed_password=""
                )
                db.add(user)
                db.commit()
                db.refresh(user)

            enrollment = db.query(models.Enrollment).filter(
                models.Enrollment.user_id == user.id,
                models.Enrollment.course_id == course_id
            ).first()

            if not enrollment:
                enrollment = models.Enrollment(user_id=user.id, course_id=course_id)
                db.add(enrollment)
                db.commit()

    except Exception as e:
        print(f"Bulk enrollment failed: {e}")
    finally:
        db.close()


@router.post("/courses/{course_id}/enroll/bulk", status_code=status.HTTP_202_ACCEPTED)
async def bulk_enroll_users(
    course_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    content = await file.read()
    background_tasks.add_task(process_bulk_enrollment, course_id, content)
    return {"message": "Bulk enrollment started in background"}


# ── Certificate Management ───────────────────────────────────────────────────

@router.post("/certificates/{certificate_id}/revoke")
def revoke_certificate(
    certificate_id: int,
    reason: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    from sqlalchemy.sql import func
    cert = db.query(models.Certificate).filter(
        models.Certificate.id == certificate_id
    ).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")

    cert.revoked_at = func.now()
    cert.revoked_reason = reason

    log = models.AuditLog(
        admin_id=current_user.id,
        action_type="revoke_certificate",
        target_entity=f"Certificate {cert.certificate_code}"
    )
    db.add(log)
    db.commit()
    return {"message": f"Certificate {cert.certificate_code} revoked successfully."}
