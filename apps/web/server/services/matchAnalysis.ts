import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pool } from "@/server/db/client";
import { ensureServerEnv } from "@/server/env";
import {
  buildCompactMatchProfile, checklistOmittedProposalDocuments, COMPACT_PROFILE_VERSION, guardEconomicComparisons, guardParticipationActions,
  isAdministrativeRequirement, MATCH_POLICY_VERSION, SEMANTIC_GUARD_VERSION, SOURCE_CATEGORIES, splitMatchRequirements,
} from "./matchAnalysisPolicy";

const PROMPT_VERSION = MATCH_POLICY_VERSION;
const SCORE_BANDS = {
  verde: [85, 100],
  ambar: [50, 84],
  gris: [0, 100],
  rojo: [0, 29],
} as const;
const STATES = new Set([
  "cumple",
  "cumple_con_accion",
  "no_cumple",
  "requiere_revision",
]);
const CATEGORIES = new Set([
  "experiencia_economica",
  "experiencia",
  "personal_clave",
  "formacion",
  "licencia",
  "seguro",
  "equipamiento",
  "certificacion",
  "documentacion",
  "identidad",
  "otro",
]);

type JsonRecord = Record<string, unknown>;
type Verdict = keyof typeof SCORE_BANDS;
type Requirement = {
  source_index: number;
  source_facet: string;
  evidence_refs: string[];
  evidence_explanation: string;
  economic_comparison: JsonRecord[];
  economic_validation_reasons?: string[];
  requisito: string;
  categoria: string;
  estado: string;
  critico: boolean;
  gap: string | null;
  accion: string | null;
};

export type DirectMatchAnalysisResult =
  | { skipped: "unchanged"; match_id: number }
  | { match_id: number; verdict: Verdict; score: number | null };

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function clip(value: unknown, limit: number): string | null {
  const text =
    typeof value === "string" ? value.trim() : String(value ?? "").trim();
  if (!text) return null;
  if (text.length <= limit) return text;
  const cut = text
    .slice(0, limit)
    .replace(/\s+\S*$/, "")
    .trim();
  return `${cut || text.slice(0, limit)}…`;
}

function canonicalHash(value: unknown): string {
  // El payload usado aquí es una lista ordenada de strings, equivalente al
  // canonical_hash del worker para facets_hash.
  return crypto
    .createHash("sha256")
    .update(JSON.stringify(value))
    .digest("hex");
}

function clampScore(verdict: Verdict, score: number): number {
  const [low, high] = SCORE_BANDS[verdict];
  return Math.max(low, Math.min(high, Math.trunc(score)));
}

function deriveVerdict(requirements: Requirement[]): Verdict {
  if (!requirements.length) return "gris";
  if (requirements.some((r) => r.estado === "no_cumple" && r.critico))
    return "rojo";
  if (requirements.some((r) => r.estado === "requiere_revision" && r.critico))
    return "gris";
  if (requirements.some((r) => r.estado !== "cumple")) return "ambar";
  return "verde";
}

function prompt(): string {
  const candidates = [
    path.resolve(
      process.cwd(),
      "../../workers/seace/prompts/match_analysis_v5.txt",
    ),
    path.resolve(process.cwd(), "workers/seace/prompts/match_analysis_v5.txt"),
    "/app/workers/seace/prompts/match_analysis_v5.txt",
  ];
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) throw new Error("No se encontró el prompt match_analysis_v5.");
  return fs.readFileSync(found, "utf8");
}

function analysisPrompt(): string {
  return `${prompt()}\nConserva source_index exacto y el orden de cada requisito recibido. source_facet copia su tipo. evidence_refs solo contiene document_id de fuentes del perfil pertinentes; [] si no existe respaldo. evidence_explanation explica esa evidencia y sus límites. Para aprobar experiencia con monto mínimo, economic_comparison identifica cada document_id incluido en evidence_refs, amount_index desde cero en extraction.amounts, moneda exigida (PEN si las bases no especifican otra), monto literal computable y cita compatible; full_amount_comparable y period_supported deben estar documentados. No cuentes contratos mixtos completos ni conviertas moneda. [] si no puedes acreditar la comparación. La entrada ya excluye trámites administrativos; no los agregues ni uses para el score.`;
}

