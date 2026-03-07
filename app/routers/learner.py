from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from sqlalchemy.sql import func
from .. import models, schemas, database, auth, completion_engine

router = APIRouter(
    prefix="/learner",
    tags=["learner"],
)

@router.get("/courses", response_model=List[schemas.EnrolledCourse])
def read_my_courses(db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_learner)):
    # Return courses the user is enrolled in that are not deleted
    enrollments = db.query(models.Enrollment).join(models.Course).filter(
        models.Enrollment.user_id == current_user.id,
        models.Course.is_deleted == False
    ).all()
    courses_with_progress = []
    for enr in enrollments:
        prog = completion_engine.calculate_dynamic_progress(db, current_user.id, enr.course_id)
        course_data = {c.name: getattr(enr.course, c.name) for c in enr.course.__table__.columns}
        courses_with_progress.append(schemas.EnrolledCourse(**course_data, progress=prog))
    return courses_with_progress

@router.post("/enroll/{course_id}")
def enroll_course(course_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_learner)):

    existing_enrollment = db.query(models.Enrollment).filter(models.Enrollment.user_id == current_user.id, models.Enrollment.course_id == course_id).first()
    if existing_enrollment:
        raise HTTPException(status_code=400, detail="Already enrolled")

    # Guard: paid courses require a completed payment
    course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")

    if course.is_paid:
        completed_payment = db.query(models.CoursePayment).filter(
            models.CoursePayment.user_id == current_user.id,
            models.CoursePayment.course_id == course_id,
            models.CoursePayment.status == "success",
        ).first()
        if not completed_payment:
            raise HTTPException(
                status_code=402,
                detail="Payment required. Please complete payment before enrolling in this course.",
            )

    enrollment = models.Enrollment(user_id=current_user.id, course_id=course_id)
    db.add(enrollment)
    db.commit()
    return {"message": "Enrolled successfully"}

@router.post("/courses/{course_id}/lessons/{lesson_id}/complete")
def mark_lesson_complete(
    course_id: int, 
    lesson_id: int, 
    db: Session = Depends(database.get_db), 
    current_user: models.User = Depends(auth.require_learner)
):
    # Verify enrollment
    enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.user_id == current_user.id, 
        models.Enrollment.course_id == course_id
    ).first()
    
    if not enrollment:
        raise HTTPException(status_code=403, detail="Not enrolled in this course")
        
    # Verify lesson belongs to course
    lesson = db.query(models.Lesson).join(models.Module).filter(
        models.Lesson.id == lesson_id,
        models.Module.course_id == course_id
    ).first()
    
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found in this course")
        
    # Check if already completed
    existing_completion = db.query(models.LessonCompletion).filter(
        models.LessonCompletion.user_id == current_user.id,
        models.LessonCompletion.lesson_id == lesson_id
    ).first()
    
    if not existing_completion:
        completion = models.LessonCompletion(user_id=current_user.id, lesson_id=lesson_id)
        db.add(completion)
        db.commit()
        
    db.commit()

    # Recalculate dynamic progress
    progress = completion_engine.calculate_dynamic_progress(db, current_user.id, course_id)
        
    enrollment.progress = progress
    db.commit()
    
    # TRIGGER COMPLETION ENGINE
    completion_engine.check_course_completion(db, current_user.id, course_id)
    
    
    return {"message": "Lesson marked as complete", "progress": progress}

