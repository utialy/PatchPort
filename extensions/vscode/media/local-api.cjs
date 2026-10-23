"use strict";

// Transport only: durable authorization, context, and terminal CAS belong to the host/core.
const http = require("node:http");
const dns = require("node:dns").promises;
const { createHash } = require("node:crypto");
const { TextDecoder } = require("node:util");

const LIMITS = Object.freeze({ requestBytes: 128 * 1024, frameBytes: 256 * 1024,
  streamBytes: 2 * 1024 * 1024, answerCharacters: 32000 });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const sha256 = value => createHash("sha256").update(value, "utf8").digest("hex");
const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
const integer = (value, min, max) => Number.isSafeInteger(value) && value >= min && value <= max;

class LocalApiError extends Error {
  constructor(code) { super(code); this.name = "LocalApiError"; this.code = code; }
}
const fail = code => { throw new LocalApiError(code); };

function normalizeOrigin(value) {
  // Match the original spelling before URL can normalize numeric/escaped IP variants.
  if (typeof value !== "string") fail("INVALID_ORIGIN");
  const match = value.match(/^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(?::([1-9][0-9]{0,4}))?\/?$/);
  if (!match) fail("INVALID_ORIGIN");
  const port = match[2] === undefined ? 80 : Number(match[2]);
  if (!integer(port, 1, 65535)) fail("INVALID_ORIGIN");
  return new URL(value).origin;
}

