from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import shutil
import os
import uuid
import csv
import io
from app.clients.user_portal_client import user_portal_client, ServiceError
from .. import models, schemas, database, auth, completion_engine
from ..utils import UPLOAD_DIR

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)


@router.get("/stats")
async def get_admin_stats(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    try:
        portal_users = await user_portal_client.list_users(limit=500)
        active_portal_ids = {str(pu.get("user_id")) for pu in portal_users}
        
        # Prune any local orphaned LMS users soft-deleted on central portal
        orphans = db.query(models.User).filter(
            models.User.role == "learner",
            ~models.User.id.in_(active_portal_ids)
        ).all()
        for orphan in orphans:
            db.query(models.Enrollment).filter(models.Enrollment.user_id == orphan.id).delete()
            db.query(models.LessonCompletion).filter(models.LessonCompletion.user_id == orphan.id).delete()
            db.query(models.Wishlist).filter(models.Wishlist.user_id == orphan.id).delete()
            db.query(models.LessonComment).filter(models.LessonComment.user_id == orphan.id).delete()
            db.delete(orphan)
        if orphans:
            db.commit()
    except Exception:
        pass

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
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Deleted physical file: {file_path}")
    except Exception as e:
        print(f"Failed to delete physical file {file_url}: {e}")

@router.post("/upload")
async def upload_file(file: UploadFile = File(...), current_user: models.User = Depends(auth.require_admin)):
    file_extension = os.path.splitext(file.filename)[1]
    unique_filename = f"{uuid.uuid4()}{file_extension}"
    
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, unique_filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Return relative URL
    return {"url": f"/static/uploads/{unique_filename}"}

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

@router.post("/courses/{course_id}/generate-final-assignment", response_model=schemas.Assignment)
def generate_final_assignment(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    """
    Consolidate all questions from all lesson-based assignments in the course 
    into a single course-level Final Assignment.
    """
    course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # 1. Find all lesson-assignments in this course
    lesson_assignments = db.query(models.Assignment).join(models.Lesson).join(models.Module).filter(
        models.Module.course_id == course_id
    ).all()

    if not lesson_assignments:
        raise HTTPException(status_code=400, detail="No assignments found in lessons to consolidate.")

    # 2. Check if a final assignment already exists (course_id set, lesson_id None)
    db_final = db.query(models.Assignment).filter(
        models.Assignment.course_id == course_id,
        models.Assignment.lesson_id == None
    ).first()

    if db_final:
        # Optional: could update it, let's delete and recreation for simplicity or just error
        # Let's delete existing questions and recreate
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

    # 3. Copy questions
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

    # 4. Enable require_final_assignment for the course
    course.require_final_assignment = True
    
    db.commit()
    db.refresh(db_final)
    return db_final

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

    result = []
    for pu in portal_users:
        user_id = str(pu.get("user_id"))
        email = pu.get("email")
        full_name = pu.get("full_name") or (email.split("@")[0].title() if email else "")
        roles = pu.get("roles", [])
        primary_role = "admin" if "admin" in roles else "learner"

        db_user = db.query(models.User).filter(models.User.id == user_id).first()
        if not db_user and email:
            db_user = db.query(models.User).filter(models.User.email == email).first()

        if db_user:
            db_user.id = user_id
            db_user.email = email
            db_user.full_name = full_name
            db_user.role = primary_role
            db_user.is_active = True
            db.commit()
            db.refresh(db_user)
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
            db.commit()
            db.refresh(db_user)

        result.append(db_user)

    return result

# Admin Course Management
@router.post("/courses", response_model=schemas.Course)
def create_course(course: schemas.CourseCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
    db_course = models.Course(**course.dict(exclude={"discounted_price"}), status=models.CourseStatus.DRAFT, is_deleted=False)
    db.add(db_course)
    db.commit()
    db.refresh(db_course)
    return db_course

def parse_course_file(content: str):
    import re
    # Split by the section markers
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
                        # Join the rest of the lines as content
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
                    # Check for (is_correct=true)
                    is_correct = 'is_correct=true' in opt_body.lower()
                    opt_text = re.sub(r'\(is_correct=(true|false)\)', '', opt_body, flags=re.IGNORECASE).strip()
                    current_question['options'].append({'text': opt_text, 'is_correct': is_correct})
                elif ':' in line:
                    key, val = line.split(':', 1)
                    current_question[key.strip().lower().replace(' ', '_')] = val.strip()
            current_assignment['questions'].append(current_question)
            
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
                db.flush()

                # If assignment, create assignment and questions
                if l_data.get('type', '').lower() == 'assignment' and 'assignment_data' in l_data:
                    a_data = l_data['assignment_data']
                    db_assignment = models.Assignment(
                        course_id=db_course.id,
                        lesson_id=db_lesson.id,
                        title=a_data.get('title', l_data.get('title', 'Assignment')),
                        description=a_data.get('description', 'Assignment description')
                    )
                    db.add(db_assignment)
                    db.flush()

                    for q_idx, q_data in enumerate(a_data.get('questions', [])):
                        db_question = models.Question(
                            assignment_id=db_assignment.id,
                            question_text=q_data.get('text', q_data.get('question_text', '')),
                            question_type=q_data.get('type', 'subjective').lower(),
                            order=int(q_data.get('order', q_idx + 1))
                        )
                        db.add(db_question)
                        db.flush()

                        if db_question.question_type == 'mcq':
                            for opt in q_data.get('options', []):
                                db_option = models.QuestionOption(
                                    question_id=db_question.id,
                                    option_text=opt['text'],
                                    is_correct=opt['is_correct']
                                )
                                db.add(db_option)
        
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

@router.post("/courses/{course_id}/update-from-file", status_code=status.HTTP_200_OK)
async def update_course_from_file(
    course_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="File must be a .txt file")
    
    db_course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if not db_course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")
        
    content = await file.read()
    decoded_content = content.decode('utf-8')
    
    try:
        course_data, modules = parse_course_file(decoded_content)
        
        if not course_data:
            raise HTTPException(status_code=400, detail="Course data missing in file")
            
        # Update Course Metadata
        if 'title' in course_data:
            db_course.title = course_data['title']
        if 'description' in course_data:
            db_course.description = course_data['description']
        if 'price' in course_data:
            db_course.price = float(course_data['price'])
        if 'is_paid' in course_data:
            db_course.is_paid = course_data['is_paid'].lower() == 'true'

        # Delete existing modules, lessons, assignments, and questions for this course
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

        # Re-create Modules and Lessons from file
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
                db.flush()

                if l_data.get('type', '').lower() == 'assignment' and 'assignment_data' in l_data:
                    a_data = l_data['assignment_data']
                    db_assignment = models.Assignment(
                        course_id=db_course.id,
                        lesson_id=db_lesson.id,
                        title=a_data.get('title', l_data.get('title', 'Assignment')),
                        description=a_data.get('description', 'Assignment description')
                    )
                    db.add(db_assignment)
                    db.flush()

                    for q_idx, q_data in enumerate(a_data.get('questions', [])):
                        db_question = models.Question(
                            assignment_id=db_assignment.id,
                            question_text=q_data.get('text', q_data.get('question_text', '')),
                            question_type=q_data.get('type', 'subjective').lower(),
                            order=int(q_data.get('order', q_idx + 1))
                        )
                        db.add(db_question)
                        db.flush()

                        if db_question.question_type == 'mcq':
                            for opt in q_data.get('options', []):
                                db_option = models.QuestionOption(
                                    question_id=db_question.id,
                                    option_text=opt['text'],
                                    is_correct=opt['is_correct']
                                )
                                db.add(db_option)

        db.commit()
        db.refresh(db_course)

        # Audit Log
        log = models.AuditLog(admin_id=current_user.id, action_type="update_course_from_file", target_entity=f"Course {db_course.id}")
        db.add(log)
        db.commit()

        return {"message": f"Course '{db_course.title}' updated successfully from file", "course_id": db_course.id, "title": db_course.title}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to parse or update course: {str(e)}")

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
async def suspend_user(user_id: str, suspend: bool = True, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_admin)):
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

    # Also soft delete user on Central User Portal (portal-user)
    try:
        await user_portal_client.delete_user(target_user_id)
    except Exception as e:
        print(f"Warning: Failed to soft delete user on Central User Portal: {e}")

    # Cascade delete all related LMS records
    db.query(models.Enrollment).filter(models.Enrollment.user_id == target_user_id).delete()
    db.query(models.LessonCompletion).filter(models.LessonCompletion.user_id == target_user_id).delete()
    
    # Submissions and AnswerSubmissions
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
@router.get("/submissions")
def list_submissions(
    status: Optional[str] = Query(None),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_admin)
):
    query = db.query(models.Submission)
    if status:
        query = query.filter(models.Submission.status == status)
    submissions = query.order_by(models.Submission.submitted_at.desc()).all()
    
    result = []
    for sub in submissions:
        user = db.query(models.User).filter(models.User.id == sub.user_id).first()
        assignment = sub.assignment
        answers = db.query(models.AnswerSubmission).filter(models.AnswerSubmission.submission_id == sub.id).all()
        answer_details = []
        for ans in answers:
            question = ans.question
            answer_details.append({
                "question_id": ans.question_id,
                "question_text": question.question_text if question else "",
                "question_type": question.question_type.value if (question and hasattr(question.question_type, 'value')) else str(question.question_type) if question else "",
                "answer_text": ans.answer_text,
                "selected_option_id": ans.selected_option_id,
                "is_correct": ans.is_correct
            })
        result.append({
            "id": sub.id,
            "user_id": sub.user_id,
            "user_name": user.full_name if user else "Unknown User",
            "user_email": user.email if user else "",
            "assignment_id": sub.assignment_id,
            "assignment_title": assignment.title if assignment else "",
            "status": sub.status.value if hasattr(sub.status, 'value') else str(sub.status),
            "grade": sub.grade,
            "feedback": sub.feedback,
            "submitted_at": sub.submitted_at,
            "answers": answer_details
        })
    return result

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
        if assignment:
            if assignment.lesson_id:
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
            
            # Always check for course completion if an assignment was approved
            if assignment.course_id:
                 completion_engine.check_course_completion(db, submission.user_id, assignment.course_id)
            elif assignment.lesson_id:
                lesson = db.query(models.Lesson).filter(models.Lesson.id == assignment.lesson_id).first()
                if lesson and lesson.module:
                    course_id = lesson.module.course_id
                    completion_engine.check_course_completion(db, submission.user_id, course_id)

    # If rejected, revoke certificate and remove lesson completion
    elif review_data.status == models.SubmissionStatus.REJECTED:
        assignment = submission.assignment
        course_id = assignment.course_id if assignment else None
        if not course_id and assignment and assignment.lesson_id:
            lesson = db.query(models.Lesson).filter(models.Lesson.id == assignment.lesson_id).first()
            if lesson and lesson.module:
                course_id = lesson.module.course_id

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
            from datetime import datetime
            for cert in certs:
                cert.revoked_at = datetime.utcnow()
                cert.revoked_reason = review_data.feedback or "Submission rejected during instructor review"
                db.add(cert)

    db.commit()
    return {"message": "Submission reviewed successfully"}

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
