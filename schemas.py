"""
Database Schemas for Surfbrew

Each Pydantic model represents a MongoDB collection.
Collection name is the lowercase of the class name.

- User -> "user"
- Coach -> "coach"
- School -> "school"
- Session -> "session"
- Booking -> "booking"
"""

from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List, Literal
from datetime import datetime


class User(BaseModel):
    name: str = Field(..., description="Full name of the surfer")
    email: EmailStr = Field(..., description="Email address of the surfer")
    phone: Optional[str] = Field(None, description="Contact phone number")


class Coach(BaseModel):
    name: str = Field(..., description="Coach full name")
    bio: Optional[str] = Field(None, description="Short bio for the coach")
    certification: Optional[str] = Field(None, description="Certifications or qualifications")
    school_id: Optional[str] = Field(None, description="Associated school id (if any)")
    rating: Optional[float] = Field(None, ge=0, le=5, description="Average rating 0-5")


class School(BaseModel):
    name: str = Field(..., description="School name")
    location: str = Field(..., description="Primary location / beach")
    description: Optional[str] = Field(None, description="About the school")
    website: Optional[str] = Field(None, description="Website URL")


SessionType = Literal["group", "private", "recurring"]
SkillLevel = Literal["beginner", "intermediate", "advanced", "all"]


class Session(BaseModel):
    title: str = Field(..., description="Title of the surf session")
    description: Optional[str] = Field(None, description="What’s included / focus areas")
    coach_id: Optional[str] = Field(None, description="Coach id running the session")
    school_id: Optional[str] = Field(None, description="School id (if applicable)")
    location: str = Field(..., description="Beach / city")
    level: SkillLevel = Field("all", description="Target skill level")
    session_type: SessionType = Field("group", description="group | private | recurring")
    start_time: datetime = Field(..., description="ISO datetime for the session start")
    duration_minutes: int = Field(..., gt=0, description="Duration in minutes")
    price: float = Field(..., ge=0, description="Price per person in USD")
    capacity: int = Field(1, ge=1, description="Max participants (1 for private)")


class Booking(BaseModel):
    session_id: str = Field(..., description="The session being booked")
    user_name: str = Field(..., description="Name of the surfer booking")
    user_email: EmailStr = Field(..., description="Contact email")
    participants: int = Field(1, ge=1, description="Number of participants")
    notes: Optional[str] = Field(None, description="Special requests or notes")
    status: Literal["pending", "confirmed", "cancelled"] = Field("pending", description="Booking status")
