import type { QueryResultRow } from "pg";
import { pool } from "@/server/db/client";

/** Tiempo máximo de cada consulta del panel; protege a producción de una consulta desbocada. */
export const OPS_STATEMENT_TIMEOUT_MS = 20_000;

/**
 * Ejecuta una consulta del panel en una transacción de SOLO LECTURA con tiempo límite: aunque una consulta
 * escribiera por error, PostgreSQL la rechaza. El panel no escribe nunca.
 */
export async function opsQuery<T extends QueryResultRow = QueryResultRow>(text: string, values: unknown[] = []): Promise<T[]> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN READ ONLY");
    await client.query(`SET LOCAL statement_timeout = ${OPS_STATEMENT_TIMEOUT_MS}`);
    const result = await client.query<T>(text, values);
    await client.query("COMMIT");
    return result.rows;
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw error;
  } finally {
    client.release();
  }
}
