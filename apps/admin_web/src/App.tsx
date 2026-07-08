import {
  AlertCircle,
  BarChart3,
  Clock3,
  Database,
  FileJson,
  Gauge,
  ImageIcon,
  ListFilter,
  Loader2,
  RefreshCcw,
  Search,
  ShieldCheck
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, type RecordFilters } from "./api";
import type { Meta, Platform, RecordContext, RecordRaw, RecordSummary, Report } from "./types";

type View = "records" | "reports";
type DetailTab = "images" | "author" | "content" | "metrics" | "evidence" | "json";

const emptyFilters: RecordFilters = {
  platform_key: "",
  city_name: "",
  keyword: "",
  status: "",
  published_from: "",
  published_to: "",
  captured_from: "",
  captured_to: "",
  missing_field: "",
  q: "",
  page: 1,
  page_size: 50,
  sort: "-captured_at"
};

export function App() {
  const [view, setView] = useState<View>("records");
  const [meta, setMeta] = useState<Meta | null>(null);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [filters, setFilters] = useState<RecordFilters>(emptyFilters);
  const [records, setRecords] = useState<RecordSummary[]>([]);
  const [recordsMeta, setRecordsMeta] = useState<Record<string, unknown>>({});
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [context, setContext] = useState<RecordContext | null>(null);
  const [raw, setRaw] = useState<RecordRaw | null>(null);
  const [reports, setReports] = useState<Report[]>([]);
  const [fieldGaps, setFieldGaps] = useState<Record<string, number | null>>({});
  const [detailTab, setDetailTab] = useState<DetailTab>("images");
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<string>("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [metaData, platformData, recordsPayload, reportsData, gapsData] = await Promise.all([
        api.meta(),
        api.platforms(),
        api.records(filters),
        api.reports(),
        api.overviewGaps()
      ]);
      setMeta(metaData);
      setPlatforms(platformData);
      setRecords(recordsPayload.data);
      setRecordsMeta(recordsPayload.meta);
      setReports(reportsData);
      setFieldGaps(gapsData);
      setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      setSelectedId((current) => current ?? recordsPayload.data[0]?.id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取失败");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const seconds = meta?.refresh_seconds ?? 5;
    const handle = window.setInterval(() => {
      void refresh();
    }, Math.max(3, seconds) * 1000);
    return () => window.clearInterval(handle);
  }, [meta?.refresh_seconds, refresh]);

  useEffect(() => {
    if (!selectedId) {
      setContext(null);
      setRaw(null);
      return;
    }
    setDetailLoading(true);
    setRaw(null);
    api
      .context(selectedId)
      .then(setContext)
      .catch((err) => setError(err instanceof Error ? err.message : "详情读取失败"))
      .finally(() => setDetailLoading(false));
  }, [selectedId]);

  useEffect(() => {
    if (detailTab !== "json" || !selectedId || raw) {
      return;
    }
    api.raw(selectedId).then(setRaw).catch((err) => setError(err instanceof Error ? err.message : "JSON 读取失败"));
  }, [detailTab, raw, selectedId]);

  const selectedRecord = useMemo(
    () => records.find((record) => record.id === selectedId) ?? records[0] ?? null,
    [records, selectedId]
  );

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">TP</div>
          <div>
            <div className="brand-title">TripPostCollect</div>
            <div className="brand-subtitle">Local Admin</div>
          </div>
        </div>
        <nav className="nav-block">
          <div className="nav-label">工作区</div>
          <button className={view === "records" ? "nav-item active" : "nav-item"} onClick={() => setView("records")}>
            <BarChart3 size={16} /> 记录工作台
          </button>
          <button className="nav-item" onClick={() => setView("records")}>
            <AlertCircle size={16} /> 数据质量
          </button>
        </nav>
        <nav className="nav-block">
          <div className="nav-label">运行</div>
          <button className={view === "reports" ? "nav-item active" : "nav-item"} onClick={() => setView("reports")}>
            <Clock3 size={16} /> 运行报告
          </button>
        </nav>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <div className="topbar-title">{view === "records" ? "记录工作台" : "运行报告"}</div>
            <div className="topbar-subtitle">{lastRefresh ? `最近刷新 ${lastRefresh}` : "等待数据"}</div>
          </div>
          <div className="topbar-meta">
            <span className="db-path">
              <Database size={14} /> {meta?.db_path ?? "data/trippostcollect.sqlite"}
            </span>
            <span className="status-pill ok">
              <ShieldCheck size={14} /> {meta?.readonly ? "只读模式" : "读写状态未知"}
            </span>
            <button className="icon-button" onClick={() => void refresh()} title="刷新" aria-label="刷新">
              {loading ? <Loader2 className="spin" size={16} /> : <RefreshCcw size={16} />}
            </button>
          </div>
        </header>

        {error ? <div className="error-strip">{error}</div> : null}

        {view === "records" ? (
          <RecordWorkbench
            filters={filters}
            setFilters={setFilters}
            platforms={platforms}
            records={records}
            recordsMeta={recordsMeta}
            selectedRecord={selectedRecord}
            selectedId={selectedId}
            setSelectedId={setSelectedId}
            context={context}
            detailLoading={detailLoading}
            detailTab={detailTab}
            setDetailTab={setDetailTab}
            raw={raw}
            fieldGaps={fieldGaps}
          />
        ) : (
          <ReportsView reports={reports} meta={meta} />
        )}
      </main>
    </div>
  );
}

