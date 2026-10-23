"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { Ledger } = require("../dist/test/core");
const { projectKey } = require("../dist/test/model");

test("queued local reads and writes cannot bind to a replacement ledger session", async () => {
  const calls = [], core = { epoch: 1, request: async (...args) => { calls.push(args); return {}; } };
  const project = path.resolve("local-ledger-fixture"), database = path.join(project, "state.sqlite3");
  const ledger = new Ledger(core, project);
  ledger.owners.set(projectKey(project), { id: "owner", project, database, epoch: 1 });
  ledger.sessions.set("node", { id: "old-session", project, database, owner: "owner" });
  let release;
  ledger.chain = new Promise(resolve => { release = resolve; });
  const pending = [ledger.localContext("node"), ledger.localPrepare("node", "request", {}, "{}"),
    ledger.localDispatch("node", "request", "hash"), ledger.localFinish("node", "request", "hash", {})];
  const rejected = pending.map(promise => assert.rejects(promise, /session changed/));
  ledger.sessions.set("node", { id: "new-session", project, database, owner: "owner" });
  release(); await Promise.all(rejected);
  assert.deepEqual(calls, []);
  await ledger.localContext("node");
  assert.equal(calls[0][1].session_id, "new-session");
});
