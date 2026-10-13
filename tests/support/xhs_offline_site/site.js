/* 离线回放站点自身的前端（不是项目注入的脚本）。按 V1、V16 与 R2 定值建模：
   首页输入 → 回车经 history.pushState 进入 /search_result_ai（关键词两次编码）并发不带 filters 的第 1 页；
   结果页有两种布局（<html data-layout>）：默认 AI 对话布局（Mac 人工侦察 2026-10-13）根节点为
   .ai-feeds-page.with-ai-chat，结果列表占左侧，右侧对话栏固定且可独立滚动，筛选入口在 .ai-chat-inner 内，
   面板挂在入口内部、打开时入口加 .active、收起时从 DOM 移除，每个标签前有一份重叠的 aria-hidden 副本；
   备选 V16 布局入口在 .search-layout 内，其前有一个零尺寸的同类副本，面板按 hidden 切换。两种布局的入口
   文字都在直属 span 里，且都在第 1 页结果渲染约 1.5 秒后才插入（Mac 首轮实测入口为空）；
   筛选面板按文字点选“图文”发 5 项 filters 的第 1 页（新 search_id），入口改为“已筛选”，面板不自动收起；
   虚拟列表只保留视口附近的行（不超过 30 个节点），接近底部时请求下一页，视口未填满时自行补页；
   点封面打开浮层，首图先于 feed 加载，feed 到达后预载下一张，翻图再预载；作者名 target=_blank 打开作者页；
   feed 或搜索返回 461/471 时显示验证层。同一文档内图片元素按 URL 复用，重开不再请求（模拟内存缓存）。
   全部请求由测试的 context.route 回应。 */
