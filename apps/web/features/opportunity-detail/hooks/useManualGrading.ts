"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchGradingStatus, submitGrading } from "../api/manualGrading";
import {
  GRADING_POLL_INTERVAL_MS,
  gradingEndpoint,
  gradingPresentation,
  isAlreadyGraded,
  isGradingActive,
  localGradingRequest,
  type GradingPresentation,
  type GradingProblem,
  type GradingRequest,
  type GradingSource,
  type GradingUsage,
} from "../model/manualGrading";

/**
 * Estado de la calificación manual de una licitación (menores) o convocatoria (mayores): consulta al montar,
 * pide calificar, revisa cada 20 s mientras hay una solicitud en curso y refresca la página al terminar.
 */
export function useManualGrading({
  source = "contracts",
  id,
  profileId,
  regrade,
}: {
  /** Menores (contracts) por defecto; mayores usa prod2. */
  source?: GradingSource;
  id: string | number;
  profileId?: string | null;
  regrade: boolean;
}): { view: GradingPresentation; submit: () => Promise<void> } {
  const router = useRouter();
  const endpoint = gradingEndpoint(source, id);
  const [request, setRequest] = useState<GradingRequest | null>(null);
  const [usage, setUsage] = useState<GradingUsage | null>(null);
  const [problem, setProblem] = useState<GradingProblem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const locked = useRef(false);

  useEffect(() => {
    const abort = new AbortController();
    fetchGradingStatus(endpoint, profileId, abort.signal)
      .then((snapshot) => {
        setRequest(snapshot.request);
        setUsage(snapshot.usage);
      })
      // Sin estado el botón sigue disponible: el servidor aplica el tope y evita duplicados.
      .catch(() => undefined);
    return () => abort.abort();
  }, [endpoint, profileId]);

  const active = isGradingActive(request?.status);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const abort = new AbortController();
    const poll = async () => {
      let keepPolling = true;
      try {
        const snapshot = await fetchGradingStatus(endpoint, profileId, abort.signal);
        if (cancelled) return;
        setRequest(snapshot.request);
        if (snapshot.usage) setUsage(snapshot.usage);
        keepPolling = isGradingActive(snapshot.request?.status);
        if (snapshot.request?.status === "done") router.refresh();
      } catch {
        if (cancelled) return;
      }
      if (keepPolling) timer = setTimeout(poll, GRADING_POLL_INTERVAL_MS);
    };
    timer = setTimeout(poll, GRADING_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      abort.abort();
    };
  }, [active, endpoint, profileId, router]);

  const submit = useCallback(async () => {
    if (locked.current) return;
    locked.current = true;
    setSubmitting(true);
    setProblem(null);
    try {
      const outcome = await submitGrading(endpoint, { profileId, force: regrade });
      if (outcome.usage) setUsage(outcome.usage);
      if (outcome.kind === "done") {
        setRequest(localGradingRequest("done"));
        router.refresh();
      } else if (outcome.kind === "queued") {
        setRequest(outcome.request ?? localGradingRequest("pending"));
      } else if (outcome.kind === "problem") {
        setProblem(outcome.problem);
      } else {
        setRequest(localGradingRequest("failed", outcome.reason));
        // Ya la calificó otra solicitud: recarga para mostrar la evaluación en vez de un error.
        if (isAlreadyGraded(outcome.reason)) router.refresh();
      }
    } finally {
      locked.current = false;
      setSubmitting(false);
    }
  }, [endpoint, profileId, regrade, router]);

  return { view: gradingPresentation({ request, usage, submitting, problem, regrade, source }), submit };
}
