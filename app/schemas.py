from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from .models import UserRole, CourseStatus, LessonType, SubmissionStatus, QuestionType

class UserBase(BaseModel):
    email: str
    full_name: str

class UserCreate(UserBase):
    password: str

class User(UserBase):
    id: int
    role: str
    is_suspended: bool = False
    created_at: datetime
    class Config:
        orm_mode = True

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str

class TokenRefresh(BaseModel):
    refresh_token: str

class TokenData(BaseModel):
    user_id: Optional[int] = None
    role: Optional[str] = None

class CourseBase(BaseModel):
    title: str
    description: str
    thumbnail_url: Optional[str] = None
    passing_score: Optional[int] = 70
    require_all_lessons_completed: Optional[bool] = True
    require_assignment_approval: Optional[bool] = False
    is_paid: Optional[bool] = False
    price: Optional[float] = 0.0

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
    is_deleted: Optional[bool] = None
    is_paid: Optional[bool] = None
    price: Optional[float] = None

class Course(CourseBase):
    id: int
    status: str
    is_deleted: bool
    created_at: datetime
    class Config:
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

class LessonCreate(BaseModel):
    title: str
    content: Optional[str] = None
    video_url: Optional[str] = None
    lesson_type: LessonType
    order: int

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
    order: int = 1
    options: List[QuestionOptionCreate] = []  # Only for MCQ

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
    user_id: int
    course_id: int
    issued_at: datetime
    certificate_code: str
    pdf_url: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None
    issued_by: Optional[int] = None
    verification_url: Optional[str] = None
    class Config:
        orm_mode = True

class PublicCertificateVerification(BaseModel):
    student_name: str
    course_title: str
    issue_date: datetime
    status: str
    certificate_code: str

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
    certificate_pdf_url: Optional[str] = None
    class Config:
        orm_mode = True

class AuditLogCreate(BaseModel):
    action_type: str
    target_entity: str

class AuditLog(AuditLogCreate):
    id: int
    admin_id: int
    timestamp: datetime
    class Config:
        orm_mode = True

# ── Course Payment Schemas ──────────────────────────────────────────────────

class CoursePaymentCreate(BaseModel):
    course_id: int
    amount: float
    currency: str = "INR"

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

class CoursePaymentAdmin(BaseModel):
    id: int
    user_id: int
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
