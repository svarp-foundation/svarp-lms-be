from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from .. import models, schemas, database, auth, utils
import os
import requests

router = APIRouter(
    prefix="/course-payments",
    tags=["course-payments"],
)

CPP_API_URL = os.getenv("CPP_API_URL")
CPP_APP_KEY = os.getenv("CPP_APP_KEY")
CPP_APP_SECRET = os.getenv("CPP_APP_SECRET")
SVARP_ADMIN_API_KEY = os.getenv("SVARP_ADMIN_API_KEY")
SVARP_ADMIN_BASE_URL = (os.getenv("SVARP_ADMIN_BASE_URL") or "").rstrip("/")
SVARP_VERIFY_URL = f"{SVARP_ADMIN_BASE_URL}/admin/verify-user"

COUPON_API_URL = os.getenv("COUPON_API_URL")
COUPON_APP_KEY = os.getenv("COUPON_APP_KEY")
COUPON_APP_SECRET = os.getenv("COUPON_APP_SECRET")


@router.post("/create-order", response_model=schemas.CoursePaymentOrderResponse)
def create_course_payment_order(
    payment_data: schemas.CoursePaymentCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner),
):
    """Create a Razorpay order for a paid course via CPP."""

    # NEW: Verify User Profile Status first
    phone_number = None
    verification_data = None
    try:
        verify_response = requests.get(
            f"{SVARP_VERIFY_URL}?email={current_user.email}",
            headers={"X-API-Key": SVARP_ADMIN_API_KEY}
        )
        if verify_response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="please register to our webssite with same email svarp.org and complete the documentation need to contimue to payment"
            )
        verify_response.raise_for_status()
        verification_data = verify_response.json()
        if not verification_data:
            raise HTTPException(status_code=502, detail="User verification system returned empty response")
            
        phone_number = verification_data.get("phone_number")
        
        readiness = verification_data.get("payment_readiness") or {}
        if not readiness.get("ready"):
            raise HTTPException(
                status_code=403, 
                detail={
                    "message": "Profile verification required",
                    "readiness": readiness
                }
            )
    except requests.RequestException as e:
        print(f"User Verification Error: {e}")
        # Optionally allow if verification system is down, or block. 
        # Here we block for security.
        phone_number = None
        raise HTTPException(status_code=502, detail="User verification system unavailable")

    # Validate course exists and is paid
    course = db.query(models.Course).filter(
        models.Course.id == payment_data.course_id,
        models.Course.is_deleted == False,
    ).first()

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if not course.is_paid:
        raise HTTPException(status_code=400, detail="This course is free — no payment required")
    
    # MEMBERSHIP GUARD: Members get it for free
    if utils.is_active_member(verification_data):
         raise HTTPException(status_code=400, detail="You have an active membership. You can enroll in this course for free from the course page.")

    # Check if already enrolled (payment already done)
    existing_enrollment = db.query(models.Enrollment).filter(
        models.Enrollment.user_id == current_user.id,
        models.Enrollment.course_id == payment_data.course_id,
    ).first()
    if existing_enrollment:
        raise HTTPException(status_code=400, detail="Already enrolled in this course")

    # Calculate GST (18%) and apply coupon discount if applicable
    discount_amount = 0.0
    coupon_id = None
    
    if payment_data.coupon_code:
        try:
            coupon_res = requests.post(
                f"{COUPON_API_URL}/coupons/validate",
                headers={
                    "x-api-key": COUPON_APP_KEY,
                    "x-api-secret": COUPON_APP_SECRET,
                },
                json={
                    "code": payment_data.coupon_code,
                    "user_id": current_user.email,
                    "subtotal": float(course.price),
                    "items": [
                        {
                            "id": str(course.id),
                            "price": float(course.price),
                            "quantity": 1
                        }
                    ]
                }
            )
            if coupon_res.status_code in (400, 404):
                try:
                    res_json = coupon_res.json()
                    msg = res_json.get("detail") or res_json.get("message") or "Invalid coupon code"
                except Exception:
                    msg = "Invalid coupon code"
                raise HTTPException(status_code=400, detail=msg)

            coupon_res.raise_for_status()
            coupon_res_data = coupon_res.json()
            if coupon_res_data.get("valid"):
                discount_amount = float(coupon_res_data.get("discount_amount") or 0.0)
                coupon_id = coupon_res_data.get("coupon_id")
            else:
                raise HTTPException(status_code=400, detail=coupon_res_data.get("message") or "Invalid coupon code")
        except HTTPException:
            raise
        except requests.RequestException as e:
            print(f"Coupon Validation Error: {e}")
            raise HTTPException(status_code=502, detail="Discount coupon validator service currently unavailable")

    gst_rate = 0.18
    base_amount = course.price
    discounted_base = max(0.0, base_amount - discount_amount)
    gst_amount = discounted_base * gst_rate
    total_amount = discounted_base + gst_amount

    # Call CPP to create a Razorpay order
    headers = {
        "x-app-key": CPP_APP_KEY,
        "x-app-secret": CPP_APP_SECRET,
    }
    payload = {
        "user_id": current_user.email,
        "amount": int(total_amount * 100) if payment_data.currency == "INR" else int(total_amount),
        "currency": payment_data.currency,
        "media_type": "application/json",
        "plan_type": "course",
        "metadata_info": {
            "course_id": payment_data.course_id,
            "user_id": current_user.id,
            "base_amount": base_amount,
            "gst_amount": gst_amount,
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
        amount=total_amount,
        currency=payment_data.currency,
        status=order_data.get("status", "created"),
        coupon_code=payment_data.coupon_code,
        coupon_id=coupon_id,
        discount_amount=discount_amount,
    )
    db.add(db_payment)
    db.commit()
    db.refresh(db_payment)

    return schemas.CoursePaymentOrderResponse(
        id=db_payment.id,
        razorpay_order_id=order_data.get("razorpay_order_id"),
        amount=total_amount,
        currency=payment_data.currency,
        key_id=order_data.get("key_id"),
        app_name="SVARP GLOBAL ACADEMY",
        status=db_payment.status,
        phone_number=phone_number,
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

    # Log successful coupon claim in Coupon Portal if applied
    if db_payment.coupon_id:
        try:
            claim_response = requests.post(
                f"{COUPON_API_URL}/coupons/claim",
                headers={
                    "x-api-key": COUPON_APP_KEY,
                    "x-api-secret": COUPON_APP_SECRET,
                },
                json={
                    "coupon_id": db_payment.coupon_id,
                    "user_id": current_user.email,
                    "order_id": db_payment.payment_id or "unknown",
                    "payment_verified": True
                }
            )
            claim_response.raise_for_status()
        except Exception as e:
            print(f"Coupon Claim Error: {e}")

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


@router.post("/validate-coupon")
def validate_checkout_coupon(
    request_data: schemas.CouponValidateRequest,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.require_learner),
):
    """Validate coupon code and return calculated discount details."""
    course = db.query(models.Course).filter(
        models.Course.id == request_data.course_id,
        models.Course.is_deleted == False,
    ).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
        
    try:
        coupon_res = requests.post(
            f"{COUPON_API_URL}/coupons/validate",
            headers={
                "x-api-key": COUPON_APP_KEY,
                "x-api-secret": COUPON_APP_SECRET,
            },
            json={
                "code": request_data.code,
                "user_id": current_user.email,
                "subtotal": float(course.price),
                "items": [
                    {
                        "id": str(course.id),
                        "price": float(course.price),
                        "quantity": 1
                    }
                ]
            }
        )
        if coupon_res.status_code in (400, 404):
            try:
                res_json = coupon_res.json()
                msg = res_json.get("detail") or res_json.get("message") or "Invalid coupon code"
            except Exception:
                msg = "Invalid coupon code"
            return {"valid": False, "message": msg, "discount_amount": 0.0}

        coupon_res.raise_for_status()
        coupon_res_data = coupon_res.json()
        return coupon_res_data
    except requests.RequestException as e:
        print(f"Coupon Validation Error: {e}")
        return {"valid": False, "message": "Coupon service is temporarily unavailable. Please try again later.", "discount_amount": 0.0}

