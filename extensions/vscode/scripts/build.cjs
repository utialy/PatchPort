const esbuild = require("esbuild");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
process.chdir(root);
require("./patch-nodepty.cjs");
fs.mkdirSync("dist", { recursive: true });
fs.cpSync(path.resolve(root, "../../src/agent_bridge"), path.join(root, "core/agent_bridge"), { recursive: true, filter: source => !source.includes("__pycache__") && !source.endsWith(".pyc") });
const helperAssets = ["role_flow", "role_review", "role_manage", "project_overview", "flow_lifecycle", "flow_cleanup", "flow_archive", "flow_archive_writer"];
const helpers = path.join(root, "core/agent_bridge/_connect_assets/tools");
fs.mkdirSync(helpers, { recursive: true });
for (const name of helperAssets) fs.copyFileSync(path.resolve(root, "../../tools", name + ".py"), path.join(helpers, name + ".py"));
const runtimeFiles = [];
const collect = directory => { for (const item of fs.readdirSync(directory, { withFileTypes: true })) { const file = path.join(directory, item.name); if (item.isDirectory()) collect(file); else if (item.name.endsWith(".py")) runtimeFiles.push({ path: path.relative(root, file).split(path.sep).join("/"), sha256: require("node:crypto").createHash("sha256").update(fs.readFileSync(file)).digest("hex") }); } };
collect(path.join(root, "core/agent_bridge"));
runtimeFiles.sort((a, b) => a.path.localeCompare(b.path));
fs.writeFileSync(path.join(root, "runtime-manifest.json"), JSON.stringify({ schema: 1, extension_version: require(path.join(root, "package.json")).version, files: runtimeFiles }, null, 2));
Promise.all([
  esbuild.build({ entryPoints: ["src/extension.ts"], bundle: true, outfile: "dist/extension.js", platform: "node", target: "node20", external: ["vscode", "node-pty"], sourcemap: false }),
  esbuild.build({ entryPoints: ["src/webview.ts"], bundle: true, outfile: "dist/webview.js", platform: "browser", target: "es2022", sourcemap: false }),
  esbuild.build({ entryPoints: ["src/model.ts", "src/session.ts", "src/core.ts", "src/providerUsage.ts", "src/workflow.ts", "src/stateStore.ts"], bundle: true, outdir: "dist/test", platform: "node", target: "node20", external: ["node-pty"] })
]).then(() => {
  fs.copyFileSync("node_modules/@xterm/xterm/css/xterm.css", "dist/xterm.css");
  const notices = ["@xterm/xterm", "@xterm/addon-fit", "@xterm/headless", "@xterm/addon-serialize", "node-pty"].map(name => {
    const own = path.join("node_modules", name, "LICENSE");
    // These xterm subpackages use the repository's shared MIT license.
    const license = fs.existsSync(own) ? own : path.join("node_modules/@xterm/xterm/LICENSE");
    return `## ${name}\n\n${fs.readFileSync(license, "utf8")}`;
  }).join("\n\n");
  fs.writeFileSync("THIRD_PARTY_NOTICES.md", `# Third-party notices\n\nPatchPort applies two small Windows lifecycle fixes to node-pty 1.1.0: dispose the output worker after a natural exit, and tolerate an already exited console during process enumeration. The reproducible patch is in scripts/patch-nodepty.cjs in the source distribution. Original license notices are retained.\n\n${notices}`);
  console.log("Built extension, terminal board, and test modules.");
}).catch(error => { console.error(error); process.exitCode = 1; });
