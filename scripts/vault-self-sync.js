// Copies Curator's vault/self files to the master copy and to every agent that reads them.
// Curator is the only writer; everyone else gets a copy. No LLM, no cost.
const fs = require("fs");
const ROOT = process.env.DATA_ROOT || "/data";
const FILES = ["profile.md", "relationships.md"];
const SRC = `${ROOT}/agents/curator/workspace/vault/self`;
const TARGETS = [
  `${ROOT}/vault/self`,
  ...["savannah", "nainoa", "drill-master"].map((a) => `${ROOT}/agents/${a}/workspace/vault/self`),
];
let changed = 0;
for (const f of FILES) {
  let src;
  try { src = fs.readFileSync(`${SRC}/${f}`, "utf8"); } catch { console.error(`ABORT: cannot read ${SRC}/${f}`); process.exit(1); }
  if (src.length < 1000) { console.error(`ABORT: ${f} looks too small (${src.length} chars), not copying`); process.exit(1); }
  for (const dir of TARGETS) {
    if (!fs.existsSync(dir)) { console.log(`skip (no dir): ${dir}`); continue; }
    const dest = `${dir}/${f}`;
    let cur = null;
    try { cur = fs.readFileSync(dest, "utf8"); } catch {}
    if (cur === src) continue;
    const tmp = `${dest}.tmp`;
    fs.writeFileSync(tmp, src);
    if (process.getuid && process.getuid() === 0) fs.chownSync(tmp, 1000, 1000);
    fs.renameSync(tmp, dest);
    console.log(`updated ${dest}`);
    changed++;
  }
}
console.log(`vault-self-sync: ${changed} file(s) updated`);
