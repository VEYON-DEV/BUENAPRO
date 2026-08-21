import test from "node:test";
import assert from "node:assert/strict";
import { buildCodexArgs, codexSandboxMode } from "./codexCommand.ts";

test("aplica el sandbox configurado antes de reanudar una sesión Codex", () => {
  const args = buildCodexArgs({
    prompt: "continúa",
    schemaPath: "/workspace/schema.json",
    workspacePath: "/workspace",
    threadId: "thread-123",
    model: "gpt-5.5",
    sandboxMode: "danger-full-access",
  });

  assert.deepEqual(args.slice(0, 4), [
    "exec",
    "--sandbox",
    "danger-full-access",
    "resume",
  ]);
  assert.ok(args.indexOf("--sandbox") < args.indexOf("resume"));
  assert.equal(args.at(-2), "thread-123");
  assert.equal(args.at(-1), "continúa");
});

test("aplica el mismo sandbox al iniciar una sesión Codex", () => {
  const args = buildCodexArgs({
    prompt: "inicia",
    schemaPath: "/workspace/schema.json",
    workspacePath: "/workspace",
    sandboxMode: "danger-full-access",
  });

  assert.deepEqual(args.slice(0, 3), [
    "exec",
    "--sandbox",
    "danger-full-access",
  ]);
  assert.equal(args.includes("resume"), false);
});

test("usa workspace-write ante una configuración de sandbox inválida", () => {
  assert.equal(codexSandboxMode("sin-validar"), "workspace-write");
});
