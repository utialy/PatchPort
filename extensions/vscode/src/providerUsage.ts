import * as fs from "node:fs/promises";
import * as path from "node:path";

export type Metrics = Record<"input_tokens" | "output_tokens" | "cache_read_tokens" | "cache_write_tokens" | "cost_usd" | "elapsed_seconds", number | null>;
const numeric = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const token = (value: unknown): number | null => numeric(value) !== null && Number.isSafeInteger(value) ? value as number : null;
export const unknownMetrics = (): Metrics => ({ input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, elapsed_seconds: null });
export interface Sample { metrics: Metrics; model: string; source: string; coverage: string }

async function lines(file: string): Promise<any[]> {
  const info = await fs.lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 64 * 1024 * 1024 || path.resolve(file) !== await fs.realpath(file)) throw new Error("Unsupported usage file");
  // Read only one exact, owned session file; never enumerate historical content.
  const raw = await fs.readFile(file, "utf8");
  return raw.split("\n").filter(line => line.trim()).flatMap(line => { try { return [JSON.parse(line)]; } catch { return []; } });
}

export async function claudeUsage(file: string, sessionId: string, started: number): Promise<Sample> {
  if (!path.isAbsolute(file) || path.basename(file) !== `${sessionId}.jsonl`) throw new Error("Transcript identity mismatch");
  const rows = await lines(file), messages = new Map<string, any>();
  let incomplete = false;
  for (const row of rows) {
    if (row.sessionId && row.sessionId !== sessionId) continue;
    if (row.type !== "assistant") continue;
    const timestamp = Date.parse(row.timestamp);
    if (!Number.isFinite(timestamp)) { incomplete = true; continue; }
    if (timestamp < started) continue;
    const message = row.message;
    if (typeof message?.id !== "string" || !message.usage) { incomplete = true; continue; }
    messages.set(message.id, message);
  }
  const metrics = unknownMetrics();
  const mappings: [keyof Metrics, string][] = [["input_tokens", "input_tokens"], ["output_tokens", "output_tokens"], ["cache_read_tokens", "cache_read_input_tokens"], ["cache_write_tokens", "cache_creation_input_tokens"]];
  if (messages.size) for (const [key, field] of mappings) {
    const values = [...messages.values()].map(m => token(m.usage[field]));
    metrics[key] = values.every(v => v !== null) ? values.reduce<number>((sum, v) => sum + v!, 0) : null;
  }
  if (metrics.input_tokens !== null && metrics.cache_read_tokens !== null && metrics.cache_write_tokens !== null) metrics.input_tokens += metrics.cache_read_tokens + metrics.cache_write_tokens;
  else metrics.input_tokens = null;
  const models = new Set([...messages.values()].map(m => m.model).filter(m => typeof m === "string"));
  return { metrics, model: models.size === 1 ? [...models][0] : models.size > 1 ? "MULTIPLE" : "UNKNOWN", source: "CLAUDE_TRANSCRIPT_MESSAGE_USAGE", coverage: incomplete ? "partial; main transcript only" : "main transcript only; subagent tokens not included" };
}

export async function codexUsage(home: string, sessionId: string, project: string, started: number): Promise<Sample> {
  if (!path.isAbsolute(home) || !/^[a-f0-9-]{36}$/i.test(sessionId)) throw new Error("Invalid Codex session identity");
  const candidates: string[] = [];
  for (const time of new Set([started, Date.now(), started - 86400000])) {
    const date = new Date(time), dir = path.join(home, "sessions", String(date.getUTCFullYear()), String(date.getUTCMonth() + 1).padStart(2, "0"), String(date.getUTCDate()).padStart(2, "0"));
    try { for (const name of await fs.readdir(dir)) if (name.endsWith(`-${sessionId}.jsonl`) && /^rollout-/.test(name)) candidates.push(path.join(dir, name)); } catch { /* An absent day is not an unknown value of zero. */ }
  }
  const unique = [...new Set(candidates)];
  if (unique.length !== 1) throw new Error("Exact Codex rollout unavailable");
  const rows = await lines(unique[0]);
  const meta = rows.find(row => row.type === "session_meta")?.payload;
  if (meta?.id !== sessionId || typeof meta.cwd !== "string" || !Number.isFinite(Date.parse(meta.timestamp)) || path.resolve(meta.cwd).toLowerCase() !== path.resolve(project).toLowerCase() || Date.parse(meta.timestamp) < started - 5000) throw new Error("Resumed or unrelated Codex rollout");
  const events = rows.filter(row => row.type === "event_msg" && row.payload?.type === "token_count" && row.payload.info?.total_token_usage);
  const totals = events.at(-1)?.payload.info.total_token_usage;
  if (!totals) throw new Error("Codex usage not yet reported");
  for (const field of ["input_tokens", "output_tokens", "cached_input_tokens"]) for (let i = 1; i < events.length; i++) {
    const before = token(events[i - 1].payload.info.total_token_usage[field]), after = token(events[i].payload.info.total_token_usage[field]);
    if (before !== null && after !== null && after < before) throw new Error("Codex cumulative usage reset");
  }
  const metrics = unknownMetrics();
  metrics.input_tokens = token(totals.input_tokens); metrics.output_tokens = token(totals.output_tokens); metrics.cache_read_tokens = token(totals.cached_input_tokens);
  const models = new Set(rows.filter(row => row.type === "turn_context").map(row => row.payload?.model).filter(model => typeof model === "string"));
  return { metrics, model: models.size === 1 ? [...models][0] : models.size > 1 ? "MULTIPLE" : "UNKNOWN", source: "CODEX_EXACT_ROLLOUT_TOKEN_COUNT", coverage: "fresh managed session; provider-local format; cost unavailable" };
}

export function statusCost(payload: any, baseline: number | null, previous: number | null): number | null {
  const value = numeric(payload.cost?.total_cost_usd);
  if (value === null || baseline === null || value < baseline || previous !== null && value < previous) return null;
  return value - baseline;
}
