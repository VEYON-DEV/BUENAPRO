"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { AppIcon } from "@/components/ui/AppIcon";
import { BrandLogo } from "@/components/ui/BrandLogo";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { apiFetch } from "@/lib/api/client";
import styles from "./OnboardingPage.module.css";

type Segment = { codigo: string; nombre: string; enabled: boolean };
type BusinessLine = { name: string; keywords: string[]; keyword_phrases?: string[]; keyword_terms?: string[]; cubso_segmentos: string[] };
type EditableBusinessLine = BusinessLine & { editorId: string };
type Analysis = { summary: string; company_keywords: string[]; business_lines: BusinessLine[]; segments: Segment[]; source: "website" | "description" | "manual" };

const STEPS = ["Empresa", "Líneas", "Capacidad", "Revisión"];
const STEP_COPY = [
  ["Cuéntanos quién eres", "Usaremos tu web o una descripción breve para preparar un punto de partida."],
  ["Define lo que vendes", "Cada línea conserva sus propias palabras clave para encontrar mejores oportunidades."],
  ["Declara tu capacidad", "Solo lo esencial para comparar requisitos: monto acreditable, personas y recursos."],
  ["Activa tu perfil", "Revisa la información que alimentará el descubrimiento y el análisis de oportunidades."],
] as const;

