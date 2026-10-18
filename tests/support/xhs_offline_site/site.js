/* 离线回放站点自身的前端（不是项目注入的脚本）。按 V1、V16 与 R2 定值建模：
   首页输入 → 回车经 history.pushState 进入 /search_result_ai（关键词两次编码）并发不带 filters 的第 1 页；
   结果页有两种布局（<html data-layout>）：默认 AI 对话布局（Mac 人工侦察与测量 2026-10-13）根节点为
   .ai-feeds-page.with-ai-chat，顶部搜索框区域 116px 固定、window 不滚动，结果列表在左侧 .feeds-scroller 容器内
   滚动，右侧对话栏并排且可独立滚动，内有与结果卡片 ID 重叠的笔记链接，筛选入口在 .ai-chat-inner 内，
   面板挂在入口内部、打开时入口加 .active、收起时从 DOM 移除，每个标签前有一份重叠的 aria-hidden 副本；
   备选 V16 布局整页滚动，入口在 .search-layout 内，其前有一个零尺寸的同类副本，面板按 hidden 切换。两种布局的入口
   文字都在直属 span 里，且都在第 1 页结果渲染约 1.5 秒后才插入（Mac 首轮实测入口为空）；
   筛选面板按文字点选“图文”发 5 项 filters 的第 1 页（新 search_id），入口改为“已筛选”，面板不自动收起；面板默认
   只随点击入口切换，悬停模式（<html data-filter-panel="hover">）另随光标开合，见 setPanel 之后的 pointermove 处理；
   虚拟列表只保留可见区附近的行（不超过 30 个节点），接近列表底部时请求下一页，可见区未填满时自行补页；
   响应里的非笔记条目（hot_query 等）同样占一个列表位置，渲染为没有封面的 section.note-item（Mac 人工取证 2026-10-14）；
   每次点击记下命中的元素类别（封面、对话栏笔记链接、相关搜索卡或其他）；
   点封面打开浮层，首图先于 feed 加载，feed 到达后预载下一张，翻图再预载；浮层作者区按 Mac 人工取证 2026-10-14：
   旧位置 div.author 下的作者名隐藏在前，可见的作者名与头像链接在 .interaction-container 的 .author-container 里，
   评论区里评论者（含作者本人）的 a.name 也在 div.author 下；作者名 target=_blank 打开作者页，评论者链接只计数；
   feed 或搜索返回 461/471 时显示验证层。同一文档内图片元素按 URL 复用，重开不再请求（模拟内存缓存）。
   全部请求由测试的 context.route 回应。 */
