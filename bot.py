import os
import psycopg2
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic
from github import Github

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

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

def list_md_files():
    files = ["profile.md"]
    for filename in os.listdir("skills"):
        if filename.endswith(".md"):
            files.append(f"skills/{filename}")
    return files

def update_md_file(filepath, new_content):
    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(GITHUB_REPO)
    file = repo.get_contents(filepath)
    repo.update_file(
        filepath,
        f"Update {filepath} via Telegram",
        new_content,
        file.sha
    )
    with open(filepath, "w") as f:
        f.write(new_content)

def is_update_request(message):
    keywords = ["update my", "change my", "edit my", "modify my", "fix my", "tone down", "update the", "change the"]
    return any(kw in message.lower() for kw in keywords)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    save_message(user_id, "user", user_message)
    history = load_history(user_id)

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    if is_update_request(user_message):
        md_files = list_md_files()
        md_contents = {}
        for f in md_files:
            with open(f, "r") as file:
                md_contents[f] = file.read()

        update_prompt = f"""The user wants to update one of their MD files. Here are the current files:

{chr(10).join([f"=== {path} ==={chr(10)}{content}" for path, content in md_contents.items()])}

The user said: "{user_message}"

Figure out which file needs updating and rewrite it incorporating the user's feedback. 
Respond in this exact format:
FILE: <filepath>
CONTENT:
<full new content of the file>"""

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": update_prompt}]
        )

        reply_text = response.content[0].text
        lines = reply_text.strip().split("\n")
        filepath = lines[0].replace("FILE: ", "").strip()
        content_start = next(i for i, l in enumerate(lines) if l == "CONTENT:") + 1
        new_content = "\n".join(lines[content_start:])

        update_md_file(filepath, new_content)
        reply = f"✅ Updated `{filepath}`. Here's what changed:\n\n{new_content[:500]}..."

    else:
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