from flask_cors import CORS, cross_origin
from flask import Flask, request, jsonify
from ask_gpt import answer_question
from services import get_supabase_client
import os
import requests  # For logging to Google Sheets

app = Flask(__name__)
CORS(app)


def _log_resident_question(question, answer_text, meta, mode, output_format):
    """Best-effort insert into resident_questions. Must NEVER break /ask:
    every failure is swallowed. Kill switch: ENABLE_QUESTION_LOG=false in
    Render env vars disables it instantly, no redeploy. No IP is stored
    (owner decision, July 2026)."""
    if os.getenv("ENABLE_QUESTION_LOG", "true").lower() == "false":
        return
    try:
        get_supabase_client().from_("resident_questions").insert({
            "question": question,
            "answer": answer_text,
            "cited_clause_ids": meta.get("cited_ids") or [],
            "prefilter_used": meta.get("prefilter_used"),
            "prefilter_clause_count": meta.get("prefilter_clause_count"),
            "whimsy": bool(meta.get("whimsy")),
            "mode": mode,
            "output_format": output_format,
        }).execute()
    except Exception as e:
        print(f"[question-log] insert failed (answer still returned): {e}")


@app.route("/ask", methods=["POST"])
def ask():
    try:
        data = request.get_json()
        question = data.get("question", "")
        mode = data.get("mode", "default")
        tags = data.get("tags", [])
        output_format = data.get("output_format", "markdown")

        meta = {}
        result = answer_question(
            question=question,
            mode=mode,
            tags=tags,
            output_format=output_format,
            meta_out=meta
        )

        answer_text = result.get("answer", "") if isinstance(result, dict) else result
        _log_resident_question(question, answer_text, meta, mode, output_format)

        if output_format == "json":
            return jsonify(result)
        else:
            return result, 200, {"Content-Type": "text/markdown"}

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/log", methods=["POST"])
@cross_origin()  # ✅ Enables CORS for Carrd's POST logging
def log_to_google_sheets():
    try:
        data = request.json
        payload = {
            "question": data.get("question", ""),
            "answer": data.get("answer", ""),
            "ip": data.get("ip", "N/A")
        }
        res = requests.post(
            "https://script.google.com/macros/s/AKfycbxMrY1STPSvA4xC96xxiDHXo08YRGYWp6_BqJ6qNKkz0OnOhUJDH9O8o7O5jecIPbmU/exec",
            json=payload
        )
        return {"status": "logged", "code": res.status_code}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"✅ Server starting on port {port}...")
    app.run(debug=False, host="0.0.0.0", port=port)

