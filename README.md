# SVARP LMS Backend

The backend for the SVARP Learning Management System (LMS), built with **FastAPI**.

## 🚀 Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **ORM**: [SQLAlchemy](https://www.sqlalchemy.org/)
- **Database**: SQLite (default)
- **Validation**: [Pydantic](https://docs.pydantic.dev/)
- **Authentication**: JWT (python-jose)
- **PDF Generation**: ReportLab & QRcode

## ✨ Core Features

- **Authentication**: Secure signup/login with JWT and bcrypt password hashing.
- **RBAC**: Role-based access control (Admin vs. Learner).
- **Course Management**: Full CRUD for courses, modules, and lessons.
- **Progress Tracking**: Dynamic calculation of course completion.
- **Assignments**: Submission and approval workflow for course assignments.
- **Payments**: Integration with Razorpay (via CPP bridge).
- **Certificates**: Automated PDF certificate generation with QR code verification upon course completion.

## 🛠️ Setup & Installation

### 1. Prerequisites

- Python 3.8+
- `pip`

### 2. Environment Setup

Create a `.env` file in this directory based on your configuration:

```env
DATABASE_URL=sqlite:///./lms.db
SECRET_KEY=your_secret_key_here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=43200

# Payment Integration (CPP)
CPP_API_URL=http://localhost:8001
CPP_APP_KEY=your_app_key
CPP_APP_SECRET=your_app_secret
```

### 3. Install Dependencies

```bash
# Create virtual environment
python3 -m venv venv

# Activate venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

### 4. Initialize Database & Admin

```bash
# Create the first admin user interactively
python -m app.create_admin
```

### 5. Run the Server

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`.

## 📖 API Documentation

FastAPI provides interactive documentation out of the box:

- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## 📂 Project Structure

- `app/`: Core application logic.
  - `routers/`: API endpoints grouped by resource (Admin, Learner, Media, etc.).
  - `models.py`: SQLAlchemy database models.
  - `schemas.py`: Pydantic data models for validation.
  - `auth.py`: JWT and security logic.
  - `certificate_generator.py`: PDF logic.
- `static/uploads/`: Storage for uploaded media and images.
