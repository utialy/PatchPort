"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { randomUUID, createHash } = require("node:crypto");
const { once } = require("node:events");
const { createLocalClient, normalizeOrigin, LocalApiError, LIMITS } = require("../media/local-api.cjs");

const model = "test-model";
const messages = () => [{ role: "user", content: "Explain this text." }];
const grant = () => randomUUID();
const ollama = (extra = {}) => ({ model, message: { role: "assistant", content: "Hello" }, done: true, done_reason: "stop", ...extra });
const lm = (delta, finish_reason = null, extra = {}) => ({ model, choices: [{ index: 0, delta, finish_reason }], ...extra });
const ndjson = (...frames) => frames.map(frame => JSON.stringify(frame) + "\n").join("");
const sse = (...frames) => frames.map(frame => `data: ${typeof frame === "string" ? frame : JSON.stringify(frame)}\n\n`).join("");

async function fixture(t, handler, backend = "ollama", overrides = {}) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      const call = { method: req.method, path: req.url, headers: req.headers, body: Buffer.concat(chunks).toString("utf8") };
      seen.push(call);
      Promise.resolve(handler(req, res, call)).catch(error => { res.destroy(error); });
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const config = { backend, endpointOrigin: `http://127.0.0.1:${server.address().port}`, modelId: model,
    contextWindow: 4096, maxOutputTokens: 256, executionLocationConfirmed: true,
    requestTimeoutMs: 3000, idleTimeoutMs: 1000, ...overrides };
  return { client: createLocalClient(config), config, seen, server };
}

function respond(res, body, backend = "ollama", status = 200) {
  res.writeHead(status, { "Content-Type": backend === "ollama" ? "application/x-ndjson" : "text/event-stream" });
  res.end(body);
}
const generate = (client, overrides = {}) => client.generate({ requestId: randomUUID(), messages: messages(), authorize: grant, ...overrides });

test("origin rejects normalized numeric aliases, credentials, paths, queries, remote hosts and malformed ports", () => {
  assert.equal(normalizeOrigin("http://127.0.0.1:11434/"), "http://127.0.0.1:11434");
  assert.equal(normalizeOrigin("http://[::1]:1234"), "http://[::1]:1234");
  assert.equal(normalizeOrigin("http://localhost:1234"), "http://localhost:1234");
  assert.equal(normalizeOrigin("http://127.0.0.1:80"), "http://127.0.0.1");
  assert.equal(normalizeOrigin(normalizeOrigin("http://[::1]:80")), "http://[::1]");
  for (const origin of ["https://127.0.0.1:1234", "http://127.1:1234", "http://2130706433:1234",
    "http://0x7f000001:1234", "http://0177.0.0.1:1234", "http://127.0.0.2:1234",
    "http://127.0.0.1:0", "http://127.0.0.1:01234", "http://127.0.0.1:65536",
    "http://127.0.0.1:1234/api/chat", "http://127.0.0.1:1234?x=1", "http://127.0.0.1:1234#x",
    "http://token@127.0.0.1:1234", "http://localhost.:1234", "http://example.com:1234",
    "http://[::ffff:127.0.0.1]:1234", " http://127.0.0.1:1234", "http://%31%32%37.0.0.1:1234"]) {
    assert.throws(() => normalizeOrigin(origin), { code: "INVALID_ORIGIN" }, origin);
  }
});

