export type AutomationRules = {
  enabled: boolean;
  min_fit_level: number;
  min_notification_score: number;
  max_daily_evaluations: number;
  min_hours_before_close: number;
};

export type TelegramRecipient = {
  id?: string | number;
  label: string;
  chat_id: string;
  enabled: boolean;
};

export type TelegramIntegration = {
  configured: boolean;
  enabled: boolean;
  bot_username?: string | null;
  token_hint?: string | null;
  recipients: TelegramRecipient[];
};

export type AlertPayload = {
  title?: string | null;
  descripcion?: string | null;
  codigo?: string | null;
  entity?: string | null;
  entidad_nombre?: string | null;
  summary?: string | null;
  modality?: string | null;
  strengths?: string[] | null;
  main_risk?: string | null;
  deadline?: string | null;
  cierre?: string | null;
  url?: string | null;
  cta_url?: string | null;
  cta?: { label?: string | null; url?: string | null } | string | null;
  score?: number | null;
  verdict?: string | null;
  veredicto?: string | null;
  id_contrato?: number | null;
  read_at?: string | Date | null;
};

export type AlertItem = {
  id: number;
  reason: string;
  status: string;
  read_at: string | Date | null;
  created_at: string | Date;
  score: number | null;
  verdict: string | null;
  id_contrato: number | null;
  codigo: string | null;
  descripcion: string | null;
  entidad_nombre: string | null;
  fec_fin_cotizacion: string | Date | null;
  payload: AlertPayload | null;
};