function freeze(value) {
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function configuration(input) {
  if (!record(input) || !["ollama", "lmstudio"].includes(input.backend)) fail("INVALID_BACKEND");
  const modelId = input.modelId ?? null;
  if (modelId !== null && (typeof modelId !== "string" || !modelId.trim() || modelId.length > 512 || /[\x00-\x1f\x7f]/.test(modelId))) fail("INVALID_MODEL");
  const contextWindow = input.contextWindow ?? null, maxOutputTokens = input.maxOutputTokens ?? 1024;
  if ((contextWindow !== null && !integer(contextWindow, 1, 2097152)) || !integer(maxOutputTokens, 1, 32768) ||
      (contextWindow !== null && maxOutputTokens >= contextWindow)) fail("INVALID_TOKEN_LIMIT");
  const token = input.token;
  if (token !== undefined && (input.backend !== "lmstudio" || typeof token !== "string" || token.length > 4096 || !/^[\x21-\x7e]+$/.test(token))) fail("INVALID_TOKEN");
  const aliases = input.modelAliases ?? [];
  if (!Array.isArray(aliases) || aliases.length > 16 || aliases.some(item => typeof item !== "string" || !item.trim() || item.length > 512 || /[\x00-\x1f\x7f]/.test(item))) fail("INVALID_ALIASES");
  const limits = { ...LIMITS };
  if (input.limits !== undefined) {
    if (!record(input.limits)) fail("INVALID_LIMIT");
    for (const [key, value] of Object.entries(input.limits)) {
      if (!(key in LIMITS) || !integer(value, 1, LIMITS[key])) fail("INVALID_LIMIT");
      limits[key] = value;
    }
  }
  const requestTimeoutMs = input.requestTimeoutMs ?? 300000;
  const idleTimeoutMs = input.idleTimeoutMs ?? 60000;
  if (!integer(requestTimeoutMs, 1, 300000) || !integer(idleTimeoutMs, 1, 60000)) fail("INVALID_TIMEOUT");
  return freeze({ backend: input.backend, endpointOrigin: normalizeOrigin(input.endpointOrigin), modelId,
    contextWindow, maxOutputTokens,
    modelAliases: [...aliases], token, limits, requestTimeoutMs, idleTimeoutMs,
    executionLocationConfirmed: input.executionLocationConfirmed === true });
}

function buildRequest(config, requestId, messages) {
  if (config.modelId === null || config.contextWindow === null) fail("UNCONFIGURED_PROFILE");
  if (typeof requestId !== "string" || !UUID.test(requestId)) fail("INVALID_REQUEST_ID");
  if (!Array.isArray(messages) || messages.length === 0 || messages.length > 257) fail("INVALID_MESSAGES");
  const clean = messages.map(message => {
    if (!record(message) || Object.keys(message).some(key => !["role", "content"].includes(key)) ||
        !["system", "user", "assistant"].includes(message.role) || typeof message.content !== "string" ||
        !message.content.trim()) fail("INVALID_MESSAGES");
    // Unpaired surrogates must not silently change when serialized to UTF-8.
    if (Buffer.from(message.content, "utf8").toString("utf8") !== message.content) fail("INVALID_UTF8");
    return { role: message.role, content: message.content };
  });
  if (clean[clean.length - 1].role !== "user") fail("INVALID_MESSAGES");
  const body = { model: config.modelId, messages: clean, stream: true };
  if (config.backend === "ollama") body.options = { num_ctx: config.contextWindow, num_predict: config.maxOutputTokens };
  else body.max_tokens = config.maxOutputTokens;
  const requestBody = JSON.stringify(body);
  const requestBytes = Buffer.byteLength(requestBody, "utf8");
  if (requestBytes > config.limits.requestBytes) fail("REQUEST_LIMIT");
  // Conservative local budget; the runtime's tokenizer/context behavior remains separate.
  if (requestBytes + config.maxOutputTokens > config.contextWindow) fail("CONTEXT_LIMIT");
  return freeze({ requestId, backend: config.backend, endpointOrigin: config.endpointOrigin,
    requestedModel: config.modelId, bodySha256: sha256(requestBody), requestBytes, requestBody });
}

function remoteMetadata(value) {
  if (!record(value)) return false;
  return Boolean(value.remote_host || value.remote_model || value.remote === true || value.is_remote === true ||
    ["remote", "cloud", "lmlink", "lm_link"].includes(value.execution_location) ||
    ["remote", "cloud", "lmlink", "lm_link"].includes(value.location));
}

function modelsFrom(backend, value) {
  const rows = backend === "ollama" ? value?.models : value?.data;
  if (!Array.isArray(rows) || rows.length > 4096) fail("UNSUPPORTED_RESPONSE");
  const ids = new Set();
  return rows.map(row => {
    const modelId = backend === "ollama" ? row?.model ?? row?.name : row?.id;
    if (!record(row) || typeof modelId !== "string" || !modelId || modelId.length > 512 || ids.has(modelId) || /[\x00-\x1f\x7f]/.test(modelId)) fail("UNSUPPORTED_RESPONSE");
    ids.add(modelId);
    const embedding = ["embedding", "embeddings"].includes(row.type) ||
      (Array.isArray(row.capabilities) && row.capabilities.includes("embedding") && !row.capabilities.includes("completion"));
    const remote = remoteMetadata(row) || remoteMetadata(row.details);
    return { modelId, kind: embedding ? "embedding" : row.type === "llm" || (Array.isArray(row.capabilities) && row.capabilities.includes("completion")) ? "chat" : "unknown",
      executionLocation: remote ? "remote" : "unknown", supported: !embedding && !remote };
  });
}

function tokenCount(value) {
  if (value === undefined || value === null) return null;
  if (!integer(value, 0, Number.MAX_SAFE_INTEGER)) fail("UNSUPPORTED_RESPONSE");
  return value;
}

function parser(config, onText, signal) {
  const data = { content: "", reportedModel: null, finishReason: null, done: false,
    inputTokens: null, outputTokens: null, totalTokens: null };
  const allowedModels = new Set([config.modelId, ...config.modelAliases]);
  let usageSeen = null;
  function model(value) {
    if (value === undefined) return;
    if (typeof value !== "string" || !value || value.length > 512) fail("UNSUPPORTED_RESPONSE");
    if (data.reportedModel !== null && data.reportedModel !== value) fail("UNSUPPORTED_RESPONSE");
    data.reportedModel = value;
    if (!allowedModels.has(value)) fail("UNSUPPORTED_RESPONSE");
  }
  function usage(value, ollama = false) {
    if (value === null || value === undefined) return;
    if (!record(value)) fail("UNSUPPORTED_RESPONSE");
    const normalized = { inputTokens: tokenCount(ollama ? value.prompt_eval_count : value.prompt_tokens),
      outputTokens: tokenCount(ollama ? value.eval_count : value.completion_tokens),
      totalTokens: ollama ? null : tokenCount(value.total_tokens) };
    if (Object.values(normalized).every(item => item === null)) return;
    const serialized = JSON.stringify(normalized);
    if (usageSeen !== null && usageSeen !== serialized) fail("UNSUPPORTED_RESPONSE");
    usageSeen = serialized;
    Object.assign(data, normalized);
  }
  function content(value) {
    if (value === undefined || value === null) return;
    if (typeof value !== "string") fail("UNSUPPORTED_RESPONSE");
    if (Buffer.from(value, "utf8").toString("utf8") !== value) fail("INVALID_UTF8");
    if (data.content.length + value.length > config.limits.answerCharacters) fail("ANSWER_LIMIT");
    data.content += value;
    if (value && onText) {
      try { onText(value); } catch { fail("DISPLAY_CALLBACK_FAILED"); }
    }
    if (signal.aborted) fail(signal.reason?.code ?? "CANCELLED");
  }
  function delta(value) {
    if (!record(value)) fail("UNSUPPORTED_RESPONSE");
    if (value.role !== undefined && value.role !== "assistant") fail("UNSUPPORTED_RESPONSE");
    if ((value.tool_calls !== undefined && (!Array.isArray(value.tool_calls) || value.tool_calls.length > 0)) || value.function_call != null) fail("UNSUPPORTED_RESPONSE");
    // Reasoning/thinking are intentionally neither returned nor sent to the display callback.
    if (Object.keys(value).some(key => !["role", "content", "thinking", "reasoning", "reasoning_content", "tool_calls", "function_call"].includes(key))) fail("UNSUPPORTED_RESPONSE");
    content(value.content);
  }
  function frame(text) {
    if (signal.aborted) fail(signal.reason?.code ?? "CANCELLED");
    if (data.done) fail("TRAILING_DATA");
    if (config.backend === "lmstudio" && text === "[DONE]") {
      if (data.finishReason === null) fail("EARLY_EOF");
      data.done = true;
      return;
    }
    let item;
    try { item = JSON.parse(text); } catch { fail("INVALID_JSON"); }
    if (!record(item)) fail("UNSUPPORTED_RESPONSE");
    if (Object.hasOwn(item, "error")) fail("SERVER_ERROR");
    if (remoteMetadata(item)) fail("REMOTE_MODEL");
    if (data.reportedModel !== null && item.model !== undefined && item.model !== data.reportedModel) fail("UNSUPPORTED_RESPONSE");
    // Preserve reported usage and its model source even if the model is incompatible.
    if (config.backend === "ollama") usage(item, true); else usage(item.usage);
    model(item.model);
    if (config.backend === "ollama") {
      if (typeof item.done !== "boolean") fail("UNSUPPORTED_RESPONSE");
      if (item.message !== undefined) delta(item.message);
      else if (!item.done) fail("UNSUPPORTED_RESPONSE");
      if (item.done) {
        data.done = true;
        data.finishReason = item.done_reason;
        if (!["stop", "length"].includes(item.done_reason)) fail("UNSUPPORTED_RESPONSE");
      }
    } else {
      if (!Array.isArray(item.choices) || item.choices.length > 1) fail("UNSUPPORTED_RESPONSE");
      if (item.choices.length === 0) {
        if (!record(item.usage)) fail("UNSUPPORTED_RESPONSE");
        return;
      }
      const choice = item.choices[0];
      if (!record(choice) || choice.index !== 0 || data.finishReason !== null) fail("UNSUPPORTED_RESPONSE");
      delta(choice.delta);
      if (choice.finish_reason !== undefined && choice.finish_reason !== null) {
        data.finishReason = choice.finish_reason;
        if (!["stop", "length"].includes(choice.finish_reason)) fail("UNSUPPORTED_RESPONSE");
      }
    }
  }
  const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
  let line = "", lineBytes = 0, frameBytes = 0, event = [], eventType = "", skipLF = false;
  function endLine() {
    if (config.backend === "ollama") {
      if (line.trim()) frame(line);
    } else if (line === "") {
      if (eventType && !["message"].includes(eventType)) fail(eventType === "error" ? "SERVER_ERROR" : "UNSUPPORTED_RESPONSE");
      if (event.length) frame(event.join("\n"));
      event = []; eventType = ""; frameBytes = 0;
    } else if (line.startsWith("data:")) {
      event.push(line.slice(5).replace(/^ /, ""));
    } else if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (!line.startsWith(":") && !line.startsWith("id:") && !line.startsWith("retry:")) {
      fail("UNSUPPORTED_RESPONSE");
    }
    line = ""; lineBytes = 0;
  }
  function decode(text) {
    for (const character of text) {
      if (skipLF) { skipLF = false; if (character === "\n") continue; }
      const size = Buffer.byteLength(character, "utf8");
      lineBytes += size; frameBytes += size;
      if (lineBytes > config.limits.frameBytes || (config.backend === "lmstudio" && frameBytes > config.limits.frameBytes)) fail("FRAME_LIMIT");
      if (character === "\r" || character === "\n") {
        endLine(); skipLF = character === "\r";
      } else line += character;
    }
  }
  return {
    data,
    push(buffer) { let text; try { text = decoder.decode(buffer, { stream: true }); } catch { fail("INVALID_UTF8"); } decode(text); },
    end() {
      let rest; try { rest = decoder.decode(); } catch { fail("INVALID_UTF8"); } decode(rest);
      if (line || event.length) fail("EARLY_EOF");
      if (!data.done) fail("EARLY_EOF");
      if (data.reportedModel === null) fail("UNSUPPORTED_RESPONSE");
      if (!data.content.trim()) fail("EMPTY_RESPONSE");
      return data.finishReason === "length" ? "TRUNCATED" : "COMPLETED";
    }
  };
}

async function pinnedHost(origin) {
  const host = new URL(origin).hostname;
  if (host !== "localhost") return host === "[::1]" ? "::1" : host;
  let resolved;
  try { resolved = await dns.lookup("localhost", { all: true, verbatim: true }); }
  catch { fail("LOCALHOST_RESOLUTION_FAILED"); }
  if (!resolved.length || resolved.some(item => !["127.0.0.1", "::1"].includes(item.address))) fail("NON_LOOPBACK_RESOLUTION");
  return resolved.find(item => item.address === "127.0.0.1")?.address ?? "::1";
}

function abortable(promise, signal) {
  if (signal.aborted) {
    Promise.resolve(promise).catch(() => {});
    return Promise.reject(new LocalApiError(signal.reason?.code ?? "CANCELLED"));
  }
  return new Promise((resolve, reject) => {
    const abort = () => { cleanup(); reject(new LocalApiError(signal.reason?.code ?? "CANCELLED")); };
    const cleanup = () => signal.removeEventListener("abort", abort);
    signal.addEventListener("abort", abort, { once: true });
    Promise.resolve(promise).then(value => { cleanup(); resolve(value); }, error => { cleanup(); reject(error); });
  });
}

async function transport(config, method, path, body, signal, consume, attempted) {
  const address = await abortable(pinnedHost(config.endpointOrigin), signal);
  if (signal.aborted) fail(signal.reason?.code ?? "CANCELLED");
  const url = new URL(config.endpointOrigin);
  return new Promise((resolve, reject) => {
    let settled = false, idleTimer, request, response, totalBytes = 0;
    const finish = error => {
      if (settled) return;
      settled = true; clearTimeout(idleTimer); signal.removeEventListener("abort", abort);
      if (error) { response?.destroy(); request?.destroy(); reject(error); }
      else resolve();
    };
    const abort = () => finish(new LocalApiError(signal.reason?.code ?? "CANCELLED"));
    const idle = () => { clearTimeout(idleTimer); idleTimer = setTimeout(() => finish(new LocalApiError("IDLE_TIMEOUT")), config.idleTimeoutMs); };
    const headers = { Host: url.host, Accept: method === "GET" ? "application/json" : config.backend === "ollama" ? "application/x-ndjson" : "text/event-stream" };
    if (body !== undefined) { headers["Content-Type"] = "application/json"; headers["Content-Length"] = Buffer.byteLength(body); }
    if (config.token) headers.Authorization = `Bearer ${config.token}`;
    // Numeric pinned address + explicit agent:false avoids DNS rebinding, pooling and proxy env.
    request = http.request({ hostname: address, port: url.port || 80, path, method, headers, agent: false }, incoming => {
      response = incoming;
      if (incoming.statusCode !== 200) { finish(new LocalApiError(incoming.statusCode >= 300 && incoming.statusCode < 400 ? "REDIRECT_BLOCKED" : "HTTP_ERROR")); return; }
      const encoding = incoming.headers["content-encoding"];
      if (encoding && encoding !== "identity") { finish(new LocalApiError("UNSUPPORTED_ENCODING")); return; }
      const type = String(incoming.headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
      const types = method === "GET" ? ["application/json"] : config.backend === "ollama" ? ["application/x-ndjson", "application/json"] : ["text/event-stream"];
      if (!types.includes(type)) { finish(new LocalApiError("UNSUPPORTED_CONTENT_TYPE")); return; }
      incoming.on("data", chunk => {
        if (settled) return;
        idle(); totalBytes += chunk.length;
        try { if (totalBytes > config.limits.streamBytes) fail("STREAM_LIMIT"); consume(chunk); }
        catch (error) { finish(error instanceof LocalApiError ? error : new LocalApiError("INVALID_RESPONSE")); }
      });
      incoming.on("end", () => finish());
      incoming.on("aborted", () => finish(new LocalApiError("CONNECTION_LOST")));
      incoming.on("error", () => finish(new LocalApiError("CONNECTION_LOST")));
    });
    request.on("error", () => finish(new LocalApiError("CONNECTION_LOST")));
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) { abort(); return; }
    idle();
    attempted?.();
    request.end(body);
  });
}

function createLocalClient(input) {
  const config = configuration(input);
  const requests = new Set(), grants = new Set(), active = new Map(), blockedModels = new Set();
  function operation(signal) {
    const controller = new AbortController();
    const cancel = () => controller.abort(new LocalApiError("CANCELLED"));
    if (signal) {
      if (signal.aborted) cancel(); else signal.addEventListener("abort", cancel, { once: true });
    }
    const timer = setTimeout(() => controller.abort(new LocalApiError("TOTAL_TIMEOUT")), config.requestTimeoutMs);
    return { controller, close() { clearTimeout(timer); signal?.removeEventListener("abort", cancel); } };
  }
  return Object.freeze({
    origin: config.endpointOrigin,
    modelId: config.modelId,
    prepareRequest({ requestId, messages }) { return buildRequest(config, requestId, messages); },
    abort(requestId) { const controller = active.get(requestId); if (!controller) return false; controller.abort(new LocalApiError("CANCELLED")); return true; },
    async listModels({ signal } = {}) {
      const op = operation(signal), chunks = [];
      try {
        await transport(config, "GET", config.backend === "ollama" ? "/api/tags" : "/v1/models", undefined,
          op.controller.signal, chunk => chunks.push(chunk));
        let value;
        try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks))); }
        catch { fail("INVALID_MODEL_RESPONSE"); }
        const models = modelsFrom(config.backend, value);
        for (const model of models) if (!model.supported) blockedModels.add(model.modelId);
        return freeze({ models, inferenceVerified: false });
      } finally { op.close(); }
    },
    async generate({ requestId, messages, authorize, signal, onText }) {
      const binding = buildRequest(config, requestId, messages);
      if (typeof authorize !== "function" || (onText !== undefined && typeof onText !== "function")) fail("INVALID_CALLBACK");
      if (requests.has(requestId)) fail("REQUEST_ALREADY_USED");
      if (active.size) fail("CLIENT_BUSY");
      requests.add(requestId);
      const op = operation(signal), stream = parser(config, onText, op.controller.signal);
      let postAttempted = false, authorizationStarted = false, authorizationSettled = false;
      let state = "FAILED", reason = null;
      active.set(requestId, op.controller);
      try {
        if (!config.executionLocationConfirmed) fail("EXECUTION_LOCATION_UNCONFIRMED");
        if (blockedModels.has(config.modelId)) fail("UNSUPPORTED_MODEL");
        const grant = await abortable(Promise.resolve().then(() => {
          if (op.controller.signal.aborted) fail(op.controller.signal.reason?.code ?? "CANCELLED");
          authorizationStarted = true;
          return authorize(binding);
        }), op.controller.signal);
        authorizationSettled = true;
        if (op.controller.signal.aborted) fail(op.controller.signal.reason?.code ?? "CANCELLED");
        if (typeof grant !== "string" || grant.length < 16 || grant.length > 4096) fail("INVALID_GRANT");
        const grantHash = sha256(grant);
        if (grants.has(grantHash)) fail("GRANT_ALREADY_USED");
        grants.add(grantHash);
        await transport(config, "POST", config.backend === "ollama" ? "/api/chat" : "/v1/chat/completions",
          binding.requestBody, op.controller.signal, chunk => stream.push(chunk), () => { postAttempted = true; });
        if (op.controller.signal.aborted) fail(op.controller.signal.reason?.code ?? "CANCELLED");
        state = stream.end();
        if (state === "TRUNCATED") reason = "OUTPUT_LIMIT";
      } catch (error) {
        reason = error instanceof LocalApiError ? error.code : "AUTHORIZATION_FAILED";
        // A lost grant response can follow a durable dispatch commit even before any POST.
        const authorizationUnknown = authorizationStarted && !authorizationSettled &&
          !(error instanceof LocalApiError && error.code === "AUTHORIZATION_DENIED");
        state = reason === "CANCELLED" ? "CANCELLED" :
          authorizationUnknown || (["TOTAL_TIMEOUT", "IDLE_TIMEOUT", "CONNECTION_LOST", "EARLY_EOF"].includes(reason) && postAttempted) ? "UNCERTAIN" : "FAILED";
      } finally { active.delete(requestId); op.close(); }
      const data = stream.data;
      const counts = [data.inputTokens, data.outputTokens, data.totalTokens];
      const usageStatus = counts.every(value => value === null) ? "UNKNOWN" : state === "COMPLETED" && data.inputTokens !== null && data.outputTokens !== null ? "REPORTED" : "PARTIAL";
      return freeze({ requestId, bodySha256: binding.bodySha256, state, reason, content: data.content,
        requestedModel: config.modelId, reportedModel: data.reportedModel, finishReason: data.finishReason,
        postAttempted, clientAborted: state === "CANCELLED", serverCancelUnconfirmed: state === "CANCELLED" && postAttempted,
        usage: { inputTokens: data.inputTokens, outputTokens: data.outputTokens, totalTokens: data.totalTokens,
          status: usageStatus, cost: null } });
    }
  });
}

module.exports = { createLocalClient, normalizeOrigin, LocalApiError, LIMITS };
