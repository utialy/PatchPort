import * as path from "node:path";
import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import type { Profile } from "./model";

export const workflowActions = new Set(["context.inventory", "copies.list", "copies.prune.preview", "batch.preview", "proposal.inspect", "proposal.preview", "recovery.preview", "roles.overview", "roles.preview", "roles.inspect", "roles.log", "roles.promote.preview", "roles.recover.preview", "roles.prune.preview", "archive.preview", "archive.resume.preview"]);
export function cleanupRoots(action: string, report: any): string[] {
  if (action === "roles.prune.preview") return typeof report.workspace === "string" ? [report.workspace] : [];
  if (action === "copies.prune.preview") return (report.items || []).map((item: any) => item.artifact?.path).filter((value: unknown) => typeof value === "string");
  return [];
}
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
  if (profile.provider === "local") throw new Error("Local chat cannot resume an existing provider session. Start a new conversation.");
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(providerSession)) throw new Error("Check the recorded provider session ID.");
  if (profile.args.some(a => ["--resume", "-r", "--continue", "-c", "resume", "--last", "--all", "--session-id"].includes(a))) throw new Error("Remove existing resume or session arguments from the profile before selecting a recorded session.");
  if (!["claude", "codex"].includes(profile.provider)) throw new Error("Only supported AI sessions can be resumed.");
  return { ...profile, args: [...profile.args, ...(profile.provider === "claude" ? ["--resume", providerSession] : ["resume", providerSession])] };
}
export async function verifyLocalHelpers(extension: string): Promise<{ schema: 1; files: number; integrity: "VERIFIED" }> {
  const root = path.resolve(extension), manifestPath = path.join(root, "local-helper-manifest.json");
  const safeFile = async (file: string): Promise<void> => {
    for (let current = file; ; current = path.dirname(current)) {
      const info = await fs.lstat(current);
      if (info.isSymbolicLink() || (info.isFile() && info.nlink !== 1)) throw new Error("Linked local helper files cannot be executed.");
      if (current === file && !info.isFile()) throw new Error("The local helper is not a regular file.");
      if (current === path.dirname(current)) break;
    }
  };
  await safeFile(manifestPath);
  const data = await fs.readFile(manifestPath);
  if (data.length > 16384) throw new Error("Local helper manifest exceeds the size limit");
  const manifest = JSON.parse(data.toString("utf8"));
  const expected = new Set(["media/local-api.cjs", "media/local-terminal.cjs"]);
  if (manifest.schema !== 1 || !Array.isArray(manifest.files) || manifest.files.length !== expected.size) throw new Error("Local helper manifest format mismatch. Reinstall a verified extension.");
  for (const item of manifest.files) {
    if (!item || !expected.delete(item.path) || typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256)) throw new Error("Local helper manifest file inventory mismatch.");
    const file = path.join(root, item.path); await safeFile(file);
    if (createHash("sha256").update(await fs.readFile(file)).digest("hex") !== item.sha256) throw new Error(`Local helper file mismatch: ${item.path}`);
  }
  return { schema: 1, files: manifest.files.length, integrity: "VERIFIED" };
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
