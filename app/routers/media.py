from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
import os
import mimetypes
from .. import auth, database, models

router = APIRouter(
    prefix="/media",
    tags=["media"]
)

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
                    
                    content_bytes = certificate_generator.generate_certificate_bytes(
                        student_name=student.full_name,
                        course_title=course.title,
                        cert_code=cert.certificate_code,
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