(() => {
  'use strict';

  const SEARCH_URL = 'https://so.xiaohongshu.com/api/sns/web/v2/search/notes';
  const FEED_URL = 'https://edith.xiaohongshu.com/api/sns/web/v1/feed';
  const CAPTCHA_URL = 'https://www.xiaohongshu.com/website-login/captcha';
  const PROFILE_URL = 'https://www.xiaohongshu.com/user/profile/';
  const VERIFIED_MESSAGE = 'tpc-offline-verified';
  const AI_CHAT = document.documentElement.dataset.layout !== 'v16';
  const HOVER_PANEL = document.documentElement.dataset.filterPanel === 'hover';
  // 与 site.css 一致：V16 列表自文档 120px 起，AI 对话布局自滚动容器内 56px 起；每行 360px、4 列；尾部 200px。
  const LIST_TOP = AI_CHAT ? 56 : 120;
  const ROW_HEIGHT = 360;
  const COLUMNS = 4;
  // AI 对话布局下结果列表只占左侧 65%，列宽收窄、左边距缩小以容纳 4 列（与 site.css 一致）。
  const COLUMN_WIDTH = AI_CHAT ? 200 : 300;
  const LIST_LEFT = AI_CHAT ? 16 : 40;
  const CHAT_NOTE_REFS = 4;
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
    // 面板开合与选项点击的先后：press_open/press_close 为点入口，hover_open 为光标移入入口，leave_close 为光标移出
    // 入口与面板的并集，option 为点选标签。
    panelEvents: [],
    sessions: [],
    captchaShown: 0,
    overlaysOpened: 0,
    hiddenTagClicks: 0,
    chatScrolls: 0,
    maxWindowScrollY: 0,
    maxListScrollTop: 0,
    coverClicks: 0,
    chatNoteClicks: 0,
    strayClicks: [],
    queryCardsRendered: 0,
    queryCardClicks: 0,
    authorNameClicks: 0,
    commentLinkClicks: 0,
  };
  window.__tpcOffline = audit;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  // 逐层嵌套的 div：返回自外向内的各层节点。
  const wrapped = (...classNames) => {
    const nodes = classNames.map((className) => el('div', className));
    nodes.slice(1).forEach((node, index) => nodes[index].appendChild(node));
    return nodes;
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
    section.style.left = `${LIST_LEFT + (index % COLUMNS) * COLUMN_WIDTH}px`;
    if (item.model_type !== 'note') {
      audit.queryCardsRendered += 1;
      section.appendChild(el('div', 'query-note-wrapper', '相关搜索'));
      return section;
    }
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

  // 列表的滚动位置：AI 对话布局是 .feeds-scroller 的，V16 是整页的。
  const listScrollTop = () => (AI_CHAT ? ui.scroller.scrollTop : window.scrollY);

  const renderList = () => {
    if (!ui) return;
    const container = ui.container;
    container.style.height = `${Math.ceil(state.items.length / COLUMNS) * ROW_HEIGHT}px`;
    const currentRow = Math.max(0, Math.floor((listScrollTop() - LIST_TOP) / ROW_HEIGHT));
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
    if (AI_CHAT) {
      const { scroller } = ui;
      return scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - PREFETCH_PX;
    }
    const root = document.scrollingElement || document.documentElement;
    return window.scrollY + window.innerHeight >= root.scrollHeight - PREFETCH_PX;
  };

  const onListScroll = () => {
    renderList();
    maybeLoadMore('scroll');
  };

  // 对话栏引用的笔记（ID 与本会话结果重叠）：带 data-note-id 的 /explore/ 链接、/search_result/ 链接与引用块。
  const renderChatNotes = () => {
    if (!ui || !ui.chatNotes) return;
    const notes = state.items.filter((item) => item.model_type === 'note');
    const refs = notes.slice(0, CHAT_NOTE_REFS).map((item, index) => {
      const ref = el(index % 3 === 2 ? 'div' : 'a', 'ai-note-ref', '参考笔记');
      if (index % 3 === 0) ref.href = `/explore/${item.id}`;
      if (index % 3 === 1) ref.href = `/search_result/${item.id}?xsec_token=offline&xsec_source=`;
      if (index % 3 !== 1) ref.setAttribute('data-note-id', item.id);
      return ref;
    });
    ui.chatNotes.replaceChildren(...refs);
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
      if (item && item.id) state.items.push(item);
    });
    state.page = number;
    state.hasMore = data.has_more === true;
    renderList();
    if (number === 1) renderChatNotes();
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
    if (ui && ui.scroller) ui.scroller.scrollTop = 0;
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
    audit.panelEvents.push('option');
    state.selection[type] = label;
    state.filterApplied = FILTER_GROUPS.some((group) => state.selection[group.type] !== group.options[0][0]);
    refreshFilterUi();
    newSession(buildFilters()); // 面板保持打开，再点入口才收起
  };

  const panelOpen = () => (AI_CHAT ? ui.panelHost.isConnected : !ui.panel.hidden);

  // AI 对话布局：面板挂在入口内部，收起时从 DOM 移除；V16：面板在 .search-layout 内按 hidden 切换。`cause` 为
  // press、hover 或 leave，与开合方向一起记入 audit.panelEvents。
  const setPanel = (open, cause) => {
    if (open === panelOpen()) return;
    if (!AI_CHAT) {
      ui.panel.hidden = !open;
    } else {
      if (open) ui.entry.appendChild(ui.panelHost);
      else ui.panelHost.remove();
      ui.entry.classList.toggle('active', open);
    }
    audit.panelEvents.push(`${cause}_${open ? 'open' : 'close'}`);
  };

  // 悬停模式，与 Python 假站点的 hover_opens_panel 一致：光标从入口外移入入口时打开面板（已开着不变），面板开着时
  // 光标移到入口与面板的并集之外即收起，点入口仍切换。按真实 pointermove 的命中元素判断：面板被浮层或验证层盖住时
  // 命中的是遮罩；AI 对话布局的面板挂在入口内部，命中面板不算移入入口。
  document.addEventListener('pointermove', (event) => {
    if (!HOVER_PANEL || !ui) return;
    const target = event.target instanceof Node ? event.target : null;
    const inPanel = Boolean(target) && ui.panel.contains(target);
    const onEntry = Boolean(target) && ui.entry.contains(target) && !inPanel;
    const entered = onEntry && !ui.pointerOnEntry;
    ui.pointerOnEntry = onEntry;
    if (entered) setPanel(true, 'hover');
    else if (!onEntry && !inPanel) setPanel(false, 'leave');
  }, { capture: true, passive: true });

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
    let scroller = null;
    let chatNotes = null;
    if (AI_CHAT) {
      const main = el('div', 'search-layout__main');
      main.append(container, end);
      layout.append(el('div', 'search-layout__top'), el('div', 'search-layout__middle'), main);
      scroller = el('div', 'feeds-scroller');
      scroller.appendChild(layout);
      scroller.addEventListener('scroll', () => {
        audit.maxListScrollTop = Math.max(audit.maxListScrollTop, scroller.scrollTop);
        onListScroll();
      }, { passive: true });
      entry = filterEntry('filter ai-chat-filter');
      panelHost = el('div', 'filter-panel');
      panelHost.appendChild(panel);
      entryHost = el('div', 'ai-chat-inner');
      const messages = el('div', 'ai-chat-messages');
      chatNotes = el('div', 'ai-chat-notes');
      messages.appendChild(chatNotes);
      for (let index = 0; index < 12; index += 1) messages.appendChild(el('div', 'ai-chat-message', '点点 ai'));
      messages.addEventListener('scroll', () => { audit.chatScrolls += 1; }, { passive: true });
      const chat = el('div', 'ai-chat-section');
      chat.append(entryHost, messages);
      page.append(scroller, chat);
    } else {
      entryHost = el('div', 'filter-bar');
      entryHost.appendChild(filterEntry('filter decoy'));
      entry = filterEntry('filter');
      panel.hidden = true;
      layout.append(entryHost, panel, container, end);
      page.appendChild(layout);
    }
    entry.addEventListener('click', (event) => {
      if (!event.target.closest('.filters-wrapper')) setPanel(!panelOpen(), 'press');
    });
    document.getElementById('app').replaceChildren(page);
    ui = {
      entryHost, entry, entryLabel: entry.firstChild, panel, panelHost, container, scroller, chatNotes, entryShown: false,
      pointerOnEntry: false,
    };
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
    const profileLink = (className) => {
      const link = el('a', className);
      link.target = '_blank';
      return link;
    };
    // 旧位置：.note-container 的直接子节点，隐藏，排在可见作者名之前。
    const [oldAuthor, , oldInfo] = wrapped('author', 'author-wrapper', 'info');
    const oldName = profileLink('name');
    oldInfo.appendChild(oldName);
    const [author, , info] = wrapped('author-container', 'author-wrapper', 'info');
    const [avatarClick, avatarBox] = wrapped('avatar-click', 'avatar-container');
    const avatarLink = profileLink('');
    avatarBox.appendChild(avatarLink);
    const name = profileLink('name');
    info.append(avatarClick, name);
    const content = el('div', 'note-content');
    const title = el('div', 'title');
    title.id = 'detail-title';
    const desc = el('div', 'desc');
    desc.id = 'detail-desc';
    content.append(title, desc);
    const [comments, commentsBox, commentList] = wrapped('comments-el', 'comments-container', 'list-container');
    // 评论区总数行与空状态（操作人 2026-10-18 实地结构）：按 feed 的评论数插入其一，不按 hidden 切换。
    const commentTotal = el('div', 'total');
    const noComments = el('div', 'no-comments-text', '这是一片荒地 ');
    noComments.appendChild(el('span', 'focus-comments', '点击评论'));
    // 底部互动栏（同上）：.buttons.engage-bar-style 的 .left 内依次为点赞、收藏、评论的 span.count，分享只有图标。
    const counter = (className) => {
      const wrapper = el('span', className);
      const count = el('span', 'count');
      wrapper.append(el('span', 'icon'), count);
      return [wrapper, count];
    };
    const [likeWrapper, likeCount] = counter('like-wrapper');
    const [collectWrapper, collectCount] = counter('collect-wrapper');
    const [chatWrapper, chatCount] = counter('chat-wrapper');
    const engageLeft = el('div', 'left');
    engageLeft.append(likeWrapper, collectWrapper, chatWrapper);
    const shareWrapper = el('div', 'share-wrapper');
    const shareIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    shareIcon.setAttribute('class', 'share-icon');
    shareWrapper.appendChild(shareIcon);
    const engageBar = el('div', 'buttons engage-bar-style');
    engageBar.append(engageLeft, shareWrapper);
    const side = el('div', 'interaction-container');
    side.append(author, content, comments, engageBar);
    const container = el('div', 'note-container');
    container.append(media, oldAuthor, side);
    mask.append(close, container);
    // 浮层内的点击：评论者链接只计数、不打开；笔记作者名计数后照常由 target=_blank 打开作者页。
    mask.addEventListener('click', (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) return;
      if (target.closest('.comment-item a')) {
        event.preventDefault();
        audit.commentLinkClicks += 1;
      } else if (target.closest('.author-container a.name')) {
        audit.authorNameClicks += 1;
      }
    }, true);
    document.body.appendChild(mask);
    return {
      mask, slide, fraction, left, right, author, oldName, avatarLink, name, content, title, desc, comments, commentList,
      commentsBox, commentTotal, noComments, likeCount, collectCount, chatCount,
    };
  };
  const profileHref = (userId) => `${PROFILE_URL}${encodeURIComponent(userId || '')}?xsec_token=offline&xsec_source=pc_note`;
  // 评论者：两位其他用户与笔记作者本人（链接同样指向作者主页）。
  const commentNode = (userId, nickname) => {
    const parent = el('div', 'parent-comment');
    const [item, , right, , authorBox] = wrapped('comment-item', 'comment-inner-container', 'right', 'author-wrapper', 'author');
    const link = el('a', 'name', nickname);
    link.target = '_blank';
    link.href = profileHref(userId);
    authorBox.appendChild(link);
    right.appendChild(el('div', 'content', '去过，很好看'));
    // 评论的点赞同样是 .like-wrapper span.count，且在文档顺序上先于底部互动栏。
    const like = el('span', 'like-wrapper');
    like.appendChild(el('span', 'count', '5'));
    right.appendChild(like);
    parent.appendChild(item);
    return parent;
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
    overlayUi.avatarLink.replaceChildren();
    if (user.avatar) {
      const avatar = cachedImage(user.avatar);
      avatar.className = 'avatar-item';
      overlayUi.avatarLink.appendChild(avatar);
    }
    [overlayUi.oldName, overlayUi.name].forEach((link) => {
      link.textContent = user.nickname || '作者';
      link.href = profileHref(user.user_id);
    });
    overlayUi.avatarLink.href = profileHref(user.user_id);
    overlayUi.title.textContent = note.title || '';
    overlayUi.desc.textContent = note.desc || '';
    // 计数为空串时按实地观察显示：点赞文字为空、收藏显示“收藏”、评论显示“评论”，评论区为空状态。
    const interact = note.interact_info || {};
    const shown = (key, label) => (interact[key] === undefined || interact[key] === null || interact[key] === ''
      ? label : String(interact[key]));
    overlayUi.likeCount.textContent = shown('liked_count', '');
    overlayUi.collectCount.textContent = shown('collected_count', '收藏');
    overlayUi.chatCount.textContent = shown('comment_count', '评论');
    const hasComments = shown('comment_count', '') !== '';
    overlayUi.commentTotal.remove();
    overlayUi.noComments.remove();
    if (hasComments) {
      overlayUi.commentTotal.textContent = `共 ${interact.comment_count} 条评论`;
      overlayUi.commentsBox.insertBefore(overlayUi.commentTotal, overlayUi.commentList);
      overlayUi.commentList.replaceChildren(
        commentNode('commenter-1', '路人甲'), commentNode(user.user_id, user.nickname || '作者'),
        commentNode('commenter-2', '路人乙'),
      );
    } else {
      overlayUi.commentsBox.insertBefore(overlayUi.noComments, overlayUi.commentList);
      overlayUi.commentList.replaceChildren();
    }
    overlayUi.author.hidden = false;
    overlayUi.content.hidden = false;
    overlayUi.comments.hidden = false;
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
    overlayUi.comments.hidden = true;
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
    audit.maxWindowScrollY = Math.max(audit.maxWindowScrollY, window.scrollY);
    if (!AI_CHAT) onListScroll();
  }, { passive: true });
  // 结果页上的点击按命中元素归类（捕获阶段，先于各自的处理）：封面、对话栏笔记引用（不跳转）、筛选与浮层之外的
  // 其余点击都记为 stray（class 链首段）。
  document.addEventListener('click', (event) => {
    if (!ui) return;
    const target = event.target instanceof Element ? event.target : null;
    if (!target || target.closest('.note-detail-mask, .captcha-layer, div.filter, .filters-wrapper')) return;
    const card = target.closest('section.note-item');
    if (target.closest('section.note-item a.cover')) {
      audit.coverClicks += 1;
    } else if (target.closest('.ai-chat-section [data-note-id], .ai-chat-section a[href]')) {
      event.preventDefault();
      audit.chatNoteClicks += 1;
    } else if (card && card.querySelector('.query-note-wrapper')) {
      audit.queryCardClicks += 1;
    } else {
      audit.strayClicks.push(`${target.tagName.toLowerCase()}.${target.className || ''}`);
    }
  }, true);
})();
