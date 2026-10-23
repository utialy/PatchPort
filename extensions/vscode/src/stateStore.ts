import * as fs from "node:fs";
import * as path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { Board, projectKey } from "./model";

export type Configs = Record<string, string | null>;
export interface ManagementRecord { id: string; project: string; action: string; stage: "MIRROR_PENDING" | "DONE"; removed: string[] }
export interface Snapshot { schema: number; board: Board; configs: Configs; version: string; management?: ManagementRecord }

export function selectBoardSnapshot(snapshot: Snapshot | undefined, mirrorV2: unknown, mirrorV1: unknown, marker: unknown): unknown {
  if (marker !== undefined && (typeof marker !== "number" || ![1, 2, 3].includes(marker))) throw new Error("Unsupported board storage format.");
  if (snapshot) {
    if (snapshot.schema < 3 && (mirrorV2 !== undefined || marker === 3)) throw new Error("The new board marker conflicts with the legacy snapshot. Local cards were preserved.");
    return snapshot.board;
  }
  if (mirrorV2 !== undefined || marker === 3) {
    const board = mirrorV2 as Board | undefined;
    if (!board || board.version !== 2 || !["nodes", "edges", "groups"].every(key => Array.isArray((board as any)[key]))) throw new Error("The local board mirror is missing. No legacy fallback was applied.");
    return board;
  }
  if (mirrorV1 && (mirrorV1 as Board).version !== 1) throw new Error("The legacy board location has a newer format marker. Inspect the original snapshot.");
  return mirrorV1;
}

export class StateStore {
  readonly file: string;
  private expected?: string;
  private broken = false;
  private current?: Snapshot;
  constructor(root: string, projects: string[]) {
    const identity = createHash("sha256").update(JSON.stringify(projects.map(projectKey).sort())).digest("hex");
    this.file = path.join(root, "boards", identity + ".json");
  }
  private hash(data: Buffer): string { return createHash("sha256").update(data).digest("hex"); }
  private safe(file: string): void {
    for (let item = file; ; item = path.dirname(item)) {
      try { const info = fs.lstatSync(item); if (info.isSymbolicLink() || info.isFile() && info.nlink !== 1) throw new Error("A linked board-storage path cannot be used."); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
      if (item === path.dirname(item)) break;
    }
  }
  load(): Snapshot | undefined {
    try {
      this.safe(this.file);
      if (!fs.existsSync(this.file)) return undefined;
      const data = fs.readFileSync(this.file);
      if (data.length > 256 * 1024) throw new Error("Board snapshot exceeds the size limit");
      const snapshot = JSON.parse(data.toString("utf8"));
      if (![1, 2, 3].includes(snapshot.schema) || snapshot.board?.version !== (snapshot.schema === 3 ? 2 : 1) || !["nodes", "edges", "groups"].every(k => Array.isArray(snapshot.board[k])) || (snapshot.schema !== 3 && snapshot.board.nodes.some((node: any) => node?.provider === "local")) || !snapshot.configs || Array.isArray(snapshot.configs) || typeof snapshot.configs !== "object" || Object.values(snapshot.configs).some(v => !(snapshot.schema >= 2 && v === null) && (typeof v !== "string" || !path.isAbsolute(v)))) throw new Error("Board snapshot schema is invalid");
      const m = snapshot.management;
      if (m && (snapshot.schema < 2 || typeof m.id !== "string" || typeof m.project !== "string" || typeof m.action !== "string" || !["MIRROR_PENDING", "DONE"].includes(m.stage) || !Array.isArray(m.removed) || m.removed.some((id: unknown) => typeof id !== "string"))) throw new Error("Board management record is invalid");
      this.expected = this.hash(data); this.current = snapshot; return structuredClone(snapshot);
    } catch (error) { this.broken = true; throw error; }
  }
  get pending(): ManagementRecord | undefined { return this.current?.management?.stage === "MIRROR_PENDING" ? structuredClone(this.current.management) : undefined; }
  snapshot(): Snapshot | undefined { return this.current ? structuredClone(this.current) : undefined; }
  assertUnchanged(): void {
    if (this.broken) throw new Error("The saved board is corrupt. Its existing file was preserved.");
    this.safe(this.file);
    const current = fs.existsSync(this.file) ? this.hash(fs.readFileSync(this.file)) : undefined;
    if (current !== this.expected) throw new Error("Another window changed the board. Inspect its snapshot and reopen the window.");
  }
  private write(snapshot: Snapshot): void {
    this.assertUnchanged();
    const data = Buffer.from(JSON.stringify(snapshot));
    if (data.length > 256 * 1024) throw new Error("The board snapshot exceeds its size limit.");
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const lock = this.file + ".lock"; this.safe(lock);
    try { fs.mkdirSync(lock); } catch { throw new Error("Board storage is locked. Inspect other windows or an interrupted write first."); }
    const staging = this.file + "." + randomUUID() + ".tmp";
    try { this.assertUnchanged(); const descriptor = fs.openSync(staging, "wx", 0o600); try { fs.writeFileSync(descriptor, data); fs.fsyncSync(descriptor); } finally { fs.closeSync(descriptor); } fs.renameSync(staging, this.file); this.expected = this.hash(data); this.current = structuredClone(snapshot); }
    finally { if (fs.existsSync(staging)) fs.unlinkSync(staging); fs.rmdirSync(lock); }
  }
  private format(board: Board): { schema: number; board: Board } {
    if (![1, 2].includes(board.version)) throw new Error("Unsupported board format.");
    const local = this.current?.schema === 3 || board.version === 2 || board.nodes.some(node => node.provider === "local");
    return { schema: local ? 3 : 2, board: { ...board, version: local ? 2 : 1 } };
  }
  save(board: Board, configs: Configs, version: string): void {
    if (this.pending) throw new Error("Configuration persistence needs finalization. Explicitly resume in Data & settings.");
    this.write({ ...this.format(board), configs, version, ...(this.current?.management ? { management: this.current.management } : {}) });
  }
  beginManagement(board: Board, configs: Configs, version: string, record: Omit<ManagementRecord, "stage">): Snapshot {
    if (this.pending) throw new Error("Finalize the previous configuration change first.");
    this.write({ ...this.format(board), configs, version, management: { ...record, stage: "MIRROR_PENDING" } });
    return this.snapshot()!;
  }
  finishManagement(id: string): void {
    if (!this.pending || this.pending.id !== id) throw new Error("The configuration-change ID to finalize does not match.");
    this.write({ ...this.current!, management: { ...this.current!.management!, stage: "DONE" } });
  }
}
