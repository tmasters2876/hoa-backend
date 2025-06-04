import os
import re
import openai
from supabase import create_client
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(supabase_url, supabase_key)

# Precedence label map
def get_precedence_label(level):
    if level == 1:
        return "🏛️ State Law – Highest Authority"
    elif level == 2:
        return "🏛️ County Resolution – Legally Binding"
    elif level == 3:
        return "📜 Declaration – Foundational HOA Rule"
    elif level == 4:
        return "📘 Amendment – Overrides Prior Rules"
    elif level == 5:
        return "📁 Corporate Docs – Internal Governance"
    elif level == 6:
        return "📄 Board Resolution – Board-Enforced Policy"
    elif level == 7:
        return "📝 Builder Guideline – Design-Only Reference"
    elif level == 8:
        return "🔧 ARC Note – Lowest Authority"
    return "📎 Unknown Source"

# Format clauses for GPT prompt
def format_clauses_for_prompt(clauses):
    grouped = defaultdict(list)
    for clause in clauses:
        grouped[clause.get("document", "Other")].append(clause)

    # Sort all clauses by precedence
    sorted_clauses = sorted(clauses, key=lambda x: x.get("precedence_level", 99))

    formatted = []
    idx = 1
    for clause in sorted_clauses:
        citation = clause.get("citation", f"[Clause {idx}]")
        summary = clause.get("plain_summary", "No summary provided.")
        source = clause.get("match_source", "Unknown")
        clause_id = clause.get("clause_id", "")
        precedence_label = get_precedence_label(clause.get("precedence_level"))

        if citation and citation.startswith("http"):
            link_html = f'<a href="{citation}" target="_blank" rel="noopener noreferrer">[citation]</a>'
        else:
            link_html = citation

        entry = (
            f"{link_html}<br>"
            f"<strong>{precedence_label}</strong><br>"
            f"<strong>Summary of Clause:</strong> {summary}<br>"
            f"<em><strong>Matched Source:</strong> {source}</em><br>"
            f"<code><strong>Reviewer ID:</strong> {clause_id}</code><br><br>"
        )
        formatted.append(entry)
        idx += 1

    return "\n".join(formatted)

# Prompt builder
def build_gpt_prompt(question, clause_text, no_matches=False):
    fallback_msg = (
        "📎 There were no direct matches to this question. "
        "Below are general HOA rules that might still help you respond.<br><br>"
        if no_matches else ""
    )

    return f"""You are an HOA policy assistant. Based on the provided Clause Data, answer the resident's question in clear, friendly, and accurate language.

{fallback_msg}

📍 Resident Question:  
{question}

🧠 Write your response in this format:  
1. Brief summary of each Clause that might apply  
2. State whether the rules clearly answer the question  
3. If unclear, suggest checking with the ARC  
4. Always close with: "If you have any other questions, feel free to ask!"

Use HTML for citations like this: `<a href="link" target="_blank">Link</a>`.
{clause_text}
"""

# Vector + fallback clause matcher
def fetch_matching_clauses(question, tags=None, structure_type=None, concern_level=None):
    embedding_response = client.embeddings.create(
        model="text-embedding-ada-002",
        input=question
    )
    query_embedding = embedding_response.data[0].embedding

    response = supabase.rpc('match_clauses', {
        "query_embedding": query_embedding,
        "match_threshold": 0.82,
        "match_count": 8
    }).execute()

    vector_matches = response.data or []
    if vector_matches:
        unique_clauses = {}
        for clause in vector_matches:
            clause["match_source"] = "Vector Match"
            unique_clauses[clause["clause_id"]] = clause
        return list(unique_clauses.values())

    # Soft fallback if vector returns nothing
    query = supabase.from_("clauses").select("*")
    if tags: query = query.contains("tags", tags)
    if structure_type: query = query.eq("structure_type", structure_type)
    if concern_level: query = query.eq("concern_level", concern_level)

    fallback_matches = query.limit(15).execute().data
    for clause in fallback_matches:
        clause["match_source"] = "Keyword Fallback"
    return fallback_matches

# Main entry point
def answer_question(question, tags=None, mode="default", structure_type=None, concern_level=None, output_format="markdown"):
    raw_clauses = fetch_matching_clauses(
        question, tags=tags, structure_type=structure_type, concern_level=concern_level
    )

    clause_text = format_clauses_for_prompt(raw_clauses)
    no_matches = len(raw_clauses) == 0

    # Whimsy detection
    whimsy_keywords = ['dragon', 'castle', 'moat', 'wizard', 'unicorn', 'magic', 'fortress', 'fairy', 'goblin']
    if any(word in question.lower() for word in whimsy_keywords):
        clause_text = (
            "🧚Note: This question appears whimsical or fantastical (e.g., involving dragons or moats).<br>"
            "We're responding with a brief, friendly touch of humor before returning to the HOA's real policies.<br><br>"
        ) + clause_text

    prompt = build_gpt_prompt(question, clause_text, no_matches)

    gpt_response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert HOA assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.4
    )

    final_answer = gpt_response.choices[0].message.content

    # Format response safely
    if output_format == "json":
        return {
            "question": question,
            "answer": final_answer,
            "mode": mode,
            "output_format": "json"
        }

    return f"{final_answer}<br><br>{clause_text}"
