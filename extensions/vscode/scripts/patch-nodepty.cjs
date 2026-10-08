const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../node_modules/node-pty");
if (JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version !== "1.1.0") throw new Error("Review the terminal cleanup patch before changing node-pty.");
const agentPath = path.join(root, "lib/windowsPtyAgent.js");
let agent = fs.readFileSync(agentPath, "utf8");
if (!agent.includes("PatchPort: release the output worker after a natural exit.")) {
  const needle = "this._outSocket.destroy();";
  if (agent.split(needle).length !== 2) throw new Error("Unexpected node-pty cleanup implementation.");
  agent = agent.replace(needle, "/* PatchPort: release the output worker after a natural exit. */\n        this._conoutSocketWorker.dispose();\n        " + needle);
  fs.writeFileSync(agentPath, agent);
}
const helperPath = path.join(root, "lib/conpty_console_list_agent.js");
let helper = fs.readFileSync(helperPath, "utf8");
if (!helper.includes("PatchPort: an already exited console has no remaining processes.")) {
  const needle = "var consoleProcessList = getConsoleProcessList(shellPid);";
  if (!helper.includes(needle)) throw new Error("Unexpected node-pty console enumeration implementation.");
  helper = helper.replace(needle, "/* PatchPort: an already exited console has no remaining processes. */\nvar consoleProcessList = [];\ntry { consoleProcessList = getConsoleProcessList(shellPid); } catch (error) { if (!String(error).includes(\"AttachConsole failed\")) throw error; }");
  fs.writeFileSync(helperPath, helper);
}
