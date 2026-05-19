# WhatsApp Backend (FastAPI + uv)

## Setup with uv

1. Install dependencies:

```bash
uv sync
```

2. Run the API:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Environment Variables

Create `.env` in this folder and define:

- `META_ACCESS_TOKEN`
- `PHONE_NUMBER_ID`
- `VERIFY_TOKEN`
- `OPENAI_KEY` (optional, AI replies fallback when missing)
- `OPENAI_EMBEDDING_MODEL` (optional, default: `text-embedding-3-small`)
- `OPENAI_EMBEDDING_DIM` (optional, default: `1536`)
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `SUPABASE_SERVICE_ROLE_KEY` (optional, required for some storage/admin operations)
- `QDRANT_URL` (optional, enable Qdrant vector store)
- `QDRANT_API_KEY` (optional)
- `QDRANT_GRPC_PORT` (optional)
- `QDRANT_COLLECTION` (optional, default: `agent_chunks_v2`)
- `UNSTRUCTURED_API_KEY` (optional, advanced document parsing)
- `UNSTRUCTURED_API_URL` (optional)
- `GROQ_API_KEY` (optional, advanced intent classification)
- `VOYAGE_API_KEY` (optional, advanced reranking)
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `JWT_SECRET`
- `JWT_ALGORITHM` (optional, default: `HS256`)

## Supabase Table Setup (Required)

This backend expects the following tables:

- `public.whatsapp_conversations`
- `public.service_agent_setup`
- `public.service_agent_documents`

1. Open your Supabase project dashboard.
2. Go to SQL Editor.
3. Run [sql/001_create_whatsapp_conversations.sql](sql/001_create_whatsapp_conversations.sql).
4. Run [sql/002_service_agent_setup.sql](sql/002_service_agent_setup.sql).

If these tables are missing, the onboarding and dashboard routes will fail or fall back to preview mode.

## Project Structure

```text
backend/
  sql/
    001_create_whatsapp_conversations.sql
  app/
    api/
      routes/
        auth.py
        clients.py
        health.py
        webhook.py
    core/
      config.py
      security.py
    db/
      supabase_client.py
    models/
      schemas.py
    services/
      ai.py
      whatsapp.py
    main.py
  auth.py          # compatibility shim
  main.py          # compatibility entrypoint
  pyproject.toml
  requirements.txt
```
