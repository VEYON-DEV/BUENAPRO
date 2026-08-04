import {
  Bell,
  Building2,
  CalendarClock,
  Check,
  ChevronRight,
  CircleDollarSign,
  Filter,
  Search,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { IconButton } from "@/components/ui/IconButton";
import { Input } from "@/components/ui/Input";
import { MetricPanel } from "@/components/ui/MetricPanel";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Select } from "@/components/ui/Select";
import { Table } from "@/components/ui/Table";
import { AppShell } from "@/features/shell";
import styles from "./DesignSystemPage.module.css";

const swatches = [
  { name: "Canvas", value: "#F1F3FF", className: styles.canvas },
  { name: "Violeta", value: "#4C3CFF", className: styles.violet },
  { name: "Accion", value: "#050609", className: styles.action },
  { name: "Exito", value: "#249A68", className: styles.success },
  { name: "Alerta", value: "#C77800", className: styles.warning },
];

export function DesignSystemPage() {
  return (
    <AppShell title="Sistema de diseno">
      <div className={styles.page}>
        <header className={styles.hero}>
          <div>
            <span className={styles.eyebrow}><Sparkles size={15} /> BuenaPro Glass</span>
            <h1>Componentes y lenguaje visual</h1>
            <p>La referencia operativa para construir todas las vistas de BuenaPro.</p>
          </div>
          <Button variant="accent"><Check size={17} /> Sistema activo</Button>
        </header>

        <section className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span>01</span><h2>Color y materiales</h2></div>
            <p>Ambiente de vidrio para contexto; blanco solido para trabajo preciso.</p>
          </div>
          <div className={styles.swatches}>
            {swatches.map((swatch) => (
              <div className={styles.swatch} key={swatch.name}>
                <span className={swatch.className} />
                <strong>{swatch.name}</strong>
                <small>{swatch.value}</small>
              </div>
            ))}
          </div>
          <div className={styles.materials}>
            <GlassSurface tone="ambient">
              <span className={styles.materialLabel}>Vidrio ambiental</span>
              <strong>Contexto y lectura ejecutiva</strong>
              <p>KPIs, resumenes, navegación y paneles de apoyo.</p>
            </GlassSurface>
            <GlassSurface tone="strong">
              <span className={styles.materialLabel}>Vidrio fuerte</span>
              <strong>Controles y seleccion</strong>
              <p>Filtros, buscadores y bloques de decision.</p>
            </GlassSurface>
            <GlassSurface tone="work">
              <span className={styles.materialLabel}>Superficie de trabajo</span>
              <strong>Datos y formularios</strong>
              <p>Tablas, editores, documentos y flujos densos.</p>
            </GlassSurface>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span>02</span><h2>Acciones y controles</h2></div>
            <p>Jerarquia clara: negro para avanzar, violeta para seleccionar.</p>
          </div>
          <GlassSurface className={styles.controlBoard} tone="work">
            <div className={styles.controlGroup}>
              <label>Botones</label>
              <div className={styles.row}>
                <Button><Sparkles size={17} /> Analizar oportunidad</Button>
                <Button variant="accent">Aplicar filtros</Button>
                <Button variant="secondary">Guardar borrador</Button>
                <Button variant="ghost">Cancelar</Button>
                <IconButton aria-label="Notificaciones"><Bell size={18} /></IconButton>
              </div>
            </div>
            <div className={styles.controlGrid}>
              <label className={styles.field}><span>Buscar</span><span className={styles.inputWrap}><Search size={18} /><Input placeholder="Codigo, entidad o proveedor..." /></span></label>
              <label className={styles.field}><span>Segmento</span><Select defaultValue="technology"><option value="technology">Tecnologia</option><option>Consultoria</option></Select></label>
              <label className={styles.field}><span>Resultado</span><Select defaultValue="all"><option value="all">Todos</option><option>Adjudicado</option><option>Desierto</option></Select></label>
            </div>
            <div className={styles.row}>
              <SegmentedControl items={[{ label: "Resumen", href: "#summary", active: true }, { label: "Contratos", href: "#contracts" }, { label: "Empresas", href: "#suppliers" }]} />
              <Badge tone="green">Adjudicado</Badge>
              <Badge tone="amber">En evaluacion</Badge>
              <Badge tone="red">Cierre proximo</Badge>
              <Badge tone="brand">Alta afinidad</Badge>
              <Badge>Sin resultado</Badge>
            </div>
          </GlassSurface>
        </section>

        <section className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span>03</span><h2>Metricas y decision</h2></div>
            <p>Informacion breve, comparable y accionable.</p>
          </div>
          <div className={styles.metrics}>
            <MetricPanel label="Oportunidades vigentes" value="35" detail="5 cierran hoy" icon={<CalendarClock size={20} />} />
            <MetricPanel label="Precio mediano" value="S/ 28,540" detail="Rango S/ 18k - 42k" icon={<CircleDollarSign size={20} />} tone="green" />
            <MetricPanel label="Actividad mensual" value="+12.5%" detail="vs. mes anterior" icon={<TrendingUp size={20} />} tone="amber" />
          </div>
          <GlassSurface className={styles.decision} tone="ambient" padding="large">
            <div>
              <span className={styles.materialLabel}>Decision asistida</span>
              <h3>Tu oferta esta dentro del rango competitivo</h3>
              <p>Se comparo con 18 adjudicaciones del mismo CUBSO y 4 procesos de la misma entidad.</p>
            </div>
            <div className={styles.score}><strong>84</strong><span>Alta afinidad</span></div>
            <Button>Revisar analisis <ChevronRight size={17} /></Button>
          </GlassSurface>
        </section>

        <section className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span>04</span><h2>Tabla de trabajo</h2></div>
            <Button variant="secondary" size="compact"><Filter size={16} /> Filtrar</Button>
          </div>
          <Table>
            <thead><tr><th>Contrato</th><th>Entidad</th><th>Resultado</th><th>Ganador</th><th>Precio</th><th aria-label="Abrir" /></tr></thead>
            <tbody>
              <tr><td><strong>CM-182-2026-OFIS</strong><small>Software y licencias</small></td><td><span className={styles.entity}><Building2 size={17} /> Ministerio de Educacion</span></td><td><Badge tone="green">Adjudicado</Badge></td><td>TechSys Peru S.A.C.</td><td className={styles.money}>S/ 45,000.00</td><td><ChevronRight size={18} /></td></tr>
              <tr><td><strong>CM-105-2026-UFO</strong><small>Soporte informatico</small></td><td><span className={styles.entity}><Building2 size={17} /> Relaciones Exteriores</span></td><td><Badge tone="amber">Evaluacion</Badge></td><td>Por definir</td><td className={styles.money}>S/ 28,500.00</td><td><ChevronRight size={18} /></td></tr>
              <tr><td><strong>CM-43-2026-GRLL</strong><small>Infraestructura</small></td><td><span className={styles.entity}><Building2 size={17} /> Gobierno Regional</span></td><td><Badge tone="red">Desierto</Badge></td><td>-</td><td className={styles.money}>-</td><td><ChevronRight size={18} /></td></tr>
            </tbody>
          </Table>
        </section>
      </div>
    </AppShell>
  );
}
