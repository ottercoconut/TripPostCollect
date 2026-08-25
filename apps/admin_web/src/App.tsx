import {
  AlertCircle,
  ArrowLeft,
  BarChart3,
  Clock3,
  Database,
  ExternalLink,
  FileJson,
  Gauge,
  ImageIcon,
  LocateFixed,
  ListFilter,
  Loader2,
  PanelRightOpen,
  RefreshCcw,
  Search,
  ShieldCheck
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, type RecordFilters } from "./api";
import type { KeywordUsage, Meta, Platform, RecordContext, RecordRaw, RecordSummary, Report } from "./types";

type View = "records" | "quality" | "reports";

const emptyFilters: RecordFilters = {
  topic_scope: "relevant",
  platform_key: "",
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
  const navigate = useNavigate();
  const location = useLocation();
  const view = viewFromPath(location.pathname);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [keywords, setKeywords] = useState<KeywordUsage[]>([]);
  const [filters, setFilters] = useState<RecordFilters>(emptyFilters);
  const [records, setRecords] = useState<RecordSummary[]>([]);
  const [recordsMeta, setRecordsMeta] = useState<Record<string, unknown>>({});
  const [reports, setReports] = useState<Report[]>([]);
  const [fieldGaps, setFieldGaps] = useState<Record<string, number | null>>({});
  const [qualityGroups, setQualityGroups] = useState<Record<string, RecordSummary[]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<string>("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [metaData, platformData, keywordData, recordsPayload, reportsData, gapsData, missingImages, missingPublished, missingFollowers] = await Promise.all([
        api.meta(),
        api.platforms(),
        api.keywords(filters.topic_scope),
        api.records(filters),
        api.reports(),
        api.overviewGaps(),
        api.records({ missing_field: "images", page_size: 8, sort: "-captured_at" }),
        api.records({ missing_field: "published_at", page_size: 8, sort: "-captured_at" }),
        api.records({ missing_field: "author_followers", page_size: 8, sort: "-captured_at" })
      ]);
      setMeta(metaData);
      setPlatforms(platformData);
      setKeywords(keywordData);
      setRecords(recordsPayload.data);
      setRecordsMeta(recordsPayload.meta);
      setReports(reportsData);
      setFieldGaps(gapsData as Record<string, number | null>);
      setQualityGroups({
        images: missingImages.data,
        published_at: missingPublished.data,
        author_followers: missingFollowers.data
      });
      setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
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

  const locateIssue = (missingField: string, recordId?: number) => {
    setFilters({ ...emptyFilters, missing_field: missingField, page: 1, sort: "-captured_at" });
    navigate(recordId ? `/records/${recordId}` : "/records");
  };

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
          <button className={view === "records" ? "nav-item active" : "nav-item"} onClick={() => navigate("/records")}>
            <BarChart3 size={16} /> 记录工作台
          </button>
          <button className={view === "quality" ? "nav-item active" : "nav-item"} onClick={() => navigate("/quality")}>
            <AlertCircle size={16} /> 数据质量
          </button>
        </nav>
        <nav className="nav-block">
          <div className="nav-label">运行</div>
          <button className={view === "reports" ? "nav-item active" : "nav-item"} onClick={() => navigate("/reports")}>
            <Clock3 size={16} /> 运行报告
          </button>
        </nav>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <div className="topbar-title">{titleFromPath(location.pathname)}</div>
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

        <Routes>
          <Route path="/" element={<Navigate to="/records" replace />} />
          <Route
            path="/records"
            element={
              <RecordWorkbench
                filters={filters}
                setFilters={setFilters}
                platforms={platforms}
                keywords={keywords}
                records={records}
                recordsMeta={recordsMeta}
                fieldGaps={fieldGaps}
                locateIssue={locateIssue}
                openRecord={(recordId) => navigate(`/records/${recordId}`)}
              />
            }
          />
          <Route path="/records/:recordId" element={<RecordDetailPage />} />
          <Route path="/quality" element={<QualityView fieldGaps={fieldGaps} qualityGroups={qualityGroups} locateIssue={locateIssue} />} />
          <Route path="/reports" element={<ReportsView reports={reports} meta={meta} />} />
          <Route path="*" element={<Navigate to="/records" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function RecordWorkbench(props: {
  filters: RecordFilters;
  setFilters: (filters: RecordFilters) => void;
  platforms: Platform[];
  keywords: KeywordUsage[];
  records: RecordSummary[];
  recordsMeta: Record<string, unknown>;
  fieldGaps: Record<string, number | null>;
  locateIssue: (missingField: string, recordId?: number) => void;
  openRecord: (recordId: number) => void;
}) {
  const { filters, setFilters, platforms, keywords, records, recordsMeta } = props;
  const total = Number(recordsMeta.total ?? records.length);
  const page = Number(filters.page ?? recordsMeta.page ?? 1);
  const pageSize = Number(filters.page_size ?? recordsMeta.page_size ?? 50);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const setPage = (nextPage: number) => {
    setFilters({ ...filters, page: Math.min(Math.max(1, nextPage), totalPages) });
  };
  const setPageSize = (nextPageSize: number) => {
    setFilters({ ...filters, page_size: nextPageSize, page: 1 });
  };
  return (
    <section className="content">
      <div className="filter-panel">
        <div className="filter-grid">
          <Field label="主题范围">
            <select
              value={filters.topic_scope}
              onChange={(event) =>
                setFilters({
                  ...filters,
                  topic_scope: event.target.value as RecordFilters["topic_scope"],
                  keyword: "",
                  page: 1
                })
              }
            >
              <option value="relevant">相关</option>
              <option value="irrelevant">不相关</option>
              <option value="all">全部</option>
            </select>
          </Field>
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
          <Field label="关键词">
            <input
              list="record-keyword-options"
              value={filters.keyword}
              onChange={(event) => setFilters({ ...filters, keyword: event.target.value, page: 1 })}
              placeholder="输入或选择关键词"
            />
            <datalist id="record-keyword-options">
              {keywords.map((item) => (
                <option key={item.keyword} value={item.keyword} />
              ))}
            </datalist>
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
        <div className="keyword-browser">
          <div className="keyword-browser-label">
            <span>全部关键词</span>
            <strong>{keywords.length}</strong>
          </div>
          <div className="keyword-browser-list">
            {keywords.length > 0 ? (
              keywords.map((item) => {
                const active = filters.keyword === item.keyword;
                return (
                  <button
                    type="button"
                    key={item.keyword}
                    className={active ? "keyword-filter active" : "keyword-filter"}
                    aria-pressed={active}
                    onClick={() => setFilters({ ...filters, keyword: active ? "" : item.keyword, page: 1 })}
                  >
                    <span>{item.keyword}</span>
                    <small>{item.record_count}</small>
                  </button>
                );
              })
            ) : (
              <span className="keyword-browser-empty">暂无关键词</span>
            )}
          </div>
        </div>
      </div>

      <div className="quality-strip">
        <QualityMetric label="缺图片" value={props.fieldGaps.missing_images} onClick={() => props.locateIssue("images")} />
        <QualityMetric
          label="缺发布时间"
          value={props.fieldGaps.missing_published_at}
          onClick={() => props.locateIssue("published_at")}
        />
        <QualityMetric
          label="缺粉丝量"
          value={props.fieldGaps.missing_author_followers}
          onClick={() => props.locateIssue("author_followers")}
        />
      </div>

      <div className="workbench">
        <section className="panel record-list">
          <div className="panel-header">
            <div>
              <div className="panel-title">记录列表</div>
              <div className="panel-subtitle">
                {total} 条记录 · 第 {page} / {totalPages} 页
              </div>
            </div>
            <ListFilter size={17} />
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="col-platform">平台</th>
                  <th>记录</th>
                  <th className="col-keyword">关键词</th>
                  <th className="col-author">作者</th>
                  <th className="col-followers">粉丝量</th>
                  <th className="col-date">发布时间</th>
                  <th className="col-small">图片</th>
                  <th className="col-small">互动</th>
                  <th className="col-status">状态</th>
                  <th className="col-open">详情</th>
                </tr>
              </thead>
              <tbody>
                {records.map((record) => (
                  <tr
                    key={record.id}
                    tabIndex={0}
                    onClick={() => props.openRecord(record.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        props.openRecord(record.id);
                      }
                    }}
                  >
                    <td className="platform-cell">{record.platform_name ?? record.platform_key}</td>
                    <td>
                      <div className="record-title">{record.title || record.content_text || record.source_url}</div>
                      <div className="record-sub">
                        {record.source_type} · <TopicBadge relevant={record.topic_relevant} />
                      </div>
                    </td>
                    <td>
                      <span className="keyword-tag" title={record.keyword || "无关键词"}>
                        {record.keyword || "无关键词"}
                      </span>
                    </td>
                    <td>{record.author_display_name || "未提取"}</td>
                    <td>{compactNumber(record.author_followers_count)}</td>
                    <td>{formatDate(record.published_at)}</td>
                    <td>{record.post_images_count}</td>
                    <td>{compactNumber((record.post_likes_count ?? 0) + (record.post_comments_count ?? 0))}</td>
                    <td>
                      <span className={`tag ${statusTone(record.status)}`}>{record.status || "unknown"}</span>
                    </td>
                    <td>
                      <button
                        className="row-action"
                        title="打开详情页"
                        aria-label="打开详情页"
                        onClick={(event) => {
                          event.stopPropagation();
                          props.openRecord(record.id);
                        }}
                      >
                        <PanelRightOpen size={15} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pagination-bar">
            <div className="page-size">
              <span>每页</span>
              <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}>
                <option value={25}>25</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
                <option value={200}>200</option>
              </select>
            </div>
            <div className="page-actions">
              <button onClick={() => setPage(1)} disabled={page <= 1}>
                首页
              </button>
              <button onClick={() => setPage(page - 1)} disabled={page <= 1}>
                上一页
              </button>
              <span>
                {page} / {totalPages}
              </span>
              <button onClick={() => setPage(page + 1)} disabled={page >= totalPages}>
                下一页
              </button>
              <button onClick={() => setPage(totalPages)} disabled={page >= totalPages}>
                末页
              </button>
            </div>
          </div>
        </section>
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

function RecordDetailPage() {
  const params = useParams();
  const navigate = useNavigate();
  const recordId = Number(params.recordId);
  const [context, setContext] = useState<RecordContext | null>(null);
  const [raw, setRaw] = useState<RecordRaw | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!Number.isInteger(recordId) || recordId <= 0) {
      setError("记录 ID 无效");
      setLoading(false);
      return;
    }
    let active = true;
    setLoading(true);
    setError(null);
    setContext(null);
    setRaw(null);
    Promise.all([api.context(recordId), api.raw(recordId)])
      .then(([contextData, rawData]) => {
        if (!active) return;
        setContext(contextData);
        setRaw(rawData);
      })
      .catch((err) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : "详情读取失败");
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [recordId]);

  const record = context?.record;
  return (
    <section className="content record-page">
      <div className="record-page-toolbar">
        <button className="text-button" onClick={() => navigate("/records")}>
          <ArrowLeft size={16} /> 返回列表
        </button>
        {record?.source_url ? (
          <a className="text-button" href={record.canonical_url || record.source_url} target="_blank" rel="noreferrer">
            <ExternalLink size={16} /> 来源页面
          </a>
        ) : null}
      </div>

      {error ? <div className="error-strip inline">{error}</div> : null}
      {loading ? <div className="panel empty-state">详情加载中</div> : null}

      {record ? (
        <>
          <section className="panel record-hero">
            <div>
              <div className="record-hero-title">{record.title || record.source_url}</div>
              <div className="record-hero-meta">
                <span>{record.platform_name ?? record.platform_key}</span>
                <span>{formatDate(record.published_at)}</span>
                <span>{record.status || "unknown"}</span>
                <TopicBadge relevant={record.topic_relevant} />
              </div>
            </div>
            <div className="record-hero-stats">
              <Metric label="图片" value={context.images.length} />
              <Metric label="粉丝量" value={context.author.followers_count} />
              <Metric label="互动" value={(context.metrics.likes ?? 0) + (context.metrics.comments ?? 0)} />
            </div>
          </section>

          <div className="record-detail-layout">
            <div className="record-detail-main">
              <section className="panel">
                <div className="panel-header">
                  <div>
                    <div className="panel-title">全部图片</div>
                    <div className="panel-subtitle">{context.images.length} 张记录图片</div>
                  </div>
                  <ImageIcon size={18} />
                </div>
                <div className="detail-body spacious">
                  <ImagePanel context={context} />
                </div>
              </section>

              <section className="panel">
                <div className="panel-header">
                  <div>
                    <div className="panel-title">正文与来源</div>
                    <div className="panel-subtitle">{record.keyword || "无关键词"}</div>
                  </div>
                </div>
                <div className="detail-body spacious">
                  <ContentPanel record={record} />
                </div>
              </section>
            </div>

            <aside className="record-detail-side">
              <section className="panel">
                <div className="panel-header">
                  <div className="panel-title">作者</div>
                </div>
                <div className="detail-body">
                  <AuthorPanel context={context} />
                </div>
              </section>

              <section className="panel">
                <div className="panel-header">
                  <div className="panel-title">互动</div>
                </div>
                <div className="detail-body">
                  <MetricsPanel context={context} />
                </div>
              </section>

              <section className="panel">
                <div className="panel-header">
                  <div className="panel-title">证据</div>
                </div>
                <div className="detail-body">
                  <EvidencePanel context={context} />
                </div>
              </section>

              <section className="panel">
                <div className="panel-header">
                  <div className="panel-title">原始 JSON</div>
                </div>
                <div className="detail-body">
                  <JsonPanel raw={raw} />
                </div>
              </section>
            </aside>
          </div>
        </>
      ) : null}
    </section>
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
      {images.map((image) => (
        <ImageTile key={image.id} image={image} />
      ))}
    </div>
  );
}

function ImageTile({ image }: { image: RecordContext["images"][number] }) {
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const source = image.local_path ? "本地" : "远程 URL";
  const failedText = image.local_path ? "本地缺失或不可读" : "远程图片拉取失败";
  return (
    <figure className={`image-tile ${state}`}>
      <img
        src={`/api/images/${image.id}/preview`}
        alt=""
        loading="lazy"
        onLoad={() => setState("ready")}
        onError={(event) => {
          setState("failed");
          event.currentTarget.classList.add("image-error");
        }}
      />
      <figcaption>{state === "failed" ? failedText : `${image.image_role} · ${source}`}</figcaption>
      {state === "loading" ? (
        <div className="image-state">
          <Loader2 className="spin" size={16} />
          <span>加载中</span>
        </div>
      ) : null}
      {state === "failed" ? (
        <div className="image-state failed">
          <ImageIcon size={16} />
          <span>{failedText}</span>
        </div>
      ) : null}
    </figure>
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
        <KV label="关键词" value={record.keyword} />
        <KV label="主题相关性" value={record.topic_relevant ? "相关" : "不相关"} />
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

function TopicBadge({ relevant }: { relevant: boolean }) {
  return (
    <span className={`tag ${relevant ? "green" : "amber"}`}>
      {relevant ? "相关" : "不相关"}
    </span>
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

function QualityView(props: {
  fieldGaps: Record<string, number | null>;
  qualityGroups: Record<string, RecordSummary[]>;
  locateIssue: (missingField: string, recordId?: number) => void;
}) {
  const groups = [
    { key: "images", title: "缺图片", count: props.fieldGaps.missing_images, records: props.qualityGroups.images ?? [] },
    {
      key: "published_at",
      title: "缺发布时间",
      count: props.fieldGaps.missing_published_at,
      records: props.qualityGroups.published_at ?? []
    },
    {
      key: "author_followers",
      title: "缺作者粉丝量",
      count: props.fieldGaps.missing_author_followers,
      records: props.qualityGroups.author_followers ?? []
    }
  ];
  return (
    <section className="content">
      <div className="notice-panel">数据修正走受控终端脚本，前端仅定位问题记录。</div>
      <div className="quality-grid">
        {groups.map((group) => (
          <section className="panel" key={group.key}>
            <div className="panel-header">
              <div>
                <div className="panel-title">{group.title}</div>
                <div className="panel-subtitle">{group.count ?? 0} 条记录</div>
              </div>
              <button className="icon-button" title="定位" aria-label="定位" onClick={() => props.locateIssue(group.key)}>
                <LocateFixed size={16} />
              </button>
            </div>
            <div className="issue-list">
              {group.records.map((record) => (
                <button className="issue-row" key={record.id} onClick={() => props.locateIssue(group.key, record.id)}>
                  <span>
                    <strong>{record.title || record.content_text || record.source_url}</strong>
                    <small>
                      {record.platform_name ?? record.platform_key} · {formatDate(record.captured_at)}
                    </small>
                  </span>
                  <LocateFixed size={15} />
                </button>
              ))}
              {group.records.length === 0 ? <div className="empty-state compact">暂无问题记录</div> : null}
            </div>
          </section>
        ))}
      </div>
    </section>
  );
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

function viewFromPath(pathname: string): View {
  if (pathname.startsWith("/quality")) return "quality";
  if (pathname.startsWith("/reports")) return "reports";
  return "records";
}

function titleFromPath(pathname: string): string {
  if (pathname.startsWith("/records/")) return "记录详情";
  if (pathname.startsWith("/quality")) return "数据质量";
  if (pathname.startsWith("/reports")) return "运行报告";
  return "记录工作台";
}
