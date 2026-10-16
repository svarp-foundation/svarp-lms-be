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
from sqlalchemy import func, distinct, desc, extract, case, and_, or_
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
        "learners_analytics": get_all_learners_analytics(db),
        "engagement": {
            "top_enrolled_courses": top_enrolled_courses,
            "top_revenue_courses": top_revenue_courses,
            "top_active_learners": top_active_learners
        }
    }

    _analytics_cache["data"] = result
    _analytics_cache["expires_at"] = now_mono + ANALYTICS_CACHE_TTL
    return result


# ── All Learners Overview Analytics (Batch Aggregated) ───────────────────────

def get_all_learners_analytics(db: Session) -> list:
    """
    Get all learners with aggregated enrollment count, certificates earned, 
    lessons completed, submissions, and avg grade using batch grouped queries.
    """
    learners = db.query(models.User).filter(
        models.User.role.in_([models.UserRole.LEARNER, "learner"])
    ).order_by(models.User.created_at.desc()).all()

    if not learners:
        return []

    user_ids = [u.id for u in learners]

    # Batch 1: Enrollment counts per learner
    enrollment_counts = dict(
        db.query(models.Enrollment.user_id, func.count(models.Enrollment.id))
        .filter(models.Enrollment.user_id.in_(user_ids))
        .group_by(models.Enrollment.user_id)
        .all()
    )

    # Batch 2: Certificate counts per learner
    certificate_counts = dict(
        db.query(models.Certificate.user_id, func.count(models.Certificate.id))
        .filter(
            models.Certificate.user_id.in_(user_ids),
            models.Certificate.revoked_at.is_(None)
        )
        .group_by(models.Certificate.user_id)
        .all()
    )

    # Batch 3: Lesson completion counts per learner
    lesson_comp_counts = dict(
        db.query(models.LessonCompletion.user_id, func.count(distinct(models.LessonCompletion.lesson_id)))
        .filter(models.LessonCompletion.user_id.in_(user_ids))
        .group_by(models.LessonCompletion.user_id)
        .all()
    )

    # Batch 4: Submissions count and average grade per learner
    sub_stats = db.query(
        models.Submission.id,
        models.Submission.user_id,
        models.Submission.assignment_id,
        models.Submission.grade,
        models.Submission.mcq_score,
        models.Submission.mcq_total
    ).filter(
        models.Submission.user_id.in_(user_ids)
    ).all()

    user_completed_aids = defaultdict(set)
    user_explicit_sub_aids = defaultdict(set)
    user_grades = defaultdict(list)
    user_q_stats = defaultdict(lambda: {"total_q": 0, "attempted": 0, "correct": 0, "incorrect": 0})

    for sid, uid, aid, grade, mcq_s, mcq_t in sub_stats:
        user_completed_aids[uid].add(aid)
        user_explicit_sub_aids[uid].add(aid)
        if grade is not None:
            user_grades[uid].append(grade)
        if mcq_t and mcq_t > 0:
            corr = mcq_s if mcq_s is not None else (round((grade / 100.0) * mcq_t) if grade is not None else 0)
            user_q_stats[uid]["total_q"] += mcq_t
            user_q_stats[uid]["attempted"] += mcq_t
            user_q_stats[uid]["correct"] += corr
            user_q_stats[uid]["incorrect"] += max(0, mcq_t - corr)

    # Also credit assignments whose linked lessons are completed by the user
    completed_quiz_lessons = (
        db.query(models.LessonCompletion.user_id, models.Assignment.id)
        .join(models.Lesson, models.Lesson.id == models.LessonCompletion.lesson_id)
        .join(models.Assignment, models.Assignment.lesson_id == models.Lesson.id)
        .filter(models.LessonCompletion.user_id.in_(user_ids))
        .all()
    )
    all_asgn_ids = list(set([aid for _, aid in completed_quiz_lessons]))
    asgn_q_counts = dict(
        db.query(models.Question.assignment_id, func.count(models.Question.id))
        .filter(models.Question.assignment_id.in_(all_asgn_ids))
        .group_by(models.Question.assignment_id)
        .all()
    ) if all_asgn_ids else {}

    for uid, aid in completed_quiz_lessons:
        user_completed_aids[uid].add(aid)
        if aid not in user_explicit_sub_aids[uid]:
            user_explicit_sub_aids[uid].add(aid)
            q_cnt = asgn_q_counts.get(aid, 0)
            user_grades[uid].append(100.0)
            user_q_stats[uid]["total_q"] += q_cnt
            user_q_stats[uid]["attempted"] += q_cnt
            user_q_stats[uid]["correct"] += q_cnt
            user_q_stats[uid]["incorrect"] += 0

    sub_counts = {uid: len(aids) for uid, aids in user_completed_aids.items()}
    avg_grades = {uid: round(float(sum(grades) / len(grades)), 1) if grades else None for uid, grades in user_grades.items()}

    result = []
    for u in learners:
        enr_cnt = enrollment_counts.get(u.id, 0)
        cert_cnt = certificate_counts.get(u.id, 0)
        comp_lessons = lesson_comp_counts.get(u.id, 0)
        sub_cnt = sub_counts.get(u.id, 0)
        avg_g = avg_grades.get(u.id)
        qs = user_q_stats[u.id]
        acc = round((qs["correct"] / qs["attempted"]) * 100.0, 1) if qs["attempted"] > 0 else (avg_g or 0.0)

        result.append({
            "user_id": u.id,
            "full_name": u.full_name or "Learner",
            "email": u.email,
            "role": u.role,
            "is_active": u.is_active,
            "is_suspended": u.is_suspended,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "enrolled_courses_count": enr_cnt,
            "certificates_count": cert_cnt,
            "completed_lessons_count": comp_lessons,
            "submissions_count": sub_cnt,
            "average_grade": avg_g,
            "total_questions": qs["total_q"],
            "questions_attempted": qs["attempted"],
            "total_correct": qs["correct"],
            "total_incorrect": qs["incorrect"],
            "accuracy_pct": acc
        })

    return result


