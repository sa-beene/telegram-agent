// Curator digest v3: builds pending-review.md from Supabase raw_log, keeping only
// conversation text (what Sabine said, what agents replied). Drops thinking blocks, tool
// calls/results, failed-turn markers, fallback notices, NO_REPLY, duplicate replies and cron sessions.

const fs = require("fs");

const LOOKBACK_HOURS = 216;
const PAGE = 1000;
const MAX_MSG_CHARS = 3000;
const MAX_FILE_CHARS = 150000;
const OUT = "/data/agents/curator/workspace/pending-review.md";

function extractText(content) {
  if (content == null) return "";
  let c = content;
  if (typeof c === "string") {
    const t = c.trim();
    if (t.startsWith("[") || t.startsWith("{")) {
      try { c = JSON.parse(t); } catch { return c.trim(); }
    } else return t;
  }
  if (!Array.isArray(c)) c = [c];
  const parts = [];
  for (const b of c) {
    if (typeof b === "string") parts.push(b);
    else if (b && b.type === "text" && typeof b.text === "string") parts.push(b.text);
  }
  return parts.join("\n").trim();
}

function isNoise(text) {
  if (!text) return true;
  if (text === "NO_REPLY" || text === "HEARTBEAT_OK") return true;
  if (text.includes("[assistant turn failed before producing content]")) return true;
  if (text.startsWith("↪️ Model Fallback")) return true;
  return false;
}

function buildDigest(rows, now = Date.now()) {
  const cronSessions = new Set();
  const firstUser = new Map();
  for (const r of rows) {
    if (r.role === "user" && !firstUser.has(r.session_id)) {
      firstUser.set(r.session_id, extractText(r.content));
    }
  }
  for (const [sid, t] of firstUser) if (t.startsWith("[cron:")) cronSessions.add(sid);

  const byAgent = {};
  let kept = 0, dropped = 0;
  let lastKey = null;
  for (const r of rows) {
    if (cronSessions.has(r.session_id)) { dropped++; continue; }
    if (r.role !== "user" && r.role !== "assistant") { dropped++; continue; }
    const text = extractText(r.content);
    if (isNoise(text)) { dropped++; continue; }
    const key = `${r.agent}|${r.session_id}|${r.role}|${text}`;
    if (key === lastKey) { dropped++; continue; }
    lastKey = key;
    const t = text.length > MAX_MSG_CHARS ? text.slice(0, MAX_MSG_CHARS) + " …[truncated]" : text;
    (byAgent[r.agent] ||= []).push({ ts: r.ts, who: r.role === "user" ? "Sabine" : r.agent, text: t });
    kept++;
  }

  const header = [
    "# Pending review",
    "",
    `Conversation text from the last ${LOOKBACK_HOURS} hours (generated ${new Date(now).toISOString()}).`,
    `Rows read: ${rows.length}. Messages kept: ${kept}. Dropped as noise/cron/duplicate: ${dropped}.`,
    kept === 0 ? "NOTE: no conversation messages in this window (pipeline read OK; see rows read above)." : "",
    "",
  ].join("\n");

  const sections = [];
  for (const agent of Object.keys(byAgent).sort()) {
    const lines = [`## ${agent}`, ""];
    for (const m of byAgent[agent]) lines.push(`**${m.who}** (${m.ts}):\n${m.text}\n`);
    sections.push(lines.join("\n"));
  }
  const body = sections.join("\n");
  let out = header + "\n" + body;
  if (out.length > MAX_FILE_CHARS) {
    const omitted = out.length - MAX_FILE_CHARS;
    out = header + `\n[Older content omitted: ~${omitted} chars. Showing most recent.]\n\n` +
      body.slice(body.length - (MAX_FILE_CHARS - header.length));
  }
  return out;
}

async function fetchAll(fetchFn, base, key, sinceIso) {
  const rows = [];
  for (let offset = 0; ; offset += PAGE) {
    const url = `${base}/rest/v1/raw_log?select=agent,session_id,role,content,ts&ts=gte.${encodeURIComponent(sinceIso)}&order=ts.asc,id.asc&limit=${PAGE}&offset=${offset}`;
    const res = await fetchFn(url, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
    if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
    const page = await res.json();
    rows.push(...page);
    if (page.length < PAGE) break;
  }
  return rows;
}

async function main() {
  const base = process.env.SUPABASE_URL, key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!base || !key) { console.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY"); process.exit(1); }
  let fetchFn = fetch;
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  if (proxy) {
    const { ProxyAgent, fetch: ufetch } = require("/app/node_modules/undici");
    const dispatcher = new ProxyAgent(proxy);
    fetchFn = (u, o) => ufetch(u, { ...o, dispatcher });
  }
  const since = new Date(Date.now() - LOOKBACK_HOURS * 3600e3).toISOString();
  const rows = await fetchAll(fetchFn, base, key, since);
  const md = buildDigest(rows);
  fs.writeFileSync(OUT, md);
  console.log(`curator-digest: read ${rows.length} rows, wrote ${md.length} chars to ${OUT}`);
}

module.exports = { extractText, isNoise, buildDigest };
if (require.main === module) main().catch((e) => { console.error("curator-digest failed:", e); process.exit(1); });