test("frozen request is exactly authorized before one POST and caller mutation cannot change it", async t => {
  const { client, seen } = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const input = messages(), requestId = randomUUID();
  let binding;
  const resultPromise = generate(client, { requestId, messages: input, authorize: value => { binding = value; return pending; } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(seen.length, 0);
  assert.ok(Object.isFrozen(binding));
  assert.equal(binding.requestId, requestId);
  assert.equal(binding.bodySha256, createHash("sha256").update(binding.requestBody).digest("hex"));
  input[0].content = "Changed after prepare";
  release(randomUUID());
  const result = await resultPromise;
  assert.equal(result.state, "COMPLETED");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].body, binding.requestBody);
  assert.equal(JSON.parse(seen[0].body).messages[0].content, "Explain this text.");
  assert.deepEqual(JSON.parse(seen[0].body).options, { num_ctx: 4096, num_predict: 256 });
  assert.equal(seen[0].path, "/api/chat");
  assert.equal(result.usage.status, "UNKNOWN");
  assert.equal(result.usage.cost, null);
  await assert.rejects(generate(client, { requestId }), { code: "REQUEST_ALREADY_USED" });
  assert.equal(seen.length, 1);
});

test("authorization failure, invalid grant and repeated grant never retry or POST", async t => {
  const { client, seen } = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
  let result = await generate(client, { authorize: () => { throw Error("SECRET MUST NOT ESCAPE"); } });
  assert.equal(result.reason, "AUTHORIZATION_FAILED");
  assert.equal(result.state, "UNCERTAIN");
  assert.ok(!JSON.stringify(result).includes("SECRET"));
  result = await generate(client, { authorize: () => { throw new LocalApiError("AUTHORIZATION_DENIED"); } });
  assert.equal(result.state, "FAILED");
  assert.equal(result.reason, "AUTHORIZATION_DENIED");
  result = await generate(client, { authorize: () => { throw new LocalApiError("CONNECTION_LOST"); } });
  assert.equal(result.state, "UNCERTAIN");
  assert.equal(result.reason, "CONNECTION_LOST");
  assert.equal(result.postAttempted, false);
  result = await generate(client, { authorize: () => "" });
  assert.equal(result.reason, "INVALID_GRANT");
  assert.equal(seen.length, 0);
  const oneGrant = randomUUID();
  assert.equal((await generate(client, { authorize: () => oneGrant })).state, "COMPLETED");
  result = await generate(client, { authorize: () => oneGrant });
  assert.equal(result.reason, "GRANT_ALREADY_USED");
  assert.equal(seen.length, 1);
});

