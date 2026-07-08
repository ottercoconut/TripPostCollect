import type {
  ApiEnvelope,
  Capture,
  Meta,
  Platform,
  RecordContext,
  RecordRaw,
  RecordSummary,
  Report
} from "./types";

export type RecordFilters = {
  platform_key?: string;
  city_name?: string;
  keyword?: string;
  status?: string;
  published_from?: string;
  published_to?: string;
  captured_from?: string;
  captured_to?: string;
  missing_field?: string;
  q?: string;
  page?: number;
  page_size?: number;
  sort?: string;
};

async function request<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  const payload = (await response.json()) as ApiEnvelope<T>;
  if (payload.errors.length > 0) {
    throw new Error(payload.errors.map((item) => item.message).join("; "));
  }
  return payload.data;
}

async function requestWithMeta<T>(path: string): Promise<ApiEnvelope<T>> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return (await response.json()) as ApiEnvelope<T>;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  meta: () => request<Meta>("/api/meta"),
  platforms: () => request<Platform[]>("/api/platforms"),
  records: (filters: RecordFilters) =>
    requestWithMeta<RecordSummary[]>(
      `/api/records${query({
        platform_key: filters.platform_key,
        city_name: filters.city_name,
        keyword: filters.keyword,
        status: filters.status,
        published_from: filters.published_from,
        published_to: filters.published_to,
        captured_from: filters.captured_from,
        captured_to: filters.captured_to,
        missing_field: filters.missing_field,
        q: filters.q,
        page: filters.page ?? 1,
        page_size: filters.page_size ?? 50,
        sort: filters.sort ?? "-captured_at"
      })}`
    ),
  context: (id: number) => request<RecordContext>(`/api/records/${id}/context`),
  raw: (id: number) => request<RecordRaw>(`/api/records/${id}/raw`),
  reports: () => request<Report[]>("/api/scheduler/reports?limit=8"),
  overviewGaps: () =>
    request<Record<string, number | null>>("/api/overview/field-gaps")
};
