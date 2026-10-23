"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { once } = require("node:events");
const { spawn } = require("node:child_process");
const { createHash, randomUUID } = require("node:crypto");

const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const sha256 = bytes => createHash("sha256").update(bytes).digest("hex");
const installed = path.resolve(process.argv[2]);
const ownerMode = process.argv[3] === "--owner-fixture";
const target = `${process.platform}-${process.arch}`;
const model = "packaged-fixture-model";
const token = "packaged-memory-only-" + randomUUID();
const controlToken = randomUUID();
const helper = path.join(installed, "media/local-terminal.cjs");
const secrets = new Set([token, controlToken]);
const activeTerminals = new Set();
let pty, localApi, localInvocation;

function sanitized(value) {
  let text = String(value);
  for (const secret of secrets) if (secret) text = text.split(secret).join("[REDACTED]");
  return text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

function inspectInstallation() {
  assert.ok(process.versions.electron, "Run this probe with the installed VS Code Electron executable.");
  assert.equal(require(path.join(installed, "native-manifest.json")).target, target);
  const manifestBytes = fs.readFileSync(path.join(installed, "local-helper-manifest.json"));
  const manifest = JSON.parse(manifestBytes);
  assert.equal(manifest.schema, 1);
  assert.ok(Array.isArray(manifest.files));
  assert.deepEqual(manifest.files.map(item => item.path).sort(), ["media/local-api.cjs", "media/local-terminal.cjs"]);
  for (const entry of manifest.files) {
    const file = path.join(installed, entry.path), stat = fs.lstatSync(file);
    assert.ok(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1, "Installed helper must be an ordinary unshared file.");
    assert.equal(fs.realpathSync(file), path.join(fs.realpathSync(installed), entry.path));
    assert.equal(sha256(fs.readFileSync(file)), entry.sha256, "Installed helper hash mismatch.");
  }
  pty = require(path.join(installed, "node_modules/node-pty"));
  localApi = require(path.join(installed, "media/local-api.cjs"));
  localInvocation = require(helper).localInvocation;
  assert.equal(typeof localInvocation, "function", "Installed helper must expose its platform invocation.");
  return { manifest_sha256: sha256(manifestBytes), files: manifest.files };
}

function childEnvironment(endpoint, secret) {
  const env = {};
  for (const key of ["SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"]) {
    if (process.env[key]) env[key] = process.env[key];
  }
  return { ...env, TERM: "xterm-256color", ELECTRON_RUN_AS_NODE: "1",
    ...(endpoint ? { PATCHPORT_ENDPOINT: endpoint, PATCHPORT_TOKEN: secret } : {}) };
}

async function waitFor(check, label, errors = [], timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (!check()) {
    if (errors.length) throw errors[0];
    if (Date.now() >= deadline) throw Error(`Packaged local timeout: ${label}`);
    await pause(15);
  }
  if (errors.length) throw errors[0];
}

async function serve(handler, errors) {
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("error", () => {});
    response.on("error", () => {});
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => Promise.resolve().then(() => handler(request, response, Buffer.concat(chunks).toString("utf8")))
      .catch(error => { errors.push(error); response.destroy(); }));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { origin: `http://127.0.0.1:${server.address().port}`, close: async () => {
    server.closeAllConnections();
    await Promise.race([new Promise(resolve => server.close(resolve)), pause(1000)]);
  } };
}

function streamFrame(backend, content, complete) {
  if (backend === "ollama") return JSON.stringify({ model, message: { role: "assistant", content }, done: complete,
    ...(complete ? { done_reason: "stop", prompt_eval_count: 5, eval_count: 7 } : {}) }) + "\n";
  return "data: " + JSON.stringify({ model, choices: [{ index: 0, delta: { content }, finish_reason: complete ? "stop" : null }] }) +
    "\n\n" + (complete ? "data: [DONE]\n\n" : "");
}