function RecordWorkbench(props: {
  filters: RecordFilters;
  setFilters: (filters: RecordFilters) => void;
  platforms: Platform[];
  records: RecordSummary[];
  recordsMeta: Record<string, unknown>;
  selectedRecord: RecordSummary | null;
  selectedId: number | null;
  setSelectedId: (id: number) => void;
  context: RecordContext | null;
  detailLoading: boolean;
  detailTab: DetailTab;
  setDetailTab: (tab: DetailTab) => void;
  raw: RecordRaw | null;
  fieldGaps: Record<string, number | null>;
}) {
  const { filters, setFilters, platforms, records, recordsMeta } = props;
  return (
    <section className="content">
      <div className="filter-panel">
        <div className="filter-grid">
          <Field label="平台">
            <select value={filters.platform_key} onChange={(event) => setFilters({ ...filters, platform_key: event.target.value, page: 1 })}>
              <option value="">全部平台</option>
              {platforms.map((platform) => (
                <option key={platform.platform_key} value={platform.platform_key}>
                  {platform.display_name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="城市">
            <input value={filters.city_name} onChange={(event) => setFilters({ ...filters, city_name: event.target.value, page: 1 })} />
          </Field>
          <Field label="关键词">
            <input value={filters.keyword} onChange={(event) => setFilters({ ...filters, keyword: event.target.value, page: 1 })} />
          </Field>
          <Field label="状态">
            <select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value, page: 1 })}>
              <option value="">不限</option>
              <option value="captured">captured</option>
              <option value="partial">partial</option>
              <option value="failed">failed</option>
              <option value="skipped">skipped</option>
            </select>
          </Field>
          <Field label="缺字段">
            <select value={filters.missing_field} onChange={(event) => setFilters({ ...filters, missing_field: event.target.value, page: 1 })}>
              <option value="">不限</option>
              <option value="images">缺图片</option>
              <option value="published_at">缺发布时间</option>
              <option value="author_followers">缺作者粉丝量</option>
            </select>
          </Field>
          <Field label="全文搜索">
            <div className="search-control">
              <Search size={15} />
              <input value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value, page: 1 })} />
            </div>
          </Field>
        </div>
      </div>

      <div className="quality-strip">
        <QualityMetric label="缺图片" value={props.fieldGaps.missing_images} onClick={() => setFilters({ ...filters, missing_field: "images", page: 1 })} />
        <QualityMetric
          label="缺发布时间"
          value={props.fieldGaps.missing_published_at}
          onClick={() => setFilters({ ...filters, missing_field: "published_at", page: 1 })}
        />
        <QualityMetric
          label="缺粉丝量"
          value={props.fieldGaps.missing_author_followers}
          onClick={() => setFilters({ ...filters, missing_field: "author_followers", page: 1 })}
        />
      </div>

      <div className="workbench">
        <section className="panel record-list">
          <div className="panel-header">
            <div>
              <div className="panel-title">记录列表</div>
              <div className="panel-subtitle">{String(recordsMeta.total ?? records.length)} 条记录</div>
            </div>
            <ListFilter size={17} />
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="col-platform">平台</th>
                  <th>记录</th>
                  <th className="col-author">作者</th>
                  <th className="col-date">发布时间</th>
                  <th className="col-small">图片</th>
                  <th className="col-small">互动</th>
                  <th className="col-status">状态</th>
                </tr>
              </thead>
              <tbody>
                {records.map((record) => (
                  <tr key={record.id} className={props.selectedId === record.id ? "selected" : ""} onClick={() => props.setSelectedId(record.id)}>
                    <td className="platform-cell">{record.platform_name ?? record.platform_key}</td>
                    <td>
                      <div className="record-title">{record.title || record.content_text || record.source_url}</div>
                      <div className="record-sub">
                        {record.city_name || "未标城市"} · {record.keyword || "无关键词"} · {record.source_type}
                      </div>
                    </td>
                    <td>{record.author_display_name || "未提取"}</td>
                    <td>{formatDate(record.published_at)}</td>
                    <td>{record.post_images_count}</td>
                    <td>{compactNumber((record.post_likes_count ?? 0) + (record.post_comments_count ?? 0))}</td>
                    <td>
                      <span className={`tag ${statusTone(record.status)}`}>{record.status || "unknown"}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <RecordDetail
          context={props.context}
          selectedRecord={props.selectedRecord}
          loading={props.detailLoading}
          tab={props.detailTab}
          setTab={props.setDetailTab}
          raw={props.raw}
        />
      </div>
    </section>
  );
}

function Field(props: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{props.label}</span>
      {props.children}
    </label>
  );
}

