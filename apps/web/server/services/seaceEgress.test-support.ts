import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import ts from "typescript";
import { resetSeaceEgressForTests } from "./seaceEgress.ts";

// Apoyo de las pruebas que comprueban que una pieza del servidor sale a SEACE solo por `seaceEgress`.
// Nunca toca la red: el fetch de undici es simulado y el `fetch` global del módulo probado está prohibido.

export type FakeReply = { status: number; body?: string | Uint8Array | ReadableStream | null; headers?: Record<string, string> };
export type FakeCall = { url: string; init: any };

/** Entorno de salida de prueba (sin proxy salvo que se indique) y un fetch de undici que consume `replies`. */
export function fakeEgress(replies: FakeReply[], env: Record<string, string> = {}) {
  const calls: FakeCall[] = [];
  Object.assign(process.env, {
    SEACE_EGRESS_PROXY: "", SEACE_EGRESS_REQUIRED: "", SEACE_SHARED_GATE: "", SEACE_USER_AGENT: "QA-Browser",
    WEB_SEACE_MIN_INTERVAL_SECONDS: "0", WEB_SEACE_MAX_WAIT_SECONDS: "20", WEB_SEACE_MAX_REQUESTS_PER_HOUR: "20",
    WEB_SEACE_INTERACTIVE_MAX_WAIT_SECONDS: "45", WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR: "60",
    ...env,
  });
  resetSeaceEgressForTests((async (url: string, init: any) => {
    calls.push({ url, init });
    const reply = replies.shift() ?? { status: 500 };
    return new Response((reply.body ?? null) as BodyInit | null, { status: reply.status, headers: reply.headers });
  }) as any);
  return { calls };
}

const nodeRequire = createRequire(import.meta.url);

/** Doble de `next/server` con lo mínimo que usan las rutas y los helpers HTTP. */
export const nextServerStub = (() => {
  class NextResponse extends Response {
    static json(data: unknown, init: ResponseInit = {}) {
      const headers = new Headers(init.headers);
      headers.set("content-type", "application/json");
      return new NextResponse(JSON.stringify(data), { ...init, headers });
    }
  }
  return { NextResponse };
})();

/**
 * Ejecuta un módulo TypeScript del servidor en un contexto aislado, con las dependencias inyectadas
 * (`node:*` se resuelve normal) y un `fetch` global que solo registra el intento y falla.
 */
export function loadServerModule(relativePath: string, dependencies: Record<string, unknown> = {}) {
  const source = readFileSync(new URL(relativePath, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const directFetches: string[] = [];
  const exports: Record<string, any> = {};
  vm.runInNewContext(compiled, {
    exports, process, console, Buffer, URL, URLSearchParams, Response, Headers, ReadableStream, TextDecoder, TextEncoder,
    AbortSignal, setTimeout, clearTimeout,
    fetch: async (input: unknown) => {
      directFetches.push(String(input));
      throw new Error("Unexpected direct fetch outside seaceEgress");
    },
    require: (id: string) => {
      if (id in dependencies) return dependencies[id];
      if (id.startsWith("node:")) return nodeRequire(id);
      throw new Error(`Unexpected dependency: ${id}`);
    },
  });
  return { exports, directFetches };
}
