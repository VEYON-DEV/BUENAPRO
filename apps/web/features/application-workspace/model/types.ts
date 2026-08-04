export type ApplicationItem = {
  id: string;
  selected: boolean;
  description: string;
  unit: string;
  quantity: number;
  currency: string;
  unitPrice: number | null;
  total: number | null;
};
export type ApplicationRequirement = {
  id: string;
  description: string;
  requested: string;
  offered: string;
  completed?: boolean;
};
export type ApplicationDocument = {
  id: string;
  localDocumentId?: number;
  name: string;
  mime?: string;
  category?: string;
  downloadUrl?: string;
  status?: string;
  reviewed: boolean;
};
export type ApplicationAttachment = {
  id: string;
  name: string;
  mime?: string;
  sizeBytes?: number;
  createdAt?: string;
  downloadUrl: string;
};
export type ApplicationData = {
  matchId: string;
  contractId?: number;
  code: string;
  title: string;
  entity: string;
  deadline?: string;
  status: string;
  responsibleId?: string;
  validity?: string;
  contactEmail?: string;
  contactPhone?: string;
  currency?: string;
  total?: number;
  items: ApplicationItem[];
  requirements: ApplicationRequirement[];
  documents: ApplicationDocument[];
  attachments: ApplicationAttachment[];
  events?: Array<{
    id?: string;
    label?: string;
    description?: string;
    createdAt?: string;
  }>;
};
