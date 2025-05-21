import os
import json
import asyncio
from flask import Flask, request, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import firebase_admin
from firebase_admin import credentials, firestore
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from threading import Lock
from dotenv import load_dotenv
import telegram

load_dotenv()

# Init Firebase
cred_path = os.getenv("FIREBASE_CREDENTIALS_JSON_PATH")
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Init Twilio Client
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_whatsapp_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
twilio_client = Client(account_sid, auth_token)

# Telegram admin bot
telegram_token = os.getenv("TELEGRAM_ADMIN_TOKEN")
admin_user_id = int(os.getenv("ADMIN_USER_ID"))
tg_bot = telegram.Bot(token=telegram_token)

# Flask app
app = Flask(__name__)

# Scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Lock for concurrency
lock = Lock()

# Constants
QUESTION_TIMER = 60  # seconds per question
MAX_QUESTIONS_PER_GAME = 10

# --- Helper functions ---


def send_whatsapp_message(to, body):
    message = twilio_client.messages.create(
        from_=twilio_whatsapp_number,
        body=body,
        to=to,
    )
    return message.sid


def fetch_question(question_id):
    doc = db.collection("questions").document(question_id).get()
    if doc.exists:
        return doc.to_dict()
    return None


def fetch_random_question(exclude_ids):
    # Simple firestore query to get random question excluding used ones
    # (Firestore does not support random directly; workaround: fetch all and filter in code)
    questions_ref = db.collection("questions")
    docs = questions_ref.stream()
    questions = [doc.to_dict() | {"id": doc.id} for doc in docs if doc.id not in exclude_ids]

    if not questions:
        return None
    import random
    return random.choice(questions)


def update_user_progress(user_id, question_id, answer=None, correct=None):
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    if user_doc.exists:
        data = user_doc.to_dict()
    else:
        data = {
            "answered_questions": [],
            "score": 0,
            "daily_scores": {},  # date: score
            "weekly_score": 0,
            "last_played": None,
        }

    if question_id not in data["answered_questions"]:
        data["answered_questions"].append(question_id)
        if correct is True:
            data["score"] += 1
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            data["daily_scores"][today_str] = data["daily_scores"].get(today_str, 0) + 1
            data["weekly_score"] += 1

    data["last_played"] = datetime.utcnow()
    user_ref.set(data)


def reset_weekly_scores():
    users_ref = db.collection("users")
    users = users_ref.stream()
    for user in users:
        data = user.to_dict()
        data["weekly_score"] = 0
        # keep daily_scores as is
        users_ref.document(user.id).set(data)
    print("Weekly scores reset done.")


def get_daily_leaderboard(top_n=10):
    users_ref = db.collection("users")
    users = users_ref.stream()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    leaderboard = []
    for user in users:
        data = user.to_dict()
        daily_score = data.get("daily_scores", {}).get(today_str, 0)
        leaderboard.append((user.id, daily_score))
    leaderboard.sort(key=lambda x: x[1], reverse=True)
    return leaderboard[:top_n]


def get_weekly_leaderboard():
    users_ref = db.collection("users")
    users = users_ref.stream()
    leaderboard = []
    for user in users:
        data = user.to_dict()
        leaderboard.append((user.id, data.get("weekly_score", 0)))
    leaderboard.sort(key=lambda x: x[1], reverse=True)
    return leaderboard[:10]


def format_leaderboard(leaderboard, title):
    lines = [f"🏆 {title} Leaderboard 🏆\n"]
    for idx, (user_id, score) in enumerate(leaderboard, 1):
        lines.append(f"{idx}. User {user_id}: {score} points")
    return "\n".join(lines)


# --- Scheduler jobs ---


def send_daily_leaderboard():
    leaderboard = get_daily_leaderboard()
    message = format_leaderboard(leaderboard, "Daily")
    # send message to WhatsApp group (replace with your group number)
    whatsapp_group = os.getenv("WHATSAPP_GROUP_NUMBER")
    if whatsapp_group:
        send_whatsapp_message(whatsapp_group, message)
        print("Sent daily leaderboard.")


def send_weekly_leaderboard():
    leaderboard = get_weekly_leaderboard()
    message = format_leaderboard(leaderboard, "Weekly")
    whatsapp_group = os.getenv("WHATSAPP_GROUP_NUMBER")
    if whatsapp_group:
        send_whatsapp_message(whatsapp_group, message)
        print("Sent weekly leaderboard.")


# Schedule daily leaderboard at 9 PM UTC (adjust timezone as needed)
scheduler.add_job(send_daily_leaderboard, "cron", hour=21, minute=0)

# Schedule weekly leaderboard reset and message on Sunday 9 PM UTC
scheduler.add_job(send_weekly_leaderboard, "cron", day_of_week="sun", hour=21, minute=0)
scheduler.add_job(reset_weekly_scores, "cron", day_of_week="sun", hour=21, minute=1)


# --- Flask endpoints ---

