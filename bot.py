import os
import json
import asyncio
import re
import psycopg2
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic
from github import Github
import openai
import tempfile
import google.generativeai as genai
import PIL.Image
import io

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "claude")  # claude | openai | ollama

genai.configure(api_key=GEMINI_API_KEY)

BACKGROUND_UPDATE_EVERY = 5

# Default models per mode — stored in DB per user, these are just fallbacks
MODE_MODEL_DEFAULTS = {
    "journaling": "claude-sonnet-4-5",
    "research":   "claude-opus-4-5",
    "tasks":      "claude-sonnet-4-5",
    "finance":    "claude-opus-4-5",
    "chat":       "claude-sonnet-4-5",
    "dev":        "claude-sonnet-4-5",
}

MODES = {
    "journaling": {"file": "skills/journaling.md", "emoji": "📓", "triggers": ["journaling mode", "journal mode", "lets journal", "switch to journaling"]},
    "research":   {"file": "skills/research.md",   "emoji": "🔍", "triggers": ["research mode", "lets research", "researcher mode", "switch to research"]},
    "tasks":      {"file": "skills/tasks.md",       "emoji": "✅", "triggers": ["tasks mode", "task mode", "lets do tasks", "assistant mode", "switch to tasks"]},
    "finance":    {"file": "skills/finance.md",     "emoji": "📊", "triggers": ["finance mode", "financial mode", "lets talk finance", "switch to finance"]},
    "chat":       {"file": None,                    "emoji": "💬", "triggers": ["chat mode", "normal mode", "default mode", "exit mode", "back to normal", "switch back", "switch to default"]},
    "dev":        {"file": "skills/dev.md",         "emoji": "🛠️", "triggers": ["dev mode", "developer mode", "development mode", "switch to dev"]},
}

# ── DB ────────────────────────────────────────────────────────────────────────

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
    # Table for per-mode model preferences
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mode_models (
            user_id BIGINT,
            mode TEXT,
            model TEXT,
            PRIMARY KEY (user_id, mode)
        )
    """)
    # Table for rollback — stores last working bot.py GitHub SHA
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dev_state (
            user_id BIGINT PRIMARY KEY,
            last_sha TEXT,
            last_description TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
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

def get_mode_model(user_id, mode):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT model FROM mode_models WHERE user_id = %s AND mode = %s", (user_id, mode))
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0]
    return MODE_MODEL_DEFAULTS.get(mode, "claude-sonnet-4-5")

def set_mode_model(user_id, mode, model):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO mode_models (user_id, mode, model) VALUES (%s, %s, %s)
        ON CONFLICT (user_id, mode) DO UPDATE SET model = %s
    """, (user_id, mode, model, model))
    conn.commit()
    conn.close()