async function modelServer(backend, delayed = false) {
  const errors = [], requests = [], responses = [], closed = [];
  const server = await serve((request, response, raw) => {
    assert.equal(request.method, "POST");
    assert.equal(request.url, backend === "ollama" ? "/api/chat" : "/v1/chat/completions");
    assert.ok(backend === "lmstudio" ? request.headers.authorization === `Bearer ${token}` : !request.headers.authorization,
      "Model authentication header did not match the fixture.");
    const body = JSON.parse(raw);
    assert.equal(body.model, model); assert.equal(body.stream, true);
    requests.push(body); responses.push(response); closed.push(false);
    const index = requests.length - 1;
    response.on("close", () => { closed[index] = true; });
    response.setHeader("Content-Type", backend === "ollama" ? "application/x-ndjson" : "text/event-stream");
    if (delayed) response.write(streamFrame(backend, "partial fixture answer", false));
    else response.end(streamFrame(backend, "Completed fixture answer \uD55C\uAE00 \uD83D\uDE42", true));
  }, errors);
  return { ...server, errors, requests, responses, closed, late: () => {
    for (const response of responses) response.end(streamFrame(backend, "LATE_RESPONSE_MUST_NOT_APPEAR", true));
  } };
}

async function controlServer(backend, origin, modelSecret = token, sessionSecret = controlToken) {
  const calls = [], commands = [], history = [], errors = [];
  const identity = { helperId: randomUUID(), generation: randomUUID(), coreEpoch: "packaged-epoch-1" };
  const config = { backend, endpointOrigin: origin, modelId: model, contextWindow: 8192, maxOutputTokens: 512,
    requestTimeoutMs: 15000, idleTimeoutMs: 12000, executionLocationConfirmed: true,
    ...(backend === "lmstudio" ? { token: modelSecret } : {}) };
  const client = localApi.createLocalClient(config);
  let failPoll = false, helperPid;
  const server = await serve((request, response, raw) => {
    assert.equal(request.method, "POST"); assert.equal(request.url, "/events/packaged-local");
    assert.ok(request.headers.authorization === `Bearer ${sessionSecret}`, "Control authentication header mismatch.");
    const message = JSON.parse(raw); calls.push(message);
    assert.equal(message.local, 1);
    if (message.type !== "bootstrap") for (const [key, value] of Object.entries(identity)) assert.equal(message[key], value);
    let reply = {};
    if (message.type === "bootstrap") {
      assert.ok(Number.isInteger(message.processId) && message.processId > 0 && message.processId !== process.pid,
        "Bootstrap must identify the actual helper process.");
      assert.equal(helperPid, undefined, "Helper bootstrap must not repeat.");
      helperPid = message.processId;
      reply = { ...identity, config };
    }
    else if (message.type === "poll") {
      if (failPoll) { response.destroy(); return; }
      reply = { commands: commands.splice(0) };
    } else if (message.type === "prepare") reply = { messages: [...history, { role: "user", content: message.prompt }] };
    else if (message.type === "authorize") {
      const expected = client.prepareRequest({ requestId: message.requestId, messages: [...history, { role: "user", content: message.prompt }] });
      assert.deepEqual(message.binding, expected);
      assert.equal(calls.filter(item => item.type === "authorize" && item.requestId === message.requestId).length, 1);
      reply = { grant: randomUUID() };
    } else if (message.type === "finish") {
      reply = { state: message.result.state };
      if (message.result.state === "COMPLETED") {
        const prepared = calls.find(item => item.type === "prepare" && item.requestId === message.requestId);
        assert.ok(prepared);
        history.push({ role: "user", content: prepared.prompt }, { role: "assistant", content: message.result.content });
      }
    } else assert.ok(["draft", "cancel", "reject"].includes(message.type));
    response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify({ ok: true, ...reply }));
  }, errors);
  return { ...server, calls, commands, errors, helperPid: () => helperPid, disconnect: () => { failPoll = true; } };
}

