// One-time merge: brings Savannah's unique details into Curator's (newest) vault/self copy,
// and standardises the partner's name to "Matija". Idempotent; aborts loudly if an anchor is missing.
const fs = require("fs");
const DIR = process.env.CUR_DIR || "/data/agents/curator/workspace/vault/self";

function once(text, needle, label) {
  const n = text.split(needle).length - 1;
  if (n !== 1) { console.error(`ABORT: anchor for "${label}" found ${n} times (need exactly 1)`); process.exit(1); }
}

// ---- profile.md ----
let p = fs.readFileSync(`${DIR}/profile.md`, "utf8");
if (!p.includes("Key texture: two years of stellar feedback")) {
  once(p, "  this in therapy.\n", "PIP anchor");
  p = p.replace("  this in therapy.\n",
`  this in therapy. Key texture: two years of stellar feedback, then a new manager who
  disliked her came in, tried to fire her, and told her explicitly "no one cares about your
  track record, only my word matters now." She fought her way off the PIP by building
  allies — but it continued to be weaponized against her for 1-2 years afterward
  ("she was actually on a PIP" being surfaced in staffing conversations). Core lesson
  internalized: professional safety is arbitrary and can disappear without warning,
  regardless of track record. Her main change since: became more cautious, not necessarily
  more capable — her own description.
`);
  console.log("profile.md: added PIP backstory");
} else console.log("profile.md: PIP backstory already present");

if (!p.includes("Named anxiety pattern (career-specific)")) {
  once(p, "not the logistics.)\n", "signal-reading anchor");
  p = p.replace("not the logistics.)\n",
`not the logistics.)
- Named anxiety pattern (career-specific): binary signal-reading with superiors. With
  peers and her own team, she doesn't over-analyze. With people she perceives as higher-up
  (senior leaders, leverage contacts), her brain defaults to good-sign / bad-sign — no
  stable "not enough data yet" category exists. Absence of positive signal automatically
  reads as a soft negative. Consciously named and recognized by her; not yet resolved.
`);
  console.log("profile.md: added signal-reading pattern");
} else console.log("profile.md: signal-reading pattern already present");
p = p.replace(/Mattia/g, "Matija");
fs.writeFileSync(`${DIR}/profile.md`, p);

// ---- relationships.md ----
let r = fs.readFileSync(`${DIR}/relationships.md`, "utf8");
r = r.split("\n").filter((l) => !l.includes("Note on name spelling")).join("\n");
if (!r.includes("Pete was never serious")) {
  if (!r.endsWith("\n")) r += "\n";
  r += `
### Ted Mabrey — Head of Commercial, Palantir (earlier notes)
- Very limited interaction so far (~2-3 calls on the Wendy’s account).
- After Wendy’s handover, assigned Sabine to RBI (Restaurant Brands International) — a
  concrete positive signal about his view of her, made independently of any conversation
  she’s had with him.
- Candid with her: shared insider view that "Pete was never serious" about the Wendy’s deal
  (unprompted), treating her somewhat as a peer.
- Has not yet formed a strong assessment of her — too little surface area. His view is
  still thin, probably preliminary.
- His opinion matters greatly to Sabine; she over-reads his signals (response speed,
  word choice, being thought of for accounts). Aware of this pattern but can’t easily
  override it.
- Confidence: medium — early relationship, limited data.
`;
  console.log("relationships.md: added Ted Mabrey notes");
} else console.log("relationships.md: Ted notes already present");
r = r.replace(/Mattia/g, "Matija");
fs.writeFileSync(`${DIR}/relationships.md`, r);
console.log("merge done");
