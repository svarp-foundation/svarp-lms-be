from pydantic import BaseModel
from typing import List, Optional, Union
from datetime import datetime
from .models import UserRole, CourseStatus, LessonType, SubmissionStatus, QuestionType

class UserBase(BaseModel):
    email: str
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str
    role: Optional[str] = "learner"
    specialty: Optional[str] = None
    bio: Optional[str] = None

class User(UserBase):
    id: Union[int, str]
    role: str
    is_suspended: bool = False
    created_at: Optional[datetime] = None
    membership: Optional[dict] = None
    profile_picture_url: Optional[str] = None
    class Config:
        from_attributes = True
        orm_mode = True

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: Optional[User] = None

class TokenRefresh(BaseModel):
    refresh_token: str

class TokenData(BaseModel):
    user_id: Optional[Union[int, str]] = None
    role: Optional[str] = None

class CourseBase(BaseModel):
    title: str
    description: str
    thumbnail_url: Optional[str] = None
    status: Optional[str] = "draft"
    passing_score: Optional[int] = 70
    require_all_lessons_completed: Optional[bool] = True
    require_assignment_approval: Optional[bool] = False
    require_final_assignment: Optional[bool] = False
    is_paid: Optional[bool] = False
    price: Optional[float] = 0.0
    discounted_price: Optional[float] = None
    instructor_id: Optional[str] = None
    instructor_name: Optional[str] = None

class CourseCreate(CourseBase):
    pass

class CourseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    status: Optional[CourseStatus] = None
    passing_score: Optional[int] = None
    require_all_lessons_completed: Optional[bool] = None
    require_assignment_approval: Optional[bool] = None
    require_final_assignment: Optional[bool] = None
    is_deleted: Optional[bool] = None
    is_paid: Optional[bool] = None
    price: Optional[float] = None
    instructor_id: Optional[str] = None

class Course(CourseBase):
    id: int
    status: str
    is_deleted: bool
    created_at: datetime
    class Config:
        from_attributes = True
        orm_mode = True

class EnrolledCourse(Course):
    progress: int
    class Config:
        orm_mode = True

class PublicLessonDetail(BaseModel):
    id: int
    title: str
    lesson_type: LessonType
    order: int
    class Config:
        orm_mode = True

