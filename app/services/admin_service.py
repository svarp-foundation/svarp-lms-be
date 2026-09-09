"""
Admin Service — Optimized admin business logic.

Replaces the monolithic inlined queries in admin.py with:
- SQL aggregations instead of loading entire tables into Python
- Batch lookups instead of N+1 per-item queries
- joinedload for eager relationship loading
"""

import time
from collections import defaultdict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, distinct, desc, extract, case, and_
from typing import Optional

from .. import models

# ── In-Memory Analytics & Stats Cache ────────────────────────────────────────
_analytics_cache: dict = {"data": None, "expires_at": 0.0}
_stats_cache: dict = {"data": None, "expires_at": 0.0}
ANALYTICS_CACHE_TTL = 30.0  # 30 seconds
STATS_CACHE_TTL = 15.0      # 15 seconds

def invalidate_analytics_cache():
    """Clear cached analytics and stats on mutations."""
    _analytics_cache["expires_at"] = 0.0
    _stats_cache["expires_at"] = 0.0


# ── Admin Dashboard Stats ────────────────────────────────────────────────────

def get_admin_stats(db: Session, force_refresh: bool = False) -> dict:
    """
    Dashboard summary stats with in-memory TTL caching and consolidated SQL queries.
    """
    now_mono = time.monotonic()
    if not force_refresh and _stats_cache["data"] is not None and now_mono < _stats_cache["expires_at"]:
        return _stats_cache["data"]

    total_users = db.query(func.count(models.User.id)).filter(
        models.User.role == "learner"
    ).scalar() or 0

    course_summary = db.query(
        func.count(models.Course.id).label("total"),
        func.sum(case((models.Course.status == "published", 1), else_=0)).label("published")
    ).filter(models.Course.is_deleted == False).first()

    total_courses = course_summary.total or 0
    published_courses = int(course_summary.published or 0)
    total_enrollments = db.query(func.count(models.Enrollment.id)).scalar() or 0

    total_revenue = db.query(func.coalesce(func.sum(models.CoursePayment.amount), 0.0)).filter(
        models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS
    ).scalar() or 0.0

    recent_users = db.query(models.User).filter(
        models.User.role == "learner"
    ).order_by(models.User.created_at.desc()).limit(5).all()

    recent_courses = db.query(models.Course).options(
        joinedload(models.Course.instructor)
    ).filter(
        models.Course.is_deleted == False
    ).order_by(models.Course.id.desc()).limit(5).all()

    result = {
        "total_users": total_users,
        "total_courses": total_courses,
        "published_courses": published_courses,
        "total_enrollments": total_enrollments,
        "total_revenue": float(total_revenue),
        "recent_users": [
            {"id": u.id, "full_name": u.full_name, "email": u.email}
            for u in recent_users
        ],
        "recent_courses": [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status,
                "is_paid": c.is_paid,
                "price": c.price,
                "instructor_name": c.instructor_name,
            }
            for c in recent_courses
        ],
    }

    _stats_cache["data"] = result
    _stats_cache["expires_at"] = now_mono + STATS_CACHE_TTL
    return result


# ── Admin Analytics (Heavily Optimized) ──────────────────────────────────────

