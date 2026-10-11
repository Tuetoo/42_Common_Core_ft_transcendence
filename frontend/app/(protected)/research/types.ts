// Response shapes of the research endpoints
// (services/core-service/domains/research/schema.py).

export type SentimentFields = {
  sentiment_label: "positive" | "neutral" | "negative" | null;
  sentiment_score: number | null;
  sentiment_analyzed_at: string | null;
};

export type Note = SentimentFields & {
  id: string;
  title: string;
  content: string | null;
  tags: string[];
  author: { id: string; username: string };
  organization_id: string | null;
  created_at: string;
  updated_at: string;
  is_hidden: boolean;
  can_edit: boolean;
};

export type Comment = SentimentFields & {
  id: string;
  author: { id: string; username: string };
  content: string;
  created_at: string;
  updated_at: string | null;
  is_hidden: boolean;
};

export type ResearchDocument = {
  id: string;
  owner_id: string;
  organization_id: string | null;
  filename: string;
  document_type: string;
  category: string;
  ticker: string | null;
  status: string;
  uploaded_at: string;
  content_date: string | null;
  title: string | null;
  is_system: boolean;
};

export type Organization = { id: string; name: string };

export type Paginated<T> = { items: T[]; total: number; page: number; page_size: number };