@router.get("/assignments/by-lesson/{lesson_id}", response_model=schemas.AssignmentDetail)
def get_assignment_by_lesson(
    lesson_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    assignment = db.query(models.Assignment).filter(models.Assignment.lesson_id == lesson_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="No assignment found for this lesson")
    return assignment

@router.get("/assignments/{assignment_id}", response_model=schemas.AssignmentDetail)
def get_assignment(
    assignment_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    assignment = db.query(models.Assignment).filter(models.Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return assignment

@router.post("/assignments/{assignment_id}/submit", response_model=schemas.SubmissionResult)
def submit_assignment(
    assignment_id: int,
    submit_data: schemas.AssignmentSubmit,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    assignment = db.query(models.Assignment).filter(models.Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    # Enrollment check
    if assignment.lesson_id:
        lesson = db.query(models.Lesson).filter(models.Lesson.id == assignment.lesson_id).first()
        if lesson and lesson.module:
            course_id = lesson.module.course_id
            enrollment = db.query(models.Enrollment).filter(
                models.Enrollment.user_id == current_user.id,
                models.Enrollment.course_id == course_id
            ).first()
            if not enrollment:
                raise HTTPException(status_code=403, detail="Not enrolled in this course")

    # Find or create submission
    submission = db.query(models.Submission).filter(
        models.Submission.assignment_id == assignment_id,
        models.Submission.user_id == current_user.id
    ).first()

    if submission:
        if submission.status == models.SubmissionStatus.APPROVED:
            raise HTTPException(status_code=400, detail="Assignment already approved")
        # Clear old answers
        db.query(models.AnswerSubmission).filter(
            models.AnswerSubmission.submission_id == submission.id
        ).delete()
        submission.status = models.SubmissionStatus.SUBMITTED
        submission.submitted_at = func.now()
    else:
        submission = models.Submission(
            assignment_id=assignment_id,
            user_id=current_user.id,
            status=models.SubmissionStatus.SUBMITTED
        )
        db.add(submission)
        db.flush()

    # Map submission answers
    answers_map = {a.question_id: a for a in submit_data.answers}
    mcq_score = 0
    mcq_total = 0
    has_subjective = False
    answer_results = []

    for question in assignment.questions:
        ans_data = answers_map.get(question.id)
        answer_text = None
        selected_option_id = None
        is_correct = None

        if question.question_type == models.QuestionType.MCQ:
            mcq_total += 1
            if ans_data and ans_data.selected_option_id:
                selected_option_id = ans_data.selected_option_id
                correct_option = db.query(models.QuestionOption).filter(
                    models.QuestionOption.question_id == question.id,
                    models.QuestionOption.is_correct == True
                ).first()
                is_correct = (correct_option is not None and correct_option.id == selected_option_id)
                if is_correct:
                    mcq_score += 1
        else:
            has_subjective = True
            if ans_data:
                answer_text = ans_data.answer_text

        db_answer = models.AnswerSubmission(
            submission_id=submission.id,
            question_id=question.id,
            answer_text=answer_text,
            selected_option_id=selected_option_id,
            is_correct=is_correct,
        )
        db.add(db_answer)

        answer_results.append(schemas.AnswerResult(
            question_id=question.id,
            question_text=question.question_text,
            question_type=question.question_type,
            answer_text=answer_text,
            selected_option_id=selected_option_id,
            is_correct=is_correct,
        ))

    submission.mcq_score = mcq_score
    submission.mcq_total = mcq_total

    # Auto-approve if only MCQ questions
    if not has_subjective and mcq_total > 0:
        submission.status = models.SubmissionStatus.APPROVED
        # Mark lesson complete
        if assignment.lesson_id:
            existing = db.query(models.LessonCompletion).filter(
                models.LessonCompletion.user_id == current_user.id,
                models.LessonCompletion.lesson_id == assignment.lesson_id
            ).first()
            if not existing:
                db.add(models.LessonCompletion(
                    user_id=current_user.id,
                    lesson_id=assignment.lesson_id
                ))

    db.commit()
    db.refresh(submission)

    # Trigger completion engine
    if assignment.lesson_id:
        lesson = db.query(models.Lesson).filter(models.Lesson.id == assignment.lesson_id).first()
        if lesson and lesson.module:
            completion_engine.check_course_completion(db, current_user.id, lesson.module.course_id)

    return schemas.SubmissionResult(
        submission_id=submission.id,
        status=submission.status,
        mcq_score=mcq_score if mcq_total > 0 else None,
        mcq_total=mcq_total if mcq_total > 0 else None,
        has_subjective=has_subjective,
        answers=answer_results,
    )

@router.get("/assignments/{assignment_id}/submission", response_model=schemas.SubmissionResult)
def get_my_submission(
    assignment_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    submission = db.query(models.Submission).filter(
        models.Submission.assignment_id == assignment_id,
        models.Submission.user_id == current_user.id
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="No submission found")

    answer_results = []
    for ans in submission.answers:
        answer_results.append(schemas.AnswerResult(
            question_id=ans.question_id,
            question_text=ans.question.question_text,
            question_type=ans.question.question_type,
            answer_text=ans.answer_text,
            selected_option_id=ans.selected_option_id,
            is_correct=ans.is_correct,
        ))

    return schemas.SubmissionResult(
        submission_id=submission.id,
        status=submission.status,
        mcq_score=submission.mcq_score,
        mcq_total=submission.mcq_total,
        has_subjective=any(a.question.question_type == models.QuestionType.SUBJECTIVE for a in submission.answers),
        answers=answer_results,
    )

@router.get("/courses/{course_id}/content", response_model=schemas.CourseContent)
def get_course_content(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    # Verify Enrollment
    enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.user_id == current_user.id,
        models.Enrollment.course_id == course_id
    ).first()
    
    if not enrollment:
        raise HTTPException(status_code=403, detail="Not enrolled in this course")
        
    course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")
        
    # Get all completions for this user & course
    completions = db.query(models.LessonCompletion).join(models.Lesson).join(models.Module).filter(
        models.LessonCompletion.user_id == current_user.id,
        models.Module.course_id == course_id
    ).all()
    completed_lesson_ids = {c.lesson_id for c in completions}

    # Also collect assignment lessons that have *any* submission (submitted/under_review/approved/rejected).
    # These count as "traversable" — the learner has done their part, so the next lesson
    # should not be locked even if approval is still pending.
    submitted_assignment_lesson_ids: set[int] = set()
    submitted_subs = (
        db.query(models.Submission, models.Assignment)
        .join(models.Assignment, models.Submission.assignment_id == models.Assignment.id)
        .filter(
            models.Submission.user_id == current_user.id,
            models.Assignment.lesson_id != None,
        )
        .all()
    )
    for sub, asgn in submitted_subs:
        # Verify the assignment's lesson belongs to this course
        lesson = db.query(models.Lesson).join(models.Module).filter(
            models.Lesson.id == asgn.lesson_id,
            models.Module.course_id == course_id,
        ).first()
        if lesson:
            submitted_assignment_lesson_ids.add(asgn.lesson_id)

    # A lesson is "done for traversal" if it is fully completed OR is an assignment that was submitted
    traversal_done_ids = completed_lesson_ids | submitted_assignment_lesson_ids

    # Construct response with locking logic
    modules_data = []

    sorted_modules = sorted(course.modules, key=lambda m: m.order)

    previous_traversed = True  # First lesson is always unlocked

    for module in sorted_modules:
        lessons_data = []
        sorted_lessons = sorted(module.lessons, key=lambda l: l.order)

        for lesson in sorted_lessons:
            is_completed = lesson.id in completed_lesson_ids
            is_locked = not previous_traversed

            # If already completed or submitted, never show as locked
            if lesson.id in traversal_done_ids:
                is_locked = False

            lessons_data.append(schemas.LessonStatus(
                id=lesson.id,
                title=lesson.title,
                lesson_type=lesson.lesson_type,
                content=lesson.content,
                video_url=lesson.video_url,
                completed=is_completed,
                locked=is_locked,
                order=lesson.order
            ))

            # Advance traversal gate: lesson unlocks the next one once done
            previous_traversed = lesson.id in traversal_done_ids

        modules_data.append(schemas.ModuleStatus(
            id=module.id,
            title=module.title,
            order=module.order,
            lessons=lessons_data
        ))

        
    # Fetch dynamic progression
    prog = completion_engine.calculate_dynamic_progress(db, current_user.id, course_id)

    # Fetch Certificate if it exists and is not revoked
    cert = db.query(models.Certificate).filter(
        models.Certificate.user_id == current_user.id,
        models.Certificate.course_id == course_id,
        models.Certificate.revoked_at == None
    ).first()

    return schemas.CourseContent(
        id=course.id,
        title=course.title,
        modules=modules_data,
        progress=prog,
        certificate_pdf_url=cert.pdf_url if cert else None
    )

# ─── Wishlist Endpoints ─────────────────────────────────────────────────────

@router.get("/wishlist", response_model=List[schemas.WishlistItem])
def get_wishlist(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Return all courses the learner has wishlisted that are not deleted."""
    items = db.query(models.Wishlist).join(models.Course).filter(
        models.Wishlist.user_id == current_user.id,
        models.Course.is_deleted == False
    ).all()
    return [item.course for item in items]


@router.post("/wishlist/{course_id}", status_code=201)
def add_to_wishlist(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Add a course to the learner's wishlist."""
    course = db.query(models.Course).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")

    existing = db.query(models.Wishlist).filter(
        models.Wishlist.user_id == current_user.id,
        models.Wishlist.course_id == course_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Already in wishlist")

    item = models.Wishlist(user_id=current_user.id, course_id=course_id)
    db.add(item)
    db.commit()
    return {"message": "Added to wishlist"}


@router.delete("/wishlist/{course_id}")
def remove_from_wishlist(
    course_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Remove a course from the learner's wishlist."""
    item = db.query(models.Wishlist).filter(
        models.Wishlist.user_id == current_user.id,
        models.Wishlist.course_id == course_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Course not in wishlist")

    db.delete(item)
    db.commit()
    return {"message": "Removed from wishlist"}