# ── Course-Level Learners Analytics Drill-Down ───────────────────────────────

def get_course_learners_analytics(db: Session, course_id: int) -> Optional[dict]:
    """
    Get detailed analytics for a particular course including all enrolled learners,
    dynamic progress %, completed lessons count, submission scores, certificate status,
    and payment info. Zero N+1 queries.
    """
    course = db.query(models.Course).options(
        joinedload(models.Course.instructor)
    ).filter(models.Course.id == course_id, models.Course.is_deleted == False).first()

    if not course:
        return None

    # Total modules and lessons in this course
    total_modules = db.query(func.count(models.Module.id)).filter(models.Module.course_id == course_id).scalar() or 0
    total_lessons = (
        db.query(func.count(models.Lesson.id))
        .join(models.Module, models.Module.id == models.Lesson.module_id)
        .filter(models.Module.course_id == course_id)
        .scalar() or 0
    )

    # Fetch all enrollments for this course
    enrollments = (
        db.query(models.Enrollment)
        .options(joinedload(models.Enrollment.user))
        .filter(models.Enrollment.course_id == course_id)
        .order_by(models.Enrollment.enrolled_at.desc())
        .all()
    )

    user_ids = [e.user_id for e in enrollments if e.user_id]

    user_done_lessons = defaultdict(set)
    user_last_activity = {}
    cert_map = {}
    user_submissions_stat = {}
    pay_map = {}

    if user_ids:
        # Batch 1: Completed lesson IDs per user
        comp_rows = (
            db.query(models.LessonCompletion.user_id, models.LessonCompletion.lesson_id, models.LessonCompletion.completed_at)
            .join(models.Lesson, models.Lesson.id == models.LessonCompletion.lesson_id)
            .join(models.Module, models.Module.id == models.Lesson.module_id)
            .filter(models.LessonCompletion.user_id.in_(user_ids), models.Module.course_id == course_id)
            .all()
        )
        user_lesson_comp_time = {}
        for uid, lid, comp_at in comp_rows:
            user_done_lessons[uid].add(lid)
            if comp_at:
                user_lesson_comp_time[(uid, lid)] = comp_at
                if uid not in user_last_activity or comp_at > user_last_activity[uid]:
                    user_last_activity[uid] = comp_at

        # Batch 2: Submitted assignment lesson IDs per user
        sub_asgn_rows = (
            db.query(models.Submission.user_id, models.Assignment.lesson_id, models.Submission.submitted_at)
            .join(models.Assignment, models.Submission.assignment_id == models.Assignment.id)
            .join(models.Lesson, models.Lesson.id == models.Assignment.lesson_id)
            .join(models.Module, models.Module.id == models.Lesson.module_id)
            .filter(
                models.Submission.user_id.in_(user_ids),
                models.Module.course_id == course_id,
                models.Submission.status.in_([
                    models.SubmissionStatus.SUBMITTED,
                    models.SubmissionStatus.UNDER_REVIEW,
                    models.SubmissionStatus.APPROVED
                ]),
                models.Assignment.lesson_id != None
            )
            .all()
        )
        for uid, lid, sub_at in sub_asgn_rows:
            user_done_lessons[uid].add(lid)
            if sub_at:
                if uid not in user_last_activity or sub_at > user_last_activity[uid]:
                    user_last_activity[uid] = sub_at

        # Batch 3: Certificates for this course
        certs = (
            db.query(models.Certificate)
            .filter(
                models.Certificate.course_id == course_id,
                models.Certificate.user_id.in_(user_ids),
                models.Certificate.revoked_at.is_(None)
            )
            .all()
        )
        cert_map = {c.user_id: c for c in certs}

        # Batch 4: Submissions and test results in this course
        course_assignments = (
            db.query(models.Assignment.id, models.Assignment.lesson_id, models.Assignment.title)
            .outerjoin(models.Lesson, models.Assignment.lesson_id == models.Lesson.id)
            .outerjoin(models.Module, models.Lesson.module_id == models.Module.id)
            .filter(
                or_(
                    models.Assignment.course_id == course_id,
                    models.Module.course_id == course_id
                )
            )
            .all()
        )
        assignment_ids = [a[0] for a in course_assignments]
        assignment_lesson_map = {a[0]: a[1] for a in course_assignments if a[1]}
        assignment_title_map = {a[0]: a[2] or f"Assessment #{a[0]}" for a in course_assignments}

        assignment_question_counts = dict(
            db.query(models.Question.assignment_id, func.count(models.Question.id))
            .filter(models.Question.assignment_id.in_(assignment_ids))
            .group_by(models.Question.assignment_id)
            .all()
        ) if assignment_ids else {}

        user_subs_by_aid = defaultdict(dict)
        if assignment_ids and user_ids:
            sub_rows = (
                db.query(
                    models.Submission.id,
                    models.Submission.user_id,
                    models.Submission.assignment_id,
                    models.Submission.grade,
                    models.Submission.mcq_score,
                    models.Submission.mcq_total,
                    models.Submission.status,
                    models.Submission.submitted_at
                )
                .filter(
                    models.Submission.user_id.in_(user_ids),
                    models.Submission.assignment_id.in_(assignment_ids)
                )
                .all()
            )

            sub_ids = [s[0] for s in sub_rows]
            sub_answer_stats = {}
            if sub_ids:
                ans_rows = (
                    db.query(
                        models.AnswerSubmission.submission_id,
                        func.count(models.AnswerSubmission.id).label("total_answers"),
                        func.sum(case((models.AnswerSubmission.is_correct == True, 1), else_=0)).label("correct"),
                        func.sum(case((models.AnswerSubmission.is_correct == False, 1), else_=0)).label("incorrect"),
                        func.sum(case((
                            or_(
                                models.AnswerSubmission.selected_option_id.isnot(None),
                                and_(models.AnswerSubmission.answer_text.isnot(None), models.AnswerSubmission.answer_text != "")
                            ), 1
                        ), else_=0)).label("attempted")
                    )
                    .filter(models.AnswerSubmission.submission_id.in_(sub_ids))
                    .group_by(models.AnswerSubmission.submission_id)
                    .all()
                )
                for sid, tot_ans, corr, incorr, att in ans_rows:
                    sub_answer_stats[sid] = {
                        "total_answers": tot_ans or 0,
                        "correct": corr or 0,
                        "incorrect": incorr or 0,
                        "attempted": att or 0
                    }

            for sid, uid, aid, grade, mcq_s, mcq_t, stat, max_at in sub_rows:
                q_count = assignment_question_counts.get(aid, 0)
                ans_stat = sub_answer_stats.get(sid)
                
                if ans_stat and ans_stat["total_answers"] > 0:
                    tot_q = max(q_count, ans_stat["total_answers"], mcq_t or 0)
                    corr = ans_stat["correct"]
                    incorr = ans_stat["incorrect"]
                    att = ans_stat["attempted"]
                elif mcq_t and mcq_t > 0:
                    tot_q = mcq_t
                    corr = mcq_s if mcq_s is not None else (round((grade / 100.0) * tot_q) if grade is not None else 0)
                    incorr = max(0, tot_q - corr)
                    att = tot_q
                elif q_count > 0 and grade is not None:
                    tot_q = q_count
                    corr = round((grade / 100.0) * tot_q)
                    incorr = max(0, tot_q - corr)
                    att = tot_q
                else:
                    tot_q = q_count
                    corr = mcq_s or 0
                    incorr = 0
                    att = 0

                unatt = max(0, tot_q - att)
                calc_score = float(grade) if grade is not None else (round((corr / tot_q) * 100.0, 1) if tot_q > 0 else None)

                user_subs_by_aid[uid][aid] = {
                    "submission_id": sid,
                    "assignment_id": aid,
                    "title": assignment_title_map.get(aid, f"Test #{aid}"),
                    "total_questions": tot_q,
                    "attempted": att,
                    "correct": corr,
                    "incorrect": incorr,
                    "unattempted": unatt,
                    "grade": calc_score,
                    "status": stat,
                    "submitted_at": max_at.isoformat() if max_at else None
                }
                if max_at:
                    if uid not in user_last_activity or max_at > user_last_activity[uid]:
                        user_last_activity[uid] = max_at

        for uid in user_ids:
            user_done = user_done_lessons.get(uid, set())
            completed_aid_set = set()
            grades_list = []
            test_details = []
            tot_q_sum = 0
            att_sum = 0
            corr_sum = 0
            incorr_sum = 0
            unatt_sum = 0

            # 1. Any explicit submission record
            for aid, sinfo in user_subs_by_aid.get(uid, {}).items():
                completed_aid_set.add(aid)
                if sinfo["grade"] is not None:
                    grades_list.append(sinfo["grade"])
                tot_q_sum += sinfo["total_questions"]
                att_sum += sinfo["attempted"]
                corr_sum += sinfo["correct"]
                incorr_sum += sinfo["incorrect"]
                unatt_sum += sinfo["unattempted"]

            # 2. Any assignment whose linked lesson was completed (e.g. interactive quizzes)
            for aid, lid in assignment_lesson_map.items():
                if lid in user_done:
                    completed_aid_set.add(aid)
                    if aid not in user_subs_by_aid.get(uid, {}):
                        q_count = assignment_question_counts.get(aid, 0)
                        comp_time = user_lesson_comp_time.get((uid, lid)) or user_last_activity.get(uid)
                        synthetic_sinfo = {
                            "submission_id": None,
                            "assignment_id": aid,
                            "title": assignment_title_map.get(aid, f"Assessment #{aid}"),
                            "total_questions": q_count,
                            "attempted": q_count,
                            "correct": q_count,
                            "incorrect": 0,
                            "unattempted": 0,
                            "grade": 100.0,
                            "status": "approved",
                            "submitted_at": comp_time.isoformat() if comp_time else None
                        }
                        user_subs_by_aid[uid][aid] = synthetic_sinfo
                        grades_list.append(100.0)
                        tot_q_sum += q_count
                        att_sum += q_count
                        corr_sum += q_count

            # 3. Construct test_details in consistent assignment order for all assignments in course
            for a in course_assignments:
                aid = a[0]
                if aid in user_subs_by_aid.get(uid, {}):
                    test_details.append(user_subs_by_aid[uid][aid])
                else:
                    q_count = assignment_question_counts.get(aid, 0)
                    test_details.append({
                        "submission_id": None,
                        "assignment_id": aid,
                        "title": assignment_title_map.get(aid, f"Assessment #{aid}"),
                        "total_questions": q_count,
                        "attempted": 0,
                        "correct": 0,
                        "incorrect": 0,
                        "unattempted": q_count,
                        "grade": None,
                        "status": "not_attempted",
                        "submitted_at": None
                    })

            avg_g = round(float(sum(grades_list) / len(grades_list)), 1) if grades_list else None
            accuracy = round((corr_sum / att_sum) * 100.0, 1) if att_sum > 0 else (avg_g if avg_g is not None else 0.0)

            user_submissions_stat[uid] = {
                "count": len(completed_aid_set),
                "avg_grade": avg_g,
                "total_questions": tot_q_sum,
                "attempted": att_sum,
                "correct": corr_sum,
                "incorrect": incorr_sum,
                "unattempted": unatt_sum,
                "accuracy_pct": accuracy,
                "tests_breakdown": test_details
            }

        # Batch 5: Payments for this course
        payments = (
            db.query(models.CoursePayment)
            .filter(
                models.CoursePayment.course_id == course_id,
                models.CoursePayment.user_id.in_(user_ids),
                models.CoursePayment.status == models.CoursePaymentStatus.SUCCESS
            )
            .all()
        )
        pay_map = {p.user_id: p for p in payments}

    learners_list = []
    completed_count = 0
    in_progress_count = 0
    not_started_count = 0
    total_progress_sum = 0

    for e in enrollments:
        u = e.user
        uid = e.user_id
        done_set = user_done_lessons.get(uid, set())
        done_count = len(done_set)
        progress_pct = min(100, int((done_count / total_lessons) * 100)) if total_lessons > 0 else 0
        cert = cert_map.get(uid)

        # If certificate earned, treat as 100% completed
        if cert and progress_pct < 100:
            progress_pct = 100

        if progress_pct == 100 or cert:
            progress_status = "completed"
            completed_count += 1
        elif progress_pct > 0:
            progress_status = "in_progress"
            in_progress_count += 1
        else:
            progress_status = "not_started"
            not_started_count += 1

        total_progress_sum += progress_pct

        sub_info = user_submissions_stat.get(uid, {
            "count": 0,
            "avg_grade": None,
            "total_questions": 0,
            "attempted": 0,
            "correct": 0,
            "incorrect": 0,
            "unattempted": 0,
            "accuracy_pct": 0.0,
            "tests_breakdown": []
        })
        pay = pay_map.get(uid)
        last_act = user_last_activity.get(uid) or e.enrolled_at

        learners_list.append({
            "user_id": uid,
            "full_name": u.full_name if u else "Learner",
            "email": u.email if u else "N/A",
            "role": u.role if u else "learner",
            "is_suspended": u.is_suspended if u else False,
            "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
            "progress_pct": progress_pct,
            "completed_lessons_count": done_count,
            "total_lessons_count": total_lessons,
            "progress_status": progress_status,
            "certificate": {
                "id": cert.id,
                "code": cert.certificate_code,
                "issued_at": cert.issued_at.isoformat() if cert.issued_at else None,
                "pdf_url": cert.pdf_url
            } if cert else None,
            "submissions_count": sub_info.get("count", 0),
            "total_assignments_count": len(assignment_ids),
            "average_grade": sub_info.get("avg_grade"),
            "total_questions": sub_info.get("total_questions", 0),
            "questions_attempted": sub_info.get("attempted", 0),
            "total_correct": sub_info.get("correct", 0),
            "total_incorrect": sub_info.get("incorrect", 0),
            "total_unattempted": sub_info.get("unattempted", 0),
            "accuracy_pct": sub_info.get("accuracy_pct", 0.0),
            "tests_breakdown": sub_info.get("tests_breakdown", []),
            "payment_status": "paid" if pay else ("free" if not course.is_paid else "pending"),
            "amount_paid": pay.amount if pay else 0.0,
            "last_activity_at": last_act.isoformat() if last_act else None
        })

    total_enrolled = len(enrollments)
    avg_prog = round(total_progress_sum / total_enrolled, 1) if total_enrolled > 0 else 0.0
    total_rev = sum(p.amount for p in pay_map.values()) if pay_map else 0.0

    return {
        "course": {
            "id": course.id,
            "title": course.title,
            "description": course.description,
            "status": course.status,
            "is_paid": bool(course.is_paid),
            "price": course.price or 0.0,
            "passing_score": course.passing_score,
            "instructor_id": course.instructor_id,
            "instructor_name": course.instructor_name,
            "instructor_email": course.instructor.email if course.instructor else "-",
            "total_modules": total_modules,
            "total_lessons": total_lessons,
            "total_assignments": len(course_assignments),
            "assignments": [
                {
                    "id": a[0],
                    "title": assignment_title_map.get(a[0], f"Assessment #{a[0]}"),
                    "total_questions": assignment_question_counts.get(a[0], 0),
                    "lesson_id": a[1]
                }
                for a in course_assignments
            ],
            "created_at": course.created_at.isoformat() if course.created_at else None
        },
        "summary": {
            "total_enrolled": total_enrolled,
            "completed_count": completed_count,
            "in_progress_count": in_progress_count,
            "not_started_count": not_started_count,
            "avg_progress_pct": avg_prog,
            "certificates_issued": len(cert_map),
            "total_revenue": round(total_rev, 2),
            "total_questions_attempted": sum(l["questions_attempted"] for l in learners_list),
            "total_correct_answers": sum(l["total_correct"] for l in learners_list),
            "total_incorrect_answers": sum(l["total_incorrect"] for l in learners_list),
            "overall_accuracy_pct": round(
                (sum(l["total_correct"] for l in learners_list) / sum(l["questions_attempted"] for l in learners_list)) * 100.0, 1
            ) if sum(l["questions_attempted"] for l in learners_list) > 0 else 0.0
        },
        "learners": learners_list
    }


