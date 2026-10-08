import * as path from "node:path";
import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import type { Profile } from "./model";

export const workflowActions = new Set(["context.inventory", "batch.preview", "proposal.inspect", "proposal.preview", "recovery.preview", "roles.overview", "roles.preview", "roles.inspect", "roles.log", "roles.promote.preview", "roles.recover.preview", "roles.prune.preview", "archive.preview", "archive.resume.preview"]);
export function dirtyConflicts(roots: string[], paths: string[], dirty: string[]): string[] {
  const canonical = (value: string) => { const full = path.resolve(value); return process.platform === "win32" ? full.toLowerCase() : full; };
  const targets = roots.flatMap(root => paths.map(rel => canonical(path.join(root, rel))));
  return dirty.filter(file => targets.some(target => canonical(file) === target || canonical(file).startsWith(target + path.sep)));
}
export function affectedPaths(action: string, args: Record<string, any>, report: any): string[] {
  if (action === "batch.preview") return [...(report.context?.selected || []).map((i: any) => i.path), ...(report.context?.metadata || []).map((i: any) => i.path), ...(report.context?.summaries || []).flatMap((i: any) => [i.record?.body_path, ...(i.record?.sources || []).map((s: any) => s.path)])].filter(Boolean);
  if (action === "roles.preview") return [...(report.input?.files || []).map((i: any) => i.path), "role_workflow.json"];
  return (args.paths || report.changes?.map((c: any) => c.path) || report.journal?.changes?.map((c: any) => c.path) || []);
}
export function resumeProfile(profile: Profile, providerSession: string): Profile {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(providerSession)) throw new Error("Check the recorded provider session ID.");
  if (profile.args.some(a => ["--resume", "-r", "--continue", "-c", "resume", "--last", "--all", "--session-id"].includes(a))) throw new Error("Remove existing resume or session arguments from the profile before selecting a recorded session.");
  if (!["claude", "codex"].includes(profile.provider)) throw new Error("Only supported AI sessions can be resumed.");
  return { ...profile, args: [...profile.args, ...(profile.provider === "claude" ? ["--resume", providerSession] : ["resume", providerSession])] };
}
export async function runtimeReport(extension: string): Promise<any> {
  const manifest = JSON.parse(await fs.readFile(path.join(extension, "runtime-manifest.json"), "utf8"));
  if (manifest.schema !== 1 || !Array.isArray(manifest.files) || !manifest.files.length) throw new Error("Unexpected runtime manifest. Reinstall a verified VSIX.");
  for (const item of manifest.files) {
    if (typeof item.path !== "string" || !item.path.startsWith("core/agent_bridge/") || item.path.includes("..") || item.path.includes("\\")) throw new Error("Invalid runtime manifest path");
    const data = await fs.readFile(path.join(extension, item.path));
    if (createHash("sha256").update(data).digest("hex") !== item.sha256) throw new Error(`Runtime file mismatch: ${item.path}`);
  }
  return { version: manifest.extension_version, files: manifest.files.length, integrity: "VERIFIED", manifest_sha256: createHash("sha256").update(JSON.stringify(manifest)).digest("hex") };
}
