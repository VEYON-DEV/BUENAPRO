/**
 * SQL de solo lectura para la afinidad JEV de contratos menores (tabla `prod6_profile_decisions`).
 * Sin imports: lo comparten el servicio de contratos y las pruebas contra PostgreSQL.
 *
 * Un par (contrato, perfil) puede tener varias filas porque la clave incluye contract_hash,
 * profile_hash y policy_version. Se usa siempre la más reciente, dando prioridad a las filas
 * calificadas por JEV (`decided_by = 'jev'`) y desempatando por updated_at e id. Las filas
 * `superseded` (pendientes obsoletas con una fila más nueva) nunca cuentan: sin esa exclusión una
 * pendiente vieja de JEV taparía una decisión más nueva del prefiltro. La tabla vacía produce
 * LEFT JOIN sin coincidencias, nunca un error.
 */
export const JEV_ACTIVE_DECISION_FILTER = "d.decision <> 'superseded'";

export const JEV_LATEST_DECISION_ORDER = "(d.decided_by = 'jev') DESC, d.updated_at DESC, d.id DESC";

/** LATERAL para la lista: campos `jev_*` del perfil activo ya resuelto (parámetro uuid o null). */
export function jevListLateralSql(profileParam: number) {
  return `
  LEFT JOIN LATERAL (
    SELECT d.score::float8 AS jev_score,
           d.decision AS jev_decision,
           d.subcategory AS jev_subcategory,
           d.threshold::float8 AS jev_threshold,
           d.updated_at AS jev_decided_at
    FROM prod6_profile_decisions d
    WHERE d.id_contrato = c.id_contrato AND d.profile_id = $${profileParam}::uuid
      AND ${JEV_ACTIVE_DECISION_FILTER}
    ORDER BY ${JEV_LATEST_DECISION_ORDER}
    LIMIT 1
  ) jev ON true`;
}

/**
 * Ficha: una fila por perfil activo del tenant con decisión para el contrato.
 * Parámetros: contrato, tenant y perfil activo seleccionado (uuid o null).
 */
export function jevByCompanySql(contractParam: number, tenantParam: number, profileParam: number) {
  return `
  SELECT cp.id::text AS profile_id,
         cp.razon_social AS profile_name,
         COALESCE(cp.id = $${profileParam}::uuid, false) AS is_current_profile,
         d.score::float8 AS score,
         d.threshold::float8 AS threshold,
         d.decision,
         d.decided_by,
         d.subcategory,
         d.domain,
         d.information,
         d.updated_at AS decided_at
  FROM company_profiles cp
  JOIN LATERAL (
    SELECT d.*
    FROM prod6_profile_decisions d
    WHERE d.id_contrato = $${contractParam} AND d.profile_id = cp.id
      AND ${JEV_ACTIVE_DECISION_FILTER}
    ORDER BY ${JEV_LATEST_DECISION_ORDER}
    LIMIT 1
  ) d ON true
  WHERE cp.tenant_id = $${tenantParam} AND cp.is_active = true
  ORDER BY is_current_profile DESC, d.score DESC NULLS LAST, cp.created_at, cp.id`;
}
