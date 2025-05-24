import openai
import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

# Initialize API keys
openai.api_key = os.getenv("OPENAI_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(supabase_url, supabase_key)

def format_clauses_for_prompt(clauses):
    formatted = []
    for idx, clause in enumerate(clauses[:5], 1):
        entry = (
            f"{idx}. [{clause['citation']}]({clause['link']})\n"
            f"Summary: {clause['summary']}\n"
            f"Text: {clause['original_text']}\n"
        )
        formatted.append(entry)
    return "\n".join(formatted)

def build_gpt_prompt(question, clause_text):
    return f"""
You are an HOA policy assistant. Based on the provided clause data, answer the resident's question in clear, friendly, and accurate language.

Resident Question:
{question}

Relevant Clauses:
{clause_text}

Instructions:
- If any clauses clearly answer the question, explain the rule using plain English.
- Cite specific articles using: **"[citation]"**
- If no rule directly applies, say so clearly.
- End with: “Let us know if you need help with forms or next steps.”

Final Answer:
"""

def fetch_matching_clauses(question):
    embedding_response = openai.Embedding.create(
        input=question,
        model="text-embedding-ada-002"
    )
    query_embedding = embedding_response["data"][0]["embedding"]

    response = supabase.rpc("match_clauses", {
        "query_embedding": query_embedding,
        "match_threshold": 0.78,
        "match_count": 5
    }).execute()

    if not response.data:
        return []

    return response.data

def answer_question(question):
    clauses = fetch_matching_clauses(question)
    if not clauses:
        return "There are no specific HOA rules found that address this question directly. Please consult the board for further guidance."

    clause_text = format_clauses_for_prompt(clauses)
    prompt = build_gpt_prompt(question, clause_text)

    gpt_response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert HOA assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.4
    )

    return gpt_response["choices"][0]["message"]["content"]

