import os
import uuid
from io import BytesIO
from datetime import datetime
import qrcode
import requests
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.units import inch, cm
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors

DEFAULT_FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

def generate_certificate_code() -> str:
    """Generate a unique verification code."""
    date_str = datetime.now().strftime("%Y%m%d")
    short_uuid = str(uuid.uuid4()).split('-')[0].upper()
    return f"SVARP-{date_str}-{short_uuid}"

def generate_qr_code_bytes(cert_code: str, frontend_url: str = DEFAULT_FRONTEND_URL) -> bytes:
    verify_url = f"{frontend_url}/verify/{cert_code}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(verify_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    qr_buffer = BytesIO()
    img.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)
    return qr_buffer.getvalue()

def generate_certificate_bytes(student_name: str, course_title: str, cert_code: str, profile_picture_url: str = None, frontend_url: str = DEFAULT_FRONTEND_URL, progress: int = 100) -> bytes:
    """
    Generates a premium DESIGNER PDF certificate dynamically, matching the frontend's layout perfectly.
    """
    # Use landscape for a more traditional certificate feel
    page_size = landscape(letter)
    width, height = page_size
    
    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=page_size)
    
    # 1. Background (Pure White as in Frontend container)
    c.setFillColor(colors.white)
    c.rect(0, 0, width, height, fill=1, stroke=0)
    
    # 1b. Subtle border (like the border-2 border-gray-100 in frontend)
    c.setStrokeColor(colors.HexColor("#f3f4f6")) # gray-100
    c.setLineWidth(2)
    c.rect(10, 10, width - 20, height - 20, fill=0, stroke=1)
    
    # 2. Top Accent Bar (Primary Green)
    c.setFillColor(colors.HexColor("#9bcf9b")) 
    c.rect(0, height - 12, width, 12, fill=1, stroke=0)
    
    # 3. Dynamic Watermark Background (Award Icon Placeholder)
    c.setStrokeColor(colors.HexColor("#9bcf9b"))
    c.setLineWidth(0.5)
    c.setFillAlpha(0.05)
    c.saveState()
    c.translate(width/2, height/2)
    c.rotate(12)
    # Draw a larger seal-like shape for the watermark
    c.restoreState()
    c.setFillAlpha(1.0) # Reset alpha
    
    # 4. QR Code (Top Right)
    verify_url = f"{frontend_url}/verify/{cert_code}"
    qr_bytes = generate_qr_code_bytes(cert_code, frontend_url)
    qr_image_reader = ImageReader(BytesIO(qr_bytes))
    
    qr_size = 1.2 * inch
    c.saveState()
    # Add rounded-corner-like border for QR
    c.setStrokeColor(colors.HexColor("#f3f4f6"))
    c.roundRect(width - qr_size - 60, height - qr_size - 60, qr_size + 20, qr_size + 20, 12, fill=0, stroke=1)
    c.drawImage(qr_image_reader, width - qr_size - 50, height - qr_size - 50, width=qr_size, height=qr_size)
    c.restoreState()
    
    # 5. Header Section (Left)
    c.setFillColor(colors.HexColor("#1f3b45")) # ACCENT_COLOR
    c.setFont("Helvetica-Bold", 32)
    c.drawString(60, height - 80, "Certificate of Achievement")
    
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.HexColor("#6b7280")) # gray-500
    c.drawString(60, height - 100, "SVARP GLOBAL ACADEMY")
    
    # 6. Main Content Layout
    
    # 6a. "This is to certify that"
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont("Helvetica-Oblique", 18)
    c.drawCentredString(width / 2.0, height / 2.0 + 100, "This is to certify that")
    
    # 6b. Learner Name
    c.setFillColor(colors.HexColor("#1f3b45"))
    c.setFont("Helvetica-Bold", 42)
    c.drawCentredString(width / 2.0, height / 2.0 + 50, student_name)
    
    # 6c. Profile Image (Circular)
    try:
        # User dynamic profile image URL if provided, else fallback
        profile_url = profile_picture_url or "https://i.pravatar.cc/150?u=svarp_default"
        # Since this is a default, we can try to fetch it. 
        # For simplicity and speed in a backend, we'll try it, but fall back gracefully.
        response = requests.get(profile_url, timeout=2)
        if response.status_code == 200:
            profile_reader = ImageReader(BytesIO(response.content))
        else:
            raise Exception("Failed to fetch image")
    except Exception:
        # Fallback if network fails: Draw a grey circle with a person icon placeholder
        profile_reader = None

    img_y = height / 2.0 - 60
    img_size = 3.5 * cm
    
    c.saveState()
    # Draw circular clipper
    p = c.beginPath()
    p.circle(width/2, img_y + img_size/2, img_size/2)
    c.clipPath(p, stroke=0, fill=0)
    
    if profile_reader:
        c.drawImage(profile_reader, width/2 - img_size/2, img_y, width=img_size, height=img_size)
    else:
        c.setFillColor(colors.HexColor("#e5e7eb")) # bg-muted
        c.circle(width/2, img_y + img_size/2, img_size/2, fill=1, stroke=0)
    
    c.restoreState()
    
    # Circle Border (4px border-primary/20)
    c.setStrokeColor(colors.HexColor("#9bcf9b")) 
    c.setLineWidth(3)
    c.circle(width/2, img_y + img_size/2, img_size/2, stroke=1, fill=0)
    
    # 6d. "has successfully completed"
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2.0, img_y - 25, "has successfully completed the course")
    
    # 6e. Course Title
    c.setFillColor(colors.HexColor("#9bcf9b"))
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(width / 2.0, img_y - 65, course_title)
    
    # 7. Honour Badge (if progress >= 75)
    if progress >= 75:
        c.setFillColor(colors.HexColor("#9bcf9b"))
        c.setStrokeColor(colors.white)
        c.setLineWidth(1)
        badge_y = img_y - 110
        c.roundRect(width/2 - 80, badge_y, 160, 25, 12, fill=1, stroke=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(width/2, badge_y + 7, "PASS WITH HONOUR")
    
    # 8. Footer Section
    # ID and Date (Bottom Left)
    c.setFillColor(colors.HexColor("#9ca3af")) # gray-400
    c.setFont("Helvetica-Bold", 9)
    current_date = datetime.now().strftime("%B %d, %Y")
    
    c.drawString(60, 60, f"ID: {cert_code}")
    c.drawString(60, 45, f"DATE: {current_date}")
    
    # Signatory (Bottom Right)
    c.setFillColor(colors.HexColor("#1f3b45"))
    c.setFont("Helvetica-BoldOblique", 24)
    c.drawCentredString(width - 150, 70, "Mr. Vikash Kumar")
    
    c.setStrokeColor(colors.HexColor("#d1d5db")) # gray-300
    c.setLineWidth(0.5)
    c.line(width - 250, 65, width - 50, 65)
    
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(width - 150, 50, "AUTHORIZED SIGNATORY")
    
    c.showPage()
    c.save()
    
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()
