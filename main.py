import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents
from schemas import Session as SessionSchema, Booking as BookingSchema, User as UserSchema, Coach as CoachSchema, School as SchoolSchema

app = FastAPI(title="Surfbrew API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# ---------- Bookings ----------

class BookingIn(BaseModel):
    session_id: str
    user_name: str
    user_email: str
    participants: int = 1
    notes: Optional[str] = None


@app.post("/api/bookings")
def create_booking(payload: BookingIn):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    session_doc = db["session"].find_one({"_id": {"$eq": db["session"].database.client.get_default_database()["session"].codec_options.document_class.__call__.__self__ if False else None}})
    # Above line is nonsensical; replace with straightforward lookup by string id
    from bson import ObjectId
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
