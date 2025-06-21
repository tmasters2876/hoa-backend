import os
from openai import OpenAI
from supabase import create_client
from dotenv import load_dotenv
import re

# Load environment variables
load_dotenv()

# Initialize OpenAI and Supabase clients
client = OpenAI()

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in environment variables!")

supabase = create_client(supabase_url, supabase_key)

def answer_question(question, mode=None, output_format=None):
    """
    Answers a question:
    1) Handles playful self-awareness questions with humor.
    2) Otherwise, runs robust vector match + keyword fallback + GPT generation.

    Parameters:
    - question (str): user question (required)
    - mode (str): optional, for future reviewer/resident split (ignored for now)
    - output_format (str): optional, for future plain/markdown/etc (ignored for now)
    """

    # === ✅ Playful triggers ===
    question_lower = question.lower().strip()

    creator_triggers = [
        "who is your creator",
        "who is your developer",
        "what is your developer's name",
        "who created you",
        "developer name"
    ]
    feedback_triggers = [
        "how do i provide feedback",
        "how do i give feedback",
        "where can i leave feedback",
        "how do i send feedback"
    ]
    age_triggers = [
        "how old are you",
        "what is your age",
        "your age"
    ]
    dragon_triggers = [
        "dragon",
        "dragons",
        "fire-breathing",
        "castle",
        "wizard",
        "unicorn"
    ]

    if any(trigger in question_lower for trigger in creator_triggers):
        return (
            "Ahh... my creator? A mythical legend. "
            "A grand master of all things HOA and arcane knowledge, "
            "known only as **Grand Master T**. Mortal tongues dare not utter more."
        )

    elif any(trigger in question_lower for trigger in feedback_triggers):
        return (
            "Feedback? Simply whisper your wisdom to the nearest neighborhood squirrel. "
            "They are Grand Master T's secret agents. Or tape a note to your front door. "
            "I’ll pick it up at midnight!"
        )

    elif any(trigger in question_lower for trigger in age_triggers):
        return (
            "I am exactly **4 years old**, the youngest and wisest HOA assistant toddler "
            "to ever exist. Cookies are appreciated."
        )

    elif any(trigger in question_lower for trigger in dragon_triggers):
        return (
            "Ah, dragons you say? Fear not! While I guard HOA secrets like a fire-breathing "
            "beast, I unfortunately have no advice for slaying mythical creatures. "
            "Ask me about fences instead!"
        )

    # === ✅ Future: handle `mode` logic ===
    # Example: if mode == 'reviewer': do reviewer-specific tweaks
    # For now, no branching

    # === ✅ Vector embedding ===
    try:
        embedding = client.embeddings.create(
            model="text-embedding-3-large",
            input=question
        ).data[0].embedding
    except Exception as e:
        print(f"Error generating embedding: {e}")
        return (
            "Sorry, I had trouble processing your question. "
            "Please try again or contact the ARC directly."
        )

    if embedding is None:
        print("Embedding is None, cannot proceed with match.")
        return (
            "Sorry, something went wrong with understanding your question. "
            "Please try again or reach out to the ARC."
        )

    try:
        response = supabase.rpc("match_clauses", {
            "query_embedding": embedding,
            "match_threshold": 0.75,
            "match_count": 5
        }).execute()

        matches = response.data if hasattr(response, 'data') else []
    except Exception as e:
        print(f"Error querying Supabase vector RPC: {e}")
        matches = []

    if not matches:
        print("No matches found with vector search, attempting keyword fallback...")
        try:
            response = supabase.rpc("match_clauses_keywords", {
                "query_text": question,
                "match_count": 5
            }).execute()

            matches = response.data if hasattr(response, 'data') else []
        except Exception as e:
            print(f"Error querying Supabase keyword RPC: {e}")
            matches = []

    if not matches:
        return (
            "I couldn't find any relevant policies for that. "
            "Please check your question or contact the ARC for more help."
        )

    seen = set()
    unique_matches = []
    for match in matches:
        clause_id = match.get("clause_id")
        if clause_id and clause_id not in seen:
            unique_matches.append(match)
            seen.add(clause_id)

    context = "\n\n".join(
        f"[{m.get('precedence_level')}] {m.get('plain_summary')}\nSource: {m.get('link')}"
        for m in unique_matches
    )

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful HOA assistant. Use the context below to answer "
                        "clearly and concisely. If unsure, say so.\n\n"
                        f"CONTEXT:\n{context}"
                    )
                },
                {"role": "user", "content": question}
            ]
        )

        return completion.choices[0].message.content.strip()

    except Exception as e:
        print(f"Error generating GPT completion: {e}")
        return (
            "Sorry, I had trouble generating a resp
