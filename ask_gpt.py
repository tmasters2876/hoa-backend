import os
import re
from openai import OpenAI
from supabase import create_client
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

# Initialize OpenAI and Supabase clients
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(supabase_url, supabase_key)

# Format grouped clauses for GPT prompt
def format_clauses_for_prompt(clauses):
    grouped = defaultdict(list)
    for clause in clauses:
        grouped[clause.get("document", "Other")].append(clause)

    formatted = []
    for doc, group in grouped.items():
        idx = 1
        for c in group:
            citation = c.get("citation", "Clause")
            link = c.get("link", "")
            summary = c.get("summary", "No summary provided.")
            cid = c.get("clause_id", "source")
            source = c.get("match_source", "Unknown")
            page_match = re.search(r"Pg(?:\.|\s)?(\d{1,3})", citation, re.I)
            pg_match = f"Pg {page_match.group(1)}" if page_match else ""

            entry = (
                f"**{idx}. Summary of Clause**: According to **[{citation}]({link})**, "
                f"{summary}\n\n"
                f"**Match Source**: {source} • Doc `{doc}` • {pg_match}\n"
                f"**Reviewer ID**: `{cid}`\n"
            )
            formatted.append(entry)
            idx += 1
    return "\n---\n\n".join(formatted)

# GPT Prompt Template
def build_gpt_prompt(question, clause_text):
    return f"""
You are an HOA policy assistant. Based on the provided Clause data, answer the resident’s question in clear, friendly, and accurate language.

Resident Question:
{question}

Below are relevant clause matches:
{clause_text}

Write your response in this format:
1. Brief summary of each Clause that might apply
2. State whether the rules clearly answer the question
3. If unclear, suggest checking with the ARC

Always close with: "Let us know if you need help with forms or next steps!"

Use markdown for citations like: **[citation](link)**.

---
Final Answer:
"""

# Fetch matching clauses with fallback
def fetch_matching_clauses(question, tags=None, structure_type=None, concern_level=None):
    # Step 1: Vector Match
    embedding_response = client.embeddings.create(
        input=question,
        model="text-embedding-ada-002"
    )
    query_embedding = embedding_response.data[0].embedding

    response = supabase.rpc("match_clauses", {
        "query_embedding": query_embedding,
        "match_threshold": 0.80,
        "match_count": 5
    }).execute()

    vector_matches = response.data or []
    for clause in vector_matches:
        clause["match_source"] = "Vector Match"

    # Step 2: Fallback if vector matches are low
    if len(vector_matches) < 3:
        clause_query = supabase.from_("clauses").select("*")
        if tags:
            clause_query = clause_query.ilike("tags", f"%{tags[0]}%")
        if structure_type:
            clause_query = clause_query.ilike("structure_type", f"%{structure_type}%")
        if concern_level:
            clause_query = clause_query.eq("concern_level", concern_level)

        result = clause_query.limit(3).execute()
        fallback_matches = result.data or []
        for clause in fallback_matches:
            clause["match_source"] = "Tag + Keyword Fallback"
        return vector_matches + fallback_matches

    return vector_matches

# Main GPT Answer Function
def answer_question(question, tags=None, mode="default", structure_type=None, concern_level=None, output_format="markdown"):
    raw_clauses = fetch_matching_clauses(
        question=question,
        tags=tags,
        structure_type=structure_type,
        concern_level=concern_level
    )

    # Deduplicate clauses by ID
    unique_clauses = {}
    for clause in raw_clauses:
        cid = clause.get("clause_id")
        if cid not in unique_clauses:
            unique_clauses[cid] = clause

    clauses = list(unique_clauses.values())
    if not clauses:
        return "There are no specific HOA rules found that address this question directly. Please consult the board for further guidance."

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

    final_answer = gpt_response.choices[0].message.content
    if output_format == "json":
        return {
            "question": question,
            "answer": final_answer,
            "matches": clauses,
            "mode": mode,
            "output_format": "json"
        }

    return f"{final_answer}\n\n---\n\n{clause_text}"
