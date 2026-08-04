"use client";

import { useMemo, useState } from "react";
import { AppShell } from "@/features/shell";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Table } from "@/components/ui/Table";
import { EmptyState } from "@/components/ui/EmptyState";
import styles from "./AdminPage.module.css";

type Dataset = "worker_jobs" | "pipeline_events" | "requires_review" | "api_contract_checks";

const datasets = [
  { key: "worker_jobs", label: "Worker jobs", path: "/api/admin/worker_jobs?page_size=25" },
  { key: "pipeline_events", label: "Pipeline events", path: "/api/admin/pipeline_events?page_size=25" },
  { key: "requires_review", label: "Requiere revision", path: "/api/admin/tdr_extractions/requires_review?page_size=25" },
  { key: "api_contract_checks", label: "Contract tests", path: "/api/admin/api_contract_checks?page_size=25" },
] as const;

export function AdminPage() {
  const [token, setToken] = useState("");
  const [dataset, setDataset] = useState<Dataset>("worker_jobs");
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [status, setStatus] = useState("Listo");
  const [batch, setBatch] = useState<any>(null);
  const active = useMemo(() => datasets.find((item) => item.key === dataset) ?? datasets[0], [dataset]);

  async function loadData() {
    setStatus("Cargando");
    const response = await fetch(active.path, { headers: { "x-internal-token": token } });
    const payload = await response.json();
    setRows(response.ok ? payload.data ?? [] : []);
    setStatus(response.ok ? `${payload.data?.length ?? 0} filas` : payload.error ?? "Error");
  }

  async function loadBatch() {
    const response = await fetch("/api/admin/batches/status", { headers: { "x-internal-token": token } });
    const payload = await response.json();
    setBatch(response.ok ? payload.data : null);
    setStatus(response.ok ? "Progreso actualizado" : payload.error ?? "Error");
  }

  async function retryJob(id: unknown) {
    if (!window.confirm(`Reintentar job ${id}?`)) return;
    await fetch(`/api/admin/worker_jobs/${id}/retry`, { method: "POST", headers: { "x-internal-token": token } });
    await loadData();
  }

  async function deadJob(id: unknown) {
    if (!window.confirm(`Marcar job ${id} como dead?`)) return;
    await fetch(`/api/admin/worker_jobs/${id}/dead`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-internal-token": token },
      body: JSON.stringify({ reason: "manual desde admin" }),
    });
    await loadData();
  }

  const keys = rows.length ? Object.keys(rows[0]).slice(0, 8) : [];

  return (
    <AppShell title="Admin tecnico">
      <header className={styles.header}>
        <p>Operaciones internas</p>
        <h1>Admin tecnico</h1>
        <span>Jobs, batch MVP y salud del pipeline.</span>
      </header>

      <section className={styles.panel}>
        <div className={styles.toolbar}>
          <Input aria-label="Token interno" onChange={(event) => setToken(event.target.value)} placeholder="INTERNAL_JOBS_TOKEN" type="password" value={token} />
          <Select onChange={(event) => setDataset(event.target.value as Dataset)} value={dataset}>
            {datasets.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
          </Select>
          <Button onClick={loadData}>Cargar</Button>
          <Button onClick={loadBatch} variant="secondary">Progreso</Button>
        </div>
        <p className={styles.status}>{status}</p>
      </section>

      {batch ? (
        <section className={styles.metrics}>
          {metric("Batch", batch.batch_id ?? "-")}
          {metric("Estado", batch.status ?? "-")}
          {metric("ETA", batch.eta?.text ?? "-")}
          {metric("Contratos", batch.totals?.contracts_seen ?? "-")}
          {metric("Extracciones", batch.totals?.extractions ?? "-")}
          {metric("Costo USD", batch.totals?.cost_usd ?? "-")}
        </section>
      ) : null}

      <section className={styles.panel}>
        {rows.length ? (
          <Table>
            <thead>
              <tr>
                {keys.map((key) => <th key={key}>{key}</th>)}
                {dataset === "worker_jobs" ? <th>Acciones</th> : null}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={String(row.id ?? index)}>
                  {keys.map((key) => <td key={key}>{formatCell(row[key])}</td>)}
                  {dataset === "worker_jobs" ? (
                    <td className={styles.actions}>
                      <Button onClick={() => retryJob(row.id)} variant="secondary">Retry</Button>
                      <Button onClick={() => deadJob(row.id)} variant="danger">Dead</Button>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState title="Sin datos cargados">Ingresa el token interno y carga una tabla.</EmptyState>
        )}
      </section>
    </AppShell>
  );
}

function metric(label: string, value: unknown) {
  return (
    <div className={styles.metric}>
      <span>{label}</span>
      <strong>{formatCell(value)}</strong>
    </div>
  );
}

function formatCell(value: unknown) {
  if (value == null) return "-";
  if (typeof value === "object") return JSON.stringify(value).slice(0, 130);
  return String(value);
}
