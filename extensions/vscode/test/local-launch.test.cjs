"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { localInvocation } = require("../media/local-terminal.cjs");

const command = "/d /v:off /s /c \"\"%PATCHPORT_LOCAL_EXECUTABLE%\" \"%PATCHPORT_LOCAL_HELPER%\"\"";
const executable = "C:\\Program Files\\Microsoft VS Code\\Code.exe";
const helper = "D:\\PatchPort\\media\\local-terminal.cjs";
const runtime = (platform = "win32", electron = "42.0.0", env = {}) => Object.freeze({
  platform,
  versions: Object.freeze(electron ? { electron } : {}),
  env: Object.freeze({ SystemRoot: "C:\\Windows", ...env })
});

test("Windows Electron launch uses a fixed console command and preserves special paths as environment data", () => {
  const win = runtime();
  for (const name of ["with spaces", "ampersand & name", "%NAME%", "!name!", "(parentheses)", "\uD55C\uAE00 \uD83D\uDE42", "caret^name"]) {
    const binary = "C:\\" + name + "\\Code.exe";
    const entry = "D:\\" + name + "\\media\\local-terminal.cjs";
    const invocation = localInvocation(binary, entry, win);
    assert.equal(invocation.file, "C:\\Windows\\System32\\cmd.exe");
    assert.equal(invocation.args, command);
    assert.equal(typeof invocation.args, "string", "cmd must receive the fixed raw command without CRT argument escaping");
    assert.deepEqual(invocation.env, { PATCHPORT_LOCAL_EXECUTABLE: binary, PATCHPORT_LOCAL_HELPER: entry });
    assert.ok(!invocation.args.includes(binary));
    assert.ok(!invocation.args.includes(entry));
  }
});

test("Windows launcher ignores inherited shell overrides and injected Node options", () => {
  const inherited = {
    SystemRoot: "D:\\Windows Folder",
    COMSPEC: "C:\\untrusted\\cmd.exe",
    PATH: "C:\\untrusted",
    NODE_OPTIONS: "--require injected.cjs",
    VSCODE_INSPECTOR_OPTIONS: "untrusted-inspector",
    PATCHPORT_LOCAL_EXECUTABLE: "C:\\untrusted\\replacement.exe",
    PATCHPORT_LOCAL_HELPER: "C:\\untrusted\\replacement.cjs"
  };
  const win = runtime("win32", "42.0.0", inherited);
  const before = { ...win.env };
  const invocation = localInvocation(executable, helper, win);
  assert.equal(invocation.file, "D:\\Windows Folder\\System32\\cmd.exe");
  assert.equal(invocation.args, command);
  assert.deepEqual(invocation.env, { PATCHPORT_LOCAL_EXECUTABLE: executable, PATCHPORT_LOCAL_HELPER: helper });
  assert.deepEqual(win.env, before);
  assert.ok(!Object.hasOwn(invocation.env, "NODE_OPTIONS"));
  assert.ok(!Object.hasOwn(invocation.env, "COMSPEC"));
});

test("local launcher rejects relative paths and command-breaking characters before launch", () => {
  const invalid = ["", "relative\\Code.exe", ".\\Code.exe", "..\\Code.exe", "C:Code.exe", "\\Code.exe", "/Code.exe",
    "C:\\bad\"quote\\Code.exe", "C:\\bad\nline\\Code.exe", "C:\\bad\rline\\Code.exe",
    "C:\\bad\u0000name\\Code.exe", "C:\\bad\tname\\Code.exe", null, undefined, 42];
  for (const value of invalid) {
    assert.throws(() => localInvocation(value, helper, runtime()), { code: "INVALID_HELPER_PATH" });
    assert.throws(() => localInvocation(executable, value, runtime()), { code: "INVALID_HELPER_PATH" });
  }
  for (const SystemRoot of ["relative", "C:Windows", "C:\\bad\"quote", "C:\\bad\nline", "C:\\bad\u0000name"]) {
    assert.throws(() => localInvocation(executable, helper, runtime("win32", "42.0.0", { SystemRoot })), { code: "INVALID_HELPER_PATH" });
  }
});

test("non-Electron Windows and POSIX launch directly without shell interpolation or inherited environment", () => {
  assert.deepEqual(localInvocation(executable, helper, runtime("win32", null, { NODE_OPTIONS: "--require unwanted.cjs" })),
    { file: executable, args: [helper], env: {} });
  for (const platform of ["linux", "darwin"]) {
    const binary = "/Applications/Code with spaces & !/Code";
    const entry = "/tmp/\uD55C\uAE00 $NAME (test)/media/local-terminal.cjs";
    assert.deepEqual(localInvocation(binary, entry, runtime(platform, "42.0.0", { NODE_OPTIONS: "--require unwanted.cjs" })),
      { file: binary, args: [entry], env: {} });
    assert.throws(() => localInvocation("relative/node", entry, runtime(platform)), { code: "INVALID_HELPER_PATH" });
    assert.throws(() => localInvocation(binary, "./helper.cjs", runtime(platform)), { code: "INVALID_HELPER_PATH" });
  }
});
