import os
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()

OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-ada-002")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@lru_cache(maxsize=1)
def get_openai_client() -> OpenAI:
    return OpenAI(api_key=_require_env("OPENAI_API_KEY"))


@lru_cache(maxsize=1)
def get_supabase_client():
    return create_client(
        _require_env("SUPABASE_URL"),
        _require_env("SUPABASE_SERVICE_ROLE_KEY"),
    )


def build_clause_embedding_input(clause: dict) -> str:
    parts = [
        clause.get("citation") or "",
        clause.get("plain_summary") or "",
        clause.get("clause_text") or "",
    ]
    return "\n\n".join(part for part in parts if part.strip())


def generate_embedding(text: str) -> list[float]:
    response = get_openai_client().embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=text,
    )
    return response.data[0].embedding
