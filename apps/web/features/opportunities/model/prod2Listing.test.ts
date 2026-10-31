import test from "node:test";
import assert from "node:assert/strict";
import { prod2DrilldownHref, prod2ListingHref, ROW_KEYWORD_LIMIT, rowKeywordChips, rowLineTag } from "./prod2Listing.ts";

const params = (href: string) => new URL(href, "https://buenapro.invalid").searchParams;

test("la línea solo se muestra si suma puntos: sin puntos, sin nombre o con puntos inválidos no hay etiqueta", () => {
  const base = { business_line_id: "a1", business_line_name: "Software", business_line_favorite: false };
  assert.deepEqual(rowLineTag({ ...base, keyword_points: 20 }), { id: "a1", name: "Software", favorite: false });
  assert.deepEqual(rowLineTag({ ...base, keyword_points: 10, business_line_favorite: true }), { id: "a1", name: "Software", favorite: true });
  for (const keyword_points of [0, -5, null, undefined, Number.NaN, Number.POSITIVE_INFINITY, "20" as unknown as number]) {
    assert.equal(rowLineTag({ ...base, keyword_points }), null, String(keyword_points));
  }
  for (const business_line_name of [null, undefined, "", "   "]) assert.equal(rowLineTag({ ...base, keyword_points: 20, business_line_name }), null);
  // Solo un true real marca la prioridad; un id ausente no inventa un enlace.
  assert.equal(rowLineTag({ ...base, keyword_points: 20, business_line_favorite: "true" as unknown as boolean })?.favorite, false);
  assert.equal(rowLineTag({ ...base, keyword_points: 20, business_line_id: null })?.id, null);
});

test("las palabras salen sin repetir (ni por mayúsculas), en orden, con tope y resumen del resto, y solo si la línea suma puntos", () => {
  const hits = [{ keyword: "licencia de software" }, { keyword: "Software" }, { keyword: "software" }, { keyword: "  nube  " }, { keyword: "" }, { keyword: null }, { keyword: "servidor" }, { keyword: "redes" }];
  const row = { keyword_points: 30, business_line_name: "TI", keyword_hits: hits };
  const chips = rowKeywordChips(row);
  assert.deepEqual(chips.shown, ["licencia de software", "Software", "nube"]);
  assert.equal(chips.shown.length, ROW_KEYWORD_LIMIT);
  assert.equal(chips.extra, 2);
  assert.deepEqual(chips.all, ["licencia de software", "Software", "nube", "servidor", "redes"]);
  assert.deepEqual(rowKeywordChips(row, Infinity).shown, chips.all);
  assert.equal(rowKeywordChips(row, Infinity).extra, 0);
  // Sin puntos la línea no pertenece y sus palabras tampoco se muestran.
  assert.deepEqual(rowKeywordChips({ ...row, keyword_points: 0 }).all, []);
  assert.deepEqual(rowKeywordChips({ keyword_points: 20, business_line_name: "TI", keyword_hits: null }).all, []);
  assert.deepEqual(rowKeywordChips({ keyword_points: 20, business_line_name: "TI" }).shown, []);
});

test("los enlaces de la barra conservan lo demás, quitan la página y permiten quitar un filtro", () => {
  const current = { source: "prod2", profile_id: "p1", q: "software", object: "service", with_score: "true", deadline: "all", kw: "nube", line: "l1", page: "4", page_size: "25" };
  const next = params(prod2ListingHref(current, { deadline: "active" }));
  assert.deepEqual([next.get("source"), next.get("profile_id"), next.get("q"), next.get("object"), next.get("with_score"), next.get("deadline"), next.get("kw"), next.get("line"), next.get("page_size")],
    ["prod2", "p1", "software", "service", "true", "active", "nube", "l1", "25"]);
  assert.equal(next.has("page"), false);
  const removed = params(prod2ListingHref(current, { kw: null }));
  assert.equal(removed.has("kw"), false);
  assert.equal(removed.get("line"), "l1");
  assert.equal(removed.get("q"), "software");
  assert.equal(params(prod2ListingHref({}, {})).get("source"), "prod2");
  // El texto viaja codificado, no como fragmento de URL.
  assert.equal(params(prod2ListingHref({}, { kw: "a&b=c#d" })).get("kw"), "a&b=c#d");
});

test("desde la vista rápida solo se conservan perfil, cierre, tipo, búsqueda y puntaje, y se fija una sola cosa", () => {
  const current = { source: "prod2", profile_id: "p1", q: "software", object: "good", with_score: "true", deadline: "active", kw: "nube", line: "l1", page: "3", analysis: "done", page_size: "50" };
  const byLine = params(prod2DrilldownHref(current, { line: "l9" }));
  assert.deepEqual([...byLine.keys()].sort(), ["deadline", "line", "object", "profile_id", "q", "source", "with_score"]);
  assert.deepEqual([byLine.get("source"), byLine.get("profile_id"), byLine.get("deadline"), byLine.get("object"), byLine.get("q"), byLine.get("with_score"), byLine.get("line")],
    ["prod2", "p1", "active", "good", "software", "true", "l9"]);
  const byWord = params(prod2DrilldownHref(current, { kw: "licencia de software" }));
  assert.deepEqual([...byWord.keys()].sort(), ["deadline", "kw", "object", "profile_id", "q", "source", "with_score"]);
  assert.equal(byWord.get("kw"), "licencia de software");
  assert.equal(byWord.has("line"), false);
  // Sin filtros previos solo queda la fuente y el nuevo filtro.
  assert.deepEqual([...params(prod2DrilldownHref({}, { kw: "nube" })).entries()], [["source", "prod2"], ["kw", "nube"]]);
});