# For WhatsApp webhook


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from twilio.twiml.messaging_response import MessagingResponse

    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From")

    resp = MessagingResponse()
    msg = resp.message()

    # Identify user ID by phone number
    user_id = from_number.replace("whatsapp:", "")

    # Fetch user progress
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    if user_doc.exists:
        user_data = user_doc.to_dict()
    else:
        user_data = {
            "answered_questions": [],
            "score": 0,
            "daily_scores": {},
            "weekly_score": 0,
            "last_played": None,
            "current_question_id": None,
            "current_question_start": None,
            "current_question_end": None,
        }
        user_ref.set(user_data)

    # Game state:
    # If user is waiting for answer to current question and timer not expired
    # If no current question, send new question

    now = datetime.utcnow()

    # Check if there's a current question and time left
    start = user_data.get("current_question_start")
    end = user_data.get("current_question_end")
    current_qid = user_data.get("current_question_id")

    # Parse datetime strings
    if start:
        start = datetime.fromisoformat(start)
    if end:
        end = datetime.fromisoformat(end)

    if current_qid and end and now < end:
        # We are inside the timer window, check answer
        question = fetch_question(current_qid)
        correct_answer = question.get("answer", "").lower()
        if incoming_msg.lower() == correct_answer:
            # Correct answer
            update_user_progress(user_id, current_qid, incoming_msg, True)
            msg.body("🎉 Correct! Your score has been updated.\nSend 'next' for next question.")
            # Clear current question to force next question on 'next'
            user_ref.update({
                "current_question_id": None,
                "current_question_start": None,
                "current_question_end": None,
            })
        elif incoming_msg.lower() == "next":
            # Send new question
            next_question = fetch_random_question(user_data.get("answered_questions", []))
            if not next_question:
                msg.body("No more questions available. Thanks for playing!")
                # Clear current question
                user_ref.update({
                    "current_question_id": None,
                    "current_question_start": None,
                    "current_question_end": None,
                })
            else:
                start_time = datetime.utcnow()
                end_time = start_time + timedelta(seconds=QUESTION_TIMER)
                user_ref.update({
                    "current_question_id": next_question["id"],
                    "current_question_start": start_time.isoformat(),
                    "current_question_end": end_time.isoformat(),
                })
                qtext = next_question["question"]
                options = "\n".join(next_question.get("options", []))
                msg.body(f"🕑 You have {QUESTION_TIMER} seconds to answer:\n{qtext}\nOptions:\n{options}")
        else:
            msg.body(f"❌ Wrong answer or invalid input. Try again or send 'next' for next question.")
    else:
        # Timer expired or no current question - send new question
        next_question = fetch_random_question(user_data.get("answered_questions", []))
        if not next_question:
            msg.body("No more questions available. Thanks for playing!")
            user_ref.update({
                "current_question_id": None,
                "current_question_start": None,
                "current_question_end": None,
            })
        else:
            start_time = datetime.utcnow()
            end_time = start_time + timedelta(seconds=QUESTION_TIMER)
            user_ref.update({
                "current_question_id": next_question["id"],
                "current_question_start": start_time.isoformat(),
                "current_question_end": end_time.isoformat(),
            })
            qtext = next_question["question"]
            options = "\n".join(next_question.get("options", []))
            msg.body(f"🕑 You have {QUESTION_TIMER} seconds to answer:\n{qtext}\nOptions:\n{options}")

    return str(resp)


# Telegram admin bot commands (simple webhook simulation using Flask route)
# In production, better to use python-telegram-bot or polling

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    update = telegram.Update.de_json(request.get_json(force=True), tg_bot)
    message = update.message
    if not message:
        return "ok"

    user_id = message.from_user.id
    text = message.text

    # Only admin allowed
    if user_id != admin_user_id:
        tg_bot.send_message(chat_id=user_id, text="Unauthorized.")
        return "ok"

    # Commands: /addquestion, /removequestion, /broadcast

    if text.startswith("/addquestion"):
        # Format:
        # /addquestion {"question": "...", "options": ["A","B","C","D"], "answer": "A"}
        try:
            json_str = text[len("/addquestion "):]
            qdata = json.loads(json_str)
            doc_ref = db.collection("questions").document()
            doc_ref.set(qdata)
            tg_bot.send_message(chat_id=user_id, text=f"Question added with ID: {doc_ref.id}")
        except Exception as e:
            tg_bot.send_message(chat_id=user_id, text=f"Error adding question: {e}")

    elif text.startswith("/removequestion"):
        # Format: /removequestion <question_id>
        try:
            qid = text.split(" ")[1]
            db.collection("questions").document(qid).delete()
            tg_bot.send_message(chat_id=user_id, text=f"Question {qid} removed.")
        except Exception as e:
            tg_bot.send_message(chat_id=user_id, text=f"Error removing question: {e}")

    elif text.startswith("/broadcast"):
        # Format: /broadcast your message here
        msg_to_send = text[len("/broadcast "):]
        # Broadcast to all users via WhatsApp
        users = db.collection("users").stream()
        count = 0
        for user in users:
            user_id_ = user.id
            try:
                send_whatsapp_message(f"whatsapp:+{user_id_}", msg_to_send)
                count += 1
            except Exception as e:
                print(f"Error sending to {user_id_}: {e}")
        tg_bot.send_message(chat_id=user_id, text=f"Broadcast sent to {count} users.")

    else:
        tg_bot.send_message(chat_id=user_id, text="Unknown command.")

    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
