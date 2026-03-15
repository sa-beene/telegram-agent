import os
import json
import psycopg2
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic
from github import Github
import openai
import tempfile

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

BACKGROUND_UPDATE_EVERY = 5  # run background MD check every N messages

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id BIGINT PRIMARY KEY,
            message_count INT DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
        (user_id, role, content)
    )
    cur.execute("""
        INSERT INTO user_state (user_id, message_count) VALUES (%s, 1)
        ON CONFLICT (user_id) DO UPDATE SET message_count = user_state.message_count + 1
    """, (user_id,))
    conn.commit()
    conn.close()

def get_message_count(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT message_count FROM user_state WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

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
    if os.path.exists("profile.md"):
        with open("profile.md", "r") as f:
            context += f.read() + "\n\n"
    if os.path.exists("context"):
        for filename in sorted(os.listdir("context")):
            if filename.endswith(".md"):
                with open(f"context/{filename}", "r") as f:
                    context += f.read() + "\n\n"
    if os.path.exists("skills"):
        for filename in sorted(os.listdir("skills")):
            if filename.endswith(".md"):
                with open(f"skills/{filename}", "r") as f:
                    context += f.read() + "\n\n"
    return context

def list_md_files():
    files = []
    if os.path.exists("profile.md"):
        files.append("profile.md")
    if os.path.exists("context"):
        for filename in sorted(os.listdir("context")):
            if filename.endswith(".md"):
                files.append(f"context/{filename}")
    if os.path.exists("skills"):
        for filename in sorted(os.listdir("skills")):
            if filename.endswith(".md"):
                files.append(f"skills/{filename}")
    return files

def read_md_files():
    md_contents = {}
    for f in list_md_files():
        with open(f, "r") as file:
            md_contents[f] = file.read()
    return md_contents

def update_md_file(filepath, new_content):
    dirpath = os.path.dirname(filepath)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)

    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(GITHUB_REPO)
    try:
        file = repo.get_contents(filepath)
        repo.update_file(
            filepath,
            f"Update {filepath} via Telegram",
            new_content,
            file.sha
        )
    except Exception:
        repo.create_file(
            filepath,
            f"Create {filepath} via Telegram",
            new_content
        )
    with open(filepath, "w") as f:
        f.write(new_content)

def is_explicit_update_request(message):
    keywords = [
        "update my", "change my", "edit my", "modify my", "fix my",
        "tone down", "update the", "change the", "let's update",
        "lets update", "update profile", "update context"
    ]
    return any(kw in message.lower() for kw in keywords)

async def run_background_md_update(update: Update, user_id: int, history: list):
    """Silently checks recent conversation and updates MDs if anything notable came up."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    md_contents = read_md_files()

    recent_messages = history[-BACKGROUND_UPDATE_EVERY:]
    conversation_snippet = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in recent_messages])

    prompt = f"""You are maintaining a personal knowledge base for the user. Here are their current MD files:

{chr(10).join([f"=== {path} ==={chr(10)}{content}" for path, content in md_contents.items()])}

Here is the recent conversation:
{conversation_snippet}

Your job: scan the conversation for any new facts, preferences, people, situations, or context worth saving.
Only save things that are genuinely new or that update/correct existing info — ignore small talk and things already captured.

If there's nothing worth saving, respond with exactly: NOTHING

If there is something worth saving, respond with a JSON array of updates in this format:
[
  {{
    "filepath": "profile.md",
    "new_content": "<full updated content of the file>"
  }}
]

Only return the JSON array or NOTHING. No explanation."""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )

    result = response.content[0].text.strip()

    if result == "NOTHING":
        return

    try:
        updates = json.loads(result)
        updated_files = []
        for u in updates:
            update_md_file(u["filepath"], u["new_content"])
            updated_files.append(f"`{u['filepath']}`")
        if updated_files:
            await update.message.reply_text(f"📝 Saved to {', '.join(updated_files)}")
    except Exception:
        pass  # Silent fail — don't interrupt the conversation

async def run_explicit_md_update(update: Update, user_message: str):
    """Handles explicit update requests like 'let's update my profile'."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    md_contents = read_md_files()

    prompt = f"""The user wants to update their personal knowledge base. Here are their current MD files:

{chr(10).join([f"=== {path} ==={chr(10)}{content}" for path, content in md_contents.items()])}

The user said: "{user_message}"

Figure out which file(s) need updating and rewrite them incorporating the user's request.
You may update multiple files if needed, or create a new one if appropriate.

Respond with a JSON array in this format:
[
  {{
    "filepath": "profile.md",
    "new_content": "<full updated content>"
  }}
]

Only return the JSON array. No explanation."""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )

    result = response.content[0].text.strip()

    try:
        updates = json.loads(result)
        updated_files = []
        for u in updates:
            update_md_file(u["filepath"], u["new_content"])
            updated_files.append(f"`{u['filepath']}`")
        return f"✅ Updated {', '.join(updated_files)}"
    except Exception as e:
        return f"⚠️ Something went wrong updating your files: {e}"

async def transcribe_voice(file_bytes: bytes) -> str:
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        return transcript.text
    finally:
        os.unlink(tmp_path)

async def process_message(update: Update, user_id: int, user_message: str):
    save_message(user_id, "user", user_message)
    history = load_history(user_id)
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    if is_explicit_update_request(user_message):
        reply = await run_explicit_md_update(update, user_message)
    else:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=load_context(),
            messages=history
        )
        reply = response.content[0].text

        # Background MD update every N user messages
        count = get_message_count(user_id)
        if count % BACKGROUND_UPDATE_EVERY == 0:
            await run_background_md_update(update, user_id, history)

    save_message(user_id, "assistant", reply)
    await update.message.reply_text(reply)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await process_message(update, user_id, update.message.text)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text("🎙️ Transcribing...")

    voice = update.message.voice or update.message.audio
    tg_file = await context.bot.get_file(voice.file_id)
    file_bytes = await tg_file.download_as_bytearray()

    transcript = await transcribe_voice(bytes(file_bytes))
    await update.message.reply_text(f"📝 _{transcript}_", parse_mode="Markdown")

    await process_message(update, user_id, transcript)

init_db()
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
app.run_polling()
