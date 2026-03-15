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

BACKGROUND_UPDATE_EVERY = 5

MODES = {
    "journaling": {"file": "skills/journaling.md", "emoji": "📓", "triggers": ["journaling mode", "journal mode", "lets journal", "switch to journaling", "journaling"]},
    "research": {"file": "skills/research.md", "emoji": "🔍", "triggers": ["research mode", "lets research", "researcher mode", "switch to research"]},
    "tasks": {"file": "skills/tasks.md", "emoji": "✅", "triggers": ["tasks mode", "task mode", "lets do tasks", "assistant mode", "switch to tasks"]},
    "finance": {"file": "skills/finance.md", "emoji": "📊", "triggers": ["finance mode", "financial mode", "lets talk finance", "switch to finance"]},
    "chat": {"file": None, "emoji": "💬", "triggers": ["chat mode", "normal mode", "default mode", "exit mode", "back to normal", "switch back", "switch to default"]},
}

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
            message_count INT DEFAULT 0,
            mode TEXT DEFAULT 'chat'
        )
    """)
    cur.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'chat'")
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
        INSERT INTO user_state (user_id, message_count, mode) VALUES (%s, 1, 'chat')
        ON CONFLICT (user_id) DO UPDATE SET message_count = user_state.message_count + 1
    """, (user_id,))
    conn.commit()
    conn.close()

def get_user_state(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT message_count, mode FROM user_state WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"message_count": row[0], "mode": row[1]}
    return {"message_count": 0, "mode": "chat"}

def set_mode(user_id, mode):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_state (user_id, message_count, mode) VALUES (%s, 0, %s)
        ON CONFLICT (user_id) DO UPDATE SET mode = %s
    """, (user_id, mode, mode))
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

def load_context(mode="chat"):
    context = ""
    # Always load profile
    if os.path.exists("profile.md"):
        with open("profile.md", "r") as f:
            context += f.read() + "\n\n"
    # Always load all context/ files
    if os.path.exists("context"):
        for filename in sorted(os.listdir("context")):
            if filename.endswith(".md"):
                with open(f"context/{filename}", "r") as f:
                    context += f.read() + "\n\n"
    # Load active skill file with emphasis, then others as background
    active_skill_file = MODES.get(mode, {}).get("file")
    if active_skill_file and os.path.exists(active_skill_file):
        with open(active_skill_file, "r") as f:
            context += f"## ACTIVE MODE — follow these instructions carefully:\n{f.read()}\n\n"
    # Load remaining skills as background
    if os.path.exists("skills"):
        for filename in sorted(os.listdir("skills")):
            if filename.endswith(".md"):
                filepath = f"skills/{filename}"
                if filepath != active_skill_file:
                    with open(filepath, "r") as f:
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
        repo.update_file(filepath, f"Update {filepath} via Telegram", new_content, file.sha)
    except Exception:
        repo.create_file(filepath, f"Create {filepath} via Telegram", new_content)
    with open(filepath, "w") as f:
        f.write(new_content)

def detect_mode_switch(message):
    import re
    message_clean = re.sub(r'[^\w\s]', '', message.lower()).strip()
    for mode, config in MODES.items():
        for trigger in config["triggers"]:
            if trigger in message_clean:
                return mode
    return None

def is_explicit_update_request(message):
    keywords = [
        "update my", "change my", "edit my", "modify my", "fix my",
        "tone down", "update the", "change the", "let's update",
        "lets update", "update profile", "update context"
    ]
    return any(kw in message.lower() for kw in keywords)

MEMORY_SUMMARY_TRIGGERS = [
    "what do you know about me",
    "summarise my profile",
    "summarize my profile",
    "what do you know",
    "memory summary",
    "what have you saved",
    "what do you remember",
    "show me my profile",
    "what do you know about my finances",
    "summarise my finances",
    "summarize my finances",
    "what do you know about my work",
    "summarise my work",
    "what do you know about my visa",
    "what do you know about my therapy",
    "what do you know about my relationships",
]

MODE_SUMMARY_FOCUS = {
    "finance": ["context/finances.md", "skills/finance.md"],
    "research": None,  # all files
    "tasks": ["profile.md", "context/work.md"],
    "journaling": ["profile.md", "context/relationships.md", "skills/journaling.md"],
    "chat": None,  # all files
}

def is_memory_summary_request(message):
    message_lower = message.lower()
    return any(t in message_lower for t in MEMORY_SUMMARY_TRIGGERS)

async def run_memory_summary(user_message: str, active_mode: str) -> str:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Decide which files to focus on
    focus_files = MODE_SUMMARY_FOCUS.get(active_mode)
    if focus_files:
        md_contents = {}
        for f in focus_files:
            if os.path.exists(f):
                with open(f, "r") as file:
                    md_contents[f] = file.read()
    else:
        md_contents = {}
        for f in list_md_files():
            with open(f, "r") as file:
                md_contents[f] = file.read()

    files_text = chr(10).join([f"=== {path} ==={chr(10)}{content}" for path, content in md_contents.items()])

    prompt = f"""The user wants a summary of what you know about them. Here are their context files:

