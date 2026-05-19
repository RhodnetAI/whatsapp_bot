import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"{name} environment variable is required")
    return value


@dataclass(frozen=True)
class Settings:
    meta_access_token: str
    phone_number_id: str
    verify_token: str
    openai_api_key: str
    openai_embedding_model: str
    openai_embedding_dim: int
    qdrant_url: str
    qdrant_api_key: str
    qdrant_grpc_port: int | None
    qdrant_collection: str
    unstructured_api_key: str
    unstructured_api_url: str
    groq_api_key: str
    voyage_api_key: str
    supabase_url: str
    supabase_key: str
    supabase_service_role_key: str | None
    admin_username: str
    admin_password: str


settings = Settings(
    meta_access_token=_required_env("META_ACCESS_TOKEN"),
    phone_number_id=_required_env("PHONE_NUMBER_ID"),
    verify_token=_required_env("VERIFY_TOKEN"),
    openai_api_key=os.getenv("OPENAI_KEY", ""),
    openai_embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    openai_embedding_dim=int(os.getenv("OPENAI_EMBEDDING_DIM", "1536")),
    qdrant_url=os.getenv("QDRANT_URL", ""),
    qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
    qdrant_grpc_port=int(os.getenv("QDRANT_GRPC_PORT", "0")) if os.getenv("QDRANT_GRPC_PORT") else None,
    qdrant_collection=os.getenv("QDRANT_COLLECTION", "agent_chunks_v2"),
    unstructured_api_key=os.getenv("UNSTRUCTURED_API_KEY", ""),
    unstructured_api_url=os.getenv("UNSTRUCTURED_API_URL", ""),
    groq_api_key=os.getenv("GROQ_API_KEY", ""),
    voyage_api_key=os.getenv("VOYAGE_API_KEY", ""),
    supabase_url=_required_env("SUPABASE_URL"),
    supabase_key=_required_env("SUPABASE_KEY"),
    supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
    admin_username=_required_env("ADMIN_USERNAME"),
    admin_password=_required_env("ADMIN_PASSWORD"),
)
