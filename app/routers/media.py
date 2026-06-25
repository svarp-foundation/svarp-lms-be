from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
import os
import mimetypes
import requests
from .. import auth, database, models

router = APIRouter(
    prefix="/media",
    tags=["media"]
)

SVARP_ADMIN_API_KEY = os.getenv("SVARP_ADMIN_API_KEY")
SVARP_ADMIN_BASE_URL = os.getenv("SVARP_ADMIN_BASE_URL", "https://svarp-website-be.svarp.cloud")
SVARP_VERIFY_URL = f"{SVARP_ADMIN_BASE_URL}/admin/verify-user"

@router.get("/profile-picture")
async def get_profile_picture(
    token: str = Query(..., description="JWT Bearer Token required for profile picture access"),
    db: Session = Depends(database.get_db)
):
    """
    Proxy route to fetch user profile picture from the main website backend safely.
    """
    try:
        user = await auth.get_current_user(token=token, db=db)
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid token")

    try:
        verify_response = requests.get(
            f"{SVARP_VERIFY_URL}?email={user.email}",
            headers={"X-API-Key": SVARP_ADMIN_API_KEY},
            timeout=5
        )
        if verify_response.status_code == 200:
            user_data = verify_response.json()
            profile_path = user_data.get("profile_picture_path")
            if profile_path:
                profile_url = f"{SVARP_ADMIN_BASE_URL}{profile_path}"
                img_res = requests.get(profile_url, timeout=5)
                if img_res.status_code == 200:
                    media_type = img_res.headers.get("Content-Type", "image/png")
                    return Response(content=img_res.content, media_type=media_type)
    except Exception as e:
        print(f"Error fetching profile picture via proxy: {e}")

    raise HTTPException(status_code=404, detail="Profile picture not found")

@router.get("/{filename}")
async def get_secure_media(
    filename: str,
    token: str = Query(..., description="JWT Bearer Token required for media access"),
    download: bool = Query(False, description="Set to true to trigger file download"),
    db: Session = Depends(database.get_db)
):
    """
    Serves media files securely.
    Standard video tags <video src="..."> cannot pass Authorization headers,
    so we accept the JWT token as a query parameter.
    """
    try:
        # Validate the token manually since we aren't using the precise Header Depends
        user = await auth.get_current_user(token=token, db=db)
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid or expired token for media access")
        
    # Security: Prevent directory traversal attacks
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename format")

    if filename.startswith("SVARP-") and filename.endswith((".pdf", ".png")):
        cert_code = filename[:-4]
        cert = db.query(models.Certificate).filter(models.Certificate.certificate_code == cert_code).first()
        if cert:
            course = db.query(models.Course).filter(models.Course.id == cert.course_id).first()
            student = db.query(models.User).filter(models.User.id == cert.user_id).first()
            if course and student:
                from .. import certificate_generator
                
                if filename.endswith(".pdf"):
                    from .. import completion_engine
                    progress = completion_engine.calculate_dynamic_progress(db, student.id, course.id)
                    
                    # Fetch profile picture from SVARP Admin API
                    profile_picture_url = None
                    try:
                        verify_response = requests.get(
                            f"{SVARP_VERIFY_URL}?email={student.email}",
                            headers={"X-API-Key": SVARP_ADMIN_API_KEY},
                            timeout=5
                        )
                        if verify_response.status_code == 200:
                            user_data = verify_response.json()
                            profile_path = user_data.get("profile_picture_path")
                            if profile_path:
                                profile_picture_url = f"{SVARP_ADMIN_BASE_URL}{profile_path}"
                    except Exception as e:
                        print(f"Error fetching profile picture for certificate: {e}")

                    content_bytes = certificate_generator.generate_certificate_bytes(
                        student_name=student.full_name,
                        course_title=course.title,
                        cert_code=cert.certificate_code,
                        profile_picture_url=profile_picture_url,
                        progress=progress
                    )
                    media_type = "application/pdf"
                else: # .png
                    content_bytes = certificate_generator.generate_qr_code_bytes(cert_code=cert.certificate_code)
                    media_type = "image/png"
                    
                disposition = "attachment" if download else "inline"
                return Response(
                    content=content_bytes,
                    media_type=media_type,
                    headers={"Content-Disposition": f'{disposition}; filename="{filename}"'}
                )
        raise HTTPException(status_code=404, detail="Certificate or QR Code not found")

    file_path = os.path.join("backend/static/uploads", filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    # In a fully strict system:
    # 1. We would check if the User is ENROLLED in the course that contains this video
    # 2. Or if the User is an Admin
    
    if user.role != models.UserRole.ADMIN.value:
        # Extra check: this is a learner. 
        # A more complex system would map `filename` -> `lesson.video_url` -> `course` -> `enrollment`.
        # For phase 4 MVP, ensuring they are a logged-in active user covers the primary gap.
        pass

    media_type, _ = mimetypes.guess_type(file_path)
    if not media_type:
        media_type = "application/octet-stream"

    disposition = "attachment" if download else "inline"
    return FileResponse(
        file_path, 
        media_type=media_type, 
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'}
    )
