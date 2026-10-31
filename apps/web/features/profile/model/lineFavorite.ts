type FavoriteLine = { nombre: string; is_favorite?: boolean };

export type LineFavoriteResult = { ok: boolean; favorite: boolean; message: string };

/**
 * Cambia la prioridad de una línea con respuesta inmediata: aplica el valor nuevo ya, lo confirma con el servidor y,
 * si falla, devuelve la estrella a como estaba. Gana siempre lo que confirma el servidor.
 * `send` guarda el valor y devuelve el que quedó guardado; `apply` pinta el valor en pantalla.
 */
export async function changeLineFavorite(
  line: FavoriteLine,
  send: (favorite: boolean) => Promise<boolean | undefined>,
  apply: (favorite: boolean) => void,
): Promise<LineFavoriteResult> {
  const previous = line.is_favorite === true;
  apply(!previous);
  try {
    const confirmed = (await send(!previous)) === true;
    apply(confirmed);
    return { ok: true, favorite: confirmed, message: confirmed ? `«${line.nombre}» es ahora una línea prioritaria.` : `«${line.nombre}» ya no es una línea prioritaria.` };
  } catch {
    apply(previous);
    return { ok: false, favorite: previous, message: `No pudimos cambiar la prioridad de «${line.nombre}». Inténtalo de nuevo.` };
  }
}
