"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { once } = require("node:events");
const { createHash, randomUUID } = require("node:crypto");
const { CoreClient } = require("../dist/test/core");
const { createLocalClient } = require("../media/local-api.cjs");
const { python, temporaryRoot } = require("./helpers.cjs");

const extensionRoot = path.resolve(__dirname, "..");
const requestedModel = "fixture:latest";
const reportedModel = "fixture-canonical";
const digest = value => createHash("sha256").update(value, "utf8").digest("hex");

async function closeCore(core) {
  const child = core.child;
  if (!child || child.exitCode !== null) { core.dispose(); return; }
  let timer;
  const stopped = once(child, "exit");
  core.dispose();
  try {
    await Promise.race([stopped, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("The isolated core did not exit")), 5000);
    })]);
  } finally { clearTimeout(timer); }
}

async function coreFixture(t) {
  const project = await fs.mkdtemp(path.join(temporaryRoot, "patchport-local-core-"));
  const database = path.join(project, "state", "interactive.sqlite3");
  const core = new CoreClient(python, path.join(extensionRoot, "core"));
  core.retain("local-core-test");
  t.after(async () => {
    await closeCore(core);
    assert.ok(path.resolve(project).startsWith(path.resolve(temporaryRoot) + path.sep));
    await fs.rm(project, { recursive: true, force: true });
  });
  const capabilities = await core.request("capabilities");
  assert.ok(capabilities.actions.includes("local.prepare"));
  assert.ok(capabilities.actions.includes("local.dispatch"));
  const scoped = { database, project };
  const prepared = await core.request("history.prepare.preview", scoped);
  await core.request("history.apply", { plan_id: prepared.plan_id, apply: true });
  const schema = await core.request("local.schema.preview", scoped);
  assert.equal(schema.report.eligible, true);
  await core.request("local.schema.apply", { plan_id: schema.plan_id, apply: true });
  const owner = randomUUID(), session_id = randomUUID();
  await core.request("interactive.owner.open", { ...scoped, owner, apply: true });
  await core.request("interactive.begin", { ...scoped, owner, session_id,
    node: randomUUID(), provider: "local", name: "Local mock integration", apply: true });
  return { core, scoped, mutation: { database, owner, session_id, apply: true },
    context: () => core.request("local.context", { ...scoped, session_id }),
    result: request_id => core.request("local.result", { ...scoped, request_id }),
    report: () => core.request("interactive.report", scoped) };
}

