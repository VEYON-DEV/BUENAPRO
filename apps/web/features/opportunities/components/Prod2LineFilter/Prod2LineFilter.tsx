"use client";

import { Layers } from "lucide-react";
import { useId } from "react";
import { Select } from "@/components/ui/Select";
import type { Prod2LineOption } from "../../model/prod2Listing";
import styles from "./Prod2LineFilter.module.css";

type Props = {
  /** Líneas activas del perfil, con las prioritarias primero. */
  lines: Prod2LineOption[];
  /** Valor de `line` en la URL: vacío (todas), «favorites» o el UUID de una línea. */
  selected: string;
  /** Resto de parámetros de la lista; viajan ocultos para que cambiar de línea no pierda búsqueda, tipo, cierre ni palabra. */
  hidden: Record<string, string>;
};

/**
 * Selector de línea de negocio de la lista de mayores. Es un formulario GET como el buscador: al elegir se envía solo y,
 * sin JavaScript, queda un botón «Aplicar». Siempre reinicia la paginación porque no envía `page`.
 */
export function Prod2LineFilter({ lines, selected, hidden }: Props) {
  const id = useId();
  const hasFavorites = lines.some(line => line.is_favorite);
  // Un enlace guardado puede apuntar a una línea que ya no existe o a «favorites» sin prioritarias: se muestra tal cual.
  const known = selected === "" || (selected === "favorites" ? hasFavorites : lines.some(line => line.id === selected));
  return (
    <form action="/feed" className={styles.form}>
      {Object.entries(hidden).map(([name, value]) => <input key={name} name={name} type="hidden" value={value} />)}
      <label className={styles.field} htmlFor={id}>
        <Layers aria-hidden="true" className={styles.icon} size={16} />
        <span className={styles.srOnly}>Línea de negocio</span>
        {/* La vista rápida cambia la línea con navegación suave: sin `key`, el select no controlado conservaría su valor anterior. */}
        <Select defaultValue={selected} id={id} key={selected} name="line" onChange={event => event.currentTarget.form?.requestSubmit()}>
          <option value="">Todas las líneas</option>
          {hasFavorites ? <option value="favorites">★ Prioritarias</option> : null}
          {lines.map(line => <option key={line.id} value={line.id}>{line.is_favorite ? "★ " : ""}{line.name}</option>)}
          {known ? null : <option value={selected}>Línea no disponible</option>}
        </Select>
      </label>
      <noscript><button className={styles.apply} type="submit">Aplicar</button></noscript>
    </form>
  );
}
