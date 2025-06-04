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

# Format clauses for GPT prompt
def format_clauses_for_prompt(clauses):
    grouped = defaultdict(list)
    for clause in clauses:
        grouped[clause.get("document", "Other")].append(clause)

    formatted = []
    idx = 1
    for doc, group in grouped.items():
        for c in group:
            citation = c.get("citation", f"Clause {idx}")
            link = c.get("link", "")
            summary = c.get("plain_summary", "No summary provided.")
            source = c.get("match_source", "Unknown")
            clause_id = c.get("clause_id", "")

            # ✅ Final link logic – trust existing shared link as-is
            if citation and link:
                link_html = f'<a href="{link}" target="_blank" rel="noopener noreferrer">{citation}</a>'
            else:
                link_html = citation

            entry = (
                f"<b>{idx}. <strong>Summary of Clause</strong>: According to {link_html}, {summary}.</b><br>"
                f"<strong>Match Source</strong>: {source} • "
                f"<code>{doc}</code> • "
                f"<strong>Reviewer ID</strong>: <code>{clause_id}</code><br>"
            )
            formatted.append(entry)
            idx += 1

    return "<br><br>".join(formatted)

# Prompt generator
def build_gpt_prompt(question, clause_text, no_matches=False):
    fallback_msg = (
        "⚠️ There were no direct matches to this question. Below are general HOA rules that might still help you respond.<br><br>"
        if no_matches else ""
    )

    return f"""You are an HOA policy assistant. Based on the provided Clause data, answer the resident’s question in clear, friendly, and accurate language.

Resident Question:
{question}

{fallback_msg}
Below are relevant Clause matches:
{clause_text}

Write your response in this format:
1. Brief summary of each Clause that might apply
2. State whether the rules clearly answer the question
3. If unclear, suggest checking with the ARC
4. Always close with: “If you have any other questions, feel free to ask!”

Use HTML for citations like this: <a href=\"link\" target=\"_blank\">Art. VI</a>

---

Final Answer:
"""

# Vector + tag fallback matching
def fetch_matching_clauses(question, tags=None, structure_type=None, concern_level=None):
    embedding_response = client.embeddings.create(
        model="text-embedding-ada-002",
        input=question,
    )
    query_embedding = embedding_response.data[0].embedding

    response = supabase.rpc("match_clauses", {
        "query_embedding": query_embedding,
        "match_threshold": 0.8,
        "match_count": 5
    }).execute()

    vector_matches = response.data or []
    for clause in vector_matches:
        clause["match_source"] = "Vector Match"
        clause["clause_id"] = clause.get("clause_id") or clause.get("id")

    # Step 2: fallback if needed
    if len(vector_matches) < 5:
        query = supabase.from_("clauses").select("*").ilike("plain_summary", f"%{question}%")
        if tags:
            query = query.contains("tags", tags)
        if structure_type:
            query = query.eq("structure_type", structure_type)
        if concern_level:
            query = query.eq("concern_level", concern_level)
        fallback_matches = query.limit(5).execute()
        for clause in fallback_matches.data or []:
            clause["match_source"] = "Keyword Fallback"
            clause["clause_id"] = clause.get("clause_id") or clause.get("id")
        vector_matches += fallback_matches.data or []

    return vector_matches

# Soft fallback if nothing matches
def fetch_soft_fallback_clauses():
    general_tags = ["approval", "structure", "location", "visibility", "placement"]
    query = supabase.from_("clauses").select("*").contains("tags", general_tags).limit(5)
    result = query.execute()
    for clause in result.data or []:
        clause["match_source"] = "General Soft Fallback"
        clause["clause_id"] = clause.get("clause_id") or clause.get("id")
    return result.data

# Main endpoint logic
def answer_question(question, tags=None, mode="default", structure_type=None, concern_level=None, output_format="markdown"):
    raw_clauses = fetch_matching_clauses(
        question,
        tags=tags,
        structure_type=structure_type,
        concern_level=concern_level
    )

    # De-dupe based on match_source
    unique_clauses = {}
    for clause in raw_clauses:
        cid = clause.get("clause_id")
        if cid not in unique_clauses and clause.get("match_source") == "Vector Match":
            unique_clauses[cid] = clause
    clauses = list(unique_clauses.values())

    no_matches = False
    if not clauses:
        clauses = fetch_soft_fallback_clauses()
        no_matches = True

    clause_text = format_clauses_for_prompt(clauses)
    prompt = build_gpt_prompt(question, clause_text, no_matches)

    # 🦝 Humor injection for whimsical questions
    whimsical_keywords = ["dragon", "castle", "moat", "wizard", "unicorn", "magic", "fortress", "fairy", "goblin"]
    if any(word in question.lower() for word in whimsical_keywords):
        prompt += (
            "\n\nNote: This question appears whimsical or fantastical (e.g., involving dragons or moats). "
            "Please respond with a brief, friendly touch of humor before returning to the HOA's real policies."
        )

    gpt_response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert HOA assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.4
    )

    final_answer = gpt_response.choices[0].message.content

    # ✅ Fix malformed markdown link formatting like [Page 13] (url)
    final_answer = re.sub(r"\[(.*?)\] \(", r"[\1](", final_answer)

    if output_format == "json":
        return {
            "question": question,
            "answer": final_answer,
            "clauses": clauses,
            "mode": mode,
            "format": "json"
        }

    return f"{final_answer}<br><br>{clause_text}"
