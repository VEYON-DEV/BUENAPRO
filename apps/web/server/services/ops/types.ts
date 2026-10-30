/**
 * Tipos compartidos del panel de operación («ops»). Sin dependencias: los importan tanto los
 * servicios de servidor como el feature de UI y las pruebas.
 */

export type OpsSource = "menores" | "mayores";

export const OPS_SOURCES: readonly OpsSource[] = ["menores", "mayores"];

export const OPS_SOURCE_LABEL: Record<OpsSource, string> = {
  menores: "Menores (PROD6)",
  mayores: "Mayores (PROD2)",
};

export type OpsCompany = {
  id: string;
  /** Nombre corto para filtros y columnas: razón social sin sufijo societario. */
  label: string;
  razonSocial: string;
  tenantId: string;
};

/** Qué representa una fila de una lista: define columnas y si hay traza. */
export type OpsRowKind = "licitacion" | "busqueda" | "trabajo" | "embedding";