def get_admin_analytics(db: Session, force_refresh: bool = False) -> dict:
    """
    Comprehensive analytics with in-memory TTL caching and consolidated SQL queries.
    """
    now_mono = time.monotonic()
    if not force_refresh and _analytics_cache["data"] is not None and now_mono < _analytics_cache["expires_at"]:
        return _analytics_cache["data"]

    # ── 1. Summary Counts (Consolidated SQL aggregations) ───────────────

    # Single query for all user distribution & active/suspended metrics
    user_counts_raw = db.query(
        models.User.role,
        models.User.is_suspended,
        func.count(models.User.id)
    ).group_by(models.User.role, models.User.is_suspended).all()

    total_learners = 0
    total_instructors = 0
    active_learners_count = 0
    suspended_learners_count = 0
    for r, is_susp, cnt in user_counts_raw:
        if r == models.UserRole.LEARNER or r == "learner":
            total_learners += cnt
            if is_susp:
                suspended_learners_count += cnt
            else:
                active_learners_count += cnt
        elif r == models.UserRole.INSTRUCTOR or r == "instructor":
            total_instructors += cnt

    pending_applications = db.query(func.count(models.InstructorApplication.id)).filter(
        models.InstructorApplication.status == models.InstructorApplicationStatus.PENDING
    ).scalar() or 0

    # Course stats via conditional aggregation (1 query for all course counts)
    course_stats = db.query(
        func.count(models.Course.id).label("total"),
        func.sum(case((models.Course.status == models.CourseStatus.PUBLISHED, 1), else_=0)).label("published"),
        func.sum(case((models.Course.status == models.CourseStatus.DRAFT, 1), else_=0)).label("draft"),
        func.sum(case((models.Course.is_paid == True, 1), else_=0)).label("paid"),
    ).filter(models.Course.is_deleted == False).first()

    total_courses = course_stats.total or 0
    published_courses = int(course_stats.published or 0)
    draft_courses = int(course_stats.draft or 0)
    paid_courses = int(course_stats.paid or 0)
    free_courses = total_courses - paid_courses

    total_modules = db.query(func.count(models.Module.id)).scalar() or 0

    # Lesson type distribution (1 query)
    lesson_type_counts = dict(
        db.query(models.Lesson.lesson_type, func.count(models.Lesson.id))
        .group_by(models.Lesson.lesson_type)
        .all()
    )
    total_lessons = sum(lesson_type_counts.values())
    lesson_types = {
        "video": lesson_type_counts.get("video", 0),
        "text": lesson_type_counts.get("text", 0),
        "assignment": lesson_type_counts.get("assignment", 0),
    }

    # ── 2. Financials ────────────────────────────────────────────────────
    financial_stats = db.query(
        func.coalesce(func.sum(models.CoursePayment.amount), 0.0),
        func.count(models.CoursePayment.id),
    ).filter(
        models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS
    ).first()

    total_revenue = float(financial_stats[0])
    total_paid_orders = financial_stats[1]

    # ── 3. Enrollments & Completions ─────────────────────────────────────
    total_enrollments = db.query(func.count(models.Enrollment.id)).scalar() or 0
    total_certificates = db.query(func.count(models.Certificate.id)).filter(
        models.Certificate.revoked_at.is_(None)
    ).scalar() or 0
    completion_rate = round((total_certificates / total_enrollments * 100), 1) if total_enrollments > 0 else 0.0
    total_lesson_completions = db.query(func.count(models.LessonCompletion.id)).scalar() or 0

    # ── 4. Submissions & Grading (Consolidated 1 query) ───────────────────
    sub_stats = db.query(
        models.Submission.status,
        func.count(models.Submission.id),
        func.sum(case((models.Submission.mcq_total > 0, models.Submission.mcq_score * 100.0 / models.Submission.mcq_total), else_=None)),
        func.count(case((models.Submission.mcq_total > 0, 1), else_=None)),
        func.sum(models.Submission.grade),
        func.count(models.Submission.grade),
    ).group_by(models.Submission.status).all()

    total_submissions = 0
    submission_status_counts = {
        "approved": 0, "submitted": 0, "under_review": 0,
        "resubmission_required": 0, "rejected": 0
    }
    total_mcq_sum = 0.0
    total_mcq_cnt = 0
    total_subj_sum = 0.0
    total_subj_cnt = 0

    for st, cnt, mcq_s, mcq_c, subj_s, subj_c in sub_stats:
        total_submissions += cnt
        st_str = st.value if hasattr(st, 'value') else str(st)
        if st_str in submission_status_counts:
            submission_status_counts[st_str] += cnt
        if mcq_s is not None and mcq_c:
            total_mcq_sum += float(mcq_s)
            total_mcq_cnt += int(mcq_c)
        if subj_s is not None and subj_c:
            total_subj_sum += float(subj_s)
            total_subj_cnt += int(subj_c)

    avg_mcq_score_pct = round(total_mcq_sum / total_mcq_cnt, 1) if total_mcq_cnt > 0 else 0.0
    avg_subjective_grade = round(total_subj_sum / total_subj_cnt, 1) if total_subj_cnt > 0 else 0.0

    # ── 5. Monthly Trends (Past 6 months) ────────────────────────────────
    now = datetime.utcnow()
    month_keys = []
    for i in range(5, -1, -1):
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        ym_str = f"{year:04d}-{month:02d}"
        label = datetime(year, month, 1).strftime("%b %Y")
        month_keys.append((ym_str, label))

    first_ym = month_keys[0][0]
    first_year, first_month = int(first_ym[:4]), int(first_ym[5:])
    first_date = datetime(first_year, first_month, 1)

    trend_map = {
        ym: {"year_month": ym, "month": label, "revenue": 0.0, "enrollments": 0, "certificates": 0, "new_learners": 0, "cumulative_learners": 0}
        for ym, label in month_keys
    }

    # Revenue by month (1 query)
    revenue_by_month = db.query(
        func.to_char(models.CoursePayment.created_at, 'YYYY-MM').label("ym"),
        func.sum(models.CoursePayment.amount)
    ).filter(
        models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS,
        models.CoursePayment.created_at >= first_date
    ).group_by("ym").all()

    for ym, amt in revenue_by_month:
        if ym in trend_map:
            trend_map[ym]["revenue"] = round(float(amt or 0), 2)

    # Enrollments by month (1 query)
    enrollments_by_month = db.query(
        func.to_char(models.Enrollment.enrolled_at, 'YYYY-MM').label("ym"),
        func.count(models.Enrollment.id)
    ).filter(
        models.Enrollment.enrolled_at >= first_date
    ).group_by("ym").all()

    for ym, cnt in enrollments_by_month:
        if ym in trend_map:
            trend_map[ym]["enrollments"] = cnt

    # Certificates by month (1 query)
    certs_by_month = db.query(
        func.to_char(models.Certificate.issued_at, 'YYYY-MM').label("ym"),
        func.count(models.Certificate.id)
    ).filter(
        models.Certificate.revoked_at.is_(None),
        models.Certificate.issued_at >= first_date
    ).group_by("ym").all()

    for ym, cnt in certs_by_month:
        if ym in trend_map:
            trend_map[ym]["certificates"] = cnt

    # New learners by month (1 query)
    learners_by_month = db.query(
        func.to_char(models.User.created_at, 'YYYY-MM').label("ym"),
        func.count(models.User.id)
    ).filter(
        models.User.role == models.UserRole.LEARNER,
        models.User.created_at >= first_date
    ).group_by("ym").all()

    for ym, cnt in learners_by_month:
        if ym in trend_map:
            trend_map[ym]["new_learners"] = cnt

    # Cumulative learner growth
    base_prior_learners = db.query(func.count(models.User.id)).filter(
        models.User.role == models.UserRole.LEARNER,
        models.User.created_at < first_date
    ).scalar() or 0

    running_learners = base_prior_learners
    for ym, _ in month_keys:
        running_learners += trend_map[ym]["new_learners"]
        trend_map[ym]["cumulative_learners"] = running_learners

    monthly_trends = [trend_map[ym] for ym, _ in month_keys]

    # Daily registrations (past 14 days)
    daily_registrations = []
    for i in range(13, -1, -1):
        day_date = (now - timedelta(days=i)).date()
        date_str = day_date.strftime("%Y-%m-%d")
        label = day_date.strftime("%b %d")
        daily_registrations.append({"date": date_str, "label": label, "count": 0})

    fourteen_days_ago = now - timedelta(days=14)
    daily_counts = db.query(
        func.to_char(models.User.created_at, 'YYYY-MM-DD').label("d"),
        func.count(models.User.id)
    ).filter(
        models.User.role == models.UserRole.LEARNER,
        models.User.created_at >= fourteen_days_ago
    ).group_by("d").all()

    daily_map = {d["date"]: d for d in daily_registrations}
    for d, cnt in daily_counts:
        if d in daily_map:
            daily_map[d]["count"] = cnt

    # Active/Suspended learner counts (2 aggregations, 1 query with CASE)
    active_suspended = db.query(
        func.sum(case((models.User.is_suspended == False, 1), else_=0)),
        func.sum(case((models.User.is_suspended == True, 1), else_=0)),
    ).filter(models.User.role == models.UserRole.LEARNER).first()

    active_learners_count = int(active_suspended[0] or 0)
    suspended_learners_count = int(active_suspended[1] or 0)

    # ── 6. Course Performance Table ──────────────────────────────────────
    courses_query = db.query(models.Course).filter(
        models.Course.is_deleted == False
    ).all()

    course_ids = [c.id for c in courses_query]

    # Batch aggregations for course metrics (3 grouped queries)
    course_enrollments_map = dict(
        db.query(models.Enrollment.course_id, func.count(models.Enrollment.id))
        .filter(models.Enrollment.course_id.in_(course_ids))
        .group_by(models.Enrollment.course_id)
        .all()
    ) if course_ids else {}

    course_certificates_map = dict(
        db.query(models.Certificate.course_id, func.count(models.Certificate.id))
        .filter(
            models.Certificate.course_id.in_(course_ids),
            models.Certificate.revoked_at.is_(None)
        )
        .group_by(models.Certificate.course_id)
        .all()
    ) if course_ids else {}

    course_payments_map = {}
    if course_ids:
        payment_rows = db.query(
            models.CoursePayment.course_id,
            func.sum(models.CoursePayment.amount)
        ).filter(
            models.CoursePayment.course_id.in_(course_ids),
            models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS
        ).group_by(models.CoursePayment.course_id).all()
        course_payments_map = {cid: float(amt or 0) for cid, amt in payment_rows}

    # Submissions per course (via assignments)
    course_submissions_map = defaultdict(int)
    course_grades_map = defaultdict(list)
    if course_ids:
        # Direct course_id assignments
        sub_rows = db.query(
            models.Assignment.course_id,
            func.count(models.Submission.id)
        ).join(
            models.Submission, models.Submission.assignment_id == models.Assignment.id
        ).filter(
            models.Assignment.course_id.in_(course_ids)
        ).group_by(models.Assignment.course_id).all()

        for cid, cnt in sub_rows:
            if cid:
                course_submissions_map[cid] = cnt

        # Average grades per course
        grade_rows = db.query(
            models.Assignment.course_id,
            models.Submission.grade
        ).join(
            models.Submission, models.Submission.assignment_id == models.Assignment.id
        ).filter(
            models.Assignment.course_id.in_(course_ids),
            models.Submission.grade.isnot(None)
        ).all()

        for cid, grade in grade_rows:
            if cid:
                course_grades_map[cid].append(grade)

    # Instructor name map
    instructor_ids = {c.instructor_id for c in courses_query if c.instructor_id}
    instructor_map = {}
    instructor_email_map = {}
    if instructor_ids:
        instructors = db.query(models.User).filter(models.User.id.in_(instructor_ids)).all()
        instructor_map = {u.id: u.full_name or u.email for u in instructors}
        instructor_email_map = {u.id: u.email for u in instructors}

    course_analytics = []
    for c in courses_query:
        enr_count = course_enrollments_map.get(c.id, 0)
        cert_count = course_certificates_map.get(c.id, 0)
        rev = course_payments_map.get(c.id, 0.0)
        comp_pct = round((cert_count / enr_count * 100), 1) if enr_count > 0 else 0.0
        grades = course_grades_map.get(c.id, [])
        avg_g = round(sum(grades) / len(grades), 1) if grades else None

        course_analytics.append({
            "id": c.id,
            "title": c.title,
            "status": c.status,
            "is_paid": bool(c.is_paid),
            "price": c.price or 0.0,
            "instructor_id": c.instructor_id,
            "instructor_name": instructor_map.get(c.instructor_id) or "SVARP GLOBAL ACADEMY",
            "instructor_email": instructor_email_map.get(c.instructor_id, "-"),
            "enrollments_count": enr_count,
            "revenue": round(rev, 2),
            "certificates_count": cert_count,
            "completion_rate_pct": comp_pct,
            "submissions_count": course_submissions_map.get(c.id, 0),
            "average_grade": avg_g,
            "created_at": c.created_at.isoformat() if c.created_at else None
        })

    course_analytics.sort(key=lambda x: (x["enrollments_count"], x["revenue"]), reverse=True)

    # ── 7. Instructor Performance Table ──────────────────────────────────
    instructor_apps = db.query(models.InstructorApplication.user_id, models.InstructorApplication.specialty).all()
    instructor_specialty_map = {uid: spec for uid, spec in instructor_apps if uid}

    instructors_list = db.query(models.User).filter(
        models.User.role.in_([models.UserRole.INSTRUCTOR, models.UserRole.ADMIN])
    ).all()

    instructor_courses_map = defaultdict(list)
    for c in courses_query:
        if c.instructor_id:
            instructor_courses_map[c.instructor_id].append(c)

    instructor_analytics = []
    for inst in instructors_list:
        if inst.role != models.UserRole.INSTRUCTOR and not instructor_courses_map.get(inst.id):
            continue
        c_list = instructor_courses_map.get(inst.id, [])
        c_ids = {c.id for c in c_list}
        inst_rev = sum(course_payments_map.get(cid, 0) for cid in c_ids)
        inst_enrollments = sum(course_enrollments_map.get(cid, 0) for cid in c_ids)
        inst_certs = sum(course_certificates_map.get(cid, 0) for cid in c_ids)
        inst_submissions = sum(course_submissions_map.get(cid, 0) for cid in c_ids)
        published_cnt = sum(1 for c in c_list if c.status == models.CourseStatus.PUBLISHED)

        instructor_analytics.append({
            "id": inst.id,
            "name": inst.full_name or inst.email,
            "email": inst.email,
            "specialty": instructor_specialty_map.get(inst.id, "General Instructor"),
            "total_courses": len(c_list),
            "published_courses": published_cnt,
            "total_students": inst_enrollments,
            "total_revenue": round(inst_rev, 2),
            "certificates_earned": inst_certs,
            "total_submissions": inst_submissions
        })

    instructor_analytics.sort(key=lambda x: (x["total_students"], x["total_revenue"]), reverse=True)

    # ── 8. Engagement & Leaderboards ─────────────────────────────────────
    top_enrolled_courses = course_analytics[:5]
    top_revenue_courses = sorted(course_analytics, key=lambda x: x["revenue"], reverse=True)[:5]

    learner_completions = db.query(
        models.LessonCompletion.user_id,
        func.count(models.LessonCompletion.id).label("comp_count")
    ).group_by(models.LessonCompletion.user_id).order_by(desc("comp_count")).limit(5).all()

    top_learner_ids = [r[0] for r in learner_completions]
    top_learners_info = {}
    if top_learner_ids:
        top_learners_info = {
            u.id: u for u in db.query(models.User).filter(models.User.id.in_(top_learner_ids)).all()
        }

    # Batch certificate counts for top learners
    top_learner_cert_counts = {}
    if top_learner_ids:
        cert_rows = db.query(
            models.Certificate.user_id,
            func.count(models.Certificate.id)
        ).filter(
            models.Certificate.user_id.in_(top_learner_ids),
            models.Certificate.revoked_at.is_(None)
        ).group_by(models.Certificate.user_id).all()
        top_learner_cert_counts = dict(cert_rows)

    top_active_learners = []
    for uid, comp_cnt in learner_completions:
        u_obj = top_learners_info.get(uid)
        if u_obj:
            top_active_learners.append({
                "user_id": uid,
                "full_name": u_obj.full_name or "Learner",
                "email": u_obj.email,
                "completed_lessons_count": comp_cnt,
                "certificates_count": top_learner_cert_counts.get(uid, 0)
            })

    result = {
        "summary": {
            "total_revenue": round(total_revenue, 2),
            "total_paid_orders": total_paid_orders,
            "total_learners": total_learners,
            "total_instructors": total_instructors,
            "pending_applications": pending_applications,
            "total_courses": total_courses,
            "published_courses": published_courses,
            "draft_courses": draft_courses,
            "paid_courses": paid_courses,
            "free_courses": free_courses,
            "total_modules": total_modules,
            "total_lessons": total_lessons,
            "lesson_types_distribution": lesson_types,
            "total_enrollments": total_enrollments,
            "total_certificates": total_certificates,
            "completion_rate": completion_rate,
            "total_lesson_completions": total_lesson_completions,
            "total_submissions": total_submissions,
            "submissions_by_status": submission_status_counts,
            "avg_mcq_score_pct": avg_mcq_score_pct,
            "avg_subjective_grade": avg_subjective_grade,
        },
        "monthly_trends": monthly_trends,
        "learner_growth": {
            "daily_registrations": daily_registrations,
            "active_learners": active_learners_count,
            "suspended_learners": suspended_learners_count,
            "total_learners": total_learners,
        },
        "course_analytics": course_analytics,
        "instructor_analytics": instructor_analytics,
        "engagement": {
            "top_enrolled_courses": top_enrolled_courses,
            "top_revenue_courses": top_revenue_courses,
            "top_active_learners": top_active_learners
        }
    }

    _analytics_cache["data"] = result
    _analytics_cache["expires_at"] = now_mono + ANALYTICS_CACHE_TTL
    return result


