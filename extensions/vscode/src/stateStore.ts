import * as fs from "node:fs";
import * as path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { projectKey } from "./model";

export class StateStore {
  readonly file: string;
  private expected?: string;
  private broken = false;
  constructor(root: string, projects: string[]) {
    const identity = createHash("sha256").update(JSON.stringify(projects.map(projectKey).sort())).digest("hex");
    this.file = path.join(root, "boards", identity + ".json");
  }
  private hash(data: Buffer): string { return createHash("sha256").update(data).digest("hex"); }
  load(): any {
    if (!fs.existsSync(this.file)) return undefined;
    try {
      const data = fs.readFileSync(this.file);
      if (data.length > 256 * 1024) throw new Error("Board snapshot exceeds the size limit");
      const snapshot = JSON.parse(data.toString("utf8"));
      if (snapshot.schema !== 1 || snapshot.board?.version !== 1 || !["nodes", "edges", "groups"].every(k => Array.isArray(snapshot.board[k])) || !snapshot.configs || Array.isArray(snapshot.configs) || typeof snapshot.configs !== "object" || Object.values(snapshot.configs).some(v => typeof v !== "string" || !path.isAbsolute(v))) throw new Error("Board snapshot schema is invalid");
      this.expected = this.hash(data); return snapshot;
    } catch (error) { this.broken = true; throw error; }
  }
  save(board: unknown, configs: Record<string, string>, version: string): void {
    if (this.broken) throw new Error("The saved board is corrupt. Its existing file was preserved.");
    const data = Buffer.from(JSON.stringify({ schema: 1, board, configs, version }));
    if (data.length > 256 * 1024) throw new Error("The board snapshot exceeds its size limit.");
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const lock = this.file + ".lock";
    try { fs.mkdirSync(lock); } catch { throw new Error("Board storage is locked. Inspect other windows or an interrupted write first."); }
    const staging = this.file + "." + randomUUID() + ".tmp";
    try { const current = fs.existsSync(this.file) ? this.hash(fs.readFileSync(this.file)) : undefined; if (current !== this.expected) throw new Error("Another window changed the board. Inspect its snapshot and reopen the window."); const descriptor = fs.openSync(staging, "wx", 0o600); try { fs.writeFileSync(descriptor, data); fs.fsyncSync(descriptor); } finally { fs.closeSync(descriptor); } fs.renameSync(staging, this.file); this.expected = this.hash(data); }
    finally { if (fs.existsSync(staging)) fs.unlinkSync(staging); fs.rmdirSync(lock); }
  }
}
