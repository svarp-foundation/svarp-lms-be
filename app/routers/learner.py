import os
import requests
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from typing import List
from sqlalchemy.sql import func
from .. import models, schemas, database, auth, completion_engine, utils

router = APIRouter(
    prefix="/learner",
    tags=["learner"],
)

# Note: SVARP_ADMIN_BASE_URL etc. are now in utils.py

@router.get("/courses", response_model=List[schemas.EnrolledCourse])
def read_my_courses(db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.require_learner)):
    # Return courses the user is enrolled in that are not deleted
    enrollments = (
        db.query(models.Enrollment)
        .join(models.Course, models.Course.id == models.Enrollment.course_id)
        .filter(
            models.Enrollment.user_id == current_user.id,
            models.Course.is_deleted == False
        )
        .all()
    )
    if not enrollments:
        return []

    course_ids = [enr.course_id for enr in enrollments]
    progress_map = completion_engine.calculate_dynamic_progress_batch(db, current_user.id, course_ids)

    courses_with_progress = []
    for enr in enrollments:
        prog = progress_map.get(enr.course_id, 0)
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
        # Check membership first - members get it for free
        user_data = utils.fetch_user_membership(current_user.email)
        if not utils.is_active_member(user_data):
            # Non-members must have a completed payment
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

    # Check if user already has an APPROVED attempt
    approved_submission = db.query(models.Submission).filter(
        models.Submission.assignment_id == assignment_id,
        models.Submission.user_id == current_user.id,
        models.Submission.status == models.SubmissionStatus.APPROVED
    ).first()

    if approved_submission:
        raise HTTPException(status_code=400, detail="Assignment already approved")

    # Always create a new submission for the attempt
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

    # Auto-approve/reject MCQ-only based on passing score
    if not has_subjective and mcq_total > 0:
        course_id = assignment.course_id
        if not course_id and assignment.lesson_id:
            lesson = db.query(models.Lesson).filter(models.Lesson.id == assignment.lesson_id).first()
            if lesson and lesson.module:
                course_id = lesson.module.course_id
        
        passing_score = 70
        if course_id:
            course = db.query(models.Course).filter(models.Course.id == course_id).first()
            if course and course.passing_score is not None:
                passing_score = course.passing_score

        score_percent = (mcq_score / mcq_total) * 100
        if score_percent >= passing_score:
            submission.status = models.SubmissionStatus.APPROVED
        else:
            submission.status = models.SubmissionStatus.REJECTED

    # Mark lesson complete for non-rejected submissions (provisional instant completion)
    if assignment.lesson_id and submission.status != models.SubmissionStatus.REJECTED:
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
    ).order_by(models.Submission.id.desc()).first()
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
        
    course = (
        db.query(models.Course)
        .options(joinedload(models.Course.modules).joinedload(models.Module.lessons))
        .filter(models.Course.id == course_id, models.Course.is_deleted == False)
        .first()
    )
    if not course:
        raise HTTPException(status_code=404, detail="Course not found or deleted")
        
    # Get all completions for this user & course
    completions = (
        db.query(models.LessonCompletion.lesson_id)
        .join(models.Lesson, models.Lesson.id == models.LessonCompletion.lesson_id)
        .join(models.Module, models.Module.id == models.Lesson.module_id)
        .filter(
            models.LessonCompletion.user_id == current_user.id,
            models.Module.course_id == course_id
        )
        .all()
    )
    completed_lesson_ids = {c[0] for c in completions}

    # Also collect assignment lessons that have any submission (submitted/under_review/approved/rejected)
    submitted_subs = (
        db.query(models.Assignment.lesson_id)
        .join(models.Submission, models.Submission.assignment_id == models.Assignment.id)
        .join(models.Lesson, models.Lesson.id == models.Assignment.lesson_id)
        .join(models.Module, models.Module.id == models.Lesson.module_id)
        .filter(
            models.Submission.user_id == current_user.id,
            models.Module.course_id == course_id,
            models.Assignment.lesson_id != None,
        )
        .all()
    )
    submitted_assignment_lesson_ids = {s[0] for s in submitted_subs}

    # A lesson is "done for traversal" if it is fully completed OR is an assignment that was submitted
    traversal_done_ids = completed_lesson_ids | submitted_assignment_lesson_ids

    # Construct response with locking logic
    modules_data = []
    sorted_modules = sorted(course.modules, key=lambda m: m.order)
    previous_traversed = True  # First lesson is always unlocked
    total_lessons_count = 0

    for module in sorted_modules:
        lessons_data = []
        sorted_lessons = sorted(module.lessons, key=lambda l: l.order)
        total_lessons_count += len(sorted_lessons)

        for lesson in sorted_lessons:
            is_completed = lesson.id in completed_lesson_ids
            is_locked = not previous_traversed

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

            previous_traversed = lesson.id in traversal_done_ids

        modules_data.append(schemas.ModuleStatus(
            id=module.id,
            title=module.title,
            order=module.order,
            lessons=lessons_data
        ))

    # In-memory progress calculation from eager-loaded structures (0 extra DB queries)
    if total_lessons_count > 0:
        prog = min(100, int((len(traversal_done_ids) / total_lessons_count) * 100))
    else:
        prog = 0

    # Fetch Certificate if it exists and is not revoked
    cert = db.query(models.Certificate).filter(
        models.Certificate.user_id == current_user.id,
        models.Certificate.course_id == course_id,
        models.Certificate.revoked_at == None
    ).first()

    # Fetch profile picture and verification readiness (cached)
    profile_picture_url = None
    verification_readiness = None
    user_data = utils.fetch_user_membership(current_user.email)
    if user_data:
        profile_path = user_data.get("profile_picture_path")
        if profile_path:
            profile_picture_url = "/media/profile-picture"
        verification_readiness = user_data.get("payment_readiness")

    # Fetch Final Assignment if required
    final_assignment = None
    if course.require_final_assignment:
        final_assignment = db.query(models.Assignment).filter(
            models.Assignment.course_id == course_id,
            models.Assignment.lesson_id == None
        ).first()

    return schemas.CourseContent(
        id=course.id,
        title=course.title,
        modules=modules_data,
        progress=prog,
        require_final_assignment=course.require_final_assignment,
        final_assignment=schemas.AssignmentDetail.model_validate(final_assignment, from_attributes=True) if final_assignment else None,
        certificate_pdf_url=cert.pdf_url if cert else None,
        certificate_code=cert.certificate_code if cert else None,
        profile_picture_url=profile_picture_url,
        verification_readiness=verification_readiness
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

@router.get("/certificates", response_model=List[schemas.UserCertificate])
def get_user_certificates(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Return all generated certificates for the learner."""
    certs = db.query(models.Certificate).join(models.Course).filter(
        models.Certificate.user_id == current_user.id,
        models.Certificate.revoked_at == None,
        models.Course.is_deleted == False
    ).all()
    
    result = []
    for cert in certs:
        result.append(schemas.UserCertificate(
            id=cert.id,
            course_id=cert.course_id,
            course_title=cert.course.title,
            issued_at=cert.issued_at,
            certificate_code=cert.certificate_code,
            pdf_url=cert.pdf_url
        ))
    return result

# ── Lesson Discussion/Comments Endpoints ─────────────────────────────────────

@router.post("/lessons/{lesson_id}/comments", response_model=schemas.LessonCommentOut)
def post_lesson_comment(
    lesson_id: int,
    comment_data: schemas.LessonCommentCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Post a comment or question to a specific lesson."""
    lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
        
    # Verify enrollment in the course that contains this lesson
    course_id = lesson.module.course_id
    enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.user_id == current_user.id,
        models.Enrollment.course_id == course_id
    ).first()
    if not enrollment and current_user.role != models.UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Not enrolled in this course")

    comment = models.LessonComment(
        lesson_id=lesson_id,
        user_id=current_user.id,
        content=comment_data.content
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    
    if comment.user:
        comment.user.profile_picture_url = f"/media/profile-picture?email={comment.user.email}"
        
    return comment

@router.get("/lessons/{lesson_id}/comments", response_model=List[schemas.LessonCommentOut])
def get_lesson_comments(
    lesson_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Retrieve all comments/discussion for a specific lesson."""
    lesson = db.query(models.Lesson).filter(models.Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    # Verify enrollment or admin status
    course_id = lesson.module.course_id
    enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.user_id == current_user.id,
        models.Enrollment.course_id == course_id
    ).first()
    if not enrollment and current_user.role != models.UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Not enrolled in this course")

    comments = db.query(models.LessonComment).filter(
        models.LessonComment.lesson_id == lesson_id
    ).order_by(models.LessonComment.created_at.asc()).all()
    
    for comment in comments:
        if comment.user:
            comment.user.profile_picture_url = f"/media/profile-picture?email={comment.user.email}"
            
    return comments

@router.delete("/lessons/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lesson_comment(
    comment_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Delete a lesson comment if the user is the author or an admin."""
    comment = db.query(models.LessonComment).filter(models.LessonComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
        
    if comment.user_id != current_user.id and current_user.role != models.UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")

    db.delete(comment)
    db.commit()
    return None

@router.get("/assignments/{assignment_id}/attempts", response_model=List[schemas.AttemptOut])
def get_assignment_attempts(
    assignment_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner)
):
    """Retrieve all submission attempts for a specific assignment by the current learner."""
    assignment = db.query(models.Assignment).filter(models.Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    submissions = db.query(models.Submission).filter(
        models.Submission.assignment_id == assignment_id,
        models.Submission.user_id == current_user.id
    ).order_by(models.Submission.submitted_at.desc()).all()

    result = []
    for sub in submissions:
        result.append(schemas.AttemptOut(
            submission_id=sub.id,
            status=sub.status,
            submitted_at=sub.submitted_at,
            grade=sub.grade,
            feedback=sub.feedback,
            mcq_score=sub.mcq_score,
            mcq_total=sub.mcq_total
        ))
    return result