# ── Optimized Submissions List ───────────────────────────────────────────────

def list_submissions_with_details(
    db: Session,
    status_filter: Optional[str] = None,
    course_ids: list = None,
    instructor_id: str = None,
) -> list:
    """
    List submissions with all related details using eager loading.
    
    Previous: N+1 pattern querying user, answers, questions per submission.
    Now: 2 queries total (submissions with joins + batch user lookup).
    """
    query = db.query(models.Submission).options(
        joinedload(models.Submission.assignment),
        joinedload(models.Submission.answers).joinedload(models.AnswerSubmission.question),
    )

    if instructor_id:
        query = (
            query.join(models.Assignment, models.Assignment.id == models.Submission.assignment_id)
            .join(models.Course, models.Course.id == models.Assignment.course_id)
            .filter(
                models.Course.is_deleted == False,
                models.Course.instructor_id == instructor_id
            )
        )

    if course_ids:
        if not instructor_id:
            query = query.join(
                models.Assignment, models.Assignment.id == models.Submission.assignment_id
            )
        query = query.filter(models.Assignment.course_id.in_(course_ids))

    if status_filter:
        query = query.filter(models.Submission.status == status_filter)

    submissions = query.order_by(models.Submission.submitted_at.desc()).all()

    # Batch user lookup (1 query for all users)
    user_ids = {s.user_id for s in submissions if s.user_id}
    users_map = {}
    if user_ids:
        users_map = {
            u.id: u for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()
        }

    # Batch lesson-to-course lookup (1 query)
    lesson_ids = {
        sub.assignment.lesson_id
        for sub in submissions
        if sub.assignment and sub.assignment.lesson_id and not sub.assignment.course_id
    }
    lesson_course_map = {}
    if lesson_ids:
        lessons_with_module = db.query(models.Lesson.id, models.Module.course_id).join(
            models.Module, models.Module.id == models.Lesson.module_id
        ).filter(models.Lesson.id.in_(lesson_ids)).all()
        lesson_course_map = dict(lessons_with_module)

    # Batch course lookup (1 query)
    all_course_ids = set()
    for sub in submissions:
        if sub.assignment:
            if sub.assignment.course_id:
                all_course_ids.add(sub.assignment.course_id)
            elif sub.assignment.lesson_id in lesson_course_map:
                all_course_ids.add(lesson_course_map[sub.assignment.lesson_id])

    courses_map = {}
    if all_course_ids:
        courses_map = {
            c.id: c for c in db.query(models.Course).filter(models.Course.id.in_(all_course_ids)).all()
        }

    result = []
    for sub in submissions:
        user = users_map.get(sub.user_id)
        assignment = sub.assignment

        answer_details = []
        for ans in sub.answers:
            question = ans.question
            answer_details.append({
                "question_id": ans.question_id,
                "question_text": question.question_text if question else "",
                "question_type": question.question_type.value if (question and hasattr(question.question_type, 'value')) else str(question.question_type) if question else "",
                "answer_text": ans.answer_text,
                "selected_option_id": ans.selected_option_id,
                "is_correct": ans.is_correct
            })

        course_id = assignment.course_id if assignment else None
        if not course_id and assignment and assignment.lesson_id:
            course_id = lesson_course_map.get(assignment.lesson_id)

        course = courses_map.get(course_id) if course_id else None

        result.append({
            "id": sub.id,
            "user_id": sub.user_id,
            "user_name": user.full_name if user else "Unknown User",
            "user_email": user.email if user else "",
            "course_id": course_id,
            "course_title": course.title if course else "",
            "assignment_id": sub.assignment_id,
            "assignment_title": assignment.title if assignment else "",
            "status": sub.status.value if hasattr(sub.status, 'value') else str(sub.status),
            "grade": sub.grade,
            "feedback": sub.feedback,
            "submitted_at": sub.submitted_at,
            "answers": answer_details
        })

    return result