const responseSchema = {
  type: "OBJECT",
  required: [
    "veredicto",
    "score",
    "resumen",
    "requisitos",
    "acciones_recomendadas",
  ],
  properties: {
    veredicto: { type: "STRING", enum: ["verde", "ambar", "rojo", "gris"] },
    score: { type: "INTEGER", minimum: 0, maximum: 100 },
    resumen: { type: "STRING" },
    requisitos: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        required: [
          "source_index",
          "source_facet",
          "evidence_refs",
          "evidence_explanation",
          "economic_comparison",
          "requisito",
          "categoria",
          "estado",
          "critico",
          "gap",
          "accion",
        ],
        properties: {
          source_index: { type: "INTEGER", minimum: 1 },
          source_facet: { type: "STRING" },
          evidence_refs: { type: "ARRAY", items: { type: "STRING" } },
          evidence_explanation: { type: "STRING" },
          economic_comparison: {
            type: "ARRAY",
            items: {
              type: "OBJECT",
              required: ["document_id", "amount_index", "currency", "eligible_amount_decimal", "compatible_scope_quote", "full_amount_comparable", "period_supported"],
              properties: {
                document_id: { type: "STRING" },
                amount_index: { type: "INTEGER", minimum: 0 },
                currency: { type: "STRING" },
                eligible_amount_decimal: { type: "STRING" },
                compatible_scope_quote: { type: "STRING" },
                full_amount_comparable: { type: "BOOLEAN" },
                period_supported: { type: "BOOLEAN" },
              },
            },
          },
          requisito: { type: "STRING" },
          categoria: { type: "STRING", enum: [...CATEGORIES] },
          estado: { type: "STRING", enum: [...STATES] },
          critico: { type: "BOOLEAN" },
          gap: { type: "STRING", nullable: true },
          accion: { type: "STRING", nullable: true },
        },
      },
    },
    acciones_recomendadas: {
      type: "ARRAY",
      maxItems: 4,
      items: { type: "STRING" },
    },
  },
};

async function callGemini(body: JsonRecord, systemPrompt: string) {
  ensureServerEnv();
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) throw new Error("Gemini no está configurado.");
  const primary = process.env.GEMINI_MODEL ?? "gemini-3.1-flash-lite";
  const model = primary;
  const response = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(apiKey)}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: systemPrompt }] },
        contents: [
          { role: "user", parts: [{ text: JSON.stringify(body) }] },
        ],
        generationConfig: {
          responseMimeType: "application/json",
          responseSchema,
          temperature: 0,
          maxOutputTokens: 8192,
        },
      }),
      signal: AbortSignal.timeout(60_000),
    },
  );
  if (!response.ok)
    throw new Error(`Gemini respondió HTTP ${response.status}.`);
  const payload = record(await response.json());
  const candidate = record(list(payload.candidates)[0]);
  if (String(candidate.finishReason ?? "").includes("MAX_TOKENS")) {
    throw new Error("El análisis de Gemini fue truncado.");
  }
  const content = record(candidate.content);
  const text = String(record(list(content.parts)[0]).text ?? "");
  if (!text) throw new Error("Gemini devolvió una respuesta vacía.");
  const parsed = record(JSON.parse(text));
  const usage = record(payload.usageMetadata);
  const inputTokens = typeof usage.promptTokenCount === "number" && Number.isFinite(usage.promptTokenCount) ? usage.promptTokenCount : null;
  const outputTokens = typeof usage.candidatesTokenCount === "number" && Number.isFinite(usage.candidatesTokenCount) ? usage.candidatesTokenCount + (Number(usage.thoughtsTokenCount ?? 0) || 0) : null;
  const prices =
    model === "gemini-3.1-flash-lite"
      ? { input: 0.25, output: 1.5 }
      : model === "gemini-2.5-flash-lite"
        ? { input: 0.1, output: 0.4 }
        : model === "gemini-2.5-flash"
          ? { input: 0.3, output: 2.5 }
          : null;
  return {
    parsed,
    model,
    inputTokens,
    outputTokens,
    costUsd: prices && inputTokens != null && outputTokens != null ?
      (inputTokens * prices.input + outputTokens * prices.output) /
      1_000_000 : null,
  };
}

