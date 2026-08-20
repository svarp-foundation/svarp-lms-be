import logging
import hmac
import hashlib
import json
from fastapi import APIRouter, Depends, Request, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app import models

logger = logging.getLogger("svarp-lms.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

@router.post("/payment")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receives course payment notifications from portal-payment.
    Idempotently unlocks course access and records enrollment.
    """
    body_bytes = await request.body()
    signature = request.headers.get("X-Webhook-Signature")
    
    webhook_secret = "default_webhook_secret"
    if signature and webhook_secret:
        expected_sig = hmac.new(webhook_secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            logger.warning("Invalid webhook signature on svarp-lms payment webhook")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    event = payload.get("event")
    data = payload.get("data", {})
    razorpay_order_id = data.get("razorpay_order_id")

    if event == "payment.success" and razorpay_order_id:
        logger.info(f"Course payment.success webhook received for order_id={razorpay_order_id}")
        
        course_payment = db.query(models.CoursePayment).filter(
            models.CoursePayment.payment_id == razorpay_order_id
        ).first()

        if course_payment:
            if course_payment.status != models.CoursePaymentStatus.SUCCESS:
                course_payment.status = models.CoursePaymentStatus.SUCCESS
                db.commit()

            # Ensure student is enrolled
            existing_enrollment = db.query(models.Enrollment).filter(
                models.Enrollment.user_id == course_payment.user_id,
                models.Enrollment.course_id == course_payment.course_id
            ).first()

            if not existing_enrollment:
                new_enrollment = models.Enrollment(
                    user_id=course_payment.user_id,
                    course_id=course_payment.course_id,
                    status=models.EnrollmentStatus.ACTIVE
                )
                db.add(new_enrollment)
                db.commit()
                logger.info(f"Student {course_payment.user_id} enrolled in course {course_payment.course_id} via webhook")

    return {"status": "success"}