class PublicModuleDetail(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    order: int
    lessons: List[PublicLessonDetail] = []
    class Config:
        orm_mode = True

class PublicCourseDetail(Course):
    modules: List[PublicModuleDetail] = []
    class Config:
        orm_mode = True

class LessonAdmin(BaseModel):
    id: int
    title: str
    lesson_type: LessonType
    content: Optional[str] = None
    video_url: Optional[str] = None
    order: int
    class Config:
        orm_mode = True

class ModuleAdmin(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    order: int
    lessons: List[LessonAdmin] = []
    class Config:
        orm_mode = True

class CourseAdminDetail(Course):
    modules: List[ModuleAdmin] = []
    class Config:
        orm_mode = True

class QuestionOptionCreate(BaseModel):
    option_text: str
    is_correct: bool = False

class QuestionOptionOut(BaseModel):
    id: int
    option_text: str
    class Config:
        orm_mode = True

class QuestionOptionAdmin(BaseModel):
    id: int
    option_text: str
    is_correct: bool
    class Config:
        orm_mode = True

class QuestionCreate(BaseModel):
    question_text: str
    question_type: QuestionType = QuestionType.SUBJECTIVE
    order: Optional[int] = 1
    options: List[QuestionOptionCreate] = []  # Only for MCQ

class LessonCreate(BaseModel):
    title: str
    content: Optional[str] = None
    video_url: Optional[str] = None
    lesson_type: LessonType = LessonType.TEXT
    order: Optional[int] = 1
    assignment_title: Optional[str] = None
    assignment_description: Optional[str] = None
    due_date: Optional[datetime] = None
    questions: Optional[List[QuestionCreate]] = None

class LessonUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    video_url: Optional[str] = None
    order: Optional[int] = None

class ModuleCreate(BaseModel):
    title: str
    description: Optional[str] = None
    order: int
    lessons: List[LessonCreate] = []

class QuestionOut(BaseModel):
    id: int
    question_text: str
    question_type: str
    order: int
    options: List[QuestionOptionOut] = []
    class Config:
        orm_mode = True

class QuestionAdmin(BaseModel):
    id: int
    question_text: str
    question_type: str
    order: int
    options: List[QuestionOptionAdmin] = []
    class Config:
        orm_mode = True

class AssignmentCreate(BaseModel):
    title: str
    description: str
    lesson_id: Optional[int] = None
    course_id: Optional[int] = None
    questions: List[QuestionCreate] = []

class Assignment(BaseModel):
    id: int
    title: str
    description: str
    lesson_id: Optional[int] = None
    course_id: Optional[int] = None
    class Config:
        orm_mode = True

class AssignmentDetail(BaseModel):
    """Full assignment with questions — for learner (is_correct hidden)"""
    id: int
    title: str
    description: str
    questions: List[QuestionOut] = []
    class Config:
        orm_mode = True

class AssignmentAdminDetail(BaseModel):
    """Full assignment with questions + correct answers — for admin"""
    id: int
    title: str
    description: str
    questions: List[QuestionAdmin] = []
    class Config:
        orm_mode = True

class AnswerSubmissionItem(BaseModel):
    question_id: int
    answer_text: Optional[str] = None        # For subjective
    selected_option_id: Optional[int] = None # For MCQ

class AssignmentSubmit(BaseModel):
    answers: List[AnswerSubmissionItem]

class AnswerResult(BaseModel):
    question_id: int
    question_text: str
    question_type: str
    answer_text: Optional[str] = None
    selected_option_id: Optional[int] = None
    is_correct: Optional[bool] = None  # None for subjective

class SubmissionResult(BaseModel):
    submission_id: int
    status: str
    mcq_score: Optional[int] = None
    mcq_total: Optional[int] = None
    has_subjective: bool = False
    answers: List[AnswerResult] = []

class SubmissionReview(BaseModel):
    status: SubmissionStatus
    grade: Optional[int] = None
    feedback: Optional[str] = None

class Certificate(BaseModel):
    id: int
    user_id: Union[int, str]
    course_id: int
    issued_at: datetime
    certificate_code: str
    pdf_url: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None
    issued_by: Optional[Union[int, str]] = None
    verification_url: Optional[str] = None
    class Config:
        orm_mode = True

class PublicCertificateVerification(BaseModel):
    student_name: str
    course_title: str
    issue_date: datetime
    status: str
    certificate_code: str
    profile_picture_url: Optional[str] = None

class LessonStatus(BaseModel):
    id: int
    title: str
    lesson_type: LessonType
    content: Optional[str] = None
    video_url: Optional[str] = None
    completed: bool = False
    locked: bool = True
    order: int
    class Config:
        orm_mode = True

class ModuleStatus(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    order: int
    lessons: List[LessonStatus] = []
    class Config:
        orm_mode = True

class CourseContent(BaseModel):
    id: int
    title: str
    modules: List[ModuleStatus] = []
    progress: int
    require_final_assignment: bool = False
    final_assignment: Optional[AssignmentDetail] = None
    certificate_pdf_url: Optional[str] = None
    certificate_code: Optional[str] = None
    profile_picture_url: Optional[str] = None
    verification_readiness: Optional[dict] = None
    class Config:
        orm_mode = True

class AuditLogCreate(BaseModel):
    action_type: str
    target_entity: str

class AuditLog(AuditLogCreate):
    id: int
    admin_id: Union[int, str]
    timestamp: datetime
    class Config:
        orm_mode = True

# ── Course Payment Schemas ──────────────────────────────────────────────────

class CoursePaymentCreate(BaseModel):
    course_id: int
    amount: float
    currency: str = "INR"
    coupon_code: Optional[str] = None

class CoursePaymentOrderResponse(BaseModel):
    id: int
    razorpay_order_id: str
    amount: float
    currency: str
    key_id: str
    app_name: str
    status: str
    phone_number: Optional[str] = None

class CoursePaymentVerify(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

class CouponValidateRequest(BaseModel):
    code: str
    course_id: int

class CoursePaymentAdmin(BaseModel):
    id: int
    user_id: Union[int, str]
    user_email: str
    course_id: int
    course_title: str
    payment_id: Optional[str] = None
    amount: float
    currency: str
    status: str
    created_at: datetime
    class Config:
        orm_mode = True

# ── Wishlist Schemas ───────────────────────────────────────────────────────────

class WishlistItem(Course):
    """A wishlisted course, same fields as Course."""
    class Config:
        orm_mode = True

class UserCertificate(BaseModel):
    id: int
    course_id: int
    course_title: str
    issued_at: datetime
    certificate_code: str
    pdf_url: Optional[str] = None
    class Config:
        orm_mode = True

# ── Lesson Discussion/Comments Schemas ──────────────────────────────────────────

class UserMini(BaseModel):
    id: Union[int, str]
    full_name: str
    email: str
    role: str
    profile_picture_url: Optional[str] = None
    class Config:
        orm_mode = True

class LessonCommentCreate(BaseModel):
    content: str

class LessonCommentOut(BaseModel):
    id: int
    lesson_id: int
    user_id: Union[int, str]
    content: str
    created_at: datetime
    user: UserMini

    class Config:
        orm_mode = True

# ── Quiz Attempt History Schema ───────────────────────────────────────────────

class AttemptOut(BaseModel):
    submission_id: int
    status: str
    submitted_at: datetime
    grade: Optional[int] = None
    feedback: Optional[str] = None
    mcq_score: Optional[int] = None
    mcq_total: Optional[int] = None

    class Config:
        orm_mode = True

# ── Instructor Schemas ──────────────────────────────────────────────────────────

class UserRoleUpdate(BaseModel):
    role: str

class InstructorApplicationCreate(BaseModel):
    specialty: Optional[str] = None
    bio: Optional[str] = None

class InstructorApplicationReview(BaseModel):
    status: Optional[str] = None  # approved, rejected
    action: Optional[str] = None  # alias for status
    admin_feedback: Optional[str] = None
    feedback: Optional[str] = None  # alias for admin_feedback

class InstructorApplicationResponse(BaseModel):
    id: int
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    specialty: Optional[str] = None
    bio: Optional[str] = None
    status: str
    applied_at: datetime
    reviewed_at: Optional[datetime] = None
    admin_feedback: Optional[str] = None

    class Config:
        from_attributes = True
        orm_mode = True

class InstructorStats(BaseModel):
    total_courses: int
    published_courses: int
    total_students: int
    pending_reviews: int
    certificates_issued: int
    total_revenue: float
    completion_rate: float