(() => {
  'use strict';

  const SEARCH_URL = 'https://so.xiaohongshu.com/api/sns/web/v2/search/notes';
  const FEED_URL = 'https://edith.xiaohongshu.com/api/sns/web/v1/feed';
  const CAPTCHA_URL = 'https://www.xiaohongshu.com/website-login/captcha';
  const PROFILE_URL = 'https://www.xiaohongshu.com/user/profile/';
  const VERIFIED_MESSAGE = 'tpc-offline-verified';
  // 与 site.css 一致：列表自文档 120px 起，每行 360px、4 列；尾部 200px。
  const LIST_TOP = 120;
  const ROW_HEIGHT = 360;
  const COLUMNS = 4;
  const AI_CHAT = document.documentElement.dataset.layout !== 'v16';
  // AI 对话布局下结果列表只占左侧约 2/3，列宽收窄以容纳 4 列（与 site.css 一致）。
  const COLUMN_WIDTH = AI_CHAT ? 200 : 300;
  const PREFETCH_PX = 400;
  // 视口上方保留 2 行、当前行之后 4 行：至多 7 行 28 个节点。
  const ROWS_BEFORE = 2;
  const ROWS_AFTER = 4;
  const ENTRY_DELAY_MS = 1500;
  // 各组 .tags 结构相同；“图文”既不是首组也不是组内首项，程序须按文字定位。
  const FILTER_GROUPS = [
    { type: 'sort_type', title: '排序依据', options: [['综合', 'general'], ['最新', 'time_descending'],
      ['最多点赞', 'popularity_descending'], ['最多评论', 'comment_descending'], ['最多收藏', 'collect_descending']] },
    { type: 'filter_note_type', title: '笔记类型', options: [['不限', '不限'], ['视频', '视频笔记'], ['图文', '普通笔记']] },
    { type: 'filter_note_time', title: '发布时间', options: [['不限', '不限'], ['一天内', '一天内'], ['一周内', '一周内'],
      ['半年内', '半年内']] },
    { type: 'filter_note_range', title: '搜索范围', options: [['不限', '不限'], ['已看过', '已看过'], ['未看过', '未看过'],
      ['已关注', '已关注']] },
    { type: 'filter_pos_distance', title: '位置距离', options: [['不限', '不限'], ['同城', '同城'], ['附近', '附近']] },
  ];

  const audit = {
    maxNoteNodes: 0,
    recycledNodes: 0,
    selfLoads: 0,
    scrollLoads: 0,
    clickedOptions: [],
    sessions: [],
    captchaShown: 0,
    overlaysOpened: 0,
    hiddenTagClicks: 0,
    chatScrolls: 0,
  };
  window.__tpcOffline = audit;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const defaultSelection = () => {
    const selection = {};
    FILTER_GROUPS.forEach((group) => { selection[group.type] = group.options[0][0]; });
    return selection;
  };

  const imageCache = new Map();
  const cachedImage = (url) => {
    let image = imageCache.get(url);
    if (!image) {
      image = new Image();
      image.src = url;
      imageCache.set(url, image);
    }
    return image;
  };

  const state = {
    keyword: '',
    token: 0,
    searchId: '',
    filters: null,
    items: [],
    page: 0,
    hasMore: false,
    loading: false,
    selection: defaultSelection(),
    filterApplied: false,
    overlay: null,
    overlayToken: 0,
    resultsUrl: '',
  };
  let ui = null;

  // 搜索结果页 ---------------------------------------------------------------

  const cardNode = (item, index) => {
    const card = item.note_card || {};
    const section = el('section', 'note-item');
    section.setAttribute('data-note-id', item.id);
    section.setAttribute('data-index', String(index));
    section.style.top = `${Math.floor(index / COLUMNS) * ROW_HEIGHT}px`;
    section.style.left = `${40 + (index % COLUMNS) * COLUMN_WIDTH}px`;
    const cover = el('a', 'cover');
    cover.href = `/search_result/${item.id}?xsec_token=${encodeURIComponent(item.xsec_token || '')}&xsec_source=`;
    const coverUrl = (card.cover || {}).url_pre;
    if (coverUrl) cover.appendChild(cachedImage(coverUrl));
    cover.addEventListener('click', (event) => {
      event.preventDefault();
      openOverlay(item);
    });
    section.append(cover, el('div', 'footer', card.display_title || ''));
    return section;
  };

  const renderList = () => {
    if (!ui) return;
    const container = ui.container;
    container.style.height = `${Math.ceil(state.items.length / COLUMNS) * ROW_HEIGHT}px`;
    const currentRow = Math.max(0, Math.floor((window.scrollY - LIST_TOP) / ROW_HEIGHT));
    const firstIndex = Math.max(0, currentRow - ROWS_BEFORE) * COLUMNS;
    const endIndex = Math.min(state.items.length, (currentRow + ROWS_AFTER + 1) * COLUMNS);
    const wanted = new Map();
    for (let index = firstIndex; index < endIndex; index += 1) wanted.set(index, state.items[index]);
    Array.from(container.querySelectorAll('section.note-item')).forEach((node) => {
      const index = Number(node.getAttribute('data-index'));
      const item = wanted.get(index);
      if (item && node.getAttribute('data-note-id') === item.id) {
        wanted.delete(index);
      } else {
        node.remove();
        audit.recycledNodes += 1;
      }
    });
    wanted.forEach((item, index) => container.appendChild(cardNode(item, index)));
    audit.maxNoteNodes = Math.max(audit.maxNoteNodes, container.querySelectorAll('section.note-item').length);
  };

  const nearBottom = () => {
    const root = document.scrollingElement || document.documentElement;
    return window.scrollY + window.innerHeight >= root.scrollHeight - PREFETCH_PX;
  };

  const maybeLoadMore = (reason) => {
    if (!ui || state.loading || !state.hasMore || state.page < 1 || !nearBottom()) return;
    if (reason === 'self') audit.selfLoads += 1;
    else audit.scrollLoads += 1;
    loadPage(state.page + 1);
  };

  const loadPage = async (number) => {
    const token = state.token;
    state.loading = true;
    const body = {
      keyword: state.keyword,
      page: number,
      page_size: 20,
      search_id: state.searchId,
      sort: 'general',
      note_type: 0,
      ext_flags: [],
      geo: '',
      image_formats: ['jpg', 'webp', 'avif'],
    };
    if (state.filters) body.filters = state.filters;
    let status = 0;
    let payload = null;
    try {
      const response = await fetch(SEARCH_URL, { method: 'POST', credentials: 'include', body: JSON.stringify(body) });
      status = response.status;
      payload = await response.json().catch(() => null);
    } catch (error) {
      payload = null;
    }
    if (token !== state.token) return; // 期间前端已开始新的搜索会话
    state.loading = false;
    if (status === 461 || status === 471) {
      showCaptcha();
      return;
    }
    const data = payload && payload.data;
    if (status !== 200 || !data) return;
    (data.items || []).forEach((item) => {
      if (item && item.model_type === 'note' && item.id) state.items.push(item);
    });
    state.page = number;
    state.hasMore = data.has_more === true;
    renderList();
    if (ui && !ui.entryShown) {
      ui.entryShown = true;
      const { entryHost, entry } = ui;
      setTimeout(() => entryHost.appendChild(entry), ENTRY_DELAY_MS);
    }
    // 视口未填满时前端自行补页（没有滚动）。
    setTimeout(() => maybeLoadMore('self'), 0);
  };

  const newSession = (filters) => {
    state.token += 1;
    state.searchId = `2f${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
    state.filters = filters;
    state.items = [];
    state.page = 0;
    state.hasMore = false;
    state.loading = false;
    audit.sessions.push({ search_id: state.searchId, filtered: Boolean(filters) });
    window.scrollTo(0, 0);
    if (ui) ui.container.replaceChildren();
    renderList();
    loadPage(1);
  };

  const buildFilters = () => FILTER_GROUPS.map((group) => {
    const option = group.options.find(([label]) => label === state.selection[group.type]) || group.options[0];
    return { type: group.type, tags: [option[1]] };
  });

  const refreshFilterUi = () => {
    if (!ui) return;
    ui.entryLabel.textContent = state.filterApplied ? '已筛选' : '筛选';
    ui.panel.querySelectorAll('.tags').forEach((tag) => {
      tag.classList.toggle('active', state.selection[tag.getAttribute('data-group')] === tag.textContent);
    });
  };

  const chooseFilter = (type, label) => {
    audit.clickedOptions.push([type, label]);
    state.selection[type] = label;
    state.filterApplied = FILTER_GROUPS.some((group) => state.selection[group.type] !== group.options[0][0]);
    refreshFilterUi();
    newSession(buildFilters()); // 面板保持打开，再点入口才收起
  };

  const panelOpen = () => (AI_CHAT ? ui.panelHost.isConnected : !ui.panel.hidden);

  // AI 对话布局：面板挂在入口内部，收起时从 DOM 移除；V16：面板在 .search-layout 内按 hidden 切换。
  const togglePanel = () => {
    if (!AI_CHAT) {
      ui.panel.hidden = !ui.panel.hidden;
      return;
    }
    if (panelOpen()) ui.panelHost.remove();
    else ui.entry.appendChild(ui.panelHost);
    ui.entry.classList.toggle('active', panelOpen());
  };

  const filterEntry = (className) => {
    const entry = el('div', className);
    entry.appendChild(el('span', '', '筛选'));
    return entry;
  };

  const tagNode = (group, label, hidden) => {
    const tag = el('div', 'tags', label);
    tag.setAttribute('data-group', group.type);
    if (hidden) {
      tag.setAttribute('aria-hidden', 'true');
      tag.addEventListener('click', () => { audit.hiddenTagClicks += 1; });
    } else {
      tag.addEventListener('click', () => chooseFilter(group.type, label));
    }
    return tag;
  };

  const buildPanel = () => {
    const panel = el('div', 'filters-wrapper');
    FILTER_GROUPS.forEach((group) => {
      const row = el('div', 'filters');
      const tags = el('div', 'tag-container');
      group.options.forEach(([label]) => {
        if (!AI_CHAT) {
          tags.appendChild(tagNode(group, label, false));
          return;
        }
        // 两份坐标重叠：aria-hidden 副本在前，真标签在后、叠在上面。
        const slot = el('span', 'tag-slot');
        slot.append(tagNode(group, label, true), tagNode(group, label, false));
        tags.appendChild(slot);
      });
      row.append(el('span', 'title', group.title), tags);
      panel.appendChild(row);
    });
    return panel;
  };

  const showResults = () => {
    const page = el('div', AI_CHAT ? 'ai-feeds-page with-ai-chat' : 'ai-feeds-page');
    const layout = el('div', 'search-layout');
    const container = el('div', 'feeds-container');
    const end = el('div', 'feeds-end', '- THE END -');
    const panel = buildPanel();
    let entry;
    let entryHost;
    let panelHost = panel;
    if (AI_CHAT) {
      const main = el('div', 'search-layout__main');
      main.append(container, end);
      layout.append(el('div', 'search-layout__top'), el('div', 'search-layout__middle'), main);
      entry = filterEntry('filter ai-chat-filter');
      panelHost = el('div', 'filter-panel');
      panelHost.appendChild(panel);
      entryHost = el('div', 'ai-chat-inner');
      const messages = el('div', 'ai-chat-messages');
      for (let index = 0; index < 12; index += 1) messages.appendChild(el('div', 'ai-chat-message', '点点 ai'));
      messages.addEventListener('scroll', () => { audit.chatScrolls += 1; }, { passive: true });
      const chat = el('div', 'ai-chat-section');
      chat.append(entryHost, messages);
      page.append(layout, chat);
    } else {
      entryHost = el('div', 'filter-bar');
      entryHost.appendChild(filterEntry('filter decoy'));
      entry = filterEntry('filter');
      panel.hidden = true;
      layout.append(entryHost, panel, container, end);
      page.appendChild(layout);
    }
    entry.addEventListener('click', (event) => {
      if (!event.target.closest('.filters-wrapper')) togglePanel();
    });
    document.getElementById('app').replaceChildren(page);
    ui = { entryHost, entry, entryLabel: entry.firstChild, panel, panelHost, container, entryShown: false };
    refreshFilterUi();
  };

  const startSearch = (keyword) => {
    if (!keyword) return;
    closeOverlay(false);
    state.keyword = keyword;
    state.selection = defaultSelection();
    state.filterApplied = false;
    state.resultsUrl = `/search_result_ai?keyword=${encodeURIComponent(encodeURIComponent(keyword))}&source=web_explore_feed`;
    history.pushState({ view: 'search' }, '', state.resultsUrl);
    showResults();
    newSession(null);
  };

  // 笔记浮层 -----------------------------------------------------------------

  const buildOverlay = () => {
    const mask = el('div', 'note-detail-mask');
    mask.hidden = true;
    const close = el('div', 'close-circle');
    close.addEventListener('click', () => closeOverlay(true));
    const media = el('div', 'media-container');
    const slide = el('div', 'slide');
    const fraction = el('div', 'fraction');
    const left = el('div', 'arrow-controller left');
    left.addEventListener('click', () => flip(-1));
    const right = el('div', 'arrow-controller right');
    right.addEventListener('click', () => flip(1));
    media.append(slide, fraction, left, right);
    const author = el('div', 'author');
    const name = el('a', 'name');
    name.target = '_blank';
    author.appendChild(name);
    const content = el('div', 'note-content');
    const title = el('div', 'title');
    title.id = 'detail-title';
    const desc = el('div', 'desc');
    desc.id = 'detail-desc';
    content.append(title, desc);
    const side = el('div', 'interaction-container');
    side.append(author, content);
    const container = el('div', 'note-container');
    container.append(media, side);
    mask.append(close, container);
    document.body.appendChild(mask);
    return { mask, slide, fraction, left, right, author, name, content, title, desc };
  };
  const overlayUi = buildOverlay();

  const showImage = (index) => {
    const overlay = state.overlay;
    if (!overlay || !overlay.images.length) return;
    overlay.index = Math.max(0, Math.min(index, overlay.images.length - 1));
    overlayUi.slide.replaceChildren(cachedImage(overlay.images[overlay.index]));
    overlayUi.fraction.textContent = `${overlay.index + 1}/${overlay.images.length}`;
    overlayUi.left.hidden = overlay.index === 0;
    overlayUi.right.hidden = overlay.index >= overlay.images.length - 1;
    if (overlay.index + 1 < overlay.images.length) cachedImage(overlay.images[overlay.index + 1]);
  };

  const flip = (delta) => {
    if (state.overlay) showImage(state.overlay.index + delta);
  };

  const requestFeed = async (item, token) => {
    const body = {
      source_note_id: item.id,
      image_formats: ['jpg', 'webp', 'avif'],
      extra: { need_body_topic: '1' },
      xsec_source: 'pc_search',
      xsec_token: item.xsec_token || '',
    };
    let status = 0;
    let payload = null;
    try {
      const response = await fetch(FEED_URL, { method: 'POST', credentials: 'include', body: JSON.stringify(body) });
      status = response.status;
      payload = await response.json().catch(() => null);
    } catch (error) {
      return;
    }
    const overlay = state.overlay;
    if (!overlay || overlay.token !== token) return;
    if (status === 461 || status === 471) {
      showCaptcha();
      return;
    }
    const entry = payload && payload.data && Array.isArray(payload.data.items) ? payload.data.items[0] : null;
    const note = entry && entry.note_card;
    if (status !== 200 || !note) return;
    overlay.images = (note.image_list || []).map((image) => image && image.url_default).filter(Boolean);
    const user = note.user || {};
    overlayUi.author.querySelectorAll('img').forEach((node) => node.remove());
    if (user.avatar) {
      const avatar = cachedImage(user.avatar);
      avatar.className = 'avatar-item';
      overlayUi.author.prepend(avatar);
    }
    overlayUi.name.textContent = user.nickname || '作者';
    overlayUi.name.href = `${PROFILE_URL}${encodeURIComponent(user.user_id || '')}?xsec_token=offline&xsec_source=pc_note`;
    overlayUi.title.textContent = note.title || '';
    overlayUi.desc.textContent = note.desc || '';
    overlayUi.author.hidden = false;
    overlayUi.content.hidden = false;
    showImage(0);
  };

  const openOverlay = (item) => {
    const card = item.note_card || {};
    state.overlayToken += 1;
    state.overlay = { id: item.id, images: [], index: 0, token: state.overlayToken };
    audit.overlaysOpened += 1;
    history.pushState(
      { view: 'note' }, '',
      `/explore/${item.id}?xsec_token=${encodeURIComponent(item.xsec_token || '')}&xsec_source=pc_search`,
    );
    overlayUi.author.hidden = true;
    overlayUi.content.hidden = true;
    overlayUi.fraction.textContent = '';
    overlayUi.left.hidden = true;
    overlayUi.right.hidden = true;
    overlayUi.slide.replaceChildren();
    const first = (card.cover || {}).url_default;
    if (first) overlayUi.slide.appendChild(cachedImage(first)); // 首图先于 feed 加载
    overlayUi.mask.hidden = false;
    requestFeed(item, state.overlayToken);
  };

  function closeOverlay(restoreUrl) {
    const wasOpen = state.overlay !== null;
    state.overlay = null;
    overlayUi.mask.hidden = true;
    if (wasOpen && restoreUrl && state.resultsUrl) history.pushState({ view: 'search' }, '', state.resultsUrl);
  }

  // 验证层 -------------------------------------------------------------------

  function showCaptcha() {
    if (document.querySelector('.captcha-layer')) return;
    audit.captchaShown += 1;
    const layer = el('div', 'captcha-layer');
    const frame = document.createElement('iframe');
    frame.src = `${CAPTCHA_URL}?redirectPath=${encodeURIComponent(location.pathname)}&verifyType=102`;
    layer.appendChild(frame);
    document.body.appendChild(layer);
  }

  window.addEventListener('message', (event) => {
    if (event.data !== VERIFIED_MESSAGE) return;
    const layer = document.querySelector('.captcha-layer');
    if (layer) layer.remove();
  });

  // 事件 ---------------------------------------------------------------------

  const box = document.querySelector('.search-area .input-box textarea.textarea');
  box.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    startSearch(box.value.trim());
  });
  document.addEventListener('keydown', (event) => {
    if (!state.overlay) return;
    if (event.key === 'ArrowRight') flip(1);
    else if (event.key === 'ArrowLeft') flip(-1);
  });
  window.addEventListener('scroll', () => {
    renderList();
    maybeLoadMore('scroll');
  }, { passive: true });
})();
