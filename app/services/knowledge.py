from app.services.rag import answer_query_from_rag


async def answer_query_from_knowledge(query: str, setup_config: dict[str, str] | None = None) -> str:
    return await answer_query_from_rag(query, setup_config=setup_config)
