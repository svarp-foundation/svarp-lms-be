from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Text, Enum, DateTime, JSON, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
from .database import Base

class UserRole(str, enum.Enum):
    ADMIN = "admin"
    INSTRUCTOR = "instructor"
    INSTRUCTOR_PENDING = "instructor_pending"
    LEARNER = "learner"

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    full_name = Column(String)
    role = Column(String, default=UserRole.LEARNER)
    is_active = Column(Boolean, default=True)
    is_suspended = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    enrollments = relationship("Enrollment", back_populates="user")
    submissions = relationship("Submission", back_populates="student")
    certificates = relationship("Certificate", back_populates="user", foreign_keys="[Certificate.user_id]")
    completed_lessons = relationship("LessonCompletion", back_populates="user")
    wishlists = relationship("Wishlist", back_populates="user")

class CourseStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    description = Column(Text)
    thumbnail_url = Column(String, nullable=True)
    status = Column(String, default=CourseStatus.DRAFT)
    passing_score = Column(Integer, default=70) # Percent
    require_all_lessons_completed = Column(Boolean, default=True)
    require_assignment_approval = Column(Boolean, default=False)
    require_final_assignment = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)
    is_paid = Column(Boolean, default=False)
    price = Column(Float, default=0.0)
    instructor_id = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    instructor = relationship("User", foreign_keys=[instructor_id])
    modules = relationship("Module", back_populates="course", cascade="all, delete-orphan")
    enrollments = relationship("Enrollment", back_populates="course")

class Module(Base):
    __tablename__ = "modules"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"))
    title = Column(String)
    description = Column(Text, nullable=True)
    order = Column(Integer)

    course = relationship("Course", back_populates="modules")
    lessons = relationship("Lesson", back_populates="module", cascade="all, delete-orphan")

class LessonType(str, enum.Enum):
    TEXT = "text"
    VIDEO = "video"
    ASSIGNMENT = "assignment"

class Lesson(Base):
    __tablename__ = "lessons"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"))
    title = Column(String)
    content = Column(Text) # For text lessons
    video_url = Column(String, nullable=True) # For video lessons
    lesson_type = Column(String)
    order = Column(Integer)


    module = relationship("Module", back_populates="lessons")
    assignment = relationship("Assignment", uselist=False, back_populates="lesson", cascade="all, delete-orphan")
    completions = relationship("LessonCompletion", back_populates="lesson", cascade="all, delete-orphan")
    comments = relationship("LessonComment", back_populates="lesson", cascade="all, delete-orphan")

class LessonCompletion(Base):
    __tablename__ = "lesson_completions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    lesson_id = Column(Integer, ForeignKey("lessons.id"))
    completed_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="completed_lessons")
    lesson = relationship("Lesson", back_populates="completions")


class QuestionType(str, enum.Enum):
    MCQ = "mcq"
    SUBJECTIVE = "subjective"

class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id"), nullable=True)
    title = Column(String)
    description = Column(Text)
    due_date = Column(DateTime(timezone=True), nullable=True)

    lesson = relationship("Lesson", back_populates="assignment")
    submissions = relationship("Submission", back_populates="assignment", cascade="all, delete-orphan")
    questions = relationship("Question", back_populates="assignment", order_by="Question.order", cascade="all, delete-orphan")

class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id"))
    question_text = Column(Text)
    question_type = Column(String, default=QuestionType.SUBJECTIVE)
    order = Column(Integer, default=1)

    assignment = relationship("Assignment", back_populates="questions")
    options = relationship("QuestionOption", back_populates="question", cascade="all, delete-orphan")
    answers = relationship("AnswerSubmission", back_populates="question")

class QuestionOption(Base):
    __tablename__ = "question_options"

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"))
    option_text = Column(String)
    is_correct = Column(Boolean, default=False)

    question = relationship("Question", back_populates="options")

class SubmissionStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESUBMISSION_REQUIRED = "resubmission_required"

class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id"))
    user_id = Column(String, ForeignKey("users.id"))
    content = Column(Text, nullable=True)
    file_url = Column(String, nullable=True)
    status = Column(String, default=SubmissionStatus.SUBMITTED)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())
    grade = Column(Integer, nullable=True)
    feedback = Column(Text, nullable=True)
    mcq_score = Column(Integer, nullable=True)       # auto-graded MCQ correct count
    mcq_total = Column(Integer, nullable=True)       # total MCQ questions

    assignment = relationship("Assignment", back_populates="submissions")
    student = relationship("User", back_populates="submissions")
    answers = relationship("AnswerSubmission", back_populates="submission", cascade="all, delete-orphan")

class AnswerSubmission(Base):
    __tablename__ = "answer_submissions"

    id = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"))
    question_id = Column(Integer, ForeignKey("questions.id"))
    answer_text = Column(Text, nullable=True)        # Free text for subjective
    selected_option_id = Column(Integer, ForeignKey("question_options.id"), nullable=True)  # For MCQ
    is_correct = Column(Boolean, nullable=True)     # Filled on MCQ auto-grade

    submission = relationship("Submission", back_populates="answers")
    question = relationship("Question", back_populates="answers")
    selected_option = relationship("QuestionOption")

class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    course_id = Column(Integer, ForeignKey("courses.id"))
    enrolled_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="enrollments")
    course = relationship("Course", back_populates="enrollments")

class CoursePaymentStatus(str, enum.Enum):
    CREATED = "created"
    SUCCESS = "success"
    FAILED = "failed"

class CoursePayment(Base):
    __tablename__ = "course_payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    course_id = Column(Integer, ForeignKey("courses.id"))
    payment_id = Column(String, nullable=True)  # Razorpay order ID
    amount = Column(Float)
    currency = Column(String, default="INR")
    status = Column(String, default=CoursePaymentStatus.CREATED)
    coupon_code = Column(String, nullable=True)
    coupon_id = Column(Integer, nullable=True)
    discount_amount = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")
    course = relationship("Course")

class Certificate(Base):
    __tablename__ = "certificates"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    course_id = Column(Integer, ForeignKey("courses.id"))
    issued_at = Column(DateTime(timezone=True), server_default=func.now())
    certificate_code = Column(String, unique=True, index=True) # For QR verification
    pdf_url = Column(String, nullable=True)
    
    # New fields for validity and audit
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String, nullable=True)
    issued_by = Column(String, ForeignKey("users.id"), nullable=True)
    verification_url = Column(String, nullable=True)

    user = relationship("User", back_populates="certificates", foreign_keys=[user_id])
    course = relationship("Course")

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(String, ForeignKey("users.id"))
    action_type = Column(String, index=True)
    target_entity = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    admin = relationship("User", foreign_keys=[admin_id])

class Wishlist(Base):
    __tablename__ = "wishlists"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    course_id = Column(Integer, ForeignKey("courses.id"))
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="wishlists")
    course = relationship("Course")

class LessonComment(Base):
    __tablename__ = "lesson_comments"

    id = Column(Integer, primary_key=True, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id"), nullable=False)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    lesson = relationship("Lesson", back_populates="comments")
    user = relationship("User")

class InstructorApplicationStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class InstructorApplication(Base):
    __tablename__ = "instructor_applications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)
    specialty = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    status = Column(String, default=InstructorApplicationStatus.PENDING)
    applied_at = Column(DateTime(timezone=True), server_default=func.now())
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    admin_feedback = Column(Text, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    reviewer = relationship("User", foreign_keys=[reviewed_by])

