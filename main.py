import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Depends, UploadFile, File, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents
from schemas import Session as SessionSchema, Booking as BookingSchema, User as UserSchema, Coach as CoachSchema, School as SchoolSchema

# Auth imports using PyJWT for reliability in slim envs
import jwt
from passlib.context import CryptContext
from bson import ObjectId
import base64


app = FastAPI(title="Surfbrew API", version="0.3.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Auth Setup ----------
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-surfbrew-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 24 * 7
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginIn(BaseModel):
    email: str
    password: str


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(plain_password, password_hash)
    except Exception:
        return False


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(authorization: Optional[str] = Header(None, alias="Authorization")):
    token: Optional[str] = None
    if authorization and isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    user_doc = db["user"].find_one({"email": email})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")
    return user_doc


def require_role(*allowed_roles: str):
    async def _role_dep(user = Depends(get_current_user)):
        role = user.get("role")
        if role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return _role_dep


# ---------- Utility helpers ----------

def serialize_doc(doc: Dict[str, Any]):
    if not doc:
        return doc
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # Convert datetimes to isoformat strings
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.astimezone(timezone.utc).isoformat()
    return d


def bookings_for_session(session_id: str):
    if db is None:
        return []
    return list(db["booking"].find({"session_id": session_id, "status": {"$ne": "cancelled"}}))


def session_availability(session_doc: Dict[str, Any]):
    capacity = session_doc.get("capacity", 1)
    bookings = bookings_for_session(str(session_doc.get("_id")))
    booked = sum(max(1, int(b.get("participants", 1))) for b in bookings)
    available = max(0, int(capacity) - int(booked))
    return {"booked": booked, "available": available, "capacity": capacity}


# ---------- Basic routes ----------

@app.get("/")
def read_root():
    return {"name": "Surfbrew API", "message": "Welcome to Surfbrew"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from Surfbrew backend!"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# ---------- Schemas endpoint (for viewers/tools) ----------

@app.get("/schema")
def get_schema():
    def schema_of(model):
        try:
            return model.model_json_schema()
        except Exception:
            return {}

    return {
        "user": schema_of(UserSchema),
        "coach": schema_of(CoachSchema),
        "school": schema_of(SchoolSchema),
        "session": schema_of(SessionSchema),
        "booking": schema_of(BookingSchema),
    }


# ---------- Auth routes ----------

class RegisterIn(BaseModel):
    name: str
    email: str
    password: str
    role: str  # admin|coach|school
    coach_id: Optional[str] = None
    school_id: Optional[str] = None


@app.post("/auth/register")
def register_user(payload: RegisterIn):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    if db["user"].find_one({"email": payload.email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    data = {
        "name": payload.name,
        "email": payload.email,
        "password_hash": get_password_hash(payload.password),
        "role": payload.role,
        "coach_id": payload.coach_id,
        "school_id": payload.school_id,
    }
    inserted_id = create_document("user", data)
    return {"id": inserted_id}


@app.post("/auth/login", response_model=Token)
def login(payload: LoginIn):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    user = db["user"].find_one({"email": payload.email})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": user["email"], "role": user.get("role")})
    return Token(access_token=token)


@app.get("/auth/me")
def auth_me(user = Depends(get_current_user)):
    return serialize_doc(user)


# ---------- Asset upload & serve ----------

@app.post("/api/upload")
def upload_image(file: UploadFile = File(...), user = Depends(require_role("admin", "coach", "school"))):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    content = file.file.read()
    b64 = base64.b64encode(content).decode("utf-8")
    doc = {
        "content_type": file.content_type or "application/octet-stream",
        "data": b64,
        "filename": file.filename,
    }
    asset_id = create_document("asset", doc)
    return {"url": f"/assets/{asset_id}", "id": asset_id}


@app.get("/assets/{asset_id}")
def get_asset(asset_id: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        doc = db["asset"].find_one({"_id": ObjectId(asset_id)})
    except Exception:
        doc = None
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    content = base64.b64decode(doc.get("data", ""))
    return Response(content=content, media_type=doc.get("content_type", "application/octet-stream"))


# ---------- Sessions ----------

@app.get("/api/sessions")
def list_sessions(
    q: Optional[str] = Query(None, description="Search in title/description/location"),
    location: Optional[str] = None,
    level: Optional[str] = None,
    session_type: Optional[str] = Query(None, description="group|private|recurring"),
    coach_id: Optional[str] = None,
    school_id: Optional[str] = None,
    upcoming_only: bool = True,
    limit: int = Query(50, ge=1, le=200),
):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    filt: Dict[str, Any] = {}
    if q:
        filt["$or"] = [
            {"title": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}},
            {"location": {"$regex": q, "$options": "i"}},
        ]
    if location:
        filt["location"] = {"$regex": location, "$options": "i"}
    if level:
        filt["level"] = level
    if session_type:
        filt["session_type"] = session_type
    if coach_id:
        filt["coach_id"] = coach_id
    if school_id:
        filt["school_id"] = school_id
    if upcoming_only:
        filt["start_time"] = {"$gte": datetime.now(timezone.utc)}

    cursor = db["session"].find(filt).sort("start_time", 1).limit(int(limit))
    sessions = []
    for s in cursor:
        data = serialize_doc(s)
        data["availability"] = session_availability(s)
        sessions.append(data)
    return {"items": sessions, "count": len(sessions)}


@app.post("/api/sessions")
def create_session(payload: SessionSchema, user = Depends(require_role("admin", "coach", "school"))):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    data = payload.model_dump()
    inserted_id = create_document("session", data)
    return {"id": inserted_id}


# ---------- Bookings ----------

class BookingIn(BaseModel):
    session_id: str
    user_name: str
    user_email: str
    participants: int = 1
    experience_level: str
    notes: Optional[str] = None


@app.post("/api/bookings")
def create_booking(payload: BookingIn):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    try:
        session_doc = db["session"].find_one({"_id": ObjectId(payload.session_id)})
    except Exception:
        session_doc = None

    if not session_doc:
        raise HTTPException(status_code=404, detail="Session not found")

    avail = session_availability(session_doc)
    if payload.participants > avail["available"]:
        raise HTTPException(status_code=400, detail=f"Only {avail['available']} spot(s) left")

    booking = {
        "session_id": payload.session_id,
        "user_name": payload.user_name,
        "user_email": payload.user_email,
        "participants": int(payload.participants),
        "experience_level": payload.experience_level,
        "notes": payload.notes,
        "status": "confirmed",
    }
    inserted_id = create_document("booking", booking)

    return {"id": inserted_id, "status": "confirmed"}


@app.get("/api/bookings")
def list_bookings(email: Optional[str] = None, session_id: Optional[str] = None, limit: int = 50):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    filt: Dict[str, Any] = {}
    if email:
        filt["user_email"] = email
    if session_id:
        filt["session_id"] = session_id

    items = [serialize_doc(d) for d in db["booking"].find(filt).sort("created_at", -1).limit(int(limit))]
    return {"items": items, "count": len(items)}


# Admin bookings with filters & actions
@app.get("/api/admin/bookings")
def admin_list_bookings(
    status: Optional[str] = None,
    q: Optional[str] = Query(None, description="search in name/email"),
    experience_level: Optional[str] = None,
    limit: int = 100,
    user = Depends(require_role("admin", "coach", "school"))
):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    filt: Dict[str, Any] = {}
    if status:
        filt["status"] = status
    if experience_level:
        filt["experience_level"] = experience_level
    if q:
        filt["$or"] = [
            {"user_name": {"$regex": q, "$options": "i"}},
            {"user_email": {"$regex": q, "$options": "i"}},
        ]
    items = [serialize_doc(d) for d in db["booking"].find(filt).sort("created_at", -1).limit(int(limit))]
    return {"items": items, "count": len(items)}


@app.post("/api/admin/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, user = Depends(require_role("admin", "coach", "school"))):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        res = db["booking"].update_one({"_id": ObjectId(booking_id)}, {"$set": {"status": "cancelled", "updated_at": datetime.now(timezone.utc)}})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid booking id")
    if res.modified_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"ok": True}


@app.post("/api/admin/bookings/{booking_id}/attend")
def attend_booking(booking_id: str, user = Depends(require_role("admin", "coach", "school"))):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        res = db["booking"].update_one({"_id": ObjectId(booking_id)}, {"$set": {"status": "attended", "updated_at": datetime.now(timezone.utc)}})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid booking id")
    if res.modified_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"ok": True}


# ---------- Coaches ----------

@app.get("/api/coaches")
def list_coaches(limit: int = 100):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    items = [serialize_doc(d) for d in db["coach"].find({}).sort("name", 1).limit(int(limit))]
    return {"items": items, "count": len(items)}


@app.post("/api/coaches")
def create_coach(payload: CoachSchema, user = Depends(require_role("admin", "coach", "school"))):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    inserted_id = create_document("coach", payload.model_dump())
    return {"id": inserted_id}


# ---------- Schools ----------

@app.get("/api/schools")
def list_schools(limit: int = 100):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    items = [serialize_doc(d) for d in db["school"].find({}).sort("name", 1).limit(int(limit))]
    return {"items": items, "count": len(items)}


@app.post("/api/schools")
def create_school(payload: SchoolSchema, user = Depends(require_role("admin", "coach", "school"))):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    inserted_id = create_document("school", payload.model_dump())
    return {"id": inserted_id}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