test("pre-abort and abort during pending authorization return immediately with POST zero", async t => {
  const { client, seen } = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
  const controller = new AbortController();
  controller.abort();
  let auth = 0;
  const first = await generate(client, { signal: controller.signal, authorize: () => { auth++; return grant(); } });
  assert.equal(first.state, "CANCELLED");
  assert.equal(auth, 0);
  const requestId = randomUUID();
  let release;
  const promise = generate(client, { requestId, authorize: () => new Promise(resolve => { release = resolve; }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(client.abort(requestId), true);
  const result = await promise;
  assert.equal(result.state, "CANCELLED");
  assert.equal(result.serverCancelUnconfirmed, false);
  release(randomUUID());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(seen.length, 0);
  assert.equal(client.abort(requestId), false);
});

test("Ollama handles byte-split UTF-8 and reports final usage without reasoning", async t => {
  const text = "\uD55C\uAE00 \uD83D\uDE80";
  const body = Buffer.from(ndjson(
    ollama({ message: { role: "assistant", content: text, thinking: "PRIVATE THINKING" }, done: false, done_reason: undefined }),
    ollama({ message: { role: "assistant", content: "" }, prompt_eval_count: 17, eval_count: 8 })));
  const { client } = await fixture(t, async (_req, res) => {
    res.writeHead(200, { "Content-Type": "application/x-ndjson" });
    for (const byte of body) { res.write(Buffer.from([byte])); await new Promise(resolve => setImmediate(resolve)); }
    res.end();
  });
  const display = [];
  const result = await generate(client, { onText: value => display.push(value) });
  assert.equal(result.state, "COMPLETED");
  assert.equal(result.content, text);
  assert.equal(display.join(""), text);
  assert.deepEqual(result.usage, { inputTokens: 17, outputTokens: 8, totalTokens: null, status: "REPORTED", cost: null });
  assert.ok(!JSON.stringify(result).includes("PRIVATE THINKING"));
});

test("LM Studio supports CRLF, comments, multiline data, usage after finish and [DONE]", async t => {
  const first = JSON.stringify(lm({ role: "assistant", content: "\uD55C\uAE00", reasoning_content: "SECRET" })).replace(",\"choices\"", ",\ndata: \"choices\"");
  const body = Buffer.from(`: keepalive\r\nevent: message\r\ndata: ${first}\r\n\r\n` + sse(
    lm({ content: " response" }, "stop"),
    { model, choices: [], usage: { prompt_tokens: 23, completion_tokens: 7, total_tokens: 30,
      completion_tokens_details: { reasoning_tokens: 4 }, prompt_tokens_details: { cached_tokens: 3 } } }, "[DONE]"));
  const { client, seen } = await fixture(t, async (_req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8" });
    for (const byte of body) { res.write(Buffer.from([byte])); await new Promise(resolve => setImmediate(resolve)); }
    res.end();
  }, "lmstudio");
  const result = await generate(client);
  assert.equal(result.state, "COMPLETED");
  assert.equal(result.content, "\uD55C\uAE00 response");
  assert.deepEqual(result.usage, { inputTokens: 23, outputTokens: 7, totalTokens: 30, status: "REPORTED", cost: null });
  assert.equal(seen[0].path, "/v1/chat/completions");
  const bodySent = JSON.parse(seen[0].body);
  assert.equal(bodySent.max_tokens, 256);
  assert.equal(bodySent.stream, true);
  assert.deepEqual(Object.keys(bodySent), ["model", "messages", "stream", "max_tokens"]);
});

test("GET model listing never generates or loads models and marks remote/embedding/unknown", async t => {
  for (const backend of ["ollama", "lmstudio"]) {
    await t.test(backend, async t => {
      const rows = backend === "ollama" ? { models: [{ model, capabilities: ["completion"] },
        { model: "embed", capabilities: ["embedding"] }, { model: "remote", remote_host: "https://example.invalid" }] } :
        { data: [{ id: model }, { id: "embed", type: "embedding" }, { id: "remote", execution_location: "remote" }] };
      const { client, seen } = await fixture(t, (_req, res) => {
        res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(rows));
      }, backend);
      const result = await client.listModels();
      assert.equal(result.inferenceVerified, false);
      assert.equal(result.models[0].executionLocation, "unknown");
      assert.equal(result.models[1].supported, false);
      assert.equal(result.models[2].supported, false);
      assert.equal(seen.length, 1);
      assert.equal(seen[0].method, "GET");
      assert.equal(seen[0].body, "");
      assert.equal(seen[0].path, backend === "ollama" ? "/api/tags" : "/v1/models");
    });
  }
});

test("unknown execution location and known unsupported model prevent authorization and POST", async t => {
  const { client, seen, config } = await fixture(t, (_req, res) => {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ models: [{ model, remote_model: "remote-instance" }] }));
  });
  let authorizations = 0;
  const authorize = () => { authorizations++; return grant(); };
  assert.equal((await generate(createLocalClient({ ...config, executionLocationConfirmed: false }), { authorize })).reason, "EXECUTION_LOCATION_UNCONFIRMED");
  await client.listModels();
  assert.equal((await generate(client, { authorize })).reason, "UNSUPPORTED_MODEL");
  assert.equal(authorizations, 0);
  assert.equal(seen.filter(call => call.method === "POST").length, 0);
});

