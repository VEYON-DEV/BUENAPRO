import { AppShell } from "@/features/shell";
import { PageHeader } from "@/components/ui/PageHeader";
import { query } from "@/server/db/client";
import { ProfileForm } from "../../components/ProfileForm";
import { BusinessLinesPanel } from "../../components/BusinessLinesPanel";
import { SeaceConnectionPanel } from "../../components/SeaceConnectionPanel";
import { CompanyKeywordsPanel } from "../../components/CompanyKeywordsPanel";
import styles from "./ProfilePage.module.css";

function profileCompletion(profile: any | null) {
  if (!profile) return 0;
  const hasPositiveValue = (value: unknown) => {
    if (typeof value === "number") return value > 0;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return Object.values(value).some((item) => (typeof item === "number" ? item > 0 : Boolean(item)));
    }
    return Boolean(value);
  };
  const checks = [
    profile.ruc,
    profile.razon_social,
    Array.isArray(profile.identity_json?.rnp) ? profile.identity_json.rnp.length : profile.identity_json?.rnp,
    hasPositiveValue(profile.econ_experience_json),
    Array.isArray(profile.team_json) && profile.team_json.length,
    Array.isArray(profile.hireable_roles_json) && profile.hireable_roles_json.length,
    Array.isArray(profile.experience_json) && profile.experience_json.length,
    Array.isArray(profile.equipment_json) && profile.equipment_json.length,
    Array.isArray(profile.certifications_json) && profile.certifications_json.length,
  ];
  return Math.round((checks.filter(Boolean).length / checks.length) * 100);
}

function money(value: unknown) {
  const number = Number(value ?? 0);
  if (!Number.isFinite(number) || number <= 0) return "Sin dato";
  return new Intl.NumberFormat("es-PE", { currency: "PEN", maximumFractionDigits: 0, style: "currency" }).format(number);
}

export async function ProfilePage({ tenantId }: { tenantId: string }) {
  const tenant = await query("SELECT id, name FROM tenants WHERE id = $1 LIMIT 1", [tenantId]);
  const profile = await query("SELECT * FROM company_profiles WHERE tenant_id = $1 ORDER BY created_at LIMIT 1", [tenantId]);
  const lines = await query(
    `
    SELECT bl.*
    FROM business_lines bl
    JOIN company_profiles cp ON cp.id = bl.profile_id
    WHERE cp.tenant_id = $1 AND bl.is_active = true
    ORDER BY bl.created_at
    `,
    [tenantId],
  );
  const catalogs = await query(
    `
    SELECT s.codigo, s.nombre,
      EXISTS (SELECT 1 FROM mvp_enabled_cubso_segments e WHERE e.codigo=s.codigo AND e.anio=s.anio AND e.enabled=true) AS enabled
    FROM cat_cubso_segmentos s
    WHERE s.anio = 2026
    ORDER BY s.codigo::int
    `,
  );
  const profileRow = profile.rows[0] ?? null;
  const completion = profileCompletion(profileRow);
  const stats = profileRow
    ? await query(
        `
        SELECT
          count(m.id)::int AS matches_total,
          count(m.id) FILTER (WHERE m.verdict = 'verde')::int AS verdes,
          count(m.id) FILTER (WHERE m.verdict = 'ambar')::int AS ambar,
          count(m.id) FILTER (WHERE m.verdict = 'rojo')::int AS rojos,
          count(m.id) FILTER (WHERE m.verdict = 'gris')::int AS revision,
          COALESCE(round(avg(m.score)), 0)::int AS avg_score
        FROM matches m
        WHERE m.profile_id = $1
        `,
        [profileRow.id],
      )
    : { rows: [{ matches_total: 0, verdes: 0, ambar: 0, rojos: 0, revision: 0, avg_score: 0 }] };
  const matchStats = stats.rows[0] as any;
  const keywordCount = new Set([...(profileRow?.company_keywords ?? []), ...lines.rows.flatMap((line: any) => line.keywords ?? [])]).size;

  return (
    <AppShell title="Perfil">
      <PageHeader title="Perfil de empresa" description="Configura el radar que BuenaPro usa para descubrir y evaluar oportunidades." meta={tenant.rows[0]?.name ?? "Workspace"} />
      <section className={styles.identity}>
        <div className={styles.companyMark}>{(profileRow?.razon_social ?? "BP").slice(0, 2).toUpperCase()}</div>
        <div className={styles.company}><h2>{profileRow?.razon_social ?? "Completa tu empresa"}</h2><p>{profileRow?.ruc ? `RUC ${profileRow.ruc}` : "Sin RUC registrado"}</p></div>
        <div className={styles.completion}><div><span>Completitud</span><strong>{completion}%</strong></div><i><b style={{ width: `${completion}%` }} /></i></div>
      </section>
      <section className={styles.signalStrip} aria-label="Resumen del perfil">
        <div><span>Líneas</span><strong>{lines.rows.length}</strong></div>
        <div><span>Keywords</span><strong>{keywordCount}</strong></div>
        <div><span>Experiencia acreditable</span><strong>{money(Object.values(profileRow?.econ_experience_json ?? {}).map(Number).filter(Number.isFinite).sort((a, b) => b - a)[0])}</strong></div>
        <div><span>Equipo</span><strong>{profileRow?.team_json?.length ?? 0} perfiles</strong></div>
        <div><span>Evaluadas</span><strong>{matchStats.matches_total ?? 0}</strong></div>
      </section>
      <div className={styles.workspace}>
        <div className={styles.primary}>
          {profileRow ? <CompanyKeywordsPanel keywords={(profileRow.company_keywords ?? []) as string[]} /> : null}
          <BusinessLinesPanel
            lines={lines.rows as any[]}
            catalogs={catalogs.rows as Array<{ codigo: string; nombre: string; enabled: boolean }>}
          />
        </div>
        <aside className={styles.secondary}>
          <ProfileForm profile={profileRow} />
          <SeaceConnectionPanel />
        </aside>
      </div>
    </AppShell>
  );
}