# ── Learner-Level Courses Analytics Drill-Down ───────────────────────────────

def get_learner_courses_analytics(db: Session, user_id: str) -> Optional[dict]:
    """
    Get detailed analytics for a particular learner including all enrolled courses,
    lesson progress, quiz/assignment submissions, certificate codes, and completion status.
    Zero N+1 queries.
    """
    user = db.query(models.User).filter(models.User.id == str(user_id)).first()
    if not user:
        # Try matching by email
        user = db.query(models.User).filter(models.User.email == str(user_id)).first()
    if not user:
        return None

    # Fetch all enrollments with joined courses
    enrollments = (
        db.query(models.Enrollment)
        .options(
            joinedload(models.Enrollment.course).joinedload(models.Course.instructor)
        )
        .filter(models.Enrollment.user_id == user.id)
        .order_by(models.Enrollment.enrolled_at.desc())
        .all()
    )

    valid_courses = [e.course for e in enrollments if e.course and not e.course.is_deleted]
    course_ids = [c.id for c in valid_courses]

    total_lessons_map = {}
    total_modules_map = {}
    course_done_lessons = defaultdict(set)
    course_last_activity = {}
    cert_map = {}
    course_sub_stats = defaultdict(lambda: {"count": 0, "grades": []})

    if course_ids:
        # Batch 1: Total lessons per course
        total_lessons_map = dict(
            db.query(models.Module.course_id, func.count(models.Lesson.id))
            .join(models.Lesson, models.Lesson.module_id == models.Module.id)
            .filter(models.Module.course_id.in_(course_ids))
            .group_by(models.Module.course_id)
            .all()
        )

        # Batch 2: Total modules per course
        total_modules_map = dict(
            db.query(models.Module.course_id, func.count(models.Module.id))
            .filter(models.Module.course_id.in_(course_ids))
            .group_by(models.Module.course_id)
            .all()
        )

        # Batch 3: Completed lessons for this learner
        comp_rows = (
            db.query(models.Module.course_id, models.LessonCompletion.lesson_id, models.LessonCompletion.completed_at)
            .join(models.Lesson, models.Lesson.id == models.LessonCompletion.lesson_id)
            .join(models.Module, models.Module.id == models.Lesson.module_id)
            .filter(models.LessonCompletion.user_id == user.id, models.Module.course_id.in_(course_ids))
            .all()
        )
        course_lesson_comp_time = {}
        for cid, lid, comp_at in comp_rows:
            course_done_lessons[cid].add(lid)
            if comp_at:
                course_lesson_comp_time[(cid, lid)] = comp_at
                if cid not in course_last_activity or comp_at > course_last_activity[cid]:
                    course_last_activity[cid] = comp_at

        # Batch 4: Submitted assignment lessons for this learner
        sub_asgn_rows = (
            db.query(models.Module.course_id, models.Assignment.lesson_id, models.Submission.submitted_at)
            .join(models.Assignment, models.Submission.assignment_id == models.Assignment.id)
            .join(models.Lesson, models.Lesson.id == models.Assignment.lesson_id)
            .join(models.Module, models.Module.id == models.Lesson.module_id)
            .filter(
                models.Submission.user_id == user.id,
                models.Module.course_id.in_(course_ids),
                models.Submission.status.in_([
                    models.SubmissionStatus.SUBMITTED,
                    models.SubmissionStatus.UNDER_REVIEW,
                    models.SubmissionStatus.APPROVED
                ]),
                models.Assignment.lesson_id != None
            )
            .all()
        )
        for cid, lid, sub_at in sub_asgn_rows:
            course_done_lessons[cid].add(lid)
            if sub_at:
                if cid not in course_last_activity or sub_at > course_last_activity[cid]:
                    course_last_activity[cid] = sub_at

        # Batch 5: Certificates for this learner
        certs = (
            db.query(models.Certificate)
            .filter(
                models.Certificate.user_id == user.id,
                models.Certificate.course_id.in_(course_ids),
                models.Certificate.revoked_at.is_(None)
            )
            .all()
        )
        cert_map = {c.course_id: c for c in certs}

        # Batch 6: Submissions & detailed test results in these courses
        asgns = (
            db.query(
                models.Assignment.id,
                models.Assignment.course_id,
                models.Module.course_id.label("mod_cid"),
                models.Assignment.lesson_id,
                models.Assignment.title
            )
            .outerjoin(models.Lesson, models.Assignment.lesson_id == models.Lesson.id)
            .outerjoin(models.Module, models.Lesson.module_id == models.Module.id)
            .filter(
                or_(
                    models.Assignment.course_id.in_(course_ids),
                    models.Module.course_id.in_(course_ids)
                )
            )
            .all()
        )
        asgn_to_course = {}
        total_assignments_map = {}
        asgn_lesson_map = {}
        asgn_title_map = {}
        for aid, acid, mcid, lid, atitle in asgns:
            cid = acid or mcid
            asgn_to_course[aid] = cid
            asgn_title_map[aid] = atitle or f"Assessment #{aid}"
            if cid:
                total_assignments_map[cid] = total_assignments_map.get(cid, 0) + 1
                if lid:
                    asgn_lesson_map[aid] = (cid, lid)

        all_asgn_ids = list(asgn_to_course.keys())
        asgn_question_counts = dict(
            db.query(models.Question.assignment_id, func.count(models.Question.id))
            .filter(models.Question.assignment_id.in_(all_asgn_ids))
            .group_by(models.Question.assignment_id)
            .all()
        ) if all_asgn_ids else {}

        course_completed_aids = defaultdict(set)
        course_subs_by_aid = defaultdict(dict)

        if all_asgn_ids:
            subs = (
                db.query(
                    models.Submission.id,
                    models.Submission.assignment_id,
                    models.Submission.grade,
                    models.Submission.mcq_score,
                    models.Submission.mcq_total,
                    models.Submission.status,
                    models.Submission.submitted_at
                )
                .filter(
                    models.Submission.user_id == user.id,
                    models.Submission.assignment_id.in_(all_asgn_ids)
                )
                .all()
            )

            sub_ids = [s[0] for s in subs]
            sub_answer_stats = {}
            if sub_ids:
                ans_rows = (
                    db.query(
                        models.AnswerSubmission.submission_id,
                        func.count(models.AnswerSubmission.id).label("total_answers"),
                        func.sum(case((models.AnswerSubmission.is_correct == True, 1), else_=0)).label("correct"),
                        func.sum(case((models.AnswerSubmission.is_correct == False, 1), else_=0)).label("incorrect"),
                        func.sum(case((
                            or_(
                                models.AnswerSubmission.selected_option_id.isnot(None),
                                and_(models.AnswerSubmission.answer_text.isnot(None), models.AnswerSubmission.answer_text != "")
                            ), 1
                        ), else_=0)).label("attempted")
                    )
                    .filter(models.AnswerSubmission.submission_id.in_(sub_ids))
                    .group_by(models.AnswerSubmission.submission_id)
                    .all()
                )
                for sid, tot_ans, corr, incorr, att in ans_rows:
                    sub_answer_stats[sid] = {
                        "total_answers": tot_ans or 0,
                        "correct": corr or 0,
                        "incorrect": incorr or 0,
                        "attempted": att or 0
                    }

            for sid, aid, g, mcq_s, mcq_t, stat, sub_at in subs:
                cid = asgn_to_course.get(aid)
                if cid:
                    course_completed_aids[cid].add(aid)
                    q_count = asgn_question_counts.get(aid, 0)
                    ans_stat = sub_answer_stats.get(sid)

                    if ans_stat and ans_stat["total_answers"] > 0:
                        tot_q = max(q_count, ans_stat["total_answers"], mcq_t or 0)
                        corr = ans_stat["correct"]
                        incorr = ans_stat["incorrect"]
                        att = ans_stat["attempted"]
                    elif mcq_t and mcq_t > 0:
                        tot_q = mcq_t
                        corr = mcq_s if mcq_s is not None else (round((g / 100.0) * tot_q) if g is not None else 0)
                        incorr = max(0, tot_q - corr)
                        att = tot_q
                    elif q_count > 0 and g is not None:
                        tot_q = q_count
                        corr = round((g / 100.0) * tot_q)
                        incorr = max(0, tot_q - corr)
                        att = tot_q
                    else:
                        tot_q = q_count
                        corr = mcq_s or 0
                        incorr = 0
                        att = 0

                    unatt = max(0, tot_q - att)
                    calc_score = float(g) if g is not None else (round((corr / tot_q) * 100.0, 1) if tot_q > 0 else None)

                    course_subs_by_aid[cid][aid] = {
                        "submission_id": sid,
                        "assignment_id": aid,
                        "title": asgn_title_map.get(aid, f"Test #{aid}"),
                        "total_questions": tot_q,
                        "attempted": att,
                        "correct": corr,
                        "incorrect": incorr,
                        "unattempted": unatt,
                        "grade": calc_score,
                        "status": stat,
                        "submitted_at": sub_at.isoformat() if sub_at else None
                    }

                    if sub_at:
                        if cid not in course_last_activity or sub_at > course_last_activity[cid]:
                            course_last_activity[cid] = sub_at

        # Also credit assignments whose linked lessons were completed (e.g. interactive quizzes)
        for aid, (cid, lid) in asgn_lesson_map.items():
            if lid in course_done_lessons.get(cid, set()):
                course_completed_aids[cid].add(aid)
                if aid not in course_subs_by_aid.get(cid, {}):
                    q_count = asgn_question_counts.get(aid, 0)
                    comp_time = course_lesson_comp_time.get((cid, lid)) or course_last_activity.get(cid)
                    course_subs_by_aid[cid][aid] = {
                        "submission_id": None,
                        "assignment_id": aid,
                        "title": asgn_title_map.get(aid, f"Test #{aid}"),
                        "total_questions": q_count,
                        "attempted": q_count,
                        "correct": q_count,
                        "incorrect": 0,
                        "unattempted": 0,
                        "grade": 100.0,
                        "status": "approved",
                        "submitted_at": comp_time.isoformat() if comp_time else None
                    }

        course_asgns_list = defaultdict(list)
        for aid, acid, mcid, lid, atitle in asgns:
            cid = acid or mcid
            if cid:
                course_asgns_list[cid].append((aid, atitle))

        for cid in course_ids:
            sub_dict = course_subs_by_aid.get(cid, {})
            grades_list = [s["grade"] for s in sub_dict.values() if s["grade"] is not None]
            tot_q_sum = sum(s["total_questions"] for s in sub_dict.values())
            att_sum = sum(s["attempted"] for s in sub_dict.values())
            corr_sum = sum(s["correct"] for s in sub_dict.values())
            incorr_sum = sum(s["incorrect"] for s in sub_dict.values())
            unatt_sum = sum(s["unattempted"] for s in sub_dict.values())
            accuracy = round((corr_sum / att_sum) * 100.0, 1) if att_sum > 0 else (
                round(float(sum(grades_list) / len(grades_list)), 1) if grades_list else 0.0
            )

            tests_list = []
            for aid, atitle in course_asgns_list.get(cid, []):
                if aid in sub_dict:
                    tests_list.append(sub_dict[aid])
                else:
                    q_count = asgn_question_counts.get(aid, 0)
                    tests_list.append({
                        "submission_id": None,
                        "assignment_id": aid,
                        "title": asgn_title_map.get(aid, f"Assessment #{aid}"),
                        "total_questions": q_count,
                        "attempted": 0,
                        "correct": 0,
                        "incorrect": 0,
                        "unattempted": q_count,
                        "grade": None,
                        "status": "not_attempted",
                        "submitted_at": None
                    })

            course_sub_stats[cid] = {
                "count": len(course_completed_aids.get(cid, set())),
                "grades": grades_list,
                "total_questions": tot_q_sum,
                "attempted": att_sum,
                "correct": corr_sum,
                "incorrect": incorr_sum,
                "unattempted": unatt_sum,
                "accuracy_pct": accuracy,
                "tests_breakdown": tests_list
            }

    courses_list = []
    completed_count = 0
    in_progress_count = 0
    not_started_count = 0
    total_completed_lessons = 0
    all_grades = []

    for e in enrollments:
        c = e.course
        if not c or c.is_deleted:
            continue
        cid = c.id
        tot_les = total_lessons_map.get(cid, 0)
        done_set = course_done_lessons.get(cid, set())
        done_cnt = len(done_set)
        total_completed_lessons += done_cnt

        prog_pct = min(100, int((done_cnt / tot_les) * 100)) if tot_les > 0 else 0
        cert = cert_map.get(cid)
        if cert and prog_pct < 100:
            prog_pct = 100

        if prog_pct == 100 or cert:
            prog_status = "completed"
            completed_count += 1
        elif prog_pct > 0:
            prog_status = "in_progress"
            in_progress_count += 1
        else:
            prog_status = "not_started"
            not_started_count += 1

        sub_data = course_sub_stats.get(cid, {
            "count": 0,
            "grades": [],
            "total_questions": 0,
            "attempted": 0,
            "correct": 0,
            "incorrect": 0,
            "unattempted": 0,
            "accuracy_pct": 0.0,
            "tests_breakdown": []
        })
        grades_list = sub_data.get("grades", [])
        all_grades.extend(grades_list)
        avg_g = round(sum(grades_list) / len(grades_list), 1) if grades_list else None

        last_act = course_last_activity.get(cid) or e.enrolled_at

        courses_list.append({
            "course_id": cid,
            "title": c.title,
            "description": c.description,
            "status": c.status,
            "is_paid": bool(c.is_paid),
            "price": c.price or 0.0,
            "instructor_name": c.instructor_name,
            "instructor_email": c.instructor.email if c.instructor else None,
            "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
            "progress_pct": prog_pct,
            "completed_lessons_count": done_cnt,
            "total_lessons_count": tot_les,
            "total_modules_count": total_modules_map.get(cid, 0),
            "progress_status": prog_status,
            "certificate": {
                "id": cert.id,
                "code": cert.certificate_code,
                "issued_at": cert.issued_at.isoformat() if cert.issued_at else None,
                "pdf_url": cert.pdf_url
            } if cert else None,
            "submissions_count": sub_data.get("count", 0),
            "total_assignments_count": total_assignments_map.get(cid, 0),
            "average_grade": avg_g,
            "total_questions": sub_data.get("total_questions", 0),
            "questions_attempted": sub_data.get("attempted", 0),
            "total_correct": sub_data.get("correct", 0),
            "total_incorrect": sub_data.get("incorrect", 0),
            "total_unattempted": sub_data.get("unattempted", 0),
            "accuracy_pct": sub_data.get("accuracy_pct", 0.0),
            "tests_breakdown": sub_data.get("tests_breakdown", []),
            "last_activity_at": last_act.isoformat() if last_act else None
        })

    user_avg_grade = round(sum(all_grades) / len(all_grades), 1) if all_grades else None

    return {
        "user": {
            "id": user.id,
            "full_name": user.full_name or "Learner",
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "is_suspended": user.is_suspended,
            "created_at": user.created_at.isoformat() if user.created_at else None
        },
        "summary": {
            "total_enrolled_courses": len(courses_list),
            "completed_courses_count": completed_count,
            "in_progress_courses_count": in_progress_count,
            "not_started_courses_count": not_started_count,
            "total_certificates": len(cert_map),
            "total_completed_lessons": total_completed_lessons,
            "total_submissions": sum(c["submissions_count"] for c in courses_list),
            "average_grade": user_avg_grade,
            "total_questions_attempted": sum(c["questions_attempted"] for c in courses_list),
            "total_correct_answers": sum(c["total_correct"] for c in courses_list),
            "total_incorrect_answers": sum(c["total_incorrect"] for c in courses_list),
            "overall_accuracy_pct": round(
                (sum(c["total_correct"] for c in courses_list) / sum(c["questions_attempted"] for c in courses_list)) * 100.0, 1
            ) if sum(c["questions_attempted"] for c in courses_list) > 0 else (user_avg_grade or 0.0)
        },
        "courses": courses_list
    }

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