function normalizeTag(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

function TagEditor({ label, hint, max = 30, values, onChange }: { label: string; hint?: string; max?: number; values: string[]; onChange: (values: string[]) => void }) {
  const [draft, setDraft] = useState("");

  function add() {
    const tag = normalizeTag(draft);
    if (tag && values.length < max && !values.some((item) => item.toLowerCase() === tag.toLowerCase())) onChange([...values, tag]);
    setDraft("");
  }

  return (
    <div className={styles.tagField}>
      <div className={styles.fieldHeading}><label>{label}</label>{hint ? <span>{hint}</span> : null}</div>
      <div className={styles.tagBox}>
        {values.map((value) => (
          <span className={styles.tag} key={value}>
            {value}
            <button aria-label={`Quitar ${value}`} onClick={() => onChange(values.filter((item) => item !== value))} type="button">×</button>
          </span>
        ))}
        <input
          aria-label={`Agregar en ${label}`}
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
          placeholder={values.length ? "Agregar otra…" : "Escribe y presiona Enter…"}
          value={draft}
        />
      </div>
    </div>
  );
}

export function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [company, setCompany] = useState({ ruc: "", name: "", website: "", description: "" });
  const [summary, setSummary] = useState("");
  const [companyKeywords, setCompanyKeywords] = useState<string[]>([]);
  const [lines, setLines] = useState<EditableBusinessLine[]>([]);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [econAmount, setEconAmount] = useState("");
  const [team, setTeam] = useState<string[]>([]);
  const [equipment, setEquipment] = useState<string[]>([]);
  const [status, setStatus] = useState<"idle" | "analyzing" | "saving">("idle");
  const [error, setError] = useState("");

  const keywordCount = useMemo(() => new Set([...companyKeywords, ...lines.flatMap((line) => line.keywords)].map((item) => item.toLowerCase())).size, [companyKeywords, lines]);

  async function analyze() {
    if (!company.name.trim() || !company.ruc.trim()) {
      setError("Ingresa la razón social y el RUC para continuar.");
      return;
    }
    if (!company.website.trim() && !company.description.trim()) {
      setError("Agrega la web de la empresa o describe brevemente sus servicios.");
      return;
    }
    setStatus("analyzing");
    setError("");
    try {
      const result = await apiFetch<{ data: Analysis }>("/api/onboarding/analyze", { method: "POST", json: company });
      setSummary(result.data.summary);
      setCompanyKeywords(result.data.company_keywords);
      setLines(result.data.business_lines.map((line) => ({ ...line, editorId: crypto.randomUUID() })));
      setSegments(result.data.segments);
      setStep(1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "No pudimos analizar la empresa. Puedes continuar manualmente.");
    } finally {
      setStatus("idle");
    }
  }

  function updateLine(index: number, patch: Partial<BusinessLine>) {
    setLines((current) => current.map((line, lineIndex) => lineIndex === index ? { ...line, ...patch } : line));
  }

  function validateStep() {
    setError("");
    if (step === 1 && (!companyKeywords.length || !lines.length || lines.some((line) => !line.name.trim() || !line.keywords.length || !line.cubso_segmentos.length))) {
      setError("Agrega keywords de empresa y completa cada línea con segmento CUBSO y palabras clave.");
      return false;
    }
    if (step === 2 && Number(econAmount) < 0) {
      setError("El monto acreditable no puede ser negativo.");
      return false;
    }
    return true;
  }

  async function save() {
    setStatus("saving");
    setError("");
    try {
      await apiFetch("/api/onboarding/complete", {
        method: "POST",
        json: {
          company,
          summary,
          company_keywords: companyKeywords,
          business_lines: lines.map(({ editorId: _editorId, ...line }) => line),
          econ_amount: Number(econAmount || 0),
          team,
          equipment,
        },
      });
      router.push("/feed?onboarding=completed");
      router.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "No pudimos guardar el perfil. Inténtalo nuevamente.");
      setStatus("idle");
    }
  }

  return (
    <main className={styles.page}>
      <section className={styles.shell}>
        <aside className={styles.context}>
          <Link className={styles.brand} href="/feed" aria-label="BuenaPro"><BrandLogo light /></Link>
          <div className={`${styles.art} ${styles[`art${step}`]}`} aria-hidden="true" />
          <div className={styles.contextCopy}>
            <span>{String(step + 1).padStart(2, "0")} / 04</span>
            <h1>{STEP_COPY[step][0]}</h1>
            <p>{STEP_COPY[step][1]}</p>
          </div>
          <ol className={styles.progress} aria-label="Progreso de configuración">
            {STEPS.map((label, index) => <li className={index === step ? styles.current : index < step ? styles.done : ""} key={label}><i />{label}</li>)}
          </ol>
        </aside>

        <section className={styles.workspace}>
          <header className={styles.mobileHeader}><span>Paso {step + 1} de 4</span><strong>{STEPS[step]}</strong></header>
          <div className={styles.content}>
            {step === 0 ? (
              <div className={styles.step}>
                <div className={styles.heading}><h2>Primero, tu empresa</h2><p>BuenaPro puede leer el contenido público de tu web para sugerir líneas y keywords. Tú decides qué guardar.</p></div>
                <div className={styles.twoColumns}>
                  <label>Razón social<Input autoComplete="organization" name="company_name" onChange={(event) => setCompany({ ...company, name: event.target.value })} placeholder="Ej. VEYON SAC…" value={company.name} /></label>
                  <label>RUC<Input autoComplete="off" inputMode="numeric" maxLength={11} name="ruc" onChange={(event) => setCompany({ ...company, ruc: event.target.value.replace(/\D/g, "") })} placeholder="11 dígitos…" value={company.ruc} /></label>
                </div>
                <label>Sitio web <span className={styles.optional}>Opcional</span><Input autoComplete="url" name="website" onChange={(event) => setCompany({ ...company, website: event.target.value })} placeholder="https://tuempresa.pe…" type="url" value={company.website} /></label>
                <div className={styles.or}><span>o cuéntanos directamente</span></div>
                <label>¿Qué servicios brinda la empresa?
                  <textarea maxLength={1200} name="description" onChange={(event) => setCompany({ ...company, description: event.target.value })} placeholder="Ej. Implementamos redes, desarrollamos software y brindamos soporte técnico…" value={company.description} />
                  <small>{company.description.length}/1200</small>
                </label>
              </div>
            ) : null}

            {step === 1 ? (
              <div className={styles.step}>
                <div className={styles.heading}><h2>Líneas de negocio</h2><p>{summary || "Organiza los servicios que puede brindar tu empresa."}</p></div>
                <section className={styles.companySignals} aria-labelledby="onboarding-company-keywords">
                  <div><strong id="onboarding-company-keywords">Identidad de la empresa</strong><p>Se aplica a todas las líneas. Las variaciones de género y número se reconocen automáticamente.</p></div>
                  <TagEditor hint={`${companyKeywords.length}/12 · señales transversales`} label="Keywords generales" max={12} onChange={setCompanyKeywords} values={companyKeywords} />
                </section>
                <div className={styles.lineList}>
                  {lines.map((line, index) => (
                    <article className={styles.lineEditor} key={line.editorId}>
                      <div className={styles.lineTop}>
                        <span className={styles.lineNumber}>{index + 1}</span>
                        <Input aria-label={`Nombre de la línea ${index + 1}`} onChange={(event) => updateLine(index, { name: event.target.value })} value={line.name} />
                        <button aria-label={`Eliminar ${line.name}`} className={styles.removeLine} onClick={() => setLines(lines.filter((_, lineIndex) => lineIndex !== index))} type="button">Eliminar</button>
                      </div>
                      <div className={styles.segmentField}>
                        <div className={styles.fieldHeading}><label>Segmentos CUBSO</label><span>Filtro principal</span></div>
                        <div className={styles.segmentTags}>
                          {line.cubso_segmentos.map((codigo) => {
                            const segment = segments.find((item) => item.codigo === codigo);
                            return (
                              <span className={styles.segmentTag} key={codigo} title={segment?.nombre}>
                                <b>{codigo}</b> {segment?.nombre ?? "Segmento CUBSO"}
                                {!segment?.enabled ? <em>Sin cobertura actual</em> : null}
                                <button aria-label={`Quitar segmento ${codigo}`} onClick={() => updateLine(index, { cubso_segmentos: line.cubso_segmentos.filter((item) => item !== codigo) })} type="button">×</button>
                              </span>
                            );
                          })}
                        </div>
                        <select
                          aria-label={`Agregar segmento a ${line.name}`}
                          onChange={(event) => {
                            const codigo = event.target.value;
                            if (codigo && !line.cubso_segmentos.includes(codigo)) updateLine(index, { cubso_segmentos: [...line.cubso_segmentos, codigo].slice(0, 3) });
                            event.target.value = "";
                          }}
                          defaultValue=""
                        >
                          <option value="">Agregar otro segmento…</option>
                          {segments.filter((segment) => !line.cubso_segmentos.includes(segment.codigo)).map((segment) => (
                            <option key={segment.codigo} value={segment.codigo}>{segment.codigo} · {segment.nombre}{segment.enabled ? "" : " · sin cobertura actual"}</option>
                          ))}
                        </select>
                      </div>
                      <TagEditor label="Keywords para encontrar oportunidades" values={line.keywords} onChange={(keywords) => updateLine(index, { keywords })} />
                    </article>
                  ))}
                </div>
                <Button onClick={() => setLines([...lines, { editorId: crypto.randomUUID(), name: "Nueva línea", keywords: [], cubso_segmentos: [] }])} type="button" variant="secondary">+ Agregar línea</Button>
              </div>
            ) : null}

            {step === 2 ? (
              <div className={styles.step}>
                <div className={styles.heading}><h2>Capacidad actual</h2><p>No necesitas completar un inventario. Registra lo suficiente para que el análisis entienda con qué cuenta la empresa.</p></div>
                <label>Monto de experiencia económica acreditable
                  <div className={styles.moneyInput}><span>S/</span><Input inputMode="decimal" min="0" name="econ_amount" onChange={(event) => setEconAmount(event.target.value)} placeholder="120000…" type="number" value={econAmount} /></div>
                  <small>Usaremos este monto general para compararlo con lo exigido en cada TDR.</small>
                </label>
                <TagEditor hint="Roles o perfiles disponibles" label="Equipo humano" onChange={setTeam} values={team} />
                <TagEditor hint="Herramientas, vehículos o activos" label="Recursos y equipamiento" onChange={setEquipment} values={equipment} />
              </div>
            ) : null}

            {step === 3 ? (
              <div className={styles.step}>
                <div className={styles.heading}><h2>Todo listo para empezar</h2><p>Este perfil se puede ampliar después. Por ahora ya tenemos la información esencial para descubrir y evaluar oportunidades.</p></div>
                <div className={styles.reviewHero}><div><span>Perfil de empresa</span><strong>{company.name}</strong><small>RUC {company.ruc}</small></div><AppIcon name="check" /></div>
                <dl className={styles.reviewGrid}>
                  <div><dt>Líneas activas</dt><dd>{lines.length}</dd></div>
                  <div><dt>Keywords</dt><dd>{keywordCount}</dd></div>
                  <div><dt>Monto acreditable</dt><dd>{new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN", maximumFractionDigits: 0 }).format(Number(econAmount || 0))}</dd></div>
                  <div><dt>Capacidad</dt><dd>{team.length} perfiles · {equipment.length} recursos</dd></div>
                </dl>
                <div className={styles.companyReview}><strong>Identidad de empresa</strong><span>{companyKeywords.join(" · ")}</span></div>
                <div className={styles.lineSummary}>{lines.map((line) => <div key={line.name}><strong>{line.name}<small>CUBSO {line.cubso_segmentos.join(", ")}</small></strong><span>{line.keywords.slice(0, 4).join(" · ")}{line.keywords.length > 4 ? ` · +${line.keywords.length - 4}` : ""}</span></div>)}</div>
              </div>
            ) : null}

            {error ? <p className={styles.error} role="alert">{error}</p> : null}
          </div>

          <footer className={styles.actions}>
            {step > 0 ? <Button onClick={() => { setError(""); setStep(step - 1); }} type="button" variant="ghost">Atrás</Button> : <Link href="/feed">Completar después</Link>}
            {step === 0 ? <Button disabled={status === "analyzing"} onClick={analyze} type="button">{status === "analyzing" ? "Analizando empresa…" : "Preparar mi perfil"}</Button> : null}
            {step > 0 && step < 3 ? <Button onClick={() => validateStep() && setStep(step + 1)} type="button">{step === 1 ? "Confirmar líneas" : "Revisar perfil"}</Button> : null}
            {step === 3 ? <Button disabled={status === "saving"} onClick={save} type="button">{status === "saving" ? "Activando perfil…" : "Activar y ver oportunidades"}</Button> : null}
          </footer>
        </section>
      </section>
    </main>
  );
}
