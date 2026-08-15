export type ApiEnvelope<T> = {
  data: T;
  meta: Record<string, unknown>;
  errors: Array<{ code: string; message: string; field?: string }>;
};

export type Platform = {
  platform_key: string;
  display_name: string;
  status: string;
};

export type Meta = {
  db_path: string;
  readonly: boolean;
  commands_enabled: boolean;
  refresh_seconds: number;
  table_counts: Record<string, number | null>;
};

export type RecordSummary = {
  id: number;
  platform_key: string;
  platform_name?: string;
  platform_post_id?: string;
  source_type?: string;
  source_url?: string;
  canonical_url?: string;
  title?: string;
  author_display_name?: string;
  author_followers_count?: number | null;
  published_at?: string;
  captured_at?: string;
  keyword?: string;
  content_text?: string;
  post_images_count: number;
  post_likes_count?: number | null;
  post_comments_count?: number | null;
  status?: string;
};

export type RecordImage = {
  id: number;
  web_post_id: number;
  image_index: number;
  image_url: string;
  image_role: "content";
  local_path?: string | null;
  mime_type?: string | null;
};

export type Capture = {
  id: number;
  capture_kind: string;
  site_key: string;
  target_url: string;
  final_url?: string | null;
  title?: string | null;
  ok: number;
  content_ready: number;
  captured_at: string;
  screenshot_path?: string | null;
  visible_text_path?: string | null;
  rendered_html_path?: string | null;
};

export type CaptureImage = {
  id: number;
  ctf_capture_id: number;
  image_index: number;
  image_url: string;
  saved_path?: string | null;
  ok: number;
};

export type RecordContext = {
  record: RecordSummary & {
    source_capture_id?: number | null;
    artifact_dir?: string | null;
    author_description?: string | null;
  };
  author: {
    display_name?: string | null;
    platform_id?: string | null;
    profile_url?: string | null;
    description?: string | null;
    followers_count?: number | null;
    following_count?: number | null;
    posts_count?: number | null;
    platform_level?: string | null;
    verified?: boolean | null;
    verified_text?: string | null;
  };
  metrics: {
    likes?: number | null;
    favorites?: number | null;
    comments?: number | null;
    shares?: number | null;
    reposts?: number | null;
    views?: number | null;
  };
  images: RecordImage[];
  capture?: Capture | null;
  capture_images?: CaptureImage[];
  artifacts?: Record<string, string | null>;
  raw_summary: {
    has_capture_raw_meta: boolean;
    record_json_fields: string[];
  };
};

export type RecordRaw = {
  record: Record<string, unknown>;
  capture: Record<string, unknown> | null;
};

export type Report = {
  id: number;
  run_id: string;
  started_at: string;
  finished_at?: string | null;
  status: string;
  jobs_selected: number;
  completed_count: number;
  failed_count: number;
  blocked_count: number;
  report_path?: string | null;
};