function QualityMetric(props: { label: string; value?: number | null; onClick: () => void }) {
  return (
    <button className="quality-metric" onClick={props.onClick}>
      <AlertCircle size={15} />
      <span>{props.label}</span>
      <strong>{props.value ?? 0}</strong>
    </button>
  );
}

function RecordDetail(props: {
  context: RecordContext | null;
  selectedRecord: RecordSummary | null;
  loading: boolean;
  tab: DetailTab;
  setTab: (tab: DetailTab) => void;
  raw: RecordRaw | null;
}) {
  const context = props.context;
  const record = context?.record ?? props.selectedRecord;
  if (!record) {
    return (
      <aside className="panel detail-panel">
        <div className="empty-state">暂无记录</div>
      </aside>
    );
  }
  return (
    <aside className="panel detail-panel">
      <div className="panel-header detail-header">
        <div>
          <div className="panel-title">{record.title || record.source_url}</div>
          <div className="panel-subtitle">
            {record.platform_name ?? record.platform_key} · {record.status}
          </div>
        </div>
        {props.loading ? <Loader2 className="spin" size={18} /> : null}
      </div>
      <div className="tabs">
        {(["images", "author", "content", "metrics", "evidence", "json"] as DetailTab[]).map((tab) => (
          <button key={tab} className={props.tab === tab ? "tab active" : "tab"} onClick={() => props.setTab(tab)}>
            {tabLabel(tab)}
          </button>
        ))}
      </div>
      <div className="detail-body">
        {props.tab === "images" ? <ImagePanel context={context} /> : null}
        {props.tab === "author" ? <AuthorPanel context={context} /> : null}
        {props.tab === "content" ? <ContentPanel record={record} /> : null}
        {props.tab === "metrics" ? <MetricsPanel context={context} /> : null}
        {props.tab === "evidence" ? <EvidencePanel context={context} /> : null}
        {props.tab === "json" ? <JsonPanel raw={props.raw} /> : null}
      </div>
    </aside>
  );
}

function ImagePanel({ context }: { context: RecordContext | null }) {
  const images = context?.images ?? [];
  if (!context) {
    return <div className="empty-state">加载中</div>;
  }
  if (images.length === 0) {
    return <div className="empty-state">无图片</div>;
  }
  return (
    <div className="image-grid">
      {images.slice(0, 6).map((image) => (
        <figure key={image.id} className="image-tile">
          <img src={`/api/images/${image.id}/preview`} alt="" loading="lazy" onError={(event) => event.currentTarget.classList.add("image-error")} />
          <figcaption>{image.image_role}</figcaption>
        </figure>
      ))}
    </div>
  );
}

function AuthorPanel({ context }: { context: RecordContext | null }) {
  const author = context?.author;
  return (
    <div className="kv-grid">
      <KV label="展示名" value={author?.display_name} />
      <KV label="平台 ID" value={author?.platform_id} />
      <KV label="粉丝量" value={compactNumber(author?.followers_count)} />
      <KV label="关注量" value={compactNumber(author?.following_count)} />
      <KV label="作品数" value={compactNumber(author?.posts_count)} />
      <KV label="认证" value={author?.verified_text || (author?.verified ? "已认证" : "未认证")} />
      <KV wide label="主页" value={author?.profile_url} />
      <KV wide label="简介" value={author?.description} />
    </div>
  );
}

function ContentPanel({ record }: { record: RecordSummary }) {
  return (
    <div className="content-panel">
      <div className="kv-grid">
        <KV label="城市" value={record.city_name} />
        <KV label="关键词" value={record.keyword} />
        <KV label="发布时间" value={record.published_at} />
        <KV label="抓取时间" value={record.captured_at} />
        <KV wide label="来源 URL" value={record.canonical_url || record.source_url} />
      </div>
      <p>{record.content_text || "无正文"}</p>
    </div>
  );
}

