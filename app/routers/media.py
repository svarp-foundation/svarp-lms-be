from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
    request: Request,
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
            # Security Check: Only allow the owner of the certificate OR an Admin to access
            if user.role != models.UserRole.ADMIN.value and user.id != cert.user_id:
                raise HTTPException(status_code=403, detail="Access denied to this certificate")
                
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

                    # Retrieve modules and lessons sorted by order
                    modules_list = sorted(course.modules, key=lambda m: m.order)
                    modules_data = []
                    for m in modules_list:
                        lessons_list = sorted(m.lessons, key=lambda l: l.order)
                        lessons_data = [{"title": l.title, "type": l.lesson_type} for l in lessons_list]
                        modules_data.append({
                            "title": m.title,
                            "lessons": lessons_data
                        })

                    # Dynamically determine frontend_url to handle local network testing / custom domains
                    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
                    if "localhost" in frontend_url or "127.0.0.1" in frontend_url:
                        req_host = request.base_url.hostname
                        if req_host and req_host not in ("localhost", "127.0.0.1"):
                            from urllib.parse import urlparse
                            parsed_url = urlparse(frontend_url)
                            port_str = f":{parsed_url.port}" if parsed_url.port else ""
                            frontend_url = f"{parsed_url.scheme}://{req_host}{port_str}{parsed_url.path}".rstrip("/")

                    content_bytes = certificate_generator.generate_certificate_bytes(
                        student_name=student.full_name,
                        course_title=course.title,
                        cert_code=cert.certificate_code,
                        profile_picture_url=profile_picture_url,
                        frontend_url=frontend_url,
                        progress=progress,
                        modules=modules_data
                    )
                    media_type = "application/pdf"
                else: # .png
                    # Dynamically determine frontend_url for QR code
                    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
                    if "localhost" in frontend_url or "127.0.0.1" in frontend_url:
                        req_host = request.base_url.hostname
                        if req_host and req_host not in ("localhost", "127.0.0.1"):
                            from urllib.parse import urlparse
                            parsed_url = urlparse(frontend_url)
                            port_str = f":{parsed_url.port}" if parsed_url.port else ""
                            frontend_url = f"{parsed_url.scheme}://{req_host}{port_str}{parsed_url.path}".rstrip("/")

                    content_bytes = certificate_generator.generate_qr_code_bytes(
                        cert_code=cert.certificate_code,
                        frontend_url=frontend_url
                    )
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
    # Verify access to course media resources:
    if user.role != models.UserRole.ADMIN.value:
        # 1. Check if the file is a course thumbnail (accessible to all authenticated users)
        is_thumbnail = db.query(models.Course).filter(
            models.Course.thumbnail_url.like(f"%{filename}"),
            models.Course.is_deleted == False
        ).first() is not None

        # 2. Check if the file is a lesson video
        lesson = db.query(models.Lesson).filter(
            models.Lesson.video_url.like(f"%{filename}")
        ).first()

        # 3. Check if the file is a learner submission file
        submission = db.query(models.Submission).filter(
            models.Submission.file_url.like(f"%{filename}")
        ).first()

        if lesson:
            # User must be enrolled in the course that contains this lesson video
            course_id = db.query(models.Module.course_id).filter(
                models.Module.id == lesson.module_id
            ).scalar()
            if course_id:
                enrolled = db.query(models.Enrollment).filter(
                    models.Enrollment.user_id == user.id,
                    models.Enrollment.course_id == course_id
                ).first() is not None
                if not enrolled:
                    raise HTTPException(
                        status_code=403, 
                        detail="Access denied. You must be enrolled in this course to access this media."
                    )
        elif submission:
            # Only the submitting student (or admin) can view this file
            if submission.user_id != user.id:
                raise HTTPException(
                    status_code=403,
                    detail="Access denied. You cannot view another learner's submission."
                )
        elif not is_thumbnail:
            # Not a thumbnail, video, or submission: access forbidden
            raise HTTPException(
                status_code=403,
                detail="Access denied."
            )

    media_type, _ = mimetypes.guess_type(file_path)
    if not media_type:
        media_type = "application/octet-stream"

    disposition = "attachment" if download else "inline"
    return FileResponse(
        file_path, 
        media_type=media_type, 
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'}
    )
