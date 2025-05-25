import os
import re
from openai import OpenAI
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

# Initialize OpenAI and Supabase clients
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(supabase_url, supabase_key)

# Format top N clause matches for GPT prompt
def format_clauses_for_prompt(clauses):
    formatted = []
    for idx, c in enumerate(clauses[:5], 1):
        citation = c.get("citation", "Clause")
        link     = c.get("link", "#")
        summary  = c.get("summary", "No summary provided.")
        cid      = c.get("clause_id")
        doc      = c.get("document", "")
        source   = c.get("match_source", "Unknown")
        pg_match = re.search(r"(?:Pg|Page)[\s]*([0-9\-]+)", citation, re.I)
        page_str = f"Pg {pg_match.group(1)}" if pg_match else ""

        entry = (
            f"{idx}. **[{citation}]({link})**\n"
            f"_Summary_: {summary}\n"
            f"_Match Source_: **{source}**\n"
            f"_Reviewer_: ID {cid} • Doc “{doc}” • {page_str}\n"
        )
        formatted.append(entry)
    return "\n".join(formatted)


# GPT prompt template
def build_gpt_prompt(question, clause_text):
    return f"""
You are an HOA policy assistant. Based on the provided clause data, answer the resident's question in clear, friendly, and accurate language.

Resident Question:
{question}

Below are relevant clause matches:
{clause_text}

Write your response in this format:
1. Brief summary of each clause that might apply
2. State whether the rules clearly answer the question
3. If unclear, suggest checking with the ARC
4. Always close with: “Let us know if you need help with forms or next steps!”

Use markdown for citations like: **[citation](link)**.
Final Answer:
"""

# Call embedding + Supabase vector match
def fetch_matching_clauses(question, tags=None):
    # Try vector match first
    embedding_response = client.embeddings.create(
        input=[question],
        model="text-embedding-ada-002"
    )
    query_embedding = embedding_response.data[0].embedding

    response = supabase.rpc("match_clauses", {
        "query_embedding": query_embedding,
        "match_threshold": 0.60,
        "match_count": 5
    }).execute()

    if response.data:
        for clause in response.data:
            clause["match_source"] = "Vector Match"
        return response.data

    # Fallback to keyword search with optional tags
    if tags:
        fallback = (
            supabase
            .from_("clauses")
            .select("*")
            .contains("tags", tags)
            .ilike("summary", f"%{question}%")
            .limit(5)
            .execute()
        )
    else:
        fallback = (
            supabase
            .from_("clauses")
            .select("*")
            .ilike("summary", f"%{question}%")
            .limit(5)
            .execute()
        )

    if fallback.data:
        for clause in fallback.data:
            clause["match_source"] = "Tag + Keyword Fallback" if tags else "Keyword Fallback"

    return fallback.data




# Main GPT answer logic
def answer_question(question, tags=None):
    clauses = fetch_matching_clauses(question, tags=tags)
    if not clauses:
        return "There are no specific HOA rules found that address this question  directly. Please consult the board for further guidance."

    clause_text = format_clauses_for_prompt(clauses)
    prompt = build_gpt_prompt(question, clause_text)

    gpt_response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert HOA assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.4
    )
    return gpt_response.choices[0].message.content

