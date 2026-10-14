"use client";

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { apiFetch } from "@/lib/api/client";
import styles from "./BusinessLinesPanel.module.css";

type Segment = { codigo: string; nombre: string; enabled: boolean };
type BusinessLine = {
  id: string;
  nombre: string;
  cubso_segmentos: string[];
  keywords: string[];
  is_active: boolean;
};

function unique(values: string[]) {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

function KeywordEditor({ values, onChange }: { values: string[]; onChange: (values: string[]) => void }) {
  const [draft, setDraft] = useState("");

  function add() {
    const next = draft.trim().replace(/\s+/g, " ").toLowerCase();
    if (next && !values.includes(next) && values.length < 30) onChange([...values, next]);
    setDraft("");
  }

  return (
    <div className={styles.keywordBox}>
      {values.map((keyword) => (
        <span className={styles.keyword} key={keyword}>
          {keyword}
          <button aria-label={`Quitar keyword ${keyword}`} onClick={() => onChange(values.filter((value) => value !== keyword))} type="button">×</button>
        </span>
      ))}
      <input
        aria-label="Agregar keyword"
        autoComplete="off"
        name="keyword"
        onBlur={add}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === ",") {
            event.preventDefault();
            add();
          }
        }}
        placeholder={values.length ? "Agregar otra…" : "Ej. desarrollo de software…"}
        value={draft}
      />
    </div>
  );
}

function LineEditor({
  initial,
  catalogs,
  onCancel,
  onSaved,
}: {
  initial: BusinessLine | null;
  catalogs: Segment[];
  onCancel: () => void;
  onSaved: (line: BusinessLine) => void;
}) {
  const [name, setName] = useState(initial?.nombre ?? "");
  const [segments, setSegments] = useState<string[]>(initial?.cubso_segmentos ?? []);
  const [keywords, setKeywords] = useState<string[]>(initial?.keywords ?? []);
  const [status, setStatus] = useState<"idle" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");

  async function save() {
    if (!name.trim() || !segments.length || !keywords.length) {
      setMessage("Agrega un nombre, al menos 1 segmento y 1 keyword.");
      setStatus("error");
      return;
    }
    setStatus("saving");
    setMessage("");
    try {
      const payload = { nombre: name.trim(), cubso_segmentos: unique(segments).slice(0, 3), keywords: unique(keywords).slice(0, 30) };
      const response = initial
        ? await apiFetch<{ data: BusinessLine }>(`/api/lines/${initial.id}`, { method: "PATCH", json: payload })
        : await apiFetch<{ data: BusinessLine }>("/api/lines", { method: "POST", json: payload });
      onSaved(response.data);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "No pudimos guardar la línea.");
      setStatus("error");
    }
  }

  return (
    <div className={styles.editor}>
      <div className={styles.editorHeading}>
        <div><strong>{initial ? "Editar línea" : "Nueva línea de negocio"}</strong><span>El segmento abre el universo; las keywords priorizan dentro de él.</span></div>
        <button onClick={onCancel} type="button">Cerrar</button>
      </div>
      <label>Nombre de la línea<Input autoComplete="off" name="line_name" onChange={(event) => setName(event.target.value)} placeholder="Ej. Desarrollo de software…" value={name} /></label>
      <div className={styles.fieldGroup}>
        <div className={styles.fieldLabel}><strong>Segmentos CUBSO</strong><span>Máximo 3</span></div>
        <div className={styles.segmentList}>
          {segments.map((codigo) => {
            const segment = catalogs.find((item) => item.codigo === codigo);
            return (
              <span className={styles.segment} key={codigo}>
                <b>{codigo}</b><span>{segment?.nombre ?? "Segmento CUBSO"}</span>{!segment?.enabled ? <em>Sin cobertura</em> : null}
                <button aria-label={`Quitar segmento ${codigo}`} onClick={() => setSegments(segments.filter((value) => value !== codigo))} type="button">×</button>
              </span>
            );
          })}
        </div>
        {segments.length < 3 ? (
          <select
            aria-label="Agregar segmento CUBSO"
            defaultValue=""
            name="cubso_segment"
            onChange={(event) => {
              if (event.target.value) setSegments(unique([...segments, event.target.value]));
              event.target.value = "";
            }}
          >
            <option value="">Agregar segmento…</option>
            {catalogs.filter((segment) => !segments.includes(segment.codigo)).map((segment) => (
              <option key={segment.codigo} value={segment.codigo}>{segment.codigo} · {segment.nombre}{segment.enabled ? "" : " · sin cobertura actual"}</option>
            ))}
          </select>
        ) : null}
      </div>
      <div className={styles.fieldGroup}>
        <div className={styles.fieldLabel}><strong>Keywords</strong><span>{keywords.length}/30 · Enter para agregar</span></div>
        <KeywordEditor onChange={setKeywords} values={keywords} />
      </div>
      {message ? <p className={styles.error} role="alert">{message}</p> : null}
      <div className={styles.editorActions}><Button onClick={onCancel} type="button" variant="ghost">Cancelar</Button><Button disabled={status === "saving"} onClick={save} type="button">{status === "saving" ? "Guardando…" : "Guardar línea"}</Button></div>
    </div>
  );
}

