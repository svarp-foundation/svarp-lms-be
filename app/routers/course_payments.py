from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from .. import models, schemas, database, auth
import os
import requests

router = APIRouter(
    prefix="/course-payments",
    tags=["course-payments"],
)

CPP_API_URL = os.getenv("CPP_API_URL")
CPP_APP_KEY = os.getenv("CPP_APP_KEY")
CPP_APP_SECRET = os.getenv("CPP_APP_SECRET")


@router.post("/create-order", response_model=schemas.CoursePaymentOrderResponse)
def create_course_payment_order(
    payment_data: schemas.CoursePaymentCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner),
):
    """Create a Razorpay order for a paid course via CPP."""

    # Validate course exists and is paid
    course = db.query(models.Course).filter(
        models.Course.id == payment_data.course_id,
        models.Course.is_deleted == False,
    ).first()

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if not course.is_paid:
        raise HTTPException(status_code=400, detail="This course is free — no payment required")

    # Check if already enrolled (payment already done)
    existing_enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.user_id == current_user.id,
        models.Enrollment.course_id == payment_data.course_id,
    ).first()
    if existing_enrollment:
        raise HTTPException(status_code=400, detail="Already enrolled in this course")

    # Call CPP to create a Razorpay order
    headers = {
        "x-app-key": CPP_APP_KEY,
        "x-app-secret": CPP_APP_SECRET,
    }
    payload = {
        "user_id": current_user.email,
        "amount": int(payment_data.amount * 100) if payment_data.currency == "INR" else int(payment_data.amount),
        "currency": payment_data.currency,
        "media_type": "application/json",
        "plan_type": "course",
        "metadata_info": {
            "course_id": payment_data.course_id,
            "user_id": current_user.id,
        },
    }

    try:
        response = requests.post(
            f"{CPP_API_URL}/payments/create-order",
            json=payload,
            headers=headers,
        )
        print(response)
        response.raise_for_status()
        order_data = response.json()
    except requests.RequestException as e:
        print(f"CPP Error: {e}")
        raise HTTPException(status_code=502, detail="Payment Gateway unavailable")

    # Create a local CoursePayment record
    db_payment = models.CoursePayment(
        user_id=current_user.id,
        course_id=payment_data.course_id,
        payment_id=order_data.get("razorpay_order_id"),
        amount=payment_data.amount,
        currency=payment_data.currency,
        status=order_data.get("status", "created"),
    )
    db.add(db_payment)
    db.commit()
    db.refresh(db_payment)

    return schemas.CoursePaymentOrderResponse(
        id=db_payment.id,
        razorpay_order_id=order_data.get("razorpay_order_id"),
        amount=payment_data.amount,
        currency=payment_data.currency,
        key_id=order_data.get("key_id"),
        app_name="SVARP GLOBAL ACADEMY",
        status=db_payment.status,
    )


@router.post("/verify")
def verify_course_payment(
    verify_data: schemas.CoursePaymentVerify,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner),
):
    """Verify a Razorpay payment via CPP and auto-enroll the user in the course."""

    # Call CPP to verify
    headers = {
        "x-app-key": CPP_APP_KEY,
        "x-app-secret": CPP_APP_SECRET,
    }

    try:
        response = requests.post(
            f"{CPP_API_URL}/payments/verify-payment",
            json=verify_data.dict(),
            headers=headers,
        )
        response.raise_for_status()
        verification_data = response.json()
    except requests.RequestException as e:
        print(f"CPP Verification Error: {e}")
        raise HTTPException(status_code=400, detail="Payment verification failed")

    if not verification_data.get("success"):
        raise HTTPException(status_code=400, detail="Payment verification failed by provider")

    # Find the local payment record
    db_payment = db.query(models.CoursePayment).filter(
        models.CoursePayment.payment_id == verify_data.razorpay_order_id,
        models.CoursePayment.user_id == current_user.id,
    ).first()

    if not db_payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    # Mark payment as success
    db_payment.status = "success"
    db.commit()

    # Auto-enroll the user in the course
    existing_enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.user_id == current_user.id,
        models.Enrollment.course_id == db_payment.course_id,
    ).first()

    if not existing_enrollment:
        enrollment = models.Enrollment(
            user_id=current_user.id,
            course_id=db_payment.course_id,
        )
        db.add(enrollment)
        db.commit()

    return {
        "status": "success",
        "message": "Payment verified. You have been enrolled in the course.",
        "course_id": db_payment.course_id,
    }
