import os
import openai
from supabase import create_client
from dotenv import load_dotenv
from typing import List, Dict

# Load environment variables
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(supabase_url, supabase_key)

# Format clauses for GPT prompt (basic markdown)
def format_clause_markdown(clause: Dict) -> str:
    return (
        f"**Clause ID:** {clause.get('id', 'N/A')}\n"
        f"**Page:** {clause.get('page', 'N/A')}\n"
        f"**Summary:** {clause.get('plain_english', 'N/A')}\n"
        f"[View Document]({clause.get('doc_url', '')})\n"
    )

# Format clauses for reviewer mode
def format_clause_reviewer(clause: Dict) -> str:
    return (
        f"**Clause ID:** {clause.get('id', 'N/A')}\n"
        f"**Page:** {clause.get('page', 'N/A')}\n"
        f"**Summary:** {clause.get('plain_english', 'N/A')}\n"
        f"**Original Text:** {clause.get('original_text', 'N/A')}\n"
        f"[View Document]({clause.get('doc_url', '')})\n"
    )

# Perform vector search on Supabase
def get_clause_matches(question: str, tags: List[str] = None, top_n: int = 5) -> List[Dict]:
    if tags:
        # Basic tag filtering using 'contains' (adjust depending on how tags are stored)
        tag_filter = {"tags": {"contains": tags}}
        response = supabase.table("clauses").select("*").match(tag_filter).execute()
        clauses = response.data
    else:
        # Placeholder vector search result (replace with your vector logic if needed)
        response = supabase.table("clauses").select("*").limit(top_n).execute()
        clauses = response.data

    return clauses[:top_n]

# Main function to answer question
def answer_question(
    question: str,
    mode: str = "default",
    tags: List[str] = None,
    output_format: str = "markdown"
) -> str | Dict:

    clauses = get_clause_matches(question, tags=tags, top_n=5)

    formatted_clauses = ""
    if mode == "reviewer":
        formatted_clauses = "\n---\n".join([format_clause_reviewer(c) for c in clauses])
    else:
        formatted_clauses = "\n---\n".join([format_clause_markdown(c) for c in clauses])

    system_prompt = (
        "You are a helpful HOA policy assistant. Use the clause context provided to answer clearly. "
        "If no clear rule is found, say so honestly and suggest next steps if appropriate."
    )

    user_prompt = (
        f"User Question:\n{question}\n\n"
        f"Relevant Clauses:\n{formatted_clauses}\n\n"
        "Answer the user's question in plain English. If a rule is unclear or unspecified, state that clearly."
    )

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.2,
        max_tokens=600
    )

    final_answer = response.choices[0].message["content"]

    if output_format == "json":
        return {
            "question": question,
            "answer": final_answer,
            "matches": clauses,
            "mode": mode,
            "format": "json"
        }

    return f"{final_answer}\n\n---\n{formatted_clauses}"
