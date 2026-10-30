"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchGradingStatus, submitGrading } from "../api/manualGrading";
import {
  GRADING_POLL_INTERVAL_MS,
  gradingPresentation,
  isGradingActive,
  localGradingRequest,
  type GradingPresentation,
  type GradingProblem,
  type GradingRequest,
  type GradingUsage,
} from "../model/manualGrading";

/**
 * Estado de la calificación manual de una licitación menor: consulta al montar, pide calificar,
 * revisa cada 20 s mientras hay una solicitud en curso y refresca la página al terminar.
 */
export function useManualGrading({
  idContrato,
  profileId,
  regrade,
}: {
  idContrato: string | number;
  profileId?: string | null;
  regrade: boolean;
}): { view: GradingPresentation; submit: () => Promise<void> } {
  const router = useRouter();
  const [request, setRequest] = useState<GradingRequest | null>(null);
  const [usage, setUsage] = useState<GradingUsage | null>(null);
  const [problem, setProblem] = useState<GradingProblem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const locked = useRef(false);

  useEffect(() => {
    const abort = new AbortController();
    fetchGradingStatus(idContrato, profileId, abort.signal)
      .then((snapshot) => {
        setRequest(snapshot.request);
        setUsage(snapshot.usage);
      })
      // Sin estado el botón sigue disponible: el servidor aplica el tope y evita duplicados.
      .catch(() => undefined);
    return () => abort.abort();
  }, [idContrato, profileId]);

  const active = isGradingActive(request?.status);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const abort = new AbortController();
    const poll = async () => {
      let keepPolling = true;
      try {
        const snapshot = await fetchGradingStatus(idContrato, profileId, abort.signal);
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
  }, [active, idContrato, profileId, router]);

  const submit = useCallback(async () => {
    if (locked.current) return;
    locked.current = true;
    setSubmitting(true);
    setProblem(null);
    try {
      const outcome = await submitGrading(idContrato, { profileId, force: regrade });
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
      }
    } finally {
      locked.current = false;
      setSubmitting(false);
    }
  }, [idContrato, profileId, regrade, router]);

  return { view: gradingPresentation({ request, usage, submitting, problem, regrade }), submit };
}
