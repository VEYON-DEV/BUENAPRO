import type { ProcurementScheduleStage, ScheduleDatePrecision } from "./procurementSchedule";

export type TimelineSource = "prod6" | "prod4" | "prod2";
export type TimelineOpportunity = {
  id: string;
  source: TimelineSource;
  sourceId: string;
  code: string;
  title: string;
  entity: string;
  objectType: string;
  detailHref: string;
  fitLevel: number;
  fitPoints: number;
  score: number | null;
  verdict: "verde" | "ambar" | null;
  status: "affinity" | "evaluated" | "saved";
  /** Bookmarked by the workspace: always shown while its deadline is open. */
  saved: boolean;
  schedule: ProcurementScheduleStage[];
  scheduleFetchedAt: string | null;
  deadline: { at: string; precision: ScheduleDatePrecision; name: string } | null;
};
export type TimelineResponse = {
  data: TimelineOpportunity[];
  meta: { source: TimelineSource; count: number; generatedAt: string; timezone: "America/Lima" };
};