async function runtimeFixture(t, backend) {
  const seen = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", value => chunks.push(value));
    request.on("end", () => {
      seen.push({ method: request.method, path: request.url,
        body: Buffer.concat(chunks).toString("utf8") });
      const turn = seen.length;
      const content = `Reply ${turn}: \uD55C\uAE00 \uD83D\uDE80`;
      if (backend === "ollama") {
        response.writeHead(200, { "Content-Type": "application/x-ndjson" });
        response.end(JSON.stringify({ model: reportedModel, message: { role: "assistant", content },
          done: true, done_reason: "stop", prompt_eval_count: turn * 2, eval_count: turn * 3 }) + "\n");
      } else {
        response.writeHead(200, { "Content-Type": "text/event-stream" });
        const frames = [
          { model: reportedModel, choices: [{ index: 0, delta: { role: "assistant", content }, finish_reason: "stop" }] },
          { model: reportedModel, choices: [], usage: { prompt_tokens: turn * 2, completion_tokens: turn * 3, total_tokens: turn * 5 } }
        ];
        response.end(frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("") + "data: [DONE]\n\n");
      }
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  const client = createLocalClient({ backend, endpointOrigin: origin + "/",
    modelId: requestedModel, modelAliases: [reportedModel],
    contextWindow: 4096, maxOutputTokens: 64, executionLocationConfirmed: true,
    requestTimeoutMs: 10000, idleTimeoutMs: 3000 });
  return { client, seen, origin };
}

function ledgerBinding(frozen, core) {
  return { helper_id: "isolated-helper", generation: "fixture-generation",
    core_epoch: "epoch-" + core.epoch, profile_revision: "fixture-revision",
    backend: frozen.backend, endpoint_origin: frozen.endpointOrigin,
    model_id: frozen.requestedModel, model_aliases: [reportedModel],
    body_sha256: frozen.bodySha256, origin: "human", delivery_id: null, root: null, hops: 0 };
}

function ledgerOutcome(outcome) {
  return { state: outcome.state, reason: outcome.reason || "STOP", content: outcome.content,
    reported_model: outcome.reportedModel,
    usage: { input_tokens: outcome.usage.inputTokens, output_tokens: outcome.usage.outputTokens } };
}

for (const backend of ["ollama", "lmstudio"]) {
  test(`${backend}: real JSON core authorizes exact HTTP bytes and atomically retains context and usage`,
    { timeout: 30000 }, async t => {
      const fixture = await coreFixture(t);
      const { core, mutation } = fixture;
      const { client, seen, origin } = await runtimeFixture(t, backend);
      const expectedContext = [];
      for (let turn = 1; turn <= 2; turn++) {
        const requestId = randomUUID();
        const prompt = `Question ${turn}: \uD55C\uAE00 \uD83D\uDE80`;
        const previous = await fixture.context();
        assert.deepEqual(previous.messages, expectedContext);
        let authorized, authorizationError;
        const outcome = await client.generate({ requestId,
          messages: [...previous.messages, { role: "user", content: prompt }],
          authorize: async frozen => {
            try {
              assert.equal(seen.length, turn - 1);
              assert.equal(frozen.endpointOrigin, origin);
              assert.equal(frozen.backend, backend);
              assert.equal(frozen.requestedModel, requestedModel);
              assert.equal(frozen.bodySha256, digest(frozen.requestBody));
              const prepared = await core.request("local.prepare", { ...mutation,
                request_id: requestId, binding: ledgerBinding(frozen, core), request_body: frozen.requestBody });
              assert.equal((await fixture.result(requestId)).state, "PREPARED");
              const grant = await core.request("local.dispatch", { ...mutation,
                request_id: requestId, binding_sha256: prepared.binding_sha256 });
              authorized = { frozen, binding_sha256: prepared.binding_sha256 };
              assert.equal(grant.state, "DISPATCH_AUTHORIZED");
              assert.equal(seen.length, turn - 1);
              return grant.grant;
            } catch (error) { authorizationError = error; throw error; }
          }
        });
        assert.equal(outcome.state, "COMPLETED", authorizationError?.stack || JSON.stringify(outcome));
        assert.equal(outcome.requestedModel, requestedModel);
        assert.equal(outcome.reportedModel, reportedModel);
        assert.equal(outcome.usage.status, "REPORTED");
        assert.equal(seen.length, turn);
        assert.equal(seen[turn - 1].method, "POST");
        assert.equal(seen[turn - 1].path, backend === "ollama" ? "/api/chat" : "/v1/chat/completions");
        assert.equal(seen[turn - 1].body, authorized.frozen.requestBody);
        const payload = JSON.parse(seen[turn - 1].body);
        assert.deepEqual(payload.messages, [...expectedContext, { role: "user", content: prompt }]);
        if (backend === "ollama") assert.deepEqual(payload.options, { num_ctx: 4096, num_predict: 64 });
        else assert.equal(payload.max_tokens, 64);
        const finishArgs = { ...mutation, request_id: requestId,
          binding_sha256: authorized.binding_sha256, outcome: ledgerOutcome(outcome) };
        const finished = await core.request("local.finish", finishArgs);
        assert.equal(finished.state, "COMPLETED");
        assert.equal(finished.duplicate, false);
        const stored = await fixture.result(requestId);
        assert.equal(stored.binding.body_sha256, digest(seen[turn - 1].body));
        assert.equal(stored.binding.endpoint_origin, origin);
        assert.equal(stored.binding.model_id, requestedModel);
        assert.deepEqual(stored.binding.model_aliases, [reportedModel]);
        assert.equal(stored.content, outcome.content);
        assert.equal(stored.usage.input_tokens, turn * 2);
        assert.equal(stored.usage.output_tokens, turn * 3);
        assert.equal(stored.usage.cost_usd, null);
        expectedContext.push({ role: "user", content: prompt }, { role: "assistant", content: outcome.content });
        assert.deepEqual((await fixture.context()).messages, expectedContext);
        const beforeDuplicate = await fixture.report();
        const duplicate = await core.request("local.finish", finishArgs);
        assert.equal(duplicate.duplicate, true);
        assert.deepEqual(await fixture.report(), beforeDuplicate);
        assert.equal(seen.length, turn);
      }
      const summary = (await fixture.report()).summary.metrics;
      assert.equal(summary.input_tokens.known_sum, 6);
      assert.equal(summary.output_tokens.known_sum, 9);
      assert.equal(summary.input_tokens.unknown_tasks, 0);
      assert.equal(summary.output_tokens.unknown_tasks, 0);
      assert.equal(summary.cost_usd.unknown_tasks, 1);
    });
}

test("lost durable dispatch response never POSTs, reissues a grant, or enters context",
  { timeout: 30000 }, async t => {
    const fixture = await coreFixture(t);
    const { core, mutation } = fixture;
    const { client, seen } = await runtimeFixture(t, "ollama");
    const requestId = randomUUID();
    let bindingSha256;
    const outcome = await client.generate({ requestId,
      messages: [{ role: "user", content: "Do not retry this question" }],
      authorize: async frozen => {
        const prepared = await core.request("local.prepare", { ...mutation, request_id: requestId,
          binding: ledgerBinding(frozen, core), request_body: frozen.requestBody });
        bindingSha256 = prepared.binding_sha256;
        await core.request("local.dispatch", { ...mutation, request_id: requestId,
          binding_sha256: bindingSha256 });
        throw new Error("Simulated loss after the durable dispatch commit");
      }
    });
    assert.equal(outcome.state, "UNCERTAIN");
    assert.equal(outcome.reason, "AUTHORIZATION_FAILED");
    assert.equal(outcome.postAttempted, false);
    assert.equal(seen.length, 0);
    const retained = await fixture.result(requestId);
    assert.equal(retained.state, "DISPATCH_AUTHORIZED");
    assert.equal(retained.dispatched, true);
    assert.equal(retained.grant_available, false);
    assert.equal(retained.usage.input_tokens, null);
    assert.deepEqual((await fixture.context()).messages, []);
    await assert.rejects(core.request("local.dispatch", { ...mutation, request_id: requestId,
      binding_sha256: bindingSha256 }), /LOCAL_GRANT_ALREADY_CONSUMED/);
    await assert.rejects(client.generate({ requestId,
      messages: [{ role: "user", content: "Do not retry this question" }],
      authorize: () => { throw new Error("Duplicate request reached authorization"); }
    }), { code: "REQUEST_ALREADY_USED" });
    assert.equal((await fixture.result(requestId)).state, "DISPATCH_AUTHORIZED");
    assert.equal(seen.length, 0);
  });
