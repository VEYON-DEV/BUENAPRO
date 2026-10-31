import test from "node:test";
import assert from "node:assert/strict";
import { changeLineFavorite } from "./lineFavorite.ts";

test("marcar pinta la estrella antes de la respuesta, pide el valor nuevo y se queda con lo que confirma el servidor", async () => {
  const painted: boolean[] = [];
  const sent: boolean[] = [];
  const result = await changeLineFavorite({ nombre: "Software", is_favorite: false }, async (favorite) => {
    sent.push(favorite);
    assert.deepEqual(painted, [true], "la estrella ya cambió mientras el servidor responde");
    return true;
  }, (favorite) => painted.push(favorite));
  assert.deepEqual(sent, [true]);
  assert.deepEqual(painted, [true, true]);
  assert.deepEqual(result, { ok: true, favorite: true, message: "«Software» es ahora una línea prioritaria." });
});

test("quitar la prioridad hace lo mismo al revés, y una línea sin la columna cuenta como no prioritaria", async () => {
  const painted: boolean[] = [];
  const off = await changeLineFavorite({ nombre: "Redes", is_favorite: true }, async () => false, (favorite) => painted.push(favorite));
  assert.deepEqual(painted, [false, false]);
  assert.deepEqual(off, { ok: true, favorite: false, message: "«Redes» ya no es una línea prioritaria." });
  const sent: boolean[] = [];
  await changeLineFavorite({ nombre: "Nube" }, async (favorite) => { sent.push(favorite); return true; }, () => {});
  assert.deepEqual(sent, [true]);
});

test("si el servidor falla, la estrella vuelve a como estaba y el mensaje lo dice", async () => {
  for (const is_favorite of [true, false, undefined]) {
    const painted: boolean[] = [];
    const result = await changeLineFavorite({ nombre: "Cloud", is_favorite }, async () => { throw new Error("500"); }, (favorite) => painted.push(favorite));
    const before = is_favorite === true;
    assert.deepEqual(painted, [!before, before], `${is_favorite}`);
    assert.deepEqual(result, { ok: false, favorite: before, message: "No pudimos cambiar la prioridad de «Cloud». Inténtalo de nuevo." });
  }
});

test("gana lo que el servidor guardó: una respuesta distinta o sin valor deja la estrella apagada", async () => {
  const painted: boolean[] = [];
  const different = await changeLineFavorite({ nombre: "IA", is_favorite: false }, async () => false, (favorite) => painted.push(favorite));
  assert.deepEqual(painted, [true, false]);
  assert.equal(different.favorite, false);
  assert.match(different.message, /ya no es una línea prioritaria/);
  for (const answer of [undefined, null as unknown as boolean, "true" as unknown as boolean]) {
    const result = await changeLineFavorite({ nombre: "IA", is_favorite: false }, async () => answer, () => {});
    assert.equal(result.favorite, false, String(answer));
    assert.equal(result.ok, true);
  }
});
