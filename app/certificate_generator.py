import uuid
from io import BytesIO
from datetime import datetime
import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader

def generate_certificate_code() -> str:
    """Generate a unique verification code."""
    date_str = datetime.now().strftime("%Y%m%d")
    short_uuid = str(uuid.uuid4()).split('-')[0].upper()
    return f"SVARP-{date_str}-{short_uuid}"

def generate_qr_code_bytes(cert_code: str, frontend_url: str = "http://localhost:5173") -> bytes:
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

def generate_certificate_bytes(student_name: str, course_title: str, cert_code: str, frontend_url: str = "http://localhost:5173") -> bytes:
    """
    Generates a PDF certificate dynamically in memory and returns raw bytes.
    """
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
    qr_image_reader = ImageReader(qr_buffer)
    
    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=letter)
    width, height = letter
    
    # Certificate Title
    c.setFont("Helvetica-Bold", 36)
    c.drawCentredString(width / 2.0, height - 2 * inch, "Certificate of Completion")
    
    # Subtitle
    c.setFont("Helvetica", 18)
    c.drawCentredString(width / 2.0, height - 3 * inch, "This is to certify that")
    
    # Student Name
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(width / 2.0, height - 4 * inch, student_name)
    
    # Course specific text
    c.setFont("Helvetica", 18)
    c.drawCentredString(width / 2.0, height - 5 * inch, "has successfully completed the course:")
    
    # Course Title
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width / 2.0, height - 6 * inch, course_title)
    
    # Date
    c.setFont("Helvetica", 14)
    current_date = datetime.now().strftime("%B %d, %Y")
    c.drawCentredString(width / 2.0, height - 7.5 * inch, f"Issued on: {current_date}")
    
    # Verification Code
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2.0, height - 8 * inch, f"Verification Code: {cert_code}")
    
    # Add QR Code Image
    qr_size = 1.5 * inch
    c.drawImage(qr_image_reader, (width - qr_size) / 2.0, inch, width=qr_size, height=qr_size)
    
    c.showPage()
    c.save()
    
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()