function MetricsPanel({ context }: { context: RecordContext | null }) {
  const metrics = context?.metrics ?? {};
  return (
    <div className="metrics-grid">
      <Metric label="点赞" value={metrics.likes} />
      <Metric label="收藏" value={metrics.favorites} />
      <Metric label="评论" value={metrics.comments} />
      <Metric label="分享" value={metrics.shares} />
      <Metric label="转发" value={metrics.reposts} />
      <Metric label="浏览" value={metrics.views} />
    </div>
  );
}

function EvidencePanel({ context }: { context: RecordContext | null }) {
  const capture = context?.capture;
  if (!capture) {
    return <div className="empty-state">无关联证据</div>;
  }
  return (
    <div className="evidence-list">
      <EvidenceRow label="证据 ID" value={`#${capture.id}`} />
      <EvidenceRow label="站点" value={capture.site_key} />
      <EvidenceRow label="就绪" value={capture.content_ready ? "content_ready" : "pending"} />
      <EvidenceRow label="截图" value={capture.screenshot_path ? "可读" : "无"} href={`/api/captures/${capture.id}/artifact?kind=screenshot`} />
      <EvidenceRow label="可见文本" value={capture.visible_text_path ? "可读" : "无"} href={`/api/captures/${capture.id}/artifact?kind=visible_text`} />
      <EvidenceRow label="HTML" value={capture.rendered_html_path ? "可读" : "无"} href={`/api/captures/${capture.id}/artifact?kind=rendered_html`} />
    </div>
  );
}

function JsonPanel({ raw }: { raw: RecordRaw | null }) {
  if (!raw) {
    return <div className="empty-state">加载中</div>;
  }
  return <pre className="json-viewer">{JSON.stringify(raw, null, 2)}</pre>;
}

function KV(props: { label: string; value?: string | number | null; wide?: boolean }) {
  return (
    <div className={props.wide ? "kv wide" : "kv"}>
      <div className="kv-label">{props.label}</div>
      <div className="kv-value">{props.value ?? "未提取"}</div>
    </div>
  );
}

function Metric(props: { label: string; value?: number | null }) {
  return (
    <div className="metric">
      <div className="metric-value">{compactNumber(props.value)}</div>
      <div className="metric-label">{props.label}</div>
    </div>
  );
}

function EvidenceRow(props: { label: string; value: string; href?: string }) {
  return (
    <div className="evidence-row">
      <span>{props.label}</span>
      {props.href ? (
        <a href={props.href} target="_blank" rel="noreferrer">
          {props.value}
        </a>
      ) : (
        <strong>{props.value}</strong>
      )}
    </div>
  );
}

function ReportsView({ reports, meta }: { reports: Report[]; meta: Meta | null }) {
  return (
    <section className="content">
      <div className="reports-grid">
        <div className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">运行报告</div>
              <div className="panel-subtitle">{reports.length} 条最近记录</div>
            </div>
            <Gauge size={18} />
          </div>
          <div className="report-list">
            {reports.map((report) => (
              <div className="report-row" key={report.id}>
                <div>
                  <div className="record-title">{report.run_id}</div>
                  <div className="record-sub">
                    {report.started_at} · {report.status}
                  </div>
                </div>
                <div className="report-counts">
                  <span>{report.jobs_selected} 任务</span>
                  <span>{report.completed_count} 完成</span>
                  <span>{report.failed_count} 失败</span>
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">数据库状态</div>
              <div className="panel-subtitle">只读 HTTP API</div>
            </div>
            <FileJson size={18} />
          </div>
          <div className="kv-grid padded">
            <KV label="记录" value={meta?.table_counts.web_posts ?? 0} />
            <KV label="图片" value={meta?.table_counts.web_post_images ?? 0} />
            <KV label="证据" value={meta?.table_counts.ctf_captures ?? 0} />
            <KV label="任务" value={meta?.table_counts.crawl_jobs ?? 0} />
          </div>
        </div>
      </div>
    </section>
  );
}

function tabLabel(tab: DetailTab): string {
  return {
    images: "图片",
    author: "作者",
    content: "内容",
    metrics: "互动",
    evidence: "证据",
    json: "JSON"
  }[tab];
}

function formatDate(value?: string | null): string {
  if (!value) return "未知";
  return value.slice(0, 10);
}

function compactNumber(value?: number | null): string {
  if (value === null || value === undefined) return "未提取";
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return String(value);
}

function statusTone(status?: string): string {
  if (status === "captured") return "green";
  if (status === "partial" || status === "skipped") return "amber";
  if (status === "failed") return "red";
  return "";
}