def save_dev_state(user_id, sha, description):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO dev_state (user_id, last_sha, last_description, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE SET last_sha = %s, last_description = %s, updated_at = NOW()
    """, (user_id, sha, description, sha, description))
    conn.commit()
    conn.close()

def get_dev_state(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT last_sha, last_description FROM dev_state WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"last_sha": row[0], "last_description": row[1]}
    return None

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

# ── Context / MD files ────────────────────────────────────────────────────────

def load_context(mode="chat"):
    context = ""
    if os.path.exists("profile.md"):
        with open("profile.md", "r") as f:
            context += f.read() + "\n\n"
    if os.path.exists("context"):
        for filename in sorted(os.listdir("context")):
            if filename.endswith(".md"):
                with open(f"context/{filename}", "r") as f:
                    context += f.read() + "\n\n"
    active_skill_file = MODES.get(mode, {}).get("file")
    if active_skill_file and os.path.exists(active_skill_file):
        with open(active_skill_file, "r") as f:
            context += f"## ACTIVE MODE — follow these instructions carefully:\n{f.read()}\n\n"
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

# ── LLM abstraction ───────────────────────────────────────────────────────────

def call_llm(model: str, system: str, messages: list, max_tokens: int = 1024) -> str:
    """
    Single entry point for all LLM calls.
    MODEL_PROVIDER controls which API is used.
    To switch provider: change MODEL_PROVIDER env var to 'openai' or 'ollama'.
    Model names should match the provider's naming convention.
    """
    if MODEL_PROVIDER == "claude":
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages
        )
        return response.content[0].text

    elif MODEL_PROVIDER == "openai":
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        full_messages = [{"role": "system", "content": system}] + messages
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=full_messages
        )
        return response.choices[0].message.content

    elif MODEL_PROVIDER == "ollama":
        import requests
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        full_messages = [{"role": "system", "content": system}] + messages
        response = requests.post(f"{ollama_url}/api/chat", json={
            "model": model,
            "messages": full_messages,
            "stream": False
        })
        return response.json()["message"]["content"]

    else:
        raise ValueError(f"Unknown MODEL_PROVIDER: {MODEL_PROVIDER}")

# ── Mode / model switching detection ─────────────────────────────────────────

MODEL_ALIASES = {
    "opus":   "claude-opus-4-5",
    "sonnet": "claude-sonnet-4-5",
    "haiku":  "claude-haiku-4-5",
}

def detect_model_switch(message: str):
    """
    Detects model switch requests flexibly — looks for any alias + any mode name
    anywhere in the message, in any order.
    Examples that all work:
    - "switch journaling to opus"
    - "can you use opus for my journaling mode please"
    - "I want sonnet for finance"
    - "journaling should use opus"
    - "switch to opus" (all modes)
    - "what model am I using"
    """
    msg = message.lower().strip()

    if any(p in msg for p in ["what model", "which model", "current model", "what models", "which models"]):
        return ("status", None)

    found_alias = None
    found_mode = None

    for alias in MODEL_ALIASES:
        if alias in msg.split() or alias in msg:
            # Make sure it's the word itself, not part of another word
            if re.search(rf'{alias}', msg):
                found_alias = alias
                break

    if not found_alias:
        return None

    for mode in MODES:
        if re.search(rf'{mode}', msg):
            found_mode = mode
            break

    model_name = MODEL_ALIASES[found_alias]

    if found_mode:
        return (found_mode, model_name)
    else:
        # No specific mode mentioned — apply to all
        return ("all", model_name)


def detect_mode_switch(message):
    message_clean = re.sub(r'[^\w\s]', '', message.lower()).strip()
    for mode, config in MODES.items():
        for trigger in config["triggers"]:
            if trigger in message_clean:
                return mode
    return None

# ── Memory / update detection ─────────────────────────────────────────────────

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
    "finance":    ["context/finances.md", "skills/finance.md"],
    "research":   None,
    "tasks":      ["profile.md", "context/work.md"],
    "journaling": ["profile.md", "context/relationships.md", "skills/journaling.md"],
    "chat":       None,
    "dev":        None,
}

def is_memory_summary_request(message):
    return any(t in message.lower() for t in MEMORY_SUMMARY_TRIGGERS)

# ── Core AI functions ─────────────────────────────────────────────────────────

async def run_memory_summary(user_message: str, active_mode: str, user_id: int) -> str:
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

    files_text = "\n".join([f"=== {path} ===\n{content}" for path, content in md_contents.items()])
    model = get_mode_model(user_id, active_mode)

    prompt = f"""The user wants a summary of what you know about them. Here are their context files:

{files_text}

The user asked: "{user_message}"

Write a warm, natural summary in first person ("I know that you..."). 
Be concise but complete. Group related things together. 
Don't just list the file contents — synthesize them into a readable overview.
If the question is about a specific topic (finances, visa, therapy etc), focus on that."""

    return call_llm(model, "", [{"role": "user", "content": prompt}], max_tokens=1024)

async def run_background_md_update(update: Update, user_id: int, history: list):
    md_contents = read_md_files()
    recent_messages = history[-BACKGROUND_UPDATE_EVERY:]
    conversation_snippet = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in recent_messages])
    model = get_mode_model(user_id, "chat")

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

    result = call_llm(model, "", [{"role": "user", "content": prompt}], max_tokens=2048)
    result = result.strip()
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

async def run_explicit_md_update(update: Update, user_message: str, user_id: int):
    md_contents = read_md_files()
    model = get_mode_model(user_id, "chat")

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

    result = call_llm(model, "", [{"role": "user", "content": prompt}], max_tokens=2048)
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

# ── Dev mode ──────────────────────────────────────────────────────────────────

def get_bot_from_github() -> tuple[str, str]:
    """Returns (file_content, sha)"""
    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(GITHUB_REPO)
    file = repo.get_contents("bot.py")
    return file.decoded_content.decode("utf-8"), file.sha

def push_bot_to_github(new_content: str, commit_message: str) -> str:
    """Pushes new bot.py to GitHub. Returns new SHA."""
    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(GITHUB_REPO)
    file = repo.get_contents("bot.py")
    result = repo.update_file("bot.py", commit_message, new_content, file.sha)
    return result["commit"].sha

def rollback_bot(sha: str) -> bool:
    """Restores bot.py to a specific commit SHA."""
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO)
        # Get the file at that commit
        old_content = repo.get_contents("bot.py", ref=sha)
        current_file = repo.get_contents("bot.py")
        repo.update_file(
            "bot.py",
            "🔄 Rollback to last working version",
            old_content.decoded_content.decode("utf-8"),
            current_file.sha
        )
        return True
    except Exception:
        return False

async def run_dev_request(update: Update, user_id: int, user_message: str):
    """Handles a dev mode change request."""
    await update.message.reply_text("🛠️ Reading current bot.py...")

    try:
        current_code, current_sha = get_bot_from_github()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Couldn't read bot.py from GitHub: {e}")
        return

    model = get_mode_model(user_id, "dev")

    system_prompt = """You are an expert Python developer modifying a Telegram bot.
You will receive the current bot.py code and a change request.
You must return a JSON object with exactly two fields:
{
  "new_code": "<complete updated bot.py as a string>",
  "description": "<plain English description of what you changed and what to watch for — no code, no jargon, max 3 sentences>"
}
Important: return the COMPLETE file, not just the changed parts. Only return the JSON object, nothing else."""

    prompt = f"""Current bot.py:
```python
{current_code}
```

Change request: {user_message}

Return the JSON object with new_code and description."""

    await update.message.reply_text("⚙️ Making the change...")

    try:
        result = call_llm(model, system_prompt, [{"role": "user", "content": prompt}], max_tokens=8192)
        clean = result.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        new_code = parsed["new_code"]
        description = parsed["description"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ Couldn't generate the change: {e}")
        return

    # Save rollback point BEFORE pushing
    save_dev_state(user_id, current_sha, f"Before: {user_message[:100]}")

    await update.message.reply_text("📤 Pushing to GitHub...")

    try:
        push_bot_to_github(new_code, f"[dev] {user_message[:60]}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Push failed: {e}\nRollback point saved — say 'roll back' to restore.")
        return

    await update.message.reply_text(
        f"✅ Pushed! Here's what changed:\n\n{description}\n\n"
        f"Railway is deploying now (~60s). I'll check back in with you."
    )

    # Follow-up after 75 seconds
    await asyncio.sleep(75)
    await update.message.reply_text(
        f"⏱️ Should be live now. Test it and let me know if anything looks off.\n"
        f"If something broke, just say *roll back* and I'll restore the previous version.",
        parse_mode="Markdown"
    )

async def run_rollback(update: Update, user_id: int):
    dev_state = get_dev_state(user_id)
    if not dev_state or not dev_state["last_sha"]:
        await update.message.reply_text("⚠️ No rollback point saved yet — I can only roll back after a dev mode change.")
        return

    await update.message.reply_text("🔄 Rolling back to last working version...")
    success = rollback_bot(dev_state["last_sha"])
    if success:
        await update.message.reply_text(
            f"✅ Rolled back. Railway is redeploying (~60s).\n"
            f"Previous change was: _{dev_state['last_description']}_",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Rollback failed — check GitHub manually.")

# ── Image / voice ─────────────────────────────────────────────────────────────

async def describe_image_with_gemini(image_bytes: bytes, caption: str = "") -> str:
    model = genai.GenerativeModel("gemini-2.5-flash-image")
    image = PIL.Image.open(io.BytesIO(image_bytes))
    if caption:
        prompt = (
            f"The user sent this image with the caption: \"{caption}\"\n\n"
            "Describe what you see in detail. If the caption asks a question or makes a request, "
            "address it directly. Extract any text, labels, prices, dates, or structured info visible. "
            "Be thorough but concise."
        )
    else:
        prompt = (
            "Describe what you see in this image in detail. "
            "Extract any text, labels, prices, dates, or structured info visible. "
            "Be thorough but concise."
        )
    response = model.generate_content([prompt, image])
    return response.text

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

# ── Mode detection ────────────────────────────────────────────────────────────

def auto_detect_mode(user_message: str, explicit_mode: str) -> str:
    message_lower = user_message.lower()

    journaling_signals = ["anxiety attack", "anxious", "anxiety", "panic", "scared", "worried", "sad", "crying", "feel like", "feeling", "felt", "stressed", "overwhelmed", "thinking about", "been thinking", "i dont know", "idk", "confused", "lost", "struggling", "therapy", "vent", "processing", "fight with", "argument", "breakup", "heartbroken", "depressed", "exhausted", "can't sleep", "cant sleep"]
    if any(s in message_lower for s in journaling_signals):
        return "journaling"

    if explicit_mode != "chat":
        return explicit_mode

    finance_signals = ["rsu", "stock", "tax", "invest", "money", "portfolio", "sell", "gains", "etf", "salary", "visa", "perm", "green card", "wise", "transfer", "eur", "usd", "capital"]
    if any(s in message_lower for s in finance_signals):
        return "finance"

    research_signals = ["how does", "explain", "what is", "why does", "research", "find out", "look up", "tell me about", "summarise", "compare", "difference between"]
    if any(s in message_lower for s in research_signals):
        return "research"

    tasks_signals = ["email", "calendar", "schedule", "remind", "todo", "task", "book", "draft", "send", "write to", "message", "organise", "organize"]
    if any(s in message_lower for s in tasks_signals):
        return "tasks"

    return "chat"

# ── Main message processor ────────────────────────────────────────────────────

async def process_message(update: Update, user_id: int, user_message: str):
    save_message(user_id, "user", user_message)
    state = get_user_state(user_id)
    explicit_mode = state["mode"]
    message_count = state["message_count"]

    # Rollback command
    if any(p in user_message.lower() for p in ["roll back", "rollback", "undo last change", "revert"]):
        if explicit_mode == "dev":
            await run_rollback(update, user_id)
            return

    # Model switch detection (works in any mode)
    model_switch = detect_model_switch(user_message)
    if model_switch:
        target, new_model = model_switch
        if target == "status":
            lines = [f"Current models ({MODEL_PROVIDER} provider):"]
            for mode in MODES:
                m = get_mode_model(user_id, mode)
                lines.append(f"  {MODES[mode]['emoji']} {mode}: {m}")
            reply = "\n".join(lines)
            save_message(user_id, "assistant", reply)
            await update.message.reply_text(reply)
            return
        elif target == "all":
            for mode in MODES:
                set_mode_model(user_id, mode, new_model)
            reply = f"✅ Switched all modes to {new_model}"
        else:
            set_mode_model(user_id, target, new_model)
            reply = f"✅ Switched {target} mode to {new_model}"
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    # Memory summary
    if is_memory_summary_request(user_message):
        active_mode = auto_detect_mode(user_message, explicit_mode)
        reply = await run_memory_summary(user_message, active_mode, user_id)
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    # Mode switch
    new_mode = detect_mode_switch(user_message)
    if new_mode:
        set_mode(user_id, new_mode)
        emoji = MODES[new_mode]["emoji"]
        mode_name = new_mode.capitalize()
        reply = f"{emoji} Switched to {mode_name} mode."
        if new_mode == "dev":
            reply += "\n\nTell me what you want to change and I'll do it. Say *roll back* at any time to undo the last change."
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    # Dev mode — all messages are change requests
    if explicit_mode == "dev":
        await run_dev_request(update, user_id, user_message)
        return

    # Explicit MD update
    if is_explicit_update_request(user_message):
        reply = await run_explicit_md_update(update, user_message, user_id)
    else:
        active_mode = auto_detect_mode(user_message, explicit_mode)
        model = get_mode_model(user_id, active_mode)
        history = load_history(user_id)
        reply = call_llm(model, load_context(active_mode), history, max_tokens=1024)

        if message_count % BACKGROUND_UPDATE_EVERY == 0:
            await run_background_md_update(update, user_id, history)

    save_message(user_id, "assistant", reply)
    await update.message.reply_text(reply)

# ── Telegram handlers ─────────────────────────────────────────────────────────

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

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("🔍 Analysing image...")
    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    file_bytes = await tg_file.download_as_bytearray()
    caption = update.message.caption or ""
    try:
        description = await describe_image_with_gemini(bytes(file_bytes), caption)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Couldn't analyse image: {e}")
        return
    if caption:
        combined_message = f"[Image description: {description}]\n\nUser's caption/question: {caption}"
    else:
        combined_message = f"[Image description: {description}]"
    await process_message(update, user_id, combined_message)

# ── Start ─────────────────────────────────────────────────────────────────────

init_db()
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.run_polling()
