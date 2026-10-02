import { AppShell } from "@/features/shell";
import { PageHeader } from "@/components/ui/PageHeader";
import { query } from "@/server/db/client";
import { ProfileWorkspace } from "../../components/ProfileWorkspace";
import { BusinessLinesPanel } from "../../components/BusinessLinesPanel";
import { SeaceConnectionPanel } from "../../components/SeaceConnectionPanel";
import { CompanyKeywordsPanel } from "../../components/CompanyKeywordsPanel";
import { CompanyLibraryPanel } from "../../components/CompanyLibraryPanel";
import { listCompanyLibrary } from "@/server/services/companyLibrary";
import styles from "./ProfilePage.module.css";

function profileCompletion(profile: any | null, libraryCount = 0) {
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
    libraryCount,
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
  const library = await listCompanyLibrary(tenantId);
  const libraryCount = library.knowledge.length + library.documents.length;
  const completion = profileCompletion(profileRow, libraryCount);
  const keywordCount = new Set([...(profileRow?.company_keywords ?? []), ...lines.rows.flatMap((line: any) => line.keywords ?? [])]).size;

  return (
    <AppShell title="Perfil">
      <PageHeader title="Perfil de empresa" description="Organiza tu radar, equipo y evidencia para evaluar oportunidades." meta={tenant.rows[0]?.name ?? "Empresa"} />
      <section className={styles.identity}>
        <div className={styles.companyMark}>{(profileRow?.razon_social ?? "BP").slice(0, 2).toUpperCase()}</div>
        <div className={styles.company}><h2>{profileRow?.razon_social ?? "Completa tu empresa"}</h2><p>{profileRow?.ruc ? `RUC ${profileRow.ruc}` : "Sin RUC registrado"}</p></div>
        <div className={styles.completion}><div><span>Secciones con datos</span><strong>{completion}%</strong></div><i><b style={{ width: `${completion}%` }} /></i><small>No acredita cumplimiento ni verifica documentos.</small></div>
      </section>
      <section className={styles.signalStrip} aria-label="Resumen del perfil">
        <div><span>Líneas</span><strong>{lines.rows.length}</strong></div>
        <div><span>Palabras clave</span><strong>{keywordCount}</strong></div>
        <div><span>Experiencia acreditable</span><strong>{money(Object.values(profileRow?.econ_experience_json ?? {}).map(Number).filter(Number.isFinite).sort((a, b) => b - a)[0])}</strong></div>
        <div><span>Equipo</span><strong>{profileRow?.team_json?.length ?? 0} perfiles</strong></div>
        <div><span>Biblioteca</span><strong>{libraryCount} piezas</strong></div>
      </section>
      <ProfileWorkspace profile={profileRow} radar={<>
          {profileRow ? <CompanyKeywordsPanel keywords={(profileRow.company_keywords ?? []) as string[]} /> : null}
          <BusinessLinesPanel
            lines={lines.rows as any[]}
            catalogs={catalogs.rows as Array<{ codigo: string; nombre: string; enabled: boolean }>}
          />
          </>} library={<CompanyLibraryPanel initialLibrary={library} />} connection={<SeaceConnectionPanel />} />
    </AppShell>
  );
}