def generate_sample_certificate(
    course_title: str,
    student_name: str = "John Doe",
    signatory_name: str = "Mr. Vikash Kumar",
    progress: int = 100,
    include_syllabus: bool = False,
    frontend_url: str = "http://localhost:5173",
) -> bytes:
    """
    Generate an in-memory sample certificate PDF directly using certificate_generator.
    Bypasses enrollment, progress, and payment database checks.
    """
    from .. import certificate_generator

    modules_data = None
    if include_syllabus:
        modules_data = [
            {
                "title": "Module 1: Professional Standards & Core Concepts",
                "lessons": [
                    {"title": "Foundational Principles & Methodologies"},
                    {"title": "Operational Guidelines & Industry Best Practices"},
                    {"title": "Applied Techniques & Implementation"},
                ],
            },
            {
                "title": "Module 2: Advanced Practice & Capstone Assessment",
                "lessons": [
                    {"title": "Advanced Problem Solving & Case Studies"},
                    {"title": "Evaluation, Compliance & Quality Standards"},
                    {"title": "Comprehensive Capstone Final Evaluation"},
                ],
            },
        ]

    sample_cert_code = certificate_generator.generate_certificate_code().replace("SVARP-", "SVARP-SAMPLE-")
    return certificate_generator.generate_certificate_bytes(
        student_name=student_name.strip() if (student_name and student_name.strip()) else "John Doe",
        course_title=course_title.strip() if (course_title and course_title.strip()) else "Full-Stack Web Development Bootcamp",
        cert_code=sample_cert_code,
        profile_picture_url=None,
        frontend_url=frontend_url,
        progress=progress if progress is not None else 100,
        modules=modules_data,
        signatory_name=signatory_name.strip() if (signatory_name and signatory_name.strip()) else "Mr. Vikash Kumar",
    )

