const fs = require("node:fs");
const http = require("node:http");
const { randomUUID } = require("node:crypto");
const { execFile } = require("node:child_process");
const args = process.argv.slice(2);
const provider = args[args.indexOf("--provider") + 1] || "claude";
const session = randomUUID(); let turn = 0; let input = ""; let pasted = false;
function event(body) {
  if (provider === "codex") {
    const config = args[args.indexOf("-c") + 1];
    let notify;
    try { notify = JSON.parse(config.slice("notify=".length)); } catch (e) { process.stdout.write(`NOTIFY_PARSE_ERROR ${config}\r\n`); process.exitCode = 2; return; }
    execFile(notify[0], [...notify.slice(1), JSON.stringify(body)], { env: process.env, timeout: 6000 }, error => { if (error) process.stdout.write(`NOTIFY_ERROR ${error.message}\r\n`); });
    return;
  }
  const file = args[args.indexOf("--settings") + 1];
  const hook = JSON.parse(fs.readFileSync(file, "utf8")).hooks[body.hook_event_name]?.[0]?.hooks[0];
  if (!hook) return;
  const req = http.request(hook.url, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${process.env.PATCHPORT_TOKEN}` } }, res => res.resume()); req.on("error", () => {}); req.end(JSON.stringify(body));
}
function submit() {
  if (!input.trim()) return;
  const received = input; input = ""; turn++;
  process.stdout.write(`\r\nRECEIVED ${JSON.stringify(received)}\r\n`);
  if (provider === "claude") event({ session_id: session, cwd: process.cwd(), hook_event_name: "UserPromptSubmit", prompt: received });
  setTimeout(() => {
    const text = `${provider.toUpperCase()}_REPLY_${turn}: ${received.includes("[PatchPort") ? "Peer message received." : received}`;
    process.stdout.write(text + "\r\n> ");
    event(provider === "claude" ? { session_id: session, cwd: process.cwd(), hook_event_name: "Stop", last_assistant_message: text, stop_hook_active: false } : { type: "agent-turn-complete", "thread-id": session, "turn-id": String(turn), cwd: process.cwd(), "last-assistant-message": text });
  }, 120);
}
process.stdout.write(`\u001b[?2004hPATCHPORT_FIXTURE ${provider}\r\n> `);
if (process.stdin.isTTY) process.stdin.setRawMode(true);
process.stdin.setEncoding("utf8"); process.stdin.resume();
process.stdin.on("data", chunk => {
  if (chunk.includes("\u0003") || chunk.includes("\u0004")) process.exit(0);
  for (let i = 0; i < chunk.length; i++) {
    if (chunk.slice(i).startsWith("\u001b[200~")) { pasted = true; i += 5; continue; }
    if (chunk.slice(i).startsWith("\u001b[201~")) { pasted = false; i += 5; continue; }
    const c = chunk[i]; if (c === "\r" && !pasted) submit(); else input += c;
  }
});
if (provider === "claude") setTimeout(() => event({ session_id: session, cwd: process.cwd(), hook_event_name: "SessionStart" }), 60);
