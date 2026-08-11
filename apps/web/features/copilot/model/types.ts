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
  artifacts: CopilotArtifact[];
};

export type CopilotArtifact = {
  id: string;
  name: string;
  mime: string;
  sizeBytes: number;
  downloadUrl: string;
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
