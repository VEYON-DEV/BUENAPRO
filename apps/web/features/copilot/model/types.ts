export type CopilotCitation = {
  label: string;
  source?: string;
  excerpt?: string;
};

export type CopilotMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  citations: CopilotCitation[];
  createdAt?: string;
  changeSet?: CopilotChangeSet | null;
};

export type CopilotChangeSet = {
  id: string;
  status: "pending" | "applied" | "rejected";
  changes: Record<string, unknown>;
  summary?: string;
};

export type CopilotSession = {
  id: string;
  title?: string;
  messages: CopilotMessage[];
};