function terminal(control, sessionSecret = controlToken) {
  let output = "", exit;
  const invocation = localInvocation(process.execPath, helper);
  const child = pty.spawn(invocation.file, invocation.args, { name: "xterm-256color", cols: 64, rows: 16,
    cwd: installed, env: { ...childEnvironment(`${control.origin}/events/packaged-local`, sessionSecret), ...invocation.env } });
  child.onData(data => { output += data; });
  child.onExit(value => { exit = value; activeTerminals.delete(session); });
  const session = { child, invocation, output: () => output, exit: () => exit, clean: async () => {
    const actualPid = control.helperPid();
    if (exit && (!actualPid || !processRunning(actualPid))) return;
    if (!exit) { try { child.kill(); } catch { /* Already exited. */ } }
    const deadline = Date.now() + 750;
    while (!exit && Date.now() < deadline) await pause(15);
    // A console wrapper has its own PID; clean up the authenticated helper as well.
    for (const pid of new Set([actualPid, ...(!exit ? [child.pid] : [])])) {
      if (!Number.isInteger(pid) || pid <= 0 || !processRunning(pid)) continue;
      try { process.kill(pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    await pause(250);
    activeTerminals.delete(session);
  } };
  activeTerminals.add(session);
  return session;
}

function assertSecretsAbsent(value, secrets = [token, controlToken]) {
  assert.ok(secrets.every(secret => !value.includes(secret)), "A fixture secret appeared in helper output or metadata.");
}

async function ordinaryCase(backend, scenario) {
  const started = Date.now(), api = await modelServer(backend, scenario !== "completion");
  let control, session;
  const check = async (predicate, label) => {
    await waitFor(() => { if (api.errors.length) throw api.errors[0]; return predicate(); }, `${backend}/${scenario}/${label}`, control.errors);
  };
  try {
    control = await controlServer(backend, api.origin); session = terminal(control);
    await check(() => control.calls.some(call => call.type === "poll" && call.ready), "ready");
    assert.ok(processRunning(control.helperPid()));
    assert.equal(control.helperPid() === session.child.pid, process.platform !== "win32");
    assert.equal(api.requests.length, 0);
    if (scenario === "readiness") {
      session.child.write("/exit\r");
      await check(() => session.exit(), "explicit exit"); assert.equal(session.exit().exitCode, 0);
    } else if (scenario === "completion") {
      const prompt = "\uD55C\uAE00 \uD83D\uDE42\nsecond line";
      session.child.write("\u001b[200~" + prompt + "\u001b[201~");
      await check(() => control.calls.some(call => call.type === "draft" && call.dirty), "pasted draft");
      assert.equal(api.requests.length, 0);
      session.child.resize(91, 22); session.child.write("\r");
      await check(() => control.calls.some(call => call.type === "finish"), "human completion");
      assert.equal(api.requests[0].messages.at(-1).content, prompt);
      const previousPolls = control.calls.filter(call => call.type === "poll" && call.ready).length;
      await check(() => control.calls.filter(call => call.type === "poll" && call.ready).length > previousPolls, "idle ACK");
      const command = { type: "submit", requestId: randomUUID(), deliveryId: "packaged-peer", prompt: "/exit\nTyped peer \uD55C\uAE00" };
      control.commands.push(command);
      await check(() => control.calls.filter(call => call.type === "finish").length === 2, "typed peer completion");
      assert.equal(api.requests[1].messages.at(-1).content, command.prompt);
      assert.equal(api.requests[1].messages.length, 3);
      control.commands.push(command); await pause(350);
      assert.equal(api.requests.length, 2); assert.equal(session.exit(), undefined);
      assert.ok(control.calls.filter(call => call.type === "finish").every(call => call.result.state === "COMPLETED"));
      session.child.write("/exit\r");
      await check(() => session.exit(), "explicit exit"); assert.equal(session.exit().exitCode, 0);
      assert.match(session.output(), /Completed fixture answer \uD55C\uAE00 \uD83D\uDE42/);
    } else {
      session.child.write("slow request\r");
      await check(() => session.output().includes("partial fixture answer"), "partial output");
      if (scenario === "cancel") {
        session.child.write("\u0003");
        await check(() => control.calls.some(call => call.type === "finish"), "cancel finish");
        const result = control.calls.find(call => call.type === "finish").result;
        assert.equal(result.state, "CANCELLED"); assert.equal(result.serverCancelUnconfirmed, true);
        assert.equal(control.calls.filter(call => call.type === "cancel").length, 1);
        await check(() => api.closed[0], "cancelled model socket");
        api.late(); await pause(200);
        assert.ok(!session.output().includes("LATE_RESPONSE_MUST_NOT_APPEAR"));
        assert.ok(!session.output().includes("[COMPLETED]"));
        session.child.write("/exit\r");
        await check(() => session.exit(), "explicit exit"); assert.equal(session.exit().exitCode, 0);
      } else {
        control.disconnect();
        await check(() => session.exit() && api.closed[0], "control failure exit");
        assert.equal(session.exit().exitCode, 1);
        assert.equal(control.calls.filter(call => call.type === "finish").length, 0);
      }
      assert.equal(api.requests.length, 1);
    }
    assertSecretsAbsent(session.output()); assertSecretsAbsent(JSON.stringify(control.calls));
    await check(() => !processRunning(control.helperPid()), "actual helper process exit");
    return { backend, scenario, passed: true, mock_generation_posts: api.requests.length,
      dispatch_grants: control.calls.filter(call => call.type === "authorize").length,
      finish_states: control.calls.filter(call => call.type === "finish").map(call => call.result.state),
      exit_code: session.exit().exitCode, output_secrets_absent: true, helper_exit_observed: true,
      helper_process_id: control.helperPid(), pty_process_id: session.child.pid,
      fixed_console_wrapper: process.platform === "win32",
      ...(scenario === "completion" ? { unicode_input: true, multiline_paste: true, resized_to: [91, 22], typed_peer: true, duplicate_peer_posts: 0, physical_ime_tested: false }
        : scenario === "readiness" ? { bootstrap_ready: true }
        : { model_socket_closed: api.closed[0], no_automatic_retry: true }), duration_ms: Date.now() - started };
  } catch (error) {
    console.error(JSON.stringify({ diagnostic: `${backend}/${scenario}`, helper_pid: control?.helperPid(), pty_pid: session?.child.pid,
      helper_exit: session?.exit() ?? null, control_frame_types: control?.calls.map(call => call.type) ?? [],
      mock_generation_posts: api.requests.length, helper_output_tail: sanitized(session?.output().slice(-3000) ?? "") }));
    throw error;
  } finally {
    try { await session?.clean(); }
    finally { try { await control?.close(); } finally { await api.close(); } }
  }
}

function processRunning(pid) {
  try {
    process.kill(pid, 0);
    if (process.platform === "linux") {
      const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
      if (stat.slice(stat.lastIndexOf(")") + 2).startsWith("Z")) return false;
    }
    return true;
  } catch (error) { if (error.code === "ESRCH" || error.code === "ENOENT") return false; throw error; }
}

async function ownerExitCase(backend) {
  const started = Date.now(), api = await modelServer(backend, true), messages = [], errors = [];
  let exit, helperPid, ptyPid, stderr = "";
  const owner = spawn(process.execPath, [__filename, installed, "--owner-fixture"], {
    env: childEnvironment(), windowsHide: true, stdio: ["ignore", "ignore", "pipe", "ipc"]
  });
  owner.on("error", error => errors.push(error)); owner.on("exit", code => { exit = { code }; });
  owner.on("message", message => {
    messages.push(message);
    if (message.type === "ready") { helperPid = message.helper_pid; ptyPid = message.pty_pid; }
  });
  owner.stderr.on("data", data => { stderr += data; });
  owner.send({ backend, origin: api.origin, modelSecret: token, sessionSecret: controlToken });
  try {
    await waitFor(() => helperPid, `${backend}/owner ready`, errors);
    await waitFor(() => api.requests.length === 1, `${backend}/owner model request`, api.errors);
    owner.send({ terminate: true });
    await waitFor(() => exit && api.closed[0] && !processRunning(helperPid) && !processRunning(ptyPid), `${backend}/owner and helper exit`, errors);
    assert.equal(exit.code, 17);
    const beforeExit = messages.find(message => message.type === "before_exit");
    assert.ok(beforeExit); assert.equal(beforeExit.dispatch_grants, 1); assert.equal(beforeExit.finish_count, 0);
    assert.equal(beforeExit.output_secrets_absent, true);
    api.late(); await pause(200);
    assert.equal(api.requests.length, 1);
    assertSecretsAbsent(stderr); assertSecretsAbsent(JSON.stringify(messages));
    assert.equal(stderr, "");
    return { backend, scenario: "owner_process_exit", passed: true, mock_generation_posts: 1,
      dispatch_grants: beforeExit.dispatch_grants, finish_states: [],
      owner_exit_code: exit.code, helper_exit_observed: true, helper_exit_method: "process no longer running",
      helper_process_id: helperPid, pty_process_id: ptyPid, pty_process_exit_observed: true,
      fixed_console_wrapper: process.platform === "win32",
      model_socket_closed: true, no_automatic_retry: true, output_secrets_absent: true, duration_ms: Date.now() - started };
  } finally {
    if (!exit) owner.kill();
    for (const pid of new Set([helperPid, ptyPid])) {
      if (pid && processRunning(pid)) { try { process.kill(pid, "SIGKILL"); } catch { /* Already exited. */ } }
    }
    await api.close();
  }
}

async function ownerFixture() {
  inspectInstallation();
  const config = await new Promise(resolve => process.once("message", resolve));
  secrets.add(config.modelSecret); secrets.add(config.sessionSecret);
  const control = await controlServer(config.backend, config.origin, config.modelSecret, config.sessionSecret);
  const session = terminal(control, config.sessionSecret);
  await waitFor(() => control.calls.some(call => call.type === "poll" && call.ready), "owner fixture ready", control.errors);
  assert.ok(processRunning(control.helperPid()));
  process.send({ type: "ready", helper_pid: control.helperPid(), pty_pid: session.child.pid });
  process.on("message", message => {
    if (message.terminate !== true) return;
    assertSecretsAbsent(session.output(), [config.modelSecret, config.sessionSecret]);
    assertSecretsAbsent(JSON.stringify(control.calls), [config.modelSecret, config.sessionSecret]);
    process.send({ type: "before_exit", dispatch_grants: control.calls.filter(call => call.type === "authorize").length,
      finish_count: control.calls.filter(call => call.type === "finish").length, output_secrets_absent: true }, () => process.exit(17));
  });
  session.child.write("owner disappearance request\r");
}

async function main() {
  const integrity = inspectInstallation(), cases = [];
  const readinessOnly = process.argv.includes("--readiness-only");
  if (readinessOnly) cases.push(await ordinaryCase("ollama", "readiness"));
  else {
    for (const backend of ["ollama", "lmstudio"]) {
      for (const scenario of ["completion", "cancel", "control_disconnect"]) cases.push(await ordinaryCase(backend, scenario));
      cases.push(await ownerExitCase(backend));
    }
  }
  const report = { passed: true, target, electron: process.versions.electron, node: process.versions.node,
    installed_package: true, helper_executable: process.execPath, external_node_used: false,
    installed_helper_integrity: integrity, cases, mock_generation_posts: cases.reduce((sum, item) => sum + item.mock_generation_posts, 0),
    actual_ai_calls: 0, physical_ime_tested: false };
  assert.equal(cases.length, readinessOnly ? 1 : 8); assert.equal(report.mock_generation_posts, readinessOnly ? 0 : 10);
  fs.writeFileSync(path.join(process.env.PATCHPORT_HOST_EVIDENCE, readinessOnly ? "packaged-local-readiness-result.json" : "packaged-local-result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
}

(ownerMode ? ownerFixture() : main()).catch(async error => {
  console.error(sanitized(error.stack || error));
  await Promise.allSettled([...activeTerminals].map(session => session.clean()));
  process.exitCode = 1;
  // Native PTY resources must not turn a failed probe into an unbounded launcher wait.
  setTimeout(() => process.exit(1), 500).unref();
});
