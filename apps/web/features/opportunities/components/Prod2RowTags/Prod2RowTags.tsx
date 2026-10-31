import { Star } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import styles from "./Prod2RowTags.module.css";

type Line = { name: string; favorite: boolean };

type Props = {
  isNew?: boolean;
  isActive?: boolean;
  /** Línea del perfil que calza con la licitación (solo si suma puntos); null no muestra nada. */
  line?: Line | null;
  /** Palabras clave que coinciden, ya recortadas al máximo que cabe. */
  keywords?: string[];
  /** Palabras que no caben; se resumen como «+N». */
  extra?: number;
  /** Con enlaces la línea y las palabras filtran la lista (vista rápida); sin ellos son solo etiquetas (fila). */
  lineHref?: string;
  keywordHref?: (keyword: string) => string;
  /** Sin margen superior: para contenedores que ya separan sus hijos (vista rápida). */
  flush?: boolean;
};

function LineTag({ line, href }: { line: Line; href?: string }) {
  const content = <>
    {line.favorite ? <Star aria-hidden="true" className={styles.star} fill="currentColor" size={13} strokeWidth={1.75} /> : null}
    <span className={styles.srOnly}>{line.favorite ? "Línea prioritaria: " : "Línea: "}</span>
    <span className={styles.text}>{line.name}</span>
  </>;
  const className = [styles.line, line.favorite && styles.favorite].filter(Boolean).join(" ");
  if (href) return <Link className={className} href={href} title={`Ver licitaciones de la línea ${line.name}`}>{content}</Link>;
  return <span className={className} title={line.favorite ? `Línea prioritaria: ${line.name}` : `Línea: ${line.name}`}>{content}</span>;
}

/**
 * Etiquetas compactas de una licitación mayor: Nueva, Activa, la línea de negocio que calza y las palabras que coinciden.
 * Nada que no exista se dibuja: sin etiquetas devuelve null y la fila queda como estaba.
 */
export function Prod2RowTags({ isNew = false, isActive = false, line = null, keywords = [], extra = 0, lineHref, keywordHref, flush = false }: Props) {
  if (!isNew && !isActive && !line && !keywords.length) return null;
  const linked = Boolean(lineHref || keywordHref);
  return (
    <div className={[styles.tags, linked && styles.linked, flush && styles.flush].filter(Boolean).join(" ")}>
      {isNew ? <Badge tone="brand">Nueva</Badge> : null}
      {isActive ? <Badge tone="green">Activa</Badge> : null}
      {line ? <LineTag href={lineHref} line={line} /> : null}
      {keywords.length ? <span className={styles.srOnly}>Coincidencias: </span> : null}
      {keywords.map(keyword => keywordHref
        ? <Link className={styles.keyword} href={keywordHref(keyword)} key={keyword} title={`Ver licitaciones con «${keyword}»`}><span className={styles.text}>{keyword}</span></Link>
        : <span className={styles.keyword} key={keyword} title={keyword}><span className={styles.text}>{keyword}</span></span>)}
      {extra > 0 ? <span className={styles.more} title={`${extra} ${extra === 1 ? "palabra más" : "palabras más"}`}>+{extra}</span> : null}
    </div>
  );
}
