"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api/client";
import styles from "./CompanyKeywordsPanel.module.css";

function normalize(value: string) {
  return value.trim().toLowerCase().replace(/\s+/g, " ");
}

function KeywordInput({ values, onChange }: { values: string[]; onChange: (values: string[]) => void }) {
  const [draft, setDraft] = useState("");

  function add() {
    const keyword = normalize(draft);
    if (keyword && !values.includes(keyword) && values.length < 12) onChange([...values, keyword]);
    setDraft("");
  }

  return (
    <div className={styles.inputBox}>
      {values.map((keyword) => (
        <span className={styles.keyword} key={keyword}>
          {keyword}
          <button aria-label={`Quitar keyword de empresa ${keyword}`} onClick={() => onChange(values.filter((value) => value !== keyword))} type="button">×</button>
        </span>
      ))}
      <input
        aria-label="Agregar keyword general de empresa"
        autoComplete="off"
        name="company_keyword"
        onBlur={add}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === ",") {
            event.preventDefault();
            add();
          }
        }}
        placeholder={values.length ? "Agregar otra…" : "Escribe un término o frase breve…"}
        value={draft}
      />
    </div>
  );
}

export function CompanyKeywordsPanel({ keywords }: { keywords: string[] }) {
  const [saved, setSaved] = useState(keywords);
  const [draft, setDraft] = useState(keywords);
  const [editing, setEditing] = useState(false);
  const [status, setStatus] = useState<"idle" | "saving" | "done" | "error">("idle");

  async function save() {
    if (!draft.length) {
      setStatus("error");
      return;
    }
    setStatus("saving");
    try {
      const response = await apiFetch<{ data: { company_keywords: string[] } }>("/api/profile/keywords", {
        method: "PUT",
        json: { company_keywords: draft },
      });
      setSaved(response.data.company_keywords);
      setDraft(response.data.company_keywords);
      setEditing(false);
      setStatus("done");
    } catch {
      setStatus("error");
    }
  }

  return (
    <section className={styles.panel} aria-labelledby="company-keywords-title">
      <div className={styles.intro}>
        <span className={styles.scope}>Empresa</span>
        <div>
          <h2 id="company-keywords-title">Identidad del radar</h2>
          <p>Describe a la empresa en todas sus líneas. El motor reconoce género, número y raíces lingüísticas.</p>
        </div>
      </div>
      {editing ? (
        <div className={styles.editor}>
          <div className={styles.editorMeta}><strong>Keywords generales</strong><span>{draft.length}/12</span></div>
          <KeywordInput onChange={(values) => { setDraft(values); setStatus("idle"); }} values={draft} />
          {status === "error" ? <p className={styles.error} role="alert">Agrega al menos una keyword válida.</p> : null}
          <div className={styles.actions}>
            <Button onClick={() => { setDraft(saved); setEditing(false); setStatus("idle"); }} type="button" variant="ghost">Cancelar</Button>
            <Button disabled={status === "saving"} onClick={save} type="button">{status === "saving" ? "Guardando…" : "Guardar identidad"}</Button>
          </div>
        </div>
      ) : (
        <div className={styles.summary}>
          <div className={styles.keywords}>
            {saved.length ? saved.map((keyword) => <span key={keyword}>{keyword}</span>) : <p>Aún no hay keywords generales.</p>}
          </div>
          <Button onClick={() => { setEditing(true); setStatus("idle"); }} type="button" variant="secondary">Editar identidad</Button>
        </div>
      )}
      {status === "done" && !editing ? <p className={styles.notice} aria-live="polite">Identidad guardada. El radar usará las nuevas raíces en la próxima consulta.</p> : null}
    </section>
  );
}