/** Analiza perfil vs. licitación en el backend web, sin crear jobs del worker. */
export async function analyzeMatchDirect(input: {
  tenantId: string;
  idContrato: number;
  profileId?: string;
  actorId?: string | null;
  force?: boolean;
}): Promise<DirectMatchAnalysisResult> {
  const profileResult = await pool.query(
    `SELECT * FROM company_profiles
     WHERE tenant_id=$1 AND is_active=true AND ($2::uuid IS NULL OR id=$2::uuid)
     ORDER BY updated_at DESC LIMIT 1`,
    [input.tenantId, input.profileId ?? null],
  );
  const profile = profileResult.rows[0] as JsonRecord | undefined;
  if (!profile)
    throw new Error("No se encontró un perfil activo para esta empresa.");

  const contractResult = await pool.query(
    `SELECT c.codigo, c.descripcion, c.entidad_nombre, te.id AS extraction_id,
       te.summary_json, te.quality, te.requires_human_review
     FROM seace_contracts c
     LEFT JOIN LATERAL (
       SELECT tx.id, tx.summary_json, tx.quality, tx.requires_human_review FROM contract_documents d
       JOIN tdr_extractions tx ON tx.contract_document_id=d.id AND tx.is_current=true
       WHERE d.id_contrato=c.id_contrato ORDER BY tx.created_at DESC, tx.id DESC LIMIT 1
     ) te ON true WHERE c.id_contrato=$1`,
    [input.idContrato],
  );
  const contract = contractResult.rows[0] as JsonRecord | undefined;
  if (!contract) throw new Error("No se encontró la licitación.");
  if (!contract.extraction_id || contract.quality === "failed" || contract.requires_human_review) {
    throw new Error("La licitación necesita una lectura documental válida antes de analizar su afinidad.");
  }

  const facetsResult = await pool.query(
    `SELECT facet, label, required, details_json, facet_hash
     FROM requirement_facets WHERE id_contrato=$1 AND extraction_id=$2 AND is_current=true ORDER BY facet,id`,
    [input.idContrato, contract.extraction_id],
  );
  const facets = facetsResult.rows as JsonRecord[];
  const facetsHash = canonicalHash(
    facets.map((row) => String(row.facet_hash)).sort(),
  );
  const profileHash = String(profile.profile_hash ?? "");
  const existingResult = await pool.query(
    `SELECT id, verdict, score, breakdown_json FROM matches
     WHERE profile_id=$1 AND id_contrato=$2`,
    [profile.id, input.idContrato],
  );
  const existing = existingResult.rows[0] as JsonRecord | undefined;
  const existingBreakdown = record(existing?.breakdown_json);
  const meta = record(existingBreakdown.meta);
  const systemPrompt = analysisPrompt();
  const promptSha256 = crypto.createHash("sha256").update(systemPrompt).digest("hex");
  if (
    existing &&
    !input.force &&
    meta.prompt_version === PROMPT_VERSION &&
    meta.prompt_sha256 === promptSha256 &&
    meta.semantic_guard_version === SEMANTIC_GUARD_VERSION &&
    meta.context_version === COMPACT_PROFILE_VERSION &&
    String(meta.extraction_id) === String(contract.extraction_id) &&
    meta.facets_hash === facetsHash &&
    meta.profile_hash === profileHash
  ) {
    return { skipped: "unchanged", match_id: Number(existing.id) };
  }

  const { substantive: requested, administrativeChecklist } = splitMatchRequirements(facets);
  if (!requested.length) throw new Error("La lectura no contiene requisitos sustantivos para evaluar capacidad.");
  const compactProfile = buildCompactMatchProfile(profile);
  const result = await callGemini({
    perfil: compactProfile,
    oportunidad: {
      codigo: contract.codigo,
      descripcion: contract.descripcion,
      entidad: contract.entidad_nombre,
      resumen: contract.summary_json,
    },
    requisitos: requested,
  }, systemPrompt);
  const rawRequirements = list(result.parsed.requisitos).map(record);
  const substantive = checklistOmittedProposalDocuments(rawRequirements, requested, administrativeChecklist);
  if (!substantive.length) {
    throw new Error("La respuesta no evaluó ningún requisito sustantivo; no se guardó un puntaje ni se reintentó automáticamente.");
  }
  if (rawRequirements.length !== substantive.length || rawRequirements.some((item, index) => item.source_index !== substantive[index].source_index || isAdministrativeRequirement(item))) {
    throw new Error("La respuesta no cubre exactamente los requisitos sustantivos; no se guardó un puntaje ni se reintentó automáticamente.");
  }
  const evidenceIds = new Set(list(compactProfile.experience_json).flatMap(experience => list(record(record(experience).documentary_summary).sources).map(source => String(record(source).document_id))));
  if (rawRequirements.some(item => !Array.isArray(item.evidence_refs) || list(item.evidence_refs).some(ref => typeof ref !== "string" || !evidenceIds.has(ref)) || typeof item.evidence_explanation !== "string")) {
    throw new Error("La respuesta contiene referencias documentales desconocidas; no se guardó un puntaje ni se reintentó automáticamente.");
  }
  const requirements: Requirement[] = rawRequirements.map((item, index) => ({
    source_index: Number(item.source_index),
    source_facet: String(substantive[index].tipo),
    evidence_refs: Array.from(new Set(list(item.evidence_refs).map(String))),
    evidence_explanation: String(item.evidence_explanation),
    economic_comparison: list(item.economic_comparison).map(record),
    requisito: clip(item.requisito, 90) ?? "Requisito",
    categoria: SOURCE_CATEGORIES[String(substantive[index].tipo)] ?? "otro",
    estado: STATES.has(String(item.estado))
      ? String(item.estado)
      : "requiere_revision",
    critico: Boolean(substantive[index].obligatorio && item.critico),
    gap: clip(item.gap, 140),
    accion: clip(item.accion, 110),
  }));
  const economicValidation = guardEconomicComparisons(requirements, compactProfile, substantive);
  const unsupportedConclusions: number[] = [];
  for (const requirement of requirements) {
    const experience = ["economic_experience", "general_experience", "specific_experience"].includes(requirement.source_facet);
    const unsupportedExperience = experience && ["cumple", "no_cumple"].includes(requirement.estado) && !requirement.evidence_refs.length;
    const evidenceAbsent = requirement.estado === "no_cumple" && /sin (?:evidencia|respaldo)|ausencia|no (?:se (?:ha )?(?:identific|acredit)|consta|hay (?:evidencia|respaldo)|existe (?:evidencia|respaldo)|acredita|presenta|documentad)|falta (?:acreditar|evidencia|respaldo)|(?:evidencia|respaldo|documentacion).*(?:ausente|no adjuntad)/i.test(`${requirement.gap ?? ""} ${requirement.evidence_explanation}`);
    if (unsupportedExperience || evidenceAbsent) {
      unsupportedConclusions.push(requirement.source_index);
      requirement.estado = "requiere_revision";
      if (unsupportedExperience) {
        requirement.gap = "No se atribuyó una fuente documental pertinente que respalde esta conclusión.";
        requirement.accion = "Verificar objeto, moneda, monto y respaldo compatibles con las bases.";
      }
    }
  }
  // Economic compatibility is judged against documentary object/currency and
  // evidence. A generic total, or a set of areas, cannot override that judgment.
  const actions = guardParticipationActions(requirements, list(result.parsed.acciones_recomendadas), contract.summary_json);
  const verdict = deriveVerdict(requirements);
  const rawModelScore = Number(result.parsed.score ?? 0);
  const scoreValidated = economicValidation.scoreValidated && unsupportedConclusions.length === 0;
  const score = scoreValidated ? clampScore(verdict, rawModelScore) : Math.max(0, Math.min(100, Math.trunc(rawModelScore)));
  const analyzedAt = new Date().toISOString();
  const breakdown = {
    resumen: scoreValidated ? clip(result.parsed.resumen, 320) : "La evidencia del modelo necesita validar compatibilidad, montos y respaldos. El puntaje no se publica hasta completar esa comprobación.",
    requisitos: requirements,
    administrative_checklist: administrativeChecklist,
    acciones_recomendadas: actions
      .slice(0, 4)
      .map((item) => clip(item, 110))
      .filter(Boolean),
    meta: {
      model: result.model,
      prompt_version: PROMPT_VERSION,
      prompt_sha256: promptSha256,
      semantic_guard_version: SEMANTIC_GUARD_VERSION,
      profile_hash: profileHash,
      facets_hash: facetsHash,
      extraction_id: contract.extraction_id,
      context_version: COMPACT_PROFILE_VERSION,
      score_basis: "substantive_requirements_only",
      administrative_score_impact: false,
      single_request_no_fallback: true,
      raw_model_score: rawModelScore,
      raw_model_summary: result.parsed.resumen,
      score_validated: scoreValidated,
      economic_validation_policy: SEMANTIC_GUARD_VERSION,
      invalid_economic_comparisons: economicValidation.invalidEconomicComparisons,
      unsupported_conclusions: unsupportedConclusions,
      input_tokens: result.inputTokens,
      output_tokens: result.outputTokens,
      cost_usd: result.costUsd,
      analyzed_at: analyzedAt,
      execution: "web_direct",
    },
  };
  const missing = requirements
    .filter((item) => item.estado !== "cumple")
    .map((item) => ({
      facet: item.categoria,
      label: item.requisito,
      estado: item.estado,
      accion: item.accion,
      gap: item.gap,
      critico: item.critico,
    }));

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const saved = await client.query(
      `INSERT INTO matches (profile_id,id_contrato,score,verdict,breakdown_json,missing_actions_json,updated_at)
       VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,now())
       ON CONFLICT (profile_id,id_contrato) DO UPDATE SET
         score=EXCLUDED.score, verdict=EXCLUDED.verdict,
         breakdown_json=EXCLUDED.breakdown_json,
         missing_actions_json=EXCLUDED.missing_actions_json,
         matched_at=now(), updated_at=now()
       RETURNING id`,
      [
        profile.id,
        input.idContrato,
        score,
        verdict,
        JSON.stringify(breakdown),
        JSON.stringify(missing),
      ],
    );
    const matchId = Number(saved.rows[0].id);
    await client.query(
      `INSERT INTO match_events (match_id,event_type,payload,actor_id)
       VALUES (
         $1,
         'ai_analysis_completed',
         $2::jsonb,
         CASE WHEN EXISTS (
           SELECT 1 FROM tenant_members WHERE tenant_id=$4::uuid AND user_id=$3::uuid
         ) THEN $3::uuid ELSE NULL END
       )`,
      [
        matchId,
        JSON.stringify({
          verdict,
          score,
          previous_verdict: existing?.verdict ?? null,
          previous_score: existing?.score ?? null,
          model: result.model,
          prompt_version: PROMPT_VERSION,
          input_tokens: result.inputTokens,
          output_tokens: result.outputTokens,
          cost_usd: result.costUsd,
          execution: "web_direct",
        }),
        input.actorId ?? null,
        input.tenantId,
      ],
    );
    await client.query(
      `UPDATE worker_jobs SET status='done',finished_at=now(),last_error=NULL
       WHERE job_type='analyze_match' AND status='pending'
         AND payload->>'profile_id'=$1
         AND (payload->>'id_contrato')::bigint=$2`,
      [profile.id, input.idContrato],
    );
    await client.query("COMMIT");
    return { match_id: matchId, verdict: scoreValidated ? verdict : "gris", score: scoreValidated ? score : null };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}
