import os
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz

# Init Flask app
app = Flask(__name__)

# Initialize Firebase Admin SDK
cred = credentials.Certificate("D:\clg\dcet\serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Twilio WhatsApp client
TWILIO_SID = os.getenv("AC855e17bf8ba55babef13cc79bce47af2")
TWILIO_AUTH = os.getenv("51cd891deae37d7a8ed54aacc31284c1")
WHATSAPP_FROM = "whatsapp:+14027266682"  # Twilio sandbox
ADMIN_WA = os.getenv("ADMIN_WA", "whatsapp:+919741092786")
client = Client(TWILIO_SID, TWILIO_AUTH)

# Timezone for scheduler (adjust as needed)
TIMEZONE = pytz.timezone("Asia/Kolkata")

# Scheduler
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()


########## Helper functions ##########

def send_whatsapp(to, body):
    try:
        client.messages.create(body=body, from_=WHATSAPP_FROM, to=to)
    except Exception as e:
        print(f"Failed to send WhatsApp message to {to}: {e}")

def get_top_users(field="score", limit=10):
    users = db.collection("users").stream()
    user_list = []
    for doc in users:
        data = doc.to_dict()
        score = data.get(field, 0)
        if score > 0:
            user_list.append({
                "id": doc.id,
                "name": data.get("name", doc.id[-4:]),
                "score": score,
                "wa_number": data.get("wa_number", None)
            })
    user_list.sort(key=lambda x: x["score"], reverse=True)
    return user_list[:limit]

def format_leaderboard_message(title, users):
    lines = [f"*{title}*", ""]
    for i, u in enumerate(users, 1):
        lines.append(f"{i}. {u['name']} — {u['score']} pts")
    return "\n".join(lines)

def log_leaderboard(collection_name, doc_id, users):
    winners_ref = db.collection(collection_name).document(doc_id)
    data = {str(i+1): {"name": u["name"], "score": u["score"]} for i, u in enumerate(users)}
    winners_ref.set(data)

def notify_winners(users, daily=True):
    for i, u in enumerate(users):
        if not u.get("wa_number"):
            continue
        rank = i + 1
        if daily:
            msg = f"🎉 Congrats {u['name']}! You ranked #{rank} in today’s quiz with {u['score']} points!\nKeep going — tomorrow’s leaderboard awaits!"
        else:
            msg = f"🏆 Weekly Congrats {u['name']}!\nYou ranked #{rank} this week with {u['score']} points!\nLet’s aim higher next week! 🚀"
        send_whatsapp(u["wa_number"], msg)


########## Routes ##########

# Health check
@app.route("/")
def home():
    return "WhatsApp Quiz Bot API is running."


# Telegram admin commands to add/remove questions, update user wa_number, etc.

@app.route("/admin/add_question", methods=["POST"])
def add_question():
    data = request.json
    # Expected: {"question": "...", "options": [...], "answer": "...", "category": "..."}
    if not data or not all(k in data for k in ("question", "options", "answer")):
        return jsonify({"error": "Invalid data"}), 400
    doc_ref = db.collection("questions").document()
    doc_ref.set(data)
    return jsonify({"message": "Question added", "id": doc_ref.id})


@app.route("/admin/remove_question/<question_id>", methods=["DELETE"])
def remove_question(question_id):
    doc_ref = db.collection("questions").document(question_id)
    if doc_ref.get().exists:
        doc_ref.delete()
        return jsonify({"message": "Question deleted"})
    return jsonify({"error": "Question not found"}), 404


@app.route("/admin/update_user_number/<user_id>", methods=["POST"])
def update_user_number(user_id):
    data = request.json
    wa_number = data.get("wa_number")
    if not wa_number:
        return jsonify({"error": "Missing wa_number"}), 400
    user_ref = db.collection("users").document(user_id)
    if not user_ref.get().exists:
        return jsonify({"error": "User not found"}), 404
    user_ref.update({"wa_number": wa_number})
    return jsonify({"message": "User WhatsApp number updated"})


# Leaderboard APIs for manual trigger

@app.route("/daily_leaderboard")
def daily_leaderboard():
    # Get top users by daily_score
    top_users = get_top_users("daily_score")
    if not top_users:
        return "No daily scores yet.", 200

    # Send admin WhatsApp message
    msg = format_leaderboard_message("📊 Daily Top 10 (as of 9 PM)", top_users)
    send_whatsapp(ADMIN_WA, msg)

    # Log to Firestore
    today = datetime.utcnow().strftime("%Y-%m-%d")
    log_leaderboard("daily_winners", today, top_users)

    # Notify winners personally
    notify_winners(top_users, daily=True)

    return "Daily leaderboard sent & logged."


@app.route("/weekly_leaderboard")
def weekly_leaderboard():
    # Get top users by total score
    top_users = get_top_users("score")
    if not top_users:
        return "No scores yet.", 200

    # Send admin WhatsApp message
    msg = format_leaderboard_message("🏆 Weekly Top 10 (Final Scores)", top_users)
    send_whatsapp(ADMIN_WA, msg)

    # Log to Firestore
    week = datetime.utcnow().strftime("%Y-W%U")
    log_leaderboard("weekly_winners", week, top_users)

    # Notify winners personally
    notify_winners(top_users, daily=False)

    return "Weekly leaderboard sent & logged."


########## Scheduler jobs ##########

def reset_daily_scores():
    print("Resetting daily scores at 9:01 PM IST")
    users_ref = db.collection("users")
    users = users_ref.stream()
    for user in users:
        users_ref.document(user.id).update({"daily_score": 0})

def reset_weekly_scores():
    print("Resetting weekly scores at Sunday 9:05 PM IST")
    users_ref = db.collection("users")
    users = users_ref.stream()
    for user in users:
        users_ref.document(user.id).update({"score": 0})

def scheduled_daily_leaderboard():
    print("Running daily leaderboard job at 9 PM IST")
    with app.app_context():
        daily_leaderboard()

def scheduled_weekly_leaderboard():
    print("Running weekly leaderboard job at Sunday 9 PM IST")
    with app.app_context():
        weekly_leaderboard()

# Schedule daily leaderboard and reset
scheduler.add_job(scheduled_daily_leaderboard, 'cron', hour=21, minute=0)
scheduler.add_job(reset_daily_scores, 'cron', hour=21, minute=1)

# Schedule weekly leaderboard and reset (Sunday)
scheduler.add_job(scheduled_weekly_leaderboard, 'cron', day_of_week='sun', hour=21, minute=0)
scheduler.add_job(reset_weekly_scores, 'cron', day_of_week='sun', hour=21, minute=5)


########## Run server ##########

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