const KEYWORD_PREVIEW = 10;

function KeywordPreview({ keywords }: { keywords: string[] }) {
  const [open, setOpen] = useState(false);
  const hidden = keywords.length - KEYWORD_PREVIEW;
  const shown = open || hidden <= 0 ? keywords : keywords.slice(0, KEYWORD_PREVIEW);
  return (
    <div className={styles.keywords}>
      {shown.map((keyword) => <span key={keyword}>{keyword}</span>)}
      {hidden > 0 ? <button className={styles.more} type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Ver menos" : `+${hidden} más`}</button> : null}
    </div>
  );
}

export function BusinessLinesPanel({ lines, catalogs }: { lines: BusinessLine[]; catalogs: Segment[] }) {
  const [items, setItems] = useState(lines);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [notice, setNotice] = useState("");
  const keywordTotal = useMemo(() => new Set(items.flatMap((line) => line.keywords)).size, [items]);

  function saved(line: BusinessLine) {
    setItems((current) => current.some((item) => item.id === line.id) ? current.map((item) => item.id === line.id ? line : item) : [...current, line]);
    setEditing(null);
    setNotice("Línea guardada. BuenaPro actualizará la afinidad de tus oportunidades.");
  }

  return (
    <section className={styles.panel}>
      <header className={styles.header}>
        <div><h2>Líneas de negocio</h2><p>Controlan qué contratos entran a tu radar y cómo se priorizan.</p></div>
        <div className={styles.headerActions}><span>{items.length} líneas · {keywordTotal} keywords</span><Button onClick={() => setEditing("new")} type="button">Agregar línea</Button></div>
      </header>

      {notice ? <p className={styles.notice} aria-live="polite">{notice}</p> : null}
      {editing === "new" ? <LineEditor catalogs={catalogs} initial={null} onCancel={() => setEditing(null)} onSaved={saved} /> : null}

      <div className={styles.lines}>
        {!items.length && editing !== "new" ? <div className={styles.empty}><strong>Aún no tienes líneas de negocio</strong><p>Agrega la primera para definir qué oportunidades debe priorizar BuenaPro.</p><Button onClick={() => setEditing("new")} type="button">Agregar primera línea</Button></div> : null}
        {items.map((line) => (
          <article className={styles.line} key={line.id}>
            {editing === line.id ? (
              <LineEditor catalogs={catalogs} initial={line} onCancel={() => setEditing(null)} onSaved={saved} />
            ) : (
              <>
                <div className={styles.lineHeader}>
                  <div><h3>{line.nombre}</h3><p>{line.keywords.length} keywords activas</p></div>
                  <Button onClick={() => { setNotice(""); setEditing(line.id); }} type="button" variant="secondary">Editar</Button>
                </div>
                <div className={styles.lineBody}>
                  <div className={styles.summarySegments}>
                    {(line.cubso_segmentos ?? []).map((codigo) => {
                      const segment = catalogs.find((item) => item.codigo === codigo);
                      const name = segment?.nombre ?? "Segmento CUBSO";
                      return <span key={codigo} className={segment?.enabled ? undefined : styles.uncovered} title={`${codigo} · ${name}${segment?.enabled ? "" : " · sin cobertura actual"}`}><b>{codigo}</b><span>{name}</span>{!segment?.enabled ? <em>Sin cobertura</em> : null}</span>;
                    })}
                  </div>
                  <KeywordPreview keywords={line.keywords} />
                </div>
              </>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
