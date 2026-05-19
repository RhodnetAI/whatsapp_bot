import datetime
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.auth import router as auth_router
from app.api.routes.clients import router as clients_router
from app.api.routes.health import router as health_router
from app.api.routes.setup import router as setup_router
from app.api.routes.webhook import router as webhook_router
from app.db.supabase_client import supabase


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("whatsapp")


app = FastAPI(title="WhatsApp Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "https://whatsapp-1-dtig.onrender.com",
        "https://whatsapp-bot-qwpw.onrender.com",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def cleanup_old_messages() -> None:
    cutoff_date = datetime.date.today() - datetime.timedelta(days=5)
    try:
        (
            supabase.table("whatsapp_conversations")
            .delete()
            .lt("conversation_date", str(cutoff_date))
            .execute()
        )
        logger.info("Cleaned up messages older than %s", cutoff_date)
    except Exception as exc:
        logger.warning(
            "Failed to cleanup old messages: %s. If table is missing, run backend/sql/001_create_whatsapp_conversations.sql in Supabase SQL Editor.",
            exc,
        )


app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(clients_router)
app.include_router(setup_router)
app.include_router(health_router)
