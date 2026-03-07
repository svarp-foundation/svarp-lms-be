from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List
import shutil
import os
import uuid
import csv
import io
from .. import models, schemas, database, auth, completion_engine

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)

@router.get("/stats")
def get_admin_stats(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    total_users = db.query(models.User).filter(models.User.role == "learner").count()
    total_courses = db.query(models.Course).filter(models.Course.is_deleted == False).count()
    published_courses = db.query(models.Course).filter(models.Course.status == "published", models.Course.is_deleted == False).count()
    total_enrollments = db.query(models.Enrollment).count()
    # Revenue: sum of amounts for successful payments
    paid_payments = db.query(models.CoursePayment).filter(models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS).all()
    total_revenue = sum(p.amount for p in paid_payments)
    recent_users = db.query(models.User).filter(models.User.role == "learner").order_by(models.User.id.desc()).limit(5).all()
    recent_courses = db.query(models.Course).filter(models.Course.is_deleted == False).order_by(models.Course.id.desc()).limit(5).all()
    return {
        "total_users": total_users,
        "total_courses": total_courses,
        "published_courses": published_courses,
        "total_enrollments": total_enrollments,
        "total_revenue": total_revenue,
        "recent_users": [{"id": u.id, "full_name": u.full_name, "email": u.email} for u in recent_users],
        "recent_courses": [{"id": c.id, "title": c.title, "status": c.status, "is_paid": c.is_paid, "price": c.price} for c in recent_courses],
    }


@router.get("/payments", response_model=List[schemas.CoursePaymentAdmin])
def get_all_payments(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    payments = db.query(models.CoursePayment).order_by(models.CoursePayment.created_at.desc()).all()
    
    result = []
    for p in payments:
        user = db.query(models.User).filter(models.User.id == p.user_id).first()
        course = db.query(models.Course).filter(models.Course.id == p.course_id).first()
        
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

def delete_physical_file(file_url: str):
    """Safely delete a physical file from the uploads directory."""
    if not file_url or "/static/uploads/" not in file_url:
        return
    
    try:
        filename = file_url.split("/")[-1]
        file_path = os.path.join("backend/static/uploads", filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Deleted physical file: {file_path}")
    except Exception as e:
        print(f"Failed to delete physical file {file_url}: {e}")

@router.post("/upload")
async def upload_file(file: UploadFile = File(...), current_user: models.User = Depends(auth.require_admin)):
    
    file_extension = os.path.splitext(file.filename)[1]
    unique_filename = f"{uuid.uuid4()}{file_extension}"
    file_path = f"backend/static/uploads/{unique_filename}"
    
    # Ensure directory exists (redundant with main.py check but safe)
    os.makedirs("backend/static/uploads", exist_ok=True)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Return relative URL
    return {"url": f"http://localhost:8000/static/uploads/{unique_filename}"}

@router.post("/assignments", response_model=schemas.Assignment) 
def create_assignment(
    assignment: schemas.AssignmentCreate, 
    db: Session = Depends(database.get_db), 
    current_user: models.User = Depends(auth.require_admin)
):
    # Create assignment (without questions)
    db_assignment = models.Assignment(
        title=assignment.title,
        description=assignment.description,
        lesson_id=assignment.lesson_id,
        course_id=assignment.course_id,
    )
    db.add(db_assignment)
    db.flush()  # Get ID without committing

    # Create questions and options
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
    assignment = db.query(models.Assignment).filter(models.Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return assignment

@router.get("/courses/{course_id}/assignments", response_model=List[schemas.Assignment])
def list_course_assignments(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    assignments = db.query(models.Assignment).filter(models.Assignment.course_id == course_id).all()
    return assignments

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
        # Expected request format: email, full_name, password
        email = row.get('email')
        full_name = row.get('full_name')
        password = row.get('password')
        
        if not email or not password:
            errors.append(f"Row missing email or password: {row}")
            continue
            
        # Check if user exists
        db_user = db.query(models.User).filter(models.User.email == email).first()
        if db_user:
            errors.append(f"User already exists: {email}")
            continue
            
        hashed_password = auth.get_password_hash(password)
        new_user = models.User(
            email=email,
            full_name=full_name or "",
            hashed_password=hashed_password,
            role=models.UserRole.LEARNER # Default to learner for bulk add
        )
        db.add(new_user)
        created_count += 1
        
    db.commit()
    return {"message": f"Successfully created {created_count} users", "errors": errors}

@router.get("/users", response_model=List[schemas.User])
def read_users(skip: int = 0, limit: int = 100, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    users = db.query(models.User).offset(skip).limit(limit).all()
    return users

# Admin Course Management
@router.post("/courses", response_model=schemas.Course)
def create_course(course: schemas.CourseCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    db_course = models.Course(**course.dict(), status=models.CourseStatus.DRAFT, is_deleted=False)
    db.add(db_course)
    db.commit()
    db.refresh(db_course)
    return db_course

def parse_course_file(content: str):
    import re
    # Split by the section markers
    sections = re.split(r'---(COURSE|MODULE|LESSON|END_LESSON)---', content)
    
    course_data = {}
    modules = []
    current_module = None
    
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
            lesson_info = {}
            lines = body.split('\n')
            for idx, line in enumerate(lines):
                if ':' in line:
                    key, val = line.split(':', 1)
                    k = key.strip().lower().replace(' ', '_')
                    if k == 'content':
                        # Join the rest of the lines as content
                        lesson_info['content'] = val.strip() + ("\n" + "\n".join(lines[idx+1:]) if idx+1 < len(lines) else "")
                        break
                    else:
                        lesson_info[k] = val.strip()
            current_module['lessons'].append(lesson_info)
            
    return course_data, modules

@router.post("/courses/bulk-create", status_code=status.HTTP_201_CREATED)
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
        course_data, modules = parse_course_file(decoded_content)
        
        if not course_data:
            raise HTTPException(status_code=400, detail="Course data missing in file")
            
        # Create Course
        db_course = models.Course(
            title=course_data.get('title', 'Imported Course'),
            description=course_data.get('description', ''),
            price=float(course_data.get('price', 0)),
            is_paid=course_data.get('is_paid', 'false').lower() == 'true',
            status=models.CourseStatus.DRAFT
        )
        db.add(db_course)
        db.flush()
        
        # Create Modules and Lessons
        for m_idx, m_data in enumerate(modules):
            db_module = models.Module(
                course_id=db_course.id,
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
                    lesson_type=l_data.get('type', 'text').lower(),
                    order=l_idx + 1
                )
                db.add(db_lesson)
        
        db.commit()
        db.refresh(db_course)
        
        # Audit Log
        log = models.AuditLog(admin_id=current_user.id, action_type="bulk_create_course", target_entity=f"Course {db_course.id}")
        db.add(log)
        db.commit()
        
        return {"message": "Course created successfully", "course_id": db_course.id, "title": db_course.title}
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to parse or create course: {str(e)}")

@router.put("/courses/{course_id}", response_model=schemas.Course)
def update_course(course_id: int, course_update: schemas.CourseUpdate, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    
    db_course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if db_course is None:
        raise HTTPException(status_code=404, detail="Course not found or deleted")
    
    update_data = course_update.dict(exclude_unset=True)
    
    # Validation for Publishing
    if update_data.get("status") == models.CourseStatus.PUBLISHED:
        # Check if course has modules and lessons
        has_content = db.query(models.Module).filter(models.Module.course_id == course_id).first()
        if not has_content:
            raise HTTPException(status_code=400, detail="Cannot publish a course without modules.")
        
        has_lesson = db.query(models.Lesson).join(models.Module).filter(models.Module.course_id == course_id).first()
        if not has_lesson:
             raise HTTPException(status_code=400, detail="Cannot publish a course without lessons.")

    for key, value in update_data.items():
        setattr(db_course, key, value)
        
    db.commit()
    db.refresh(db_course)

    if update_data.get("status") == models.CourseStatus.PUBLISHED:
        log = models.AuditLog(admin_id=current_user.id, action_type="publish_course", target_entity=f"Course {db_course.id}")
        db.add(log)
        db.commit()

    return db_course

@router.get("/courses", response_model=List[schemas.Course])
def read_all_courses(skip: int = 0, limit: int = 100, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    courses = db.query(models.Course).filter(models.Course.is_deleted == False).offset(skip).limit(limit).all()
    return courses

@router.get("/courses/{course_id}", response_model=schemas.CourseAdminDetail)
def read_course(course_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    return course

@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(
    course_id: int, 
    db: Session = Depends(database.get_db), 
    current_user: models.User = Depends(auth.require_admin)
):
    course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
        
    # Check for enrollments (Soft delete handles safely but best practice to warn)
    enrollment_count = db.query(models.Enrollment).filter(models.Enrollment.course_id == course_id).count()
    if enrollment_count > 0:
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot delete course with {enrollment_count} active enrollments. Archive it instead."
        )
        
    course.is_deleted = True
    
    # Audit Log
    log = models.AuditLog(admin_id=current_user.id, action_type="delete_course", target_entity=f"Course {course_id}")
    db.add(log)
    
    db.commit()
    return None

@router.post("/users/{user_id}/suspend", status_code=status.HTTP_200_OK)
def suspend_user(user_id: int, suspend: bool = True, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_suspended = suspend
    
    action = "suspend_user" if suspend else "unsuspend_user"
    log = models.AuditLog(admin_id=current_user.id, action_type=action, target_entity=f"User {user_id}")
    db.add(log)
    
    db.commit()
    return {"message": f"User suspension status set to {suspend}"}

# Module and Lesson Management (Can be added here or imported)
@router.post("/courses/{course_id}/modules")
def create_module(course_id: int, module: schemas.ModuleCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    
    module_data = module.dict(exclude={"lessons"})
    db_module = models.Module(**module_data, course_id=course_id)
    db.add(db_module)
    db.commit()
    db.refresh(db_module)
    return db_module

@router.post("/modules/{module_id}/lessons")
def create_lesson(module_id: int, lesson: schemas.LessonCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    
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
        assignment = db.query(models.Assignment).filter(models.Assignment.lesson_id == lesson_id).first()
        if assignment:
            if "title" in update_data:
                assignment.title = update_data["title"]
            if "content" in update_data:
                assignment.description = update_data["content"]

    db.commit()
    db.refresh(db_lesson)
    return db_lesson

@router.delete("/modules/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_module(module_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    module = db.query(models.Module).filter(models.Module.id == module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    # Physical File Deletion
    if module.lessons:
        for lesson in module.lessons:
            if lesson.video_url:
                delete_physical_file(lesson.video_url)

    # Audit Log
    log = models.AuditLog(admin_id=current_user.id, action_type="delete_module", target_entity=f"Module {module_id}")
    db.add(log)
    
    db.delete(module)
    db.commit()
    return None

@router.delete("/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lesson(lesson_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    # Physical File Deletion
    if lesson.video_url:
        delete_physical_file(lesson.video_url)
    # Audit Log
    log = models.AuditLog(admin_id=current_user.id, action_type="delete_lesson", target_entity=f"Lesson {lesson_id}")
    db.add(log)
    
    db.delete(lesson)
    db.commit()
    return None

@router.post("/submissions/{submission_id}/review")
def review_submission(
    submission_id: int, 
    review_data: schemas.SubmissionReview, 
    db: Session = Depends(database.get_db), 
    current_user: models.User = Depends(auth.require_admin)
):
    submission = db.query(models.Submission).filter(models.Submission.id == submission_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
        
    submission.status = review_data.status
    submission.grade = review_data.grade
    submission.feedback = review_data.feedback
    
    # If approved, mark the lesson as complete
    if review_data.status == models.SubmissionStatus.APPROVED:
        # Check if assignment is linked to a lesson
        assignment = submission.assignment
        if assignment and assignment.lesson_id:
            # Check if completion already exists
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
                
                # Recalculate progress for the course
                # We need to find the course_id first. 
                # Lesson -> Module -> Course
                lesson = db.query(models.Lesson).filter(models.Lesson.id == assignment.lesson_id).first()
                if lesson and lesson.module:
                    course_id = lesson.module.course_id
                    
                    # Verify enrollment exists
                    enrollment = db.query(models.Enrollment).filter(
                        models.Enrollment.user_id == submission.user_id,
                        models.Enrollment.course_id == course_id
                    ).first()
                    
                    if enrollment:
                        # Trigger dynamic completion check
                        completion_engine.check_course_completion(db, submission.user_id, course_id)

    db.commit()
    return {"message": "Submission reviewed successfully"}

def process_bulk_enrollment(course_id: int, file_content: bytes):
    db: Session = database.SessionLocal()
    try:
        decoded_content = file_content.decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(decoded_content))
        
        for row in csv_reader:
            email = row.get('email')
            if not email:
                continue
                
            # Check/Create User
            user = db.query(models.User).filter(models.User.email == email).first()
            if not user:
                # Create user with dummy password
                # In real app, might send invite email
                pwd = auth.get_password_hash("changeme123")
                user = models.User(email=email, full_name=email.split('@')[0], hashed_password=pwd, role=models.UserRole.LEARNER)
                db.add(user)
                db.commit()
                db.refresh(user)
                
            # Check if enrollment exists
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
        
    # Check course exists synchronously to fail fast
    course = db.query(models.Course).filter(models.Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
        
    content = await file.read()
    background_tasks.add_task(process_bulk_enrollment, course_id, content)
    
    return {"message": "Bulk enrollment started in background"}

@router.post("/certificates/{certificate_id}/revoke")
def revoke_certificate(
    certificate_id: int, 
    reason: str, 
    db: Session = Depends(database.get_db), 
    current_user: models.User = Depends(auth.require_admin)
):
    from sqlalchemy.sql import func
    cert = db.query(models.Certificate).filter(models.Certificate.id == certificate_id).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
        
    cert.revoked_at = func.now()
    cert.revoked_reason = reason
    
    # Log audit
    log = models.AuditLog(
        admin_id=current_user.id, 
        action_type="revoke_certificate", 
        target_entity=f"Certificate {cert.certificate_code}"
    )
    db.add(log)
    
    db.commit()
    return {"message": f"Certificate {cert.certificate_code} revoked successfully."}