{files_text}

The user asked: "{user_message}"

Write a warm, natural summary in first person ("I know that you..."). 
Be concise but complete. Group related things together. 
Don't just list the file contents — synthesize them into a readable overview.
If the question is about a specific topic (finances, visa, therapy etc), focus on that."""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

async def run_background_md_update(update: Update, user_id: int, history: list):
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

If there is something worth saving, respond with a JSON array of updates:
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
        clean = result.replace("```json", "").replace("```", "").strip()
        updates = json.loads(clean)
        updated_files = []
        for u in updates:
            update_md_file(u["filepath"], u["new_content"])
            updated_files.append(f"`{u['filepath']}`")
        if updated_files:
            await update.message.reply_text(f"📝 Saved to {', '.join(updated_files)}")
    except Exception:
        pass

async def run_explicit_md_update(update: Update, user_message: str):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    md_contents = read_md_files()

    prompt = f"""The user wants to update their personal knowledge base. Here are their current MD files:

{chr(10).join([f"=== {path} ==={chr(10)}{content}" for path, content in md_contents.items()])}

The user said: "{user_message}"

Figure out which file(s) need updating and rewrite them incorporating the user's request.
You may update multiple files if needed, or create a new one if appropriate.

Respond with a JSON array:
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
        clean = result.replace("```json", "").replace("```", "").strip()
        updates = json.loads(clean)
        updated_files = []
        for u in updates:
            update_md_file(u["filepath"], u["new_content"])
            updated_files.append(f"`{u['filepath']}`")
        summary_parts = [f"✅ Updated {', '.join(updated_files)}\n"]
        for u in updates:
            preview = u["new_content"][:600]
            if len(u["new_content"]) > 600:
                preview += "..."
            summary_parts.append(f"*{u['filepath']}*\n{preview}")
        return "\n".join(summary_parts)
    except Exception as e:
        return f"⚠️ Something went wrong updating your files: {e}"

async def transcribe_voice(file_bytes: bytes) -> str:
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        return transcript.text
    finally:
        os.unlink(tmp_path)

def auto_detect_mode(user_message: str, explicit_mode: str) -> str:
    """
    Infer the best mode from message content.
    Explicit mode is respected UNLESS the message has strong emotional/urgent signals,
    in which case journaling always wins.
    """
    message_lower = user_message.lower()

    # Journaling always overrides — emotional urgency takes priority
    journaling_signals = ["anxiety attack", "anxious", "anxiety", "panic", "scared", "worried", "sad", "crying", "feel like", "feeling", "felt", "stressed", "overwhelmed", "thinking about", "been thinking", "i dont know", "idk", "confused", "lost", "struggling", "therapy", "vent", "processing", "fight with", "argument", "breakup", "heartbroken", "depressed", "exhausted", "can't sleep", "cant sleep"]
    if any(s in message_lower for s in journaling_signals):
        return "journaling"

    # If explicit mode is set and no emotional override, respect it
    if explicit_mode != "chat":
        return explicit_mode

    # Finance signals
    finance_signals = ["rsu", "stock", "tax", "invest", "money", "portfolio", "sell", "gains", "etf", "salary", "visa", "perm", "green card", "wise", "transfer", "eur", "usd", "capital"]
    if any(s in message_lower for s in finance_signals):
        return "finance"

    # Research signals
    research_signals = ["how does", "explain", "what is", "why does", "research", "find out", "look up", "tell me about", "summarise", "compare", "difference between"]
    if any(s in message_lower for s in research_signals):
        return "research"

    # Tasks signals
    tasks_signals = ["email", "calendar", "schedule", "remind", "todo", "task", "book", "draft", "send", "write to", "message", "organise", "organize"]
    if any(s in message_lower for s in tasks_signals):
        return "tasks"

    return "chat"

async def process_message(update: Update, user_id: int, user_message: str):
    save_message(user_id, "user", user_message)
    state = get_user_state(user_id)
    explicit_mode = state["mode"]
    message_count = state["message_count"]

    # Memory summary takes priority over mode switching
    if is_memory_summary_request(user_message):
        active_mode = auto_detect_mode(user_message, explicit_mode)
        reply = await run_memory_summary(user_message, active_mode)
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    # Check for explicit mode switch
    new_mode = detect_mode_switch(user_message)
    if new_mode:
        set_mode(user_id, new_mode)
        emoji = MODES[new_mode]["emoji"]
        mode_name = new_mode.capitalize()
        reply = f"{emoji} Switched to {mode_name} mode."
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    if is_explicit_update_request(user_message):
        reply = await run_explicit_md_update(update, user_message)
    else:
        # Auto-detect mode for this message
        active_mode = auto_detect_mode(user_message, explicit_mode)

        history = load_history(user_id)
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=load_context(active_mode),
            messages=history
        )
        reply = response.content[0].text

        if message_count % BACKGROUND_UPDATE_EVERY == 0:
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
