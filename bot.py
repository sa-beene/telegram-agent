import os
import psycopg2
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor().execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor().execute(
        "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
        (user_id, role, content)
    )
    conn.commit()
    conn.close()

def load_history(user_id, limit=20):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM messages WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s",
        (user_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

def load_context():
    context = ""
    with open("profile.md", "r") as f:
        context += f.read() + "\n\n"
    skills_dir = "skills"
    for filename in os.listdir(skills_dir):
        if filename.endswith(".md"):
            with open(f"{skills_dir}/{filename}", "r") as f:
                context += f.read() + "\n\n"
    return context

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    save_message(user_id, "user", user_message)
    history = load_history(user_id)

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=load_context(),
        messages=history
    )

    reply = response.content[0].text
    save_message(user_id, "assistant", reply)

    await update.message.reply_text(reply)

init_db()
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.run_polling()