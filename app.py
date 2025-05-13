from flask import Flask, request, jsonify
from ask_gpt import answer_question

app = Flask(__name__)

@app.route('/answer', methods=['POST'])
def get_answer():
    data = request.json
    question = data.get("question")
    if not question:
        return jsonify({"error": "No question provided"}), 400
    answer = answer_question(question)
    return jsonify({"answer": answer})

@app.route('/')
def index():
    return "✅ HOA Assistant Backend is Running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
