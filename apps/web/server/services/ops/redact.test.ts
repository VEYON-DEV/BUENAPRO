import test from "node:test";
import assert from "node:assert/strict";
import { detailJson, redact, scrubText } from "./redact.ts";

test("los secretos se ocultan por el nombre de la clave, a cualquier profundidad", () => {
  const out = redact({
    api_key: "AIzaSyDONOTLEAK",
    Authorization: "Bearer abc.def",
    nested: { password: "hunter2", session_cookie: "x", ticket: "t-1", keep: "visible" },
    list: [{ access_token: "tok" }],
  }) as Record<string, any>;
  assert.equal(out.api_key, "[oculto]");
  assert.equal(out.Authorization, "[oculto]");
  assert.equal(out.nested.password, "[oculto]");
  assert.equal(out.nested.session_cookie, "[oculto]");
  assert.equal(out.nested.ticket, "[oculto]");
  assert.equal(out.nested.keep, "visible");
  assert.equal(out.list[0].access_token, "[oculto]");
  assert.doesNotMatch(JSON.stringify(out), /AIzaSy|hunter2|abc\.def|"tok"/);
});

test("los conteos de tokens no son secretos", () => {
  const out = redact({ input_tokens: 1200, output_tokens: 80, query_input_tokens: 5 }) as Record<string, number>;
  assert.deepEqual({ ...out }, { input_tokens: 1200, output_tokens: 80, query_input_tokens: 5 });
});

test("el texto de documentos y las respuestas crudas se omiten", () => {
  const out = redact({ raw_extraction_json: { big: "x" }, llm1_input: "texto del TDR", content: "…", response: { answers: {} }, quality: "auto" }) as Record<string, unknown>;
  assert.equal(out.raw_extraction_json, "[omitido]");
  assert.equal(out.llm1_input, "[omitido]");
  assert.equal(out.content, "[omitido]");
  assert.equal(out.response, "[omitido]");
  assert.equal(out.quality, "auto");
});

test("los textos largos y las listas largas se acotan", () => {
  const out = redact({ big: "a".repeat(5000), items: Array.from({ length: 100 }, (_, index) => index) }) as { big: string; items: unknown[] };
  assert.ok(out.big.length < 500);
  assert.match(out.big, /\+4600 caracteres/);
  assert.equal(out.items.length, 21);
  assert.match(String(out.items[20]), /\+80 elementos/);
});

test("la profundidad está limitada y las fechas salen en ISO", () => {
  let deep: any = { leaf: 1 };
  for (let index = 0; index < 12; index += 1) deep = { child: deep };
  assert.match(JSON.stringify(redact(deep)), /\[…\]/);
  assert.equal(redact(new Date("2026-10-25T05:00:00Z")), "2026-10-25T05:00:00.000Z");
  assert.equal(redact(undefined), null);
  assert.equal(redact(10n), "10");
});

test("scrubText quita parámetros de URL y credenciales de los mensajes de error", () => {
  const text = scrubText("GET https://api.example.org/v1/x?key=SECRETO&ticket=9 failed; Authorization: Bearer abc123.def token=xyz789 password: 'p4ss'");
  assert.doesNotMatch(text, /SECRETO|abc123|xyz789|p4ss|ticket=9/);
  assert.match(text, /https:\/\/api\.example\.org\/v1\/x\?…/);
  assert.equal(scrubText("sin secretos"), "sin secretos");
});

test("detailJson devuelve JSON legible y recorta lo desmedido", () => {
  assert.equal(detailJson({ a: 1 }), '{\n  "a": 1\n}');
  const long = detailJson(Object.fromEntries(Array.from({ length: 3000 }, (_, index) => [`k${index}`, "valor repetido ".repeat(3)])));
  assert.match(long, /recortado/);
  assert.ok(long.length < 21_000);
});
