import logging

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.security import create_access_token
from app.models.schemas import LoginRequest


router = APIRouter(tags=["auth"])
logger = logging.getLogger("whatsapp")


@router.post("/login")
def login(data: LoginRequest) -> dict[str, str]:
    if data.username == settings.admin_username and data.password == settings.admin_password:
        token = create_access_token({"sub": "admin"})
        return {"access_token": token}

    logger.warning("Failed login for username=%s", data.username)
    raise HTTPException(status_code=401, detail="Invalid credentials")
