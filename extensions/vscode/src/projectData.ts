import { randomUUID, createHash } from "node:crypto";
import * as path from "node:path";
import { Board, projectKey } from "./model";
import { Configs } from "./stateStore";

export type DataAction = "reset_board" | "reset_config" | "disconnect" | "select_config";
export interface DataPlan { id: string; project: string; action: DataAction; config?: string; expires: number; signature: string; revision: string; report: { cards: number; connections: number; groupsPreserved: number; config: string | null; scope: string } }
export const dataActions = new Set<DataAction>(["reset_board", "reset_config", "disconnect", "select_config"]);
export function dataSignature(board: Board, configs: Configs): string { return createHash("sha256").update(JSON.stringify({ board, configs })).digest("hex"); }
export function changedProject(board: Board, configs: Configs, project: string, action: DataAction, config?: string): { board: Board; configs: Configs; removed: string[] } {
  if (!dataActions.has(action)) throw new Error("Unsupported configuration management action.");
  if (action === "select_config" && (!config || !path.isAbsolute(config))) throw new Error("Select a configuration file by absolute path.");
  const result = { board: structuredClone(board), configs: { ...configs }, removed: [] as string[] };
  const key = projectKey(project);
  if (action === "reset_board") {
    result.removed = result.board.nodes.filter(n => projectKey(n.project) === key).map(n => n.id);
    const ids = new Set(result.removed);
    result.board.nodes = result.board.nodes.filter(n => !ids.has(n.id));
    result.board.edges = result.board.edges.filter(e => !ids.has(e.source) && !ids.has(e.target));
  } else {
    for (const old of Object.keys(result.configs)) if (projectKey(old) === key) delete result.configs[old];
    if (action === "disconnect") result.configs[key] = null;
    if (action === "select_config") result.configs[key] = config!;
  }
  return result;
}
export function planProject(board: Board, configs: Configs, project: string, action: DataAction, revision: string, config?: string): DataPlan {
  const next = changedProject(board, configs, project, action, config);
  return { id: randomUUID(), project, action, config, revision, expires: Date.now() + 600000, signature: dataSignature(board, configs),
    report: { cards: next.removed.length, connections: board.edges.length - next.board.edges.length, groupsPreserved: board.groups.length, config: next.configs[projectKey(project)] === null ? null : next.configs[projectKey(project)] || path.join(project, "bridge.json"), scope: "current_workspace_only" } };
}
export function validateDataPlan(plan: DataPlan, board: Board, configs: Configs, revision: string): void {
  if (Date.now() >= plan.expires) throw new Error("Configuration preview expired. Inspect again.");
  if (plan.signature !== dataSignature(board, configs) || plan.revision !== revision) throw new Error("Configuration or saved data changed. Create a new preview.");
}