test("redirect does not forward tokens; environment proxy is ignored; HTTP errors are sanitized", async t => {
  const token = "secret-api-token";
  const sink = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
  const { client, seen } = await fixture(t, (_req, res) => {
    res.writeHead(307, { Location: sink.config.endpointOrigin + "/secret" }); res.end(token);
  }, "lmstudio", { token });
  const result = await generate(client);
  assert.equal(result.reason, "REDIRECT_BLOCKED");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].headers.authorization, `Bearer ${token}`);
  assert.equal(sink.seen.length, 0);
  assert.ok(!JSON.stringify(result).includes(token));
  const previous = { HTTP_PROXY: process.env.HTTP_PROXY, http_proxy: process.env.http_proxy, ALL_PROXY: process.env.ALL_PROXY };
  try {
    for (const key of Object.keys(previous)) process.env[key] = sink.config.endpointOrigin;
    const direct = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
    assert.equal((await generate(direct.client)).state, "COMPLETED");
    assert.equal(sink.seen.length, 0);
  } finally {
    for (const [key, value] of Object.entries(previous)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  }
});

test("Ollama errors, early EOF, invalid schemas, tool calls, unknown finish and empty output cannot succeed", async t => {
  const cases = [
    ["server-error", ndjson({ error: "private upstream detail" }), "FAILED", "SERVER_ERROR"],
    ["invalid-json", "{broken}\n", "FAILED", "INVALID_JSON"],
    ["unfinished", ndjson(ollama({ done: false, done_reason: undefined })), "UNCERTAIN", "EARLY_EOF"],
    ["length", ndjson(ollama({ done_reason: "length" })), "TRUNCATED", "OUTPUT_LIMIT"],
    ["tool", ndjson(ollama({ message: { role: "assistant", content: "partial", tool_calls: [{ function: { name: "write_file" } }] } })), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["unknown-stop", ndjson(ollama({ done_reason: "load" })), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["missing-stop", ndjson(ollama({ done_reason: undefined })), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["empty", ndjson(ollama({ message: { content: "" } })), "FAILED", "EMPTY_RESPONSE"],
    ["reasoning-only", ndjson(ollama({ message: { thinking: "Hidden reasoning" } })), "FAILED", "EMPTY_RESPONSE"],
    ["missing-model", ndjson(ollama({ model: undefined })), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["extra-after-done", ndjson(ollama(), ollama()), "FAILED", "TRAILING_DATA"],
    ["remote-metadata", ndjson(ollama({ remote_host: "cloud.example" })), "FAILED", "REMOTE_MODEL"],
    ["nonstring-content", ndjson(ollama({ message: { content: ["text"] } })), "FAILED", "UNSUPPORTED_RESPONSE"]
  ];
  for (const [name, body, state, reason] of cases) await t.test(name, async t => {
    const { client, seen } = await fixture(t, (_req, res) => respond(res, body));
    const result = await generate(client);
    assert.equal(result.state, state);
    assert.equal(result.reason, reason);
    assert.equal(seen.length, 1);
  });
});

test("LM Studio requires one choice, known finish and [DONE], never finishes on EOF or HTTP 200", async t => {
  const cases = [
    ["no-done", sse(lm({ content: "partial" }, "stop")), "UNCERTAIN", "EARLY_EOF"],
    ["no-stop", sse(lm({ content: "partial" }), "[DONE]"), "UNCERTAIN", "EARLY_EOF"],
    ["length", sse(lm({ content: "partial" }, "length"), "[DONE]"), "TRUNCATED", "OUTPUT_LIMIT"],
    ["tool-finish", sse(lm({}, "tool_calls")), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["function-call", sse(lm({ function_call: { name: "exec" } }, "stop")), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["many-choices", sse({ model, choices: [lm({}).choices[0], lm({}).choices[0]] }), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["unknown-index", sse({ model, choices: [{ index: 1, delta: { content: "x" }, finish_reason: "stop" }] }, "[DONE]"), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["error-frame", "event: error\ndata: {\"error\":\"sensitive\"}\n\n", "FAILED", "SERVER_ERROR"],
    ["error-json", sse({ error: { message: "sensitive" } }), "FAILED", "SERVER_ERROR"],
    ["after-done", sse(lm({ content: "x" }, "stop"), "[DONE]", { choices: [], usage: { completion_tokens: 4 } }), "FAILED", "TRAILING_DATA"],
    ["content-after-finish", sse(lm({ content: "x" }, "stop"), lm({ content: "late" }), "[DONE]"), "FAILED", "UNSUPPORTED_RESPONSE"],
    ["unsupported-field", sse(lm({ content: "x", audio: { data: "unimplemented" } }, "stop"), "[DONE]"), "FAILED", "UNSUPPORTED_RESPONSE"]
  ];
  for (const [name, body, state, reason] of cases) await t.test(name, async t => {
    const { client, seen } = await fixture(t, (_req, res) => respond(res, body, "lmstudio"), "lmstudio");
    const result = await generate(client);
    assert.equal(result.state, state);
    assert.equal(result.reason, reason);
    assert.equal(seen.length, 1);
  });
});

test("models match exactly or a frozen explicit alias; mismatch preserves reported usage source", async t => {
  const { client, config } = await fixture(t, (_req, res) => respond(res, ndjson(ollama({ model: "canonical-model", prompt_eval_count: 3, eval_count: 2 }))));
  const mismatch = await generate(client);
  assert.equal(mismatch.reason, "UNSUPPORTED_RESPONSE");
  assert.equal(mismatch.reportedModel, "canonical-model");
  assert.equal(mismatch.usage.outputTokens, 2);
  assert.equal(mismatch.usage.status, "PARTIAL");
  const aliases = ["canonical-model"];
  const allowed = createLocalClient({ ...config, modelAliases: aliases });
  aliases.length = 0;
  assert.equal((await generate(allowed)).state, "COMPLETED");
});

test("usage duplicates are not summed, conflicts fail, missing values stay unknown or partial", async t => {
  const usage = { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 };
  const cases = [
    ["same", [usage, usage], "COMPLETED", "REPORTED"],
    ["conflict", [usage, { ...usage, completion_tokens: 8 }], "FAILED", "PARTIAL"],
    ["partial", [{ completion_tokens: 3 }], "COMPLETED", "PARTIAL"],
    ["missing", [], "COMPLETED", "UNKNOWN"],
    ["invalid", [{ prompt_tokens: -1 }], "FAILED", "UNKNOWN"]
  ];
  for (const [name, values, state, status] of cases) await t.test(name, async t => {
    const { client } = await fixture(t, (_req, res) => respond(res, sse(lm({ content: "answer" }, "stop"),
      ...values.map(value => ({ model, choices: [], usage: value })), "[DONE]"), "lmstudio"), "lmstudio");
    const result = await generate(client);
    assert.equal(result.state, state);
    assert.equal(result.usage.status, status);
    if (name === "same") assert.equal(result.usage.totalTokens, 5);
    if (name === "missing") assert.equal(result.usage.outputTokens, null);
  });
});

test("bounds reject oversized requests before authorization and never silently truncate responses", async t => {
  const { client, config, seen } = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
  let authorizations = 0;
  await assert.rejects(generate(client, { messages: [{ role: "user", content: "x".repeat(128 * 1024) }], authorize: () => { authorizations++; return grant(); } }), { code: "REQUEST_LIMIT" });
  await assert.rejects(generate(client, { messages: [{ role: "user", content: "x".repeat(4000) }] }), { code: "CONTEXT_LIMIT" });
  await assert.rejects(generate(client, { messages: [{ role: "user", content: "\ud800" }] }), { code: "INVALID_UTF8" });
  await assert.rejects(generate(client, { messages: [{ role: "user", content: "Hi", tools: [] }] }), { code: "INVALID_MESSAGES" });
  assert.equal(authorizations, 0);
  assert.equal(seen.length, 0);
  assert.throws(() => createLocalClient({ ...config, limits: { streamBytes: LIMITS.streamBytes + 1 } }), { code: "INVALID_LIMIT" });
  for (const [limits, reason] of [[{ frameBytes: 16 }, "FRAME_LIMIT"], [{ streamBytes: 16 }, "STREAM_LIMIT"], [{ answerCharacters: 3 }, "ANSWER_LIMIT"]]) {
    const result = await generate(createLocalClient({ ...config, limits }));
    assert.equal(result.state, "FAILED");
    assert.equal(result.reason, reason);
  }
});

test("message and alias limits match the durable request contract before authorization", async t => {
  const { config, seen } = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
  const client = createLocalClient({ ...config, contextWindow: 65536 });
  const history = Array.from({ length: 257 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", content: "x" }));
  assert.equal(JSON.parse(client.prepareRequest({ requestId: randomUUID(), messages: history }).requestBody).messages.length, 257);
  let authorizations = 0;
  await assert.rejects(generate(client, { messages: [...history, { role: "user", content: "extra" }],
    authorize: () => { authorizations++; return grant(); } }), { code: "INVALID_MESSAGES" });
  const aliases = Array.from({ length: 16 }, (_, index) => `alias-${index}`);
  assert.equal(createLocalClient({ ...config, modelAliases: aliases }).modelId, model);
  assert.throws(() => createLocalClient({ ...config, modelAliases: [...aliases, "extra"] }), { code: "INVALID_ALIASES" });
  assert.equal(authorizations, 0);
  assert.equal(seen.length, 0);
});

test("invalid UTF-8, status errors and unsupported content encoding remain terminal failures", async t => {
  for (const kind of ["utf8", "status", "encoding"]) await t.test(kind, async t => {
    const { client, seen } = await fixture(t, (_req, res) => {
      res.writeHead(kind === "status" ? 401 : 200, { "Content-Type": "application/x-ndjson", ...(kind === "encoding" ? { "Content-Encoding": "gzip" } : {}) });
      res.end(kind === "utf8" ? Buffer.from([0xc3, 0x28]) : "secret error response");
    });
    const result = await generate(client);
    assert.equal(result.state, "FAILED");
    assert.equal(result.reason, { utf8: "INVALID_UTF8", status: "HTTP_ERROR", encoding: "UNSUPPORTED_ENCODING" }[kind]);
    assert.equal(seen.length, 1);
    assert.ok(!JSON.stringify(result).includes("secret error response"));
  });
});

test("cancel aborts an active stream immediately and late completion cannot replace cancellation", async t => {
  let response;
  const { client, seen } = await fixture(t, (_req, res) => {
    response = res;
    res.writeHead(200, { "Content-Type": "application/x-ndjson" });
    res.write(ndjson(ollama({ done: false, done_reason: undefined })));
  });
  const requestId = randomUUID();
  const result = await generate(client, { requestId, onText: () => client.abort(requestId) });
  assert.equal(result.state, "CANCELLED");
  assert.equal(result.clientAborted, true);
  assert.equal(result.serverCancelUnconfirmed, true);
  assert.equal(result.usage.status, "UNKNOWN");
  response.end(ndjson(ollama({ prompt_eval_count: 5, eval_count: 9 })));
  assert.equal(result.state, "CANCELLED");
  assert.equal(seen.length, 1);
});

test("total timeout is not extended by incoming tokens; idle timeout and broken connection never retry", async t => {
  for (const kind of ["total", "idle", "broken"]) await t.test(kind, async t => {
    let interval;
    t.after(() => clearInterval(interval));
    const { client, seen } = await fixture(t, (_req, res) => {
      res.writeHead(200, { "Content-Type": "application/x-ndjson" });
      res.write(ndjson(ollama({ done: false, done_reason: undefined })));
      if (kind === "total") interval = setInterval(() => res.write("\n"), 5);
      if (kind === "broken") setImmediate(() => res.destroy());
    }, "ollama", { requestTimeoutMs: kind === "total" ? 100 : 2000, idleTimeoutMs: kind === "idle" ? 50 : 1000 });
    const result = await generate(client);
    assert.equal(result.state, "UNCERTAIN");
    assert.equal(result.reason, { total: "TOTAL_TIMEOUT", idle: "IDLE_TIMEOUT", broken: "CONNECTION_LOST" }[kind]);
    assert.equal(seen.length, 1);
  });
});

test("localhost is resolved and pinned to a numeric loopback address", async t => {
  const { config, seen } = await fixture(t, (_req, res) => respond(res, ndjson(ollama())));
  const client = createLocalClient({ ...config, endpointOrigin: config.endpointOrigin.replace("127.0.0.1", "localhost") });
  assert.equal((await generate(client)).state, "COMPLETED");
  assert.equal(seen.length, 1);
  assert.ok(seen[0].headers.host.startsWith("localhost:"));
});

test("cancellation during a callback ignores later frames in the very same TCP chunk", async t => {
  const { client } = await fixture(t, (_req, res) => respond(res, ndjson(
    ollama({ message: { content: "first" }, done: false, done_reason: undefined }),
    ollama({ message: { content: "LATE CONTENT" }, prompt_eval_count: 10, eval_count: 20 }))));
  const requestId = randomUUID(), display = [];
  const result = await generate(client, { requestId, onText: value => { display.push(value); client.abort(requestId); } });
  assert.equal(result.state, "CANCELLED");
  assert.equal(result.content, "first");
  assert.deepEqual(display, ["first"]);
  assert.equal(result.usage.status, "UNKNOWN");
});

test("pending authorization is bounded, concurrent generation is refused and a late grant never fires", async t => {
  const { client, seen } = await fixture(t, (_req, res) => respond(res, ndjson(ollama())), "ollama", { requestTimeoutMs: 50 });
  let release;
  const first = generate(client, { authorize: () => new Promise(resolve => { release = resolve; }) });
  await assert.rejects(generate(client), { code: "CLIENT_BUSY" });
  const result = await first;
  assert.equal(result.reason, "TOTAL_TIMEOUT");
  assert.equal(result.state, "UNCERTAIN");
  assert.equal(result.postAttempted, false);
  release(randomUUID());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(seen.length, 0);
});

test("a changed reported model cannot relabel already collected usage", async t => {
  const { client } = await fixture(t, (_req, res) => respond(res, sse(
    lm({ content: "answer" }, "stop", { usage: { prompt_tokens: 2, completion_tokens: 3 } }),
    { model: "different-model", choices: [], usage: { prompt_tokens: 200, completion_tokens: 300 } }, "[DONE]"), "lmstudio"), "lmstudio");
  const result = await generate(client);
  assert.equal(result.reason, "UNSUPPORTED_RESPONSE");
  assert.equal(result.reportedModel, model);
  assert.equal(result.usage.inputTokens, 2);
  assert.equal(result.usage.outputTokens, 3);
});

test("model-list authentication and malformed schemas cannot silently fall back to generation", async t => {
  for (const kind of ["authentication", "schema", "duplicates"]) await t.test(kind, async t => {
    const { client, seen } = await fixture(t, (_req, res) => {
      res.writeHead(kind === "authentication" ? 401 : 200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(kind === "authentication" ? { error: "secret failure" } : kind === "duplicates" ?
        { models: [{ model }, { model }] } : { data: [] }));
    });
    await assert.rejects(client.listModels(), { code: kind === "authentication" ? "HTTP_ERROR" : "UNSUPPORTED_RESPONSE" });
    assert.equal(seen.length, 1);
    assert.equal(seen[0].method, "GET");
  });
});

test("model discovery needs only a backend and origin; inference still requires explicit model and context", async t => {
  const { config, seen } = await fixture(t, (_req, res) => {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ models: [{ model }] }));
  });
  const client = createLocalClient({ backend: "ollama", endpointOrigin: config.endpointOrigin });
  assert.equal((await client.listModels()).models[0].modelId, model);
  await assert.rejects(generate(client), { code: "UNCONFIGURED_PROFILE" });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].method, "GET");
});
