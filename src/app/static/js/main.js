/* ==========================================================================
 * main.js —— 界面逻辑（无框架，纯原生 DOM + fetch）
 * --------------------------------------------------------------------------
 * 分节：
 *   01. 常量与通用工具
 *   02. 应用状态与 localStorage
 *   03. Toast 通知系统
 *   04. DOM 引用
 *   05. 顶部栏与系统信息
 *   06. 文件上传
 *   07. 翻译设置（服务商 / 配置 / 测试连接）
 *   08. OCR 扫描件识别（引擎状态 / 模型下载 / 单页试识别）
 *   09. 翻译任务（启动 / 取消 / 轮询 / 进度 / 日志）
 *   10. 原文对照视图
 *   11. 页面预览
 *   12. 翻译产物
 *   13. 初始化与事件绑定
 * ========================================================================== */
(function () {
  'use strict';

  /* ==================================================================== *
   * 01. 常量与通用工具
   * ==================================================================== */

  /** 进度轮询间隔（毫秒） */
  var POLL_INTERVAL = 800;
  /** 轮询连续失败的最大次数 */
  var POLL_MAX_FAILS = 5;
  /** 任务终态 */
  var TERMINAL_STATUS = ['done', 'error', 'cancelled'];
  /** 段落列表每次渲染的条数（避免超长文档卡顿） */
  var RENDER_STEP = 200;

  /** status → 中文 */
  var STATUS_TEXT = {
    pending: '排队中',
    running: '进行中',
    done: '已完成',
    error: '出错',
    cancelled: '已取消'
  };

  /** stage → 中文 */
  var STAGE_TEXT = {
    queued: '排队等待',
    parsing: '正在解析 PDF',
    translating: '正在翻译',
    typesetting: '正在排版',
    exporting: '正在导出文件',
    finished: '已完成'
  };

  /** 段落 kind → 中文（未知类型回退为「段落」） */
  var KIND_TEXT = {
    text: '正文',
    paragraph: '正文',
    title: '标题',
    heading: '标题',
    toc: '目录',
    table: '表格',
    caption: '图注',
    header: '页眉',
    footer: '页脚',
    list: '列表'
  };

  /** 产物 kind → 中文名与角标 */
  var FILE_KIND_TEXT = {
    dual: { label: 'PDF 双语对照版', icon: 'PDF', cls: 'kind-dual' },
    mono: { label: 'PDF 仅译文版', icon: 'PDF', cls: 'kind-mono' },
    markdown: { label: 'Markdown 对照稿', icon: 'MD', cls: 'kind-markdown' }
  };

  var STORAGE_KEYS = {
    file: 'pdftr.file',
    taskId: 'pdftr.task_id',
    draft: 'pdftr.config_draft'
  };

  /** 按 id 取元素 */
  function $(id) { return document.getElementById(id); }

  /** 显示 / 隐藏元素 */
  function show(el) { if (el) el.classList.remove('hidden'); }
  function hide(el) { if (el) el.classList.add('hidden'); }

  /** HTML 转义，所有插进 innerHTML 的动态文本都要过一遍 */
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** 字节数 → 可读大小 */
  function formatBytes(bytes) {
    var n = Number(bytes);
    if (!isFinite(n) || n <= 0) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n = n / 1024; i++; }
    return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : 1)) + ' ' + units[i];
  }

  /** 秒 → mm:ss（超过 1 小时按 总分钟:秒 展示） */
  function formatDuration(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || s < 0) return '--:--';
    s = Math.floor(s);
    var mm = Math.floor(s / 60);
    var ss = s % 60;
    return (mm < 10 ? '0' + mm : String(mm)) + ':' + (ss < 10 ? '0' + ss : String(ss));
  }

  /** 数字范围裁剪 */
  function clamp(value, min, max) {
    var n = Number(value);
    if (!isFinite(n)) n = min;
    return Math.min(max, Math.max(min, n));
  }

  /** 简易防抖 */
  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments;
      var self = this;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { timer = null; fn.apply(self, args); }, wait);
    };
  }

  /** 提取可读的错误文本 */
  function errText(err) {
    if (!err) return '未知错误';
    if (typeof err === 'string') return err;
    return err.message || String(err);
  }

  /**
   * 关键词高亮：先转义再包 <mark>，关键词含 HTML 特殊字符时跳过高亮。
   */
  function highlight(text, keyword) {
    var safe = escapeHtml(text);
    var kw = (keyword || '').trim();
    if (!kw || /[<>&"']/.test(kw)) return safe;
    var pattern = kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return safe.replace(new RegExp(pattern, 'gi'), function (match) {
        return '<mark class="hl">' + match + '</mark>';
      });
    } catch (e) {
      return safe;
    }
  }

  /** 本地存储的安全读写（隐私模式下可能抛错） */
  function readJSON(key) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }
  function writeJSON(key, value) {
    try {
      if (value === null || value === undefined) localStorage.removeItem(key);
      else localStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* 忽略存储失败 */ }
  }

  /* ==================================================================== *
   * 02. 应用状态
   * ==================================================================== */

  var state = {
    /* 配置与服务商 */
    config: {},            // 服务端配置（含 defaults 合并结果）
    defaults: {},
    providers: [],
    languages: [],
    provider: null,        // 当前选中的服务商对象

    /* 文档 */
    file: null,            // /api/upload 返回的 data
    docInfo: null,         // /api/document/<id>/info 返回的 data

    /* 任务 */
    taskId: null,
    taskStatus: null,
    taskError: null,
    result: null,
    pollTimer: null,
    polling: false,
    pollFailed: 0,

    /* OCR */
    ocr: null,              // /api/ocr/status 返回的 data
    ocrPollTimer: null,     // 模型下载轮询定时器
    ocrPolling: false,
    ocrPollCount: 0,
    ocrTesting: false,      // 试识别进行中
    ocrLangPicked: '',      // 用户手动选过的识别语言（优先于配置值）
    ocrModePicked: '',      // 用户手动选过的 OCR 模式

    /* 视图 */
    paragraphs: [],
    filter: '',
    renderedCount: 0,
    previewPage: 1,
    previewKind: 'source',
    zoom: 1.4,
    activeTab: 'original',
    logsSignature: ''
  };

  var els = {};   // DOM 引用集合

  var previewToken = 0;        // 预览图加载令牌，用于丢弃过期回调
  var lastPreviewUrl = '';     // 上一次的预览图地址（用于「重试」时强制刷新）

  /* ==================================================================== *
   * 03. Toast 通知系统
   * ==================================================================== */

  var TOAST_META = {
    success: { icon: '✓', title: '成功', timeout: 3200 },
    warn: { icon: '!', title: '提示', timeout: 5200 },
    error: { icon: '×', title: '出错了', timeout: 8000 }
  };

  /**
   * 弹出一条提示。
   * @param {'success'|'warn'|'error'} type
   * @param {string} message  主要信息
   * @param {string} [title]  标题，缺省用类型默认标题
   * @param {number} [timeout] 自动关闭毫秒数，0 表示不自动关闭
   */
  function toast(type, message, title, timeout) {
    if (!els.toastContainer) return;
    var meta = TOAST_META[type] || TOAST_META.warn;
    var wrap = document.createElement('div');
    wrap.className = 'toast ' + type;

    var life = typeof timeout === 'number' ? timeout : meta.timeout;

    wrap.innerHTML =
      '<span class="toast-icon">' + escapeHtml(meta.icon) + '</span>' +
      '<div class="toast-body">' +
        '<div class="toast-title">' + escapeHtml(title || meta.title) + '</div>' +
        '<div class="toast-msg">' + escapeHtml(message || '') + '</div>' +
      '</div>' +
      '<button class="toast-close" type="button" aria-label="关闭">×</button>';

    var remove = function () {
      if (!wrap.parentNode) return;
      wrap.classList.add('is-out');
      setTimeout(function () {
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      }, 200);
    };

    wrap.querySelector('.toast-close').addEventListener('click', remove);
    els.toastContainer.appendChild(wrap);

    if (life > 0) setTimeout(remove, life);
  }

  /* ==================================================================== *
   * 04. DOM 引用
   * ==================================================================== */

  function cacheDom() {
    els = {
      /* 顶部栏 */
      toastContainer: $('toastContainer'),
      chipConnection: $('chipConnection'),
      chipPymupdf: $('chipPymupdf'),
      chipFont: $('chipFont'),
      chipNetwork: $('chipNetwork'),
      btnSysInfo: $('btnSysInfo'),
      sysModal: $('sysModal'),
      btnSysClose: $('btnSysClose'),
      sysInfoBody: $('sysInfoBody'),

      /* 文件区 */
      dropZone: $('dropZone'),
      fileInput: $('fileInput'),
      btnPickFile: $('btnPickFile'),
      uploadState: $('uploadState'),
      uploadBar: $('uploadBar'),
      uploadText: $('uploadText'),
      fileInfo: $('fileInfo'),
      fiName: $('fiName'),
      fiPages: $('fiPages'),
      fiSize: $('fiSize'),
      fiTextPages: $('fiTextPages'),
      fiEncrypted: $('fiEncrypted'),
      btnClearFile: $('btnClearFile'),

      /* 设置 */
      providerSelect: $('providerSelect'),
      providerHint: $('providerHint'),
      baseUrl: $('baseUrl'),
      modelSelect: $('modelSelect'),
      modelCustom: $('modelCustom'),
      apiKey: $('apiKey'),
      btnToggleKey: $('btnToggleKey'),
      targetLang: $('targetLang'),
      modeGroup: $('modeGroup'),
      pageRange: $('pageRange'),
      concurrency: $('concurrency'),
      concurrencyVal: $('concurrencyVal'),
      batchSize: $('batchSize'),
      batchSizeVal: $('batchSizeVal'),
      temperature: $('temperature'),
      temperatureVal: $('temperatureVal'),
      fontScale: $('fontScale'),
      fontScaleVal: $('fontScaleVal'),
      glossary: $('glossary'),
      glossaryHint: $('glossaryHint'),
      prompt: $('prompt'),
      btnSaveConfig: $('btnSaveConfig'),
      btnTestConn: $('btnTestConn'),
      testResult: $('testResult'),

      /* OCR */
      ocrDetails: $('ocrDetails'),
      ocrEngineStatus: $('ocrEngineStatus'),
      ocrMode: $('ocrMode'),
      ocrLang: $('ocrLang'),
      ocrLangHint: $('ocrLangHint'),
      ocrLangHintText: $('ocrLangHintText'),
      btnOcrDownload: $('btnOcrDownload'),
      ocrDownloadState: $('ocrDownloadState'),
      ocrDpi: $('ocrDpi'),
      ocrMinConf: $('ocrMinConf'),
      ocrMinConfVal: $('ocrMinConfVal'),
      ocrDetVersion: $('ocrDetVersion'),
      ocrUseCls: $('ocrUseCls'),
      ocrTestPage: $('ocrTestPage'),
      btnOcrTest: $('btnOcrTest'),
      ocrTestHint: $('ocrTestHint'),
      ocrTestResult: $('ocrTestResult'),
      ocrTestLines: $('ocrTestLines'),

      /* 嵌字字体 */
      letteringDetails: $('letteringDetails'),
      letteringStatus: $('letteringStatus'),
      letteringFamily: $('letteringFamily'),
      letteringUseSystem: $('letteringUseSystem'),
      letteringRegular: $('letteringRegular'),
      letteringBold: $('letteringBold'),
      letteringTracking: $('letteringTracking'),
      letteringTrackingVal: $('letteringTrackingVal'),
      letteringPreview: $('letteringPreview'),
      btnFontReload: $('btnFontReload'),
      btnFontList: $('btnFontList'),
      letteringFontList: $('letteringFontList'),

      /* 操作 */
      taskIdTag: $('taskIdTag'),
      btnStart: $('btnStart'),
      btnCancel: $('btnCancel'),
      startHint: $('startHint'),
      progressStage: $('progressStage'),
      progressPercent: $('progressPercent'),
      progressBar: $('progressBar'),
      progressMessage: $('progressMessage'),
      progressParas: $('progressParas'),
      progressElapsed: $('progressElapsed'),
      progressEta: $('progressEta'),
      logPanel: $('logPanel'),
      btnClearLogs: $('btnClearLogs'),

      /* 工作区 */
      emptyState: $('emptyState'),
      workArea: $('workArea'),
      docBanner: $('docBanner'),
      tabOriginal: $('tabOriginal'),
      tabPreview: $('tabPreview'),
      panelOriginal: $('panelOriginal'),
      panelPreview: $('panelPreview'),
      paraSearch: $('paraSearch'),
      paraStats: $('paraStats'),
      paraList: $('paraList'),
      btnMoreParas: $('btnMoreParas'),

      /* 预览 */
      previewKindSeg: $('previewKindSeg'),
      btnPrevPage: $('btnPrevPage'),
      btnNextPage: $('btnNextPage'),
      pageInput: $('pageInput'),
      pageTotal: $('pageTotal'),
      zoomSelect: $('zoomSelect'),
      previewStage: $('previewStage'),
      previewImg: $('previewImg'),
      previewLoading: $('previewLoading'),
      previewError: $('previewError'),
      previewErrorText: $('previewErrorText'),
      previewErrorSub: $('previewErrorSub'),
      btnPreviewRetry: $('btnPreviewRetry'),

      /* 产物 */
      resultCard: $('resultCard'),
      resultElapsed: $('resultElapsed'),
      resultFiles: $('resultFiles'),
      resultStats: $('resultStats')
    };
  }

  /* ==================================================================== *
   * 05. 顶部栏与系统信息
   * ==================================================================== */

  /** 设置顶部状态 chip 的内容 */
  function setChip(el, dotClass, text) {
    if (!el) return;
    el.innerHTML = '<i class="dot ' + dotClass + '"></i>' + escapeHtml(text);
  }

  /** 加载系统信息并刷新顶部状态 */
  function loadSystemInfo() {
    return api.getSystemInfo().then(function (info) {
      state.systemInfo = info;
      setChip(els.chipConnection, 'dot-ok', '服务正常');
      setChip(els.chipPymupdf, 'dot-ok', 'PyMuPDF ' + (info.pymupdf || '未知'));

      var fontOk = !!info.cjk_font;
      setChip(els.chipFont, fontOk ? 'dot-ok' : 'dot-bad',
        fontOk ? '中文字体可用' : '中文字体缺失');

      var netOk = info.network !== false;
      setChip(els.chipNetwork, netOk ? 'dot-ok' : 'dot-warn',
        netOk ? '网络已连接' : '网络不可用');
      return info;
    }).catch(function (err) {
      state.systemInfo = null;
      setChip(els.chipConnection, 'dot-bad', '未连接');
      setChip(els.chipPymupdf, 'dot-muted', 'PyMuPDF —');
      setChip(els.chipFont, 'dot-muted', '中文字体 —');
      setChip(els.chipNetwork, 'dot-muted', '网络 —');
      toast('error', errText(err), '无法获取系统信息');
      throw err;
    });
  }

  /** 打开「系统信息」弹窗（每次打开都重新拉取一次） */
  function openSysModal() {
    show(els.sysModal);
    els.sysInfoBody.innerHTML = '<p class="stat">正在加载…</p>';
    api.getSystemInfo().then(function (info) {
      state.systemInfo = info;
      renderSysInfo(info);
    }).catch(function (err) {
      els.sysInfoBody.innerHTML =
        '<div class="test-result test-bad">' + escapeHtml(errText(err)) + '</div>';
    });
  }

  function closeSysModal() { hide(els.sysModal); }

  /** 渲染系统信息表格 */
  function renderSysInfo(info) {
    var rows = [
      ['应用版本', info.app_version || '未知'],
      ['Python', info.python || '未知'],
      ['PyMuPDF', info.pymupdf || '未知'],
      ['运行平台', info.platform || '未知'],
      ['中文字体', info.cjk_font ? info.cjk_font : '不可用（将影响中文排版）'],
      ['联网状态', info.network === false ? '不可用' : '可用'],
      ['工作目录', info.work_dir || '未知'],
      ['服务地址', window.location.origin]
    ];
    var html = '<table class="info-table"><tbody>';
    rows.forEach(function (row) {
      html += '<tr><th>' + escapeHtml(row[0]) + '</th><td>' + escapeHtml(row[1]) + '</td></tr>';
    });
    html += '</tbody></table>';
    els.sysInfoBody.innerHTML = html;

    if (!info.cjk_font) {
      toast('warn', '未检测到可用的中文字体，译文可能出现方块字。', '字体告警');
    }
  }

  /* ==================================================================== *
   * 06. 文件上传
   * ==================================================================== */

  /** 校验并上传所选文件 */
  function handleFile(file) {
    if (!file) return;

    var isPdf = /\.pdf$/i.test(file.name) || file.type === 'application/pdf';
    if (!isPdf) {
      toast('error', '只支持 .pdf 文件，当前选择的是「' + file.name + '」。', '文件类型不支持');
      return;
    }
    if (file.size === 0) {
      toast('error', '这个文件是空的，请换一个 PDF。', '文件无效');
      return;
    }

    show(els.uploadState);
    hide(els.fileInfo);
    hide(els.btnClearFile);
    els.fileInput.value = '';
    els.uploadBar.style.width = '0%';
    els.uploadBar.classList.remove('is-done');
    els.uploadText.textContent = '正在上传 ' + file.name + ' …';

    api.upload(file, function (p) {
      els.uploadBar.style.width = clamp(p.percent, 0, 100) + '%';
      if (p.done) {
        els.uploadText.textContent = '上传完成，正在解析版面…';
      } else {
        els.uploadText.textContent =
          '正在上传… ' + p.percent + '%（' + formatBytes(p.loaded) + ' / ' + formatBytes(p.total) + '）';
      }
    }).then(function (data) {
      els.uploadBar.classList.add('is-done');
      els.uploadBar.style.width = '100%';
      els.uploadText.textContent = data.message || '解析完成';
      setTimeout(function () { hide(els.uploadState); }, 600);

      // 新文档：清掉旧任务与旧结果
      clearTaskState(false);
      state.file = data;
      state.docInfo = null;
      state.paragraphs = [];
      writeJSON(STORAGE_KEYS.file, data);

      renderFileInfo(data);
      showWorkArea();
      loadDocumentInfo();
      updateStartButton();

      toast('success',
        '已解析 ' + (data.pages || 0) + ' 页，其中 ' + (data.text_pages || 0) + ' 页含可提取文本。',
        '上传成功');
    }).catch(function (err) {
      hide(els.uploadState);
      toast('error', errText(err), '上传失败');
    });
  }

  /** 渲染文件信息卡 */
  function renderFileInfo(data) {
    if (!data) { hide(els.fileInfo); hide(els.btnClearFile); return; }
    show(els.fileInfo);
    show(els.btnClearFile);
    els.fiName.textContent = data.filename || '未命名.pdf';
    els.fiName.title = data.filename || '';
    els.fiPages.textContent = (data.pages != null ? data.pages : '—') + ' 页';
    els.fiSize.textContent = formatBytes(data.size);
    els.fiTextPages.textContent = (data.text_pages != null ? data.text_pages : '—') + ' 页';
    els.fiEncrypted.textContent = data.encrypted ? '已加密' : '未加密';
  }

  /** 移除当前文件（仅清空界面状态） */
  function clearFile() {
    state.file = null;
    state.docInfo = null;
    state.paragraphs = [];
    state.previewPage = 1;
    writeJSON(STORAGE_KEYS.file, null);
    clearTaskState(true);
    hide(els.fileInfo);
    hide(els.btnClearFile);
    hide(els.uploadState);
    els.fileInput.value = '';
    hideWorkArea();
    updateStartButton();
    toast('warn', '已移除当前文件。', '已移除');
  }

  /* ==================================================================== *
   * 07. 翻译设置
   * ==================================================================== */

  /** 拉取服务商与语言列表 */
  function initProviders() {
    return api.getProviders().then(function (data) {
      state.providers = Array.isArray(data.providers) ? data.providers : [];
      state.languages = Array.isArray(data.languages) ? data.languages : [];
      renderProviderOptions();
      renderLanguageOptions();
    }).catch(function (err) {
      state.providers = [];
      state.languages = [];
      renderProviderOptions();
      renderLanguageOptions();
      toast('error', errText(err), '无法获取服务商列表');
    });
  }

  function renderProviderOptions() {
    var sel = els.providerSelect;
    if (!sel) return;
    var current = (state.config && state.config.provider) || '';

    if (!state.providers.length) {
      sel.innerHTML = '<option value="' + escapeHtml(current) + '">' +
        escapeHtml(current ? current + '（当前配置）' : '（未获取到服务商列表）') + '</option>';
      sel.disabled = true;
      els.providerHint.textContent = '服务商列表加载失败，将直接使用下方手填的接口地址与模型。';
      return;
    }

    sel.disabled = false;
    var html = '';
    var found = false;
    state.providers.forEach(function (p) {
      if (p.id === current) found = true;
      html += '<option value="' + escapeHtml(p.id) + '">' + escapeHtml(p.label || p.id) + '</option>';
    });
    if (current && !found) {
      html += '<option value="' + escapeHtml(current) + '">' + escapeHtml(current + '（当前配置）') + '</option>';
    }
    sel.innerHTML = html;

    var target = current || (state.providers[0] && state.providers[0].id) || '';
    if (target) sel.value = target;
    applyProvider(sel.value, true);
  }

  function renderLanguageOptions() {
    var sel = els.targetLang;
    if (!sel) return;
    var current = (state.config && state.config.target_lang) || '';

    if (!state.languages.length) {
      sel.innerHTML = current
        ? '<option value="' + escapeHtml(current) + '">' + escapeHtml(current) + '</option>'
        : '<option value="">（未获取到语言列表）</option>';
      return;
    }

    var html = '';
    var found = false;
    state.languages.forEach(function (l) {
      if (l.id === current) found = true;
      html += '<option value="' + escapeHtml(l.id) + '">' + escapeHtml(l.label || l.id) + '</option>';
    });
    if (current && !found) {
      html += '<option value="' + escapeHtml(current) + '">' + escapeHtml(current) + '</option>';
    }
    sel.innerHTML = html;
    sel.value = current || (state.languages[0] && state.languages[0].id) || '';
  }

  /** 根据服务商 id 填充 base_url / 模型下拉 */
  function applyProvider(providerId, keepModel) {
    var p = null;
    state.providers.forEach(function (item) { if (item.id === providerId) p = item; });
    state.provider = p;

    if (!p) {
      els.providerHint.textContent = '';
      return;
    }

    if (!keepModel) {
      if (p.base_url) els.baseUrl.value = p.base_url;
    } else if (p.base_url && !els.baseUrl.value.trim()) {
      els.baseUrl.value = p.base_url;
    }

    var models = Array.isArray(p.models) ? p.models.slice() : [];
    var desired = keepModel ? (els.modelSelect.value || '') : (p.default_model || models[0] || '');

    var html = '';
    models.forEach(function (m) {
      html += '<option value="' + escapeHtml(m) + '">' + escapeHtml(m) + '</option>';
    });
    html += '<option value="__custom__">自定义…</option>';
    els.modelSelect.innerHTML = html;

    var inList = false;
    for (var i = 0; i < models.length; i++) {
      if (models[i] === desired) { inList = true; break; }
    }

    if (desired && inList) {
      els.modelSelect.value = desired;
      hide(els.modelCustom);
    } else if (desired) {
      els.modelSelect.value = '__custom__';
      els.modelCustom.value = desired;
      show(els.modelCustom);
    } else {
      els.modelSelect.value = models.length ? models[0] : '__custom__';
      if (!models.length) show(els.modelCustom);
      else hide(els.modelCustom);
    }

    els.providerHint.textContent = p.hint || '';
    updateStartButton();
  }

  /** 取当前模型名（处理「自定义…」） */
  function currentModel() {
    if (els.modelSelect.value === '__custom__') return els.modelCustom.value.trim();
    return els.modelSelect.value;
  }

  /** 取当前翻译模式（缺省为嵌字版：保留原版式、只换文字） */
  function currentMode() {
    var checked = els.modeGroup.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : 'mono';
  }

  /** 解析术语表文本 → { glossary, invalid } */
  function parseGlossary(text) {
    var result = {};
    var invalid = 0;
    String(text || '').split(/\r?\n/).forEach(function (line) {
      var raw = line.trim();
      if (!raw) return;
      var idx = raw.indexOf('=');
      if (idx <= 0) { invalid++; return; }
      var key = raw.slice(0, idx).trim();
      var val = raw.slice(idx + 1).trim();
      if (!key || !val) { invalid++; return; }
      result[key] = val;
    });
    return { glossary: result, invalid: invalid };
  }

  /** 术语表 → 文本（回填用） */
  function glossaryToText(glossary) {
    if (!glossary || typeof glossary !== 'object') return '';
    return Object.keys(glossary).map(function (k) {
      return k + '=' + glossary[k];
    }).join('\n');
  }

  /** 刷新术语表提示 */
  function updateGlossaryHint() {
    var parsed = parseGlossary(els.glossary.value);
    var count = Object.keys(parsed.glossary).length;
    var text = '已解析 ' + count + ' 条术语';
    if (parsed.invalid > 0) text += '，忽略 ' + parsed.invalid + ' 行格式不正确的行';
    text += '；格式为 原文=译文。';
    els.glossaryHint.textContent = text;
  }

  /** 收集表单里的配置（字段均来自 API 契约的 config） */
  function collectConfig() {
    var ocr = collectOcrConfig();
    var lettering = collectLetteringConfig();
    return {
      provider: els.providerSelect.value || (state.config.provider || ''),
      api_key: els.apiKey.value.trim(),
      base_url: els.baseUrl.value.trim(),
      model: currentModel(),
      target_lang: els.targetLang.value,
      mode: currentMode(),
      prompt: els.prompt.value,
      glossary: parseGlossary(els.glossary.value).glossary,
      batch_size: clamp(els.batchSize.value, 4, 40),
      concurrency: clamp(els.concurrency.value, 1, 8),
      temperature: clamp(els.temperature.value, 0, 1),
      font_scale: clamp(els.fontScale.value, 0.6, 1.4),
      page_range: els.pageRange.value.trim() || 'all',
      /* OCR（见 08 节） */
      ocr_mode: ocr.ocr_mode,
      ocr_lang: ocr.ocr_lang,
      ocr_dpi: ocr.ocr_dpi,
      ocr_min_confidence: ocr.ocr_min_confidence,
      ocr_det_version: ocr.ocr_det_version,
      ocr_use_cls: ocr.ocr_use_cls,
      /* 嵌字字体（见 08b 节） */
      lettering_font_family: lettering.lettering_font_family,
      lettering_use_system_fonts: lettering.lettering_use_system_fonts,
      lettering_font_regular: lettering.lettering_font_regular,
      lettering_font_bold: lettering.lettering_font_bold,
      lettering_tracking: lettering.lettering_tracking
    };
  }

  /**
   * 把配置回填到表单。
   * @param {object} cfg
   * @param {boolean} [remember] 是否把它记作「服务端配置」（应用本地草稿时传 false）
   */
  function applyConfigToForm(cfg, remember) {
    var c = cfg || {};
    if (remember !== false) state.config = c;

    if (els.providerSelect.options.length) {
      var hasProvider = false;
      for (var i = 0; i < els.providerSelect.options.length; i++) {
        if (els.providerSelect.options[i].value === c.provider) { hasProvider = true; break; }
      }
      if (hasProvider) els.providerSelect.value = c.provider;
    }

    // 服务商对象必须与下拉框保持一致（needs_key 判断与模型列表都依赖它）
    var selProvider = els.providerSelect.value;
    if (selProvider && (!state.provider || state.provider.id !== selProvider)) {
      applyProvider(selProvider, true);
    }

    els.baseUrl.value = c.base_url || '';
    els.apiKey.value = c.api_key || '';
    els.prompt.value = c.prompt || '';
    els.glossary.value = glossaryToText(c.glossary);
    els.pageRange.value = c.page_range || 'all';

    // 语言
    if (c.target_lang) {
      var hasLang = false;
      for (var j = 0; j < els.targetLang.options.length; j++) {
        if (els.targetLang.options[j].value === c.target_lang) { hasLang = true; break; }
      }
      if (hasLang) els.targetLang.value = c.target_lang;
    }

    // 模式
    var radios = els.modeGroup.querySelectorAll('input[name="mode"]');
    for (var k = 0; k < radios.length; k++) {
      radios[k].checked = (radios[k].value === (c.mode || 'mono'));
    }

    // 数值
    if (c.concurrency != null) els.concurrency.value = clamp(c.concurrency, 1, 8);
    if (c.batch_size != null) els.batchSize.value = clamp(c.batch_size, 4, 40);
    if (c.temperature != null) els.temperature.value = clamp(c.temperature, 0, 1);
    if (c.font_scale != null) els.fontScale.value = clamp(c.font_scale, 0.6, 1.4);

    // 模型：需要服务商信息决定下拉项，放在最后处理
    state.pendingModel = c.model || '';
    applyPendingModel();

    // OCR 设置（见 08 节）
    applyOcrConfigToForm(c);
    // 嵌字字体设置（见 08b 节）
    applyLetteringConfigToForm(c);

    syncRangeLabels();
    updateGlossaryHint();
  }

  /** 服务商信息就绪后，把配置里的模型填进去 */
  function applyPendingModel() {
    var model = state.pendingModel;
    if (!model) return;
    var p = state.provider;
    var models = (p && Array.isArray(p.models)) ? p.models : [];

    var inList = false;
    for (var i = 0; i < models.length; i++) {
      if (models[i] === model) { inList = true; break; }
    }
    if (inList) {
      els.modelSelect.value = model;
      hide(els.modelCustom);
    } else {
      // 下拉里可能还没渲染出自定义项
      var hasCustom = false;
      for (var j = 0; j < els.modelSelect.options.length; j++) {
        if (els.modelSelect.options[j].value === '__custom__') { hasCustom = true; break; }
      }
      if (!hasCustom) {
        els.modelSelect.innerHTML += '<option value="__custom__">自定义…</option>';
      }
      els.modelSelect.value = '__custom__';
      els.modelCustom.value = model;
      show(els.modelCustom);
    }
  }

  /** 同步滑块旁的数字显示 */
  function syncRangeLabels() {
    els.concurrencyVal.textContent = els.concurrency.value;
    els.batchSizeVal.textContent = els.batchSize.value;
    els.temperatureVal.textContent = Number(els.temperature.value).toFixed(1);
    els.fontScaleVal.textContent = Number(els.fontScale.value).toFixed(2);
  }

  /** 把表单内容暂存到 localStorage，刷新后可恢复 */
  var persistDraft = debounce(function () {
    writeJSON(STORAGE_KEYS.draft, collectConfig());
  }, 400);

  function loadDraft() {
    var draft = readJSON(STORAGE_KEYS.draft);
    if (!draft || typeof draft !== 'object') return;
    // 草稿叠加在服务端配置之上：用户在界面上改过的值优先（但不覆盖 state.config）
    applyConfigToForm(Object.assign({}, state.config, draft), false);
  }

  /** 保存为服务端默认设置 */
  function saveConfigAsDefault(silent) {
    var cfg = collectConfig();
    if (!cfg.api_key) {
      toast('warn', '还没有填写 API Key，保存后仍可继续，但翻译前需要补上。', '提示');
    }
    els.btnSaveConfig.disabled = true;
    return api.saveConfig(cfg).then(function (data) {
      els.btnSaveConfig.disabled = false;
      if (data && data.config) state.config = data.config;
      writeJSON(STORAGE_KEYS.draft, cfg);
      if (!silent) toast('success', '当前设置已保存为默认配置。', '已保存');
      return data;
    }).catch(function (err) {
      els.btnSaveConfig.disabled = false;
      toast('error', errText(err), '保存失败');
      throw err;
    });
  }

  /** 测试与模型服务的连通性 */
  function testConnection() {
    var payload = {
      provider: els.providerSelect.value || (state.config.provider || ''),
      api_key: els.apiKey.value.trim(),
      base_url: els.baseUrl.value.trim(),
      model: currentModel(),
      timeout: 30
    };

    if (!payload.base_url) {
      showTestResult('bad', '请先填写接口地址（Base URL）。');
      return;
    }
    if (providerNeedsKey() && !payload.api_key) {
      showTestResult('bad', '请先填写 API Key。');
      return;
    }

    els.btnTestConn.disabled = true;
    showTestResult('pending', '正在测试连接，请稍候…（最长 60 秒）');

    api.testConnection(payload).then(function (data) {
      els.btnTestConn.disabled = false;
      var latency = data && data.latency_ms != null ? data.latency_ms : '—';
      var model = (data && data.model) || payload.model || '—';
      var reply = (data && data.reply) ? '　返回：' + data.reply : '';
      showTestResult('ok', '连接成功，延迟 ' + latency + ' ms（模型 ' + model + '）。' + reply);
      toast('success', '延迟 ' + latency + ' ms，模型 ' + model, '连接正常');
    }).catch(function (err) {
      els.btnTestConn.disabled = false;
      showTestResult('bad', '连接失败：' + errText(err));
      toast('error', errText(err), '连接失败');
    });
  }

  function showTestResult(kind, text) {
    show(els.testResult);
    els.testResult.className = 'test-result ' +
      (kind === 'ok' ? 'test-ok' : kind === 'bad' ? 'test-bad' : 'test-pending');
    els.testResult.textContent = text;
  }

  /** 当前服务商是否需要 API Key */
  function providerNeedsKey() {
    var p = state.provider;
    if (!p) return true;
    return p.needs_key !== false;
  }

  /* ==================================================================== *
   * 08. OCR 扫描件识别
   * -------------------------------------------------------------------- *
   * 契约：docs/API.md / OCR 接口
   *   GET  /api/ocr/status   → 引擎可用性、模型目录、占用体积、语言就绪情况、下载状态
   *   POST /api/ocr/download → 后台线程下载当前语言的识别模型
   *   POST /api/ocr/test     → 对已上传文档的某一页做一次试识别
   * 说明：api.js 未单独封装 OCR 接口，这里统一复用 window.api.request，
   *       以保持「响应解包 + 中文错误提示」与既有风格一致。
   * ==================================================================== */

  /** 模型下载状态的轮询间隔（毫秒） */
  var OCR_POLL_INTERVAL = 1000;
  /** 模型下载轮询的最大次数（约 15 分钟，避免后台卡死时无限轮询） */
  var OCR_POLL_MAX = 900;
  /** 试识别结果最多渲染的行数 */
  var OCR_TEST_MAX_LINES = 200;

  /**
   * 各识别语言的模型体积**兜底**估算（MB）。
   * 正常情况用后端 /api/ocr/status 里每个语言的 download_size（真实字节数），
   * 这张表只在接口拿不到数据时兜底，保证提示文案不会显示成空。
   */
  var OCR_LANG_SIZE_MB = {
    ch: 21, ch_v4: 16, en: 12, japan: 22, korean: 52,
    latin: 13, cyrillic: 13, arabic: 13, eslav: 13, th: 13, el: 13, devanagari: 13
  };
  /** 未知语言的估算体积（MB） */
  var OCR_LANG_SIZE_DEFAULT_MB = 15;

  /** /api/ocr/status 不可用时的兜底下拉项（让控件仍可操作） */
  var OCR_MODE_FALLBACK = [
    { id: 'off', label: '关闭' },
    { id: 'auto', label: '自动（仅扫描页，推荐）' },
    { id: 'always', label: '始终（所有页面都走 OCR）' }
  ];
  /** 语言兜底项：状态未知时按「已就绪」处理，避免误提示需要下载 */
  var OCR_LANG_FALLBACK = [
    { id: 'ch', label: '中英混排（推荐）', ready: true },
    { id: 'en', label: '英文', ready: true }
  ];

  /** OCR 字段的兜底默认值（与后端 DEFAULT_CONFIG 对齐） */
  function ocrDefaults() {
    return {
      ocr_mode: 'auto',
      ocr_lang: 'ch',
      ocr_dpi: 200,
      ocr_min_confidence: 0.5,
      ocr_det_version: 'v5',
      ocr_use_cls: false
    };
  }

  /** 从表单收集 OCR 配置（区块未渲染时回落到默认值） */
  function collectOcrConfig() {
    var d = ocrDefaults();
    if (!els.ocrMode) return d;
    return {
      ocr_mode: els.ocrMode.value || d.ocr_mode,
      ocr_lang: els.ocrLang.value || d.ocr_lang,
      ocr_dpi: clamp(els.ocrDpi.value || d.ocr_dpi, 120, 400),
      ocr_min_confidence: clamp(els.ocrMinConf.value || d.ocr_min_confidence, 0, 1),
      ocr_det_version: els.ocrDetVersion.value || d.ocr_det_version,
      ocr_use_cls: !!els.ocrUseCls.checked
    };
  }

  /** 把 OCR 配置回填到表单 */
  function applyOcrConfigToForm(cfg) {
    if (!els.ocrMode) return;
    var d = ocrDefaults();
    var c = cfg || {};

    selectValue(els.ocrMode, c.ocr_mode || d.ocr_mode);
    selectValue(els.ocrLang, c.ocr_lang || d.ocr_lang);
    selectValue(els.ocrDetVersion, c.ocr_det_version || d.ocr_det_version);

    els.ocrDpi.value = String(clamp(c.ocr_dpi != null ? c.ocr_dpi : d.ocr_dpi, 120, 400));
    els.ocrMinConf.value = String(
      clamp(c.ocr_min_confidence != null ? c.ocr_min_confidence : d.ocr_min_confidence, 0, 1));
    els.ocrMinConfVal.textContent = Number(els.ocrMinConf.value).toFixed(2);
    els.ocrUseCls.checked = !!c.ocr_use_cls;

    updateOcrLangHint();
  }

  /** 安全地设置 select 的值：选项里没有该值时保持原选择 */
  function selectValue(sel, value) {
    if (!sel || value == null) return;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === value) { sel.value = value; return; }
    }
  }

  /** GET /api/ocr/status（带 lang / det 时返回对应模型的占用体积与缺件列表） */
  function fetchOcrStatus(lang, det) {
    var query = lang
      ? '?lang=' + encodeURIComponent(lang) + '&det=' + encodeURIComponent(det || 'v5')
      : '';
    return api.request('/api/ocr/status' + query, { timeout: 15000 });
  }

  /**
   * 拉取并刷新 OCR 状态。
   * 失败一律静默降级（不弹错误 toast），避免打断用户输入。
   */
  function refreshOcrStatus() {
    var cfg = collectOcrConfig();
    return fetchOcrStatus(cfg.ocr_lang, cfg.ocr_det_version).then(function (data) {
      state.ocr = data || {};
      renderOcrOptions(state.ocr);
      renderOcrEngineStatus(state.ocr);
      updateOcrLangHint();
      // 页面加载时可能已有下载在后台跑
      var download = state.ocr.download || {};
      if (download.running && !state.ocrPolling) {
        showOcrDownloadState(download.message || '正在下载模型…');
        startOcrDownloadPolling();
      }
      return state.ocr;
    }).catch(function () {
      renderOcrEngineStatus(null);
      return null;
    });
  }

  /** 页面初始化：拉一次 OCR 状态填充下拉与状态行 */
  function initOcr() {
    refreshOcrStatus();
    updateOcrTestState();
  }

  /** 渲染 OCR 模式与识别语言下拉 */
  function renderOcrOptions(data) {
    var info = data || {};
    var modes = (Array.isArray(info.modes) && info.modes.length) ? info.modes : OCR_MODE_FALLBACK;
    var languages = (Array.isArray(info.languages) && info.languages.length)
      ? info.languages : OCR_LANG_FALLBACK;
    renderOcrModeOptions(modes);
    renderOcrLangOptions(languages);
  }

  /** 渲染「OCR 模式」下拉（保留配置值或用户选择） */
  function renderOcrModeOptions(modes) {
    var sel = els.ocrMode;
    if (!sel) return;
    var current = state.ocrModePicked || (state.config && state.config.ocr_mode) ||
      sel.value || ocrDefaults().ocr_mode;

    var html = '';
    modes.forEach(function (m) {
      html += '<option value="' + escapeHtml(m.id) + '">' +
        escapeHtml(m.label || m.id) + '</option>';
    });
    sel.innerHTML = html;
    sel.value = current;
    if (sel.selectedIndex < 0 && sel.options.length) sel.selectedIndex = 0;
  }

  /** 渲染「识别语言」下拉：已就绪标 ✓，未就绪标「需下载」 */
  function renderOcrLangOptions(languages) {
    var sel = els.ocrLang;
    if (!sel) return;
    var current = state.ocrLangPicked || (state.config && state.config.ocr_lang) ||
      sel.value || ocrDefaults().ocr_lang;

    var html = '';
    languages.forEach(function (l) {
      var ready = l.ready !== false;
      html += '<option value="' + escapeHtml(l.id) + '">' +
        escapeHtml((l.label || l.id) + (ready ? ' ✓' : ' 需下载')) + '</option>';
    });
    sel.innerHTML = html;
    sel.value = current;
    if (sel.selectedIndex < 0 && sel.options.length) sel.selectedIndex = 0;
  }

  /** 取状态里某个语言的信息（接口不可用时返回 null） */
  function ocrLangInfo(lang) {
    var list = (state.ocr && Array.isArray(state.ocr.languages)) ? state.ocr.languages : [];
    var found = null;
    list.forEach(function (l) { if (l.id === lang) found = l; });
    return found;
  }

  /**
   * 该语言模型的下载体积（MB，保留 1 位小数）。
   * 优先用后端给出的真实字节数 download_size；拿不到才退回估算表。
   */
  function ocrLangSizeMB(lang) {
    var info = ocrLangInfo(lang);
    if (info && typeof info.download_size === 'number' && info.download_size > 0) {
      return Math.round(info.download_size / 1024 / 1024 * 10) / 10;
    }
    return OCR_LANG_SIZE_MB[lang] || OCR_LANG_SIZE_DEFAULT_MB;
  }

  /** 选中语言未就绪时，显示提示与「下载模型」按钮 */
  function updateOcrLangHint() {
    if (!els.ocrLangHint) return;
    var lang = els.ocrLang.value || ocrDefaults().ocr_lang;
    var info = ocrLangInfo(lang);
    // 状态未知（接口不可用）时不误报「需下载」
    var ready = !info || info.ready !== false;

    if (ready) {
      hide(els.ocrLangHint);
      return;
    }
    els.ocrLangHintText.textContent =
      '该语言模型未下载（约 ' + ocrLangSizeMB(lang) + ' MB）';
    els.btnOcrDownload.disabled = ocrDownloadRunning();
    show(els.ocrLangHint);
  }

  /** 是否正在下载模型 */
  function ocrDownloadRunning() {
    var download = state.ocr && state.ocr.download;
    return !!(download && download.running);
  }

  /** 渲染顶部引擎状态行（不可用时用警告色显示原因） */
  function renderOcrEngineStatus(data) {
    var el = els.ocrEngineStatus;
    if (!el) return;

    if (!data) {
      el.className = 'ocr-status is-warn';
      el.textContent = '未能获取 OCR 状态，OCR 相关设置可能不生效。';
      return;
    }
    if (data.engine_available === false) {
      el.className = 'ocr-status is-warn';
      el.textContent = 'OCR 引擎不可用：' + (data.engine_reason || '缺少运行依赖');
      return;
    }

    var text = 'OCR 引擎就绪 · 模型目录：' + (data.dir || '未知') +
      ' · 已占用 ' + ocrSizeMB(data.size) + ' MB';
    if (data.ready === false && Array.isArray(data.missing) && data.missing.length) {
      text += '（缺少 ' + data.missing.length + ' 个模型文件）';
    }
    el.className = 'ocr-status is-ok';
    el.textContent = text;
  }

  /** 字节 → MB 文本（保留 1 位小数） */
  function ocrSizeMB(bytes) {
    var n = Number(bytes);
    if (!isFinite(n) || n <= 0) return '0.0';
    return (n / (1024 * 1024)).toFixed(1);
  }

  /** 显示模型下载状态行 */
  function showOcrDownloadState(text, isError) {
    if (!els.ocrDownloadState) return;
    els.ocrDownloadState.textContent = text || '';
    els.ocrDownloadState.className = 'ocr-download-state' + (isError ? ' is-error' : '');
    show(els.ocrDownloadState);
  }

  /** 点击「下载模型」：POST /api/ocr/download 后开始轮询状态 */
  function handleOcrDownload() {
    var cfg = collectOcrConfig();
    els.btnOcrDownload.disabled = true;
    showOcrDownloadState('正在请求下载…');

    api.request('/api/ocr/download', {
      method: 'POST',
      body: { lang: cfg.ocr_lang, det: cfg.ocr_det_version },
      timeout: 30000
    }).then(function (data) {
      showOcrDownloadState((data && data.message) || '已开始下载…');
      startOcrDownloadPolling();
    }).catch(function (err) {
      showOcrDownloadState('下载失败：' + errText(err), true);
      els.btnOcrDownload.disabled = false;
      toast('error', errText(err), 'OCR 模型下载失败');
    });
  }

  /** 开始每 1 秒轮询一次模型下载状态 */
  function startOcrDownloadPolling() {
    if (state.ocrPolling) return;
    state.ocrPolling = true;
    state.ocrPollCount = 0;
    state.ocrPollTimer = setTimeout(ocrDownloadTick, OCR_POLL_INTERVAL);
  }

  /** 停止下载轮询 */
  function stopOcrDownloadPolling() {
    if (state.ocrPollTimer) {
      clearTimeout(state.ocrPollTimer);
      state.ocrPollTimer = null;
    }
    state.ocrPolling = false;
  }

  /** 单次下载状态轮询 */
  function ocrDownloadTick() {
    state.ocrPollTimer = null;
    state.ocrPollCount++;

    if (state.ocrPollCount > OCR_POLL_MAX) {
      stopOcrDownloadPolling();
      showOcrDownloadState('下载状态轮询超时，请稍后刷新页面查看结果。', true);
      if (els.btnOcrDownload) els.btnOcrDownload.disabled = false;
      return;
    }

    var cfg = collectOcrConfig();
    fetchOcrStatus(cfg.ocr_lang, cfg.ocr_det_version).then(function (data) {
      state.ocr = data || state.ocr;
      renderOcrOptions(state.ocr);
      renderOcrEngineStatus(state.ocr);
      updateOcrLangHint();

      var download = (state.ocr && state.ocr.download) || {};
      if (download.running) {
        showOcrDownloadState(download.message || '正在下载模型…');
        state.ocrPollTimer = setTimeout(ocrDownloadTick, OCR_POLL_INTERVAL);
        return;
      }

      stopOcrDownloadPolling();
      if (download.error) {
        showOcrDownloadState('下载失败：' + download.error, true);
        toast('error', download.error, 'OCR 模型下载失败');
      } else if (download.done) {
        showOcrDownloadState(download.message || '下载完成');
        toast('success', 'OCR 模型下载完成', '已就绪');
      } else {
        showOcrDownloadState(download.message || '下载已结束');
      }
      if (els.btnOcrDownload) els.btnOcrDownload.disabled = false;
    }).catch(function () {
      // 轮询失败不打断用户，下一轮继续
      state.ocrPollTimer = setTimeout(ocrDownloadTick, OCR_POLL_INTERVAL);
    });
  }

  /** 试识别按钮与提示的状态（没上传文件时禁用） */
  function updateOcrTestState() {
    if (!els.btnOcrTest) return;
    var hasFile = !!(state.file && state.file.file_id);
    var busy = !!state.ocrTesting;

    els.btnOcrTest.disabled = !hasFile || busy;
    els.btnOcrTest.textContent = busy ? '正在识别…' : '在这一页试识别';
    els.ocrTestPage.disabled = !hasFile || busy;

    if (!hasFile) {
      els.ocrTestHint.textContent = '请先上传 PDF 文件，再使用试识别。';
      els.ocrTestHint.classList.add('is-warn');
      return;
    }

    els.ocrTestHint.textContent = '对已上传文档的指定页做一次识别，用于确认语言与效果。';
    els.ocrTestHint.classList.remove('is-warn');

    var total = totalPages();
    if (total > 0) {
      els.ocrTestPage.max = String(total);
      if ((parseInt(els.ocrTestPage.value, 10) || 1) > total) {
        els.ocrTestPage.value = String(total);
      }
    }
  }

  /** 点击「在这一页试识别」：POST /api/ocr/test */
  function handleOcrTest() {
    if (!state.file || !state.file.file_id) {
      toast('warn', '请先上传一份 PDF 文件。', '无法试识别');
      updateOcrTestState();
      return;
    }

    var cfg = collectOcrConfig();
    var total = totalPages();
    var page = clamp(parseInt(els.ocrTestPage.value, 10) || 1, 1, total || 1);
    els.ocrTestPage.value = String(page);

    state.ocrTesting = true;
    updateOcrTestState();
    hide(els.ocrTestLines);
    showOcrTestResult('pending', '正在识别第 ' + page + ' 页，请稍候…（首次使用需加载模型，可能较慢）');

    api.request('/api/ocr/test', {
      method: 'POST',
      body: {
        file_id: state.file.file_id,
        page: page,
        lang: cfg.ocr_lang,
        dpi: cfg.ocr_dpi
      },
      timeout: 180000
    }).then(function (data) {
      state.ocrTesting = false;
      updateOcrTestState();
      renderOcrTestResult(data);
    }).catch(function (err) {
      state.ocrTesting = false;
      updateOcrTestState();
      hide(els.ocrTestLines);
      showOcrTestResult('bad', '识别失败：' + errText(err));
      toast('error', errText(err), 'OCR 试识别失败');
    });
  }

  /** 试识别结果摘要（复用 .test-result 样式） */
  function showOcrTestResult(kind, text) {
    show(els.ocrTestResult);
    els.ocrTestResult.className = 'test-result ' +
      (kind === 'ok' ? 'test-ok' : kind === 'bad' ? 'test-bad' : 'test-pending');
    els.ocrTestResult.textContent = text;
  }

  /** 渲染试识别结果：摘要 + 逐行文本 */
  function renderOcrTestResult(data) {
    var info = data || {};
    var lines = Array.isArray(info.lines) ? info.lines : [];
    var parts = [
      '第 ' + (info.page != null ? info.page : '?') + ' 页',
      '耗时 ' + (info.elapsed != null ? info.elapsed : '—') + ' 秒',
      '识别 ' + lines.length + ' 行'
    ];
    if (info.lang) parts.push('语言 ' + info.lang);
    var summary = parts.join('　·　');
    if (info.reason) summary += '　判定依据：' + info.reason;

    if (info.needed === false) {
      showOcrTestResult('pending', '这一页有文本层，不需要 OCR。' + summary);
    } else {
      showOcrTestResult('ok', summary);
    }
    renderOcrTestLines(lines);
  }

  /** 逐行渲染识别文本（每行前带两位小数的置信度） */
  function renderOcrTestLines(lines) {
    if (!els.ocrTestLines) return;
    var list = Array.isArray(lines) ? lines : [];

    if (!list.length) {
      els.ocrTestLines.textContent = '（没有识别到任何文本行）';
      show(els.ocrTestLines);
      return;
    }

    var count = Math.min(list.length, OCR_TEST_MAX_LINES);
    var html = '';
    for (var i = 0; i < count; i++) {
      var line = list[i] || {};
      var confidence = Number(line.confidence);
      var confText = isFinite(confidence) ? confidence.toFixed(2) : '--';
      html += '<span class="ocr-line">' +
        '<em class="ocr-line-conf">' + escapeHtml(confText) + '</em>' +
        escapeHtml(line.text || '') + '</span>';
    }
    if (list.length > count) {
      html += '<span class="ocr-line is-more">…… 其余 ' + (list.length - count) + ' 行已省略</span>';
    }

    els.ocrTestLines.innerHTML = html;
    els.ocrTestLines.scrollTop = 0;
    show(els.ocrTestLines);
  }

  /** 绑定 OCR 区块的事件 */
  function bindOcrEvents() {
    if (!els.ocrMode) return;   // 区块未渲染时直接跳过

    els.ocrMode.addEventListener('change', function () {
      state.ocrModePicked = els.ocrMode.value;
      persistDraft();
    });

    els.ocrLang.addEventListener('change', function () {
      state.ocrLangPicked = els.ocrLang.value;
      updateOcrLangHint();
      persistDraft();
      refreshOcrStatus();   // 语言变化会改变模型占用体积与缺件列表
    });

    els.ocrDetVersion.addEventListener('change', function () {
      persistDraft();
      refreshOcrStatus();
    });

    els.ocrDpi.addEventListener('input', persistDraft);
    els.ocrMinConf.addEventListener('input', function () {
      els.ocrMinConfVal.textContent = Number(els.ocrMinConf.value).toFixed(2);
      persistDraft();
    });
    els.ocrUseCls.addEventListener('change', persistDraft);

    els.btnOcrDownload.addEventListener('click', handleOcrDownload);
    els.btnOcrTest.addEventListener('click', handleOcrTest);
    els.ocrTestPage.addEventListener('change', function () {
      var total = totalPages();
      els.ocrTestPage.value = String(
        clamp(parseInt(els.ocrTestPage.value, 10) || 1, 1, total || 1));
    });
  }

  /* ==================================================================== *
   * 08b. 嵌字字体匹配
   *
   * 这里不做任何"猜字体"的逻辑 —— 用哪个中文字体完全由后端
   * （core/fonts.py）按"原文风格 → 本机字体"决定。前端只负责：
   *   1) 把用户的选择与自定义字体路径收进配置；
   *   2) 把后端的匹配结果和候选字体列表显示出来，
   *      让用户不用猜"我的加粗标题为什么没粗"。
   * ==================================================================== */

  /** 嵌字字段的兜底默认值（与后端 DEFAULT_CONFIG 对齐） */
  function letteringDefaults() {
    return {
      lettering_font_family: 'auto',
      lettering_use_system_fonts: true,
      lettering_font_regular: '',
      lettering_font_bold: '',
      lettering_tracking: 0.08
    };
  }

  /** 从表单收集嵌字配置（区块未渲染时回落到默认值） */
  function collectLetteringConfig() {
    var d = letteringDefaults();
    if (!els.letteringFamily) return d;
    return {
      lettering_font_family: els.letteringFamily.value || d.lettering_font_family,
      lettering_use_system_fonts: !!els.letteringUseSystem.checked,
      lettering_font_regular: els.letteringRegular.value.trim(),
      lettering_font_bold: els.letteringBold.value.trim(),
      lettering_tracking: clamp(els.letteringTracking.value, 0, 0.2)
    };
  }

  /** 把嵌字配置回填到表单 */
  function applyLetteringConfigToForm(cfg) {
    if (!els.letteringFamily) return;
    var d = letteringDefaults();
    var c = cfg || {};

    selectValue(els.letteringFamily, c.lettering_font_family || d.lettering_font_family);
    els.letteringUseSystem.checked = c.lettering_use_system_fonts !== false;
    els.letteringRegular.value = c.lettering_font_regular || '';
    els.letteringBold.value = c.lettering_font_bold || '';
    els.letteringTracking.value = String(
      clamp(c.lettering_tracking != null ? c.lettering_tracking : d.lettering_tracking, 0, 0.2));
    els.letteringTrackingVal.textContent = Number(els.letteringTracking.value).toFixed(2);
  }

  /**
   * GET /api/fonts/status —— 拉本机可用中文字体与匹配预览。
   * 失败一律静默降级，不打断用户输入。
   */
  function refreshFontStatus(force) {
    if (!els.letteringStatus) return Promise.resolve(null);
    var cfg = collectLetteringConfig();
    var query = '?target_lang=' + encodeURIComponent(els.targetLang.value || 'zh') +
      '&family=' + encodeURIComponent(cfg.lettering_font_family) +
      '&use_system=' + (cfg.lettering_use_system_fonts ? '1' : '0') +
      '&regular=' + encodeURIComponent(cfg.lettering_font_regular) +
      '&bold=' + encodeURIComponent(cfg.lettering_font_bold) +
      (force ? '&refresh=1' : '');
    return api.request('/api/fonts/status' + query, { timeout: 30000 }).then(function (data) {
      state.fonts = data || {};
      renderLetteringStatus(state.fonts);
      return state.fonts;
    }).catch(function () {
      renderLetteringStatus(null);
      return null;
    });
  }

  /** 渲染"本机字体"状态行 + 匹配预览表 */
  function renderLetteringStatus(data) {
    var info = data || {};
    var faces = Array.isArray(info.faces) ? info.faces : [];
    var usable = faces.filter(function (f) { return f.subset_safe; });

    if (info.error) {
      els.letteringStatus.textContent = '字体检测失败：' + info.error;
      els.letteringStatus.className = 'ocr-status is-warn';
    } else if (!faces.length) {
      els.letteringStatus.textContent =
        '未检测到本机中文字体，将使用 MuPDF 内置字体（没有粗体字重）。';
      els.letteringStatus.className = 'ocr-status is-warn';
    } else {
      els.letteringStatus.textContent =
        '检测到 ' + faces.length + ' 个中文字体，其中 ' + usable.length +
        ' 个可安全嵌入（TrueType）。其余为 CFF/可变字体，会让产物体积膨胀，已自动降权。';
      els.letteringStatus.className = 'ocr-status';
    }

    var preview = Array.isArray(info.preview) ? info.preview : [];
    if (!els.letteringPreview) return;
    if (!preview.length) {
      els.letteringPreview.innerHTML = '<p class="field-hint">暂无匹配结果。</p>';
    } else {
      var rows = preview.map(function (item) {
        var tag = item.faux_bold ? '<em class="tag-warn">伪粗体</em>' : '';
        return '<div class="font-row">' +
          '<span class="font-row-k">' + escapeHtml(item.style) + '</span>' +
          '<span class="font-row-v">' + escapeHtml(item.font || '—') + tag +
          '<small>' + escapeHtml(item.source || '') + '</small></span>' +
          '</div>';
      }).join('');
      els.letteringPreview.innerHTML = rows;
    }
    renderFontList(faces);
  }

  /** 渲染"全部候选字体"清单（默认折叠） */
  function renderFontList(faces) {
    if (!els.letteringFontList) return;
    if (!faces || !faces.length) {
      els.letteringFontList.textContent = '（无）';
      return;
    }
    var lines = faces.map(function (f) {
      var flags = [];
      if (f.bold) flags.push('粗体');
      if (f.serif) flags.push('衬线');
      if (!f.subset_safe) flags.push(f.variable ? '可变字体·不可用' : 'CFF·体积大');
      return '· ' + (f.label || f.name) + '  [' + (f.source || '') +
        (flags.length ? ' / ' + flags.join('·') : '') + ']';
    });
    els.letteringFontList.textContent = lines.join('\n');
  }

  /** 绑定嵌字区块的事件 */
  function bindLetteringEvents() {
    if (!els.letteringFamily) return;   // 区块未渲染时直接跳过

    ['letteringFamily', 'letteringRegular', 'letteringBold'].forEach(function (key) {
      els[key].addEventListener('change', function () {
        persistDraft();
        refreshFontStatus();   // 改了路径/风格，匹配结果会变
      });
    });
    els.letteringUseSystem.addEventListener('change', function () {
      persistDraft();
      refreshFontStatus();
    });
    els.letteringTracking.addEventListener('input', function () {
      els.letteringTrackingVal.textContent = Number(els.letteringTracking.value).toFixed(2);
      persistDraft();
    });
    if (els.btnFontReload) {
      els.btnFontReload.addEventListener('click', function () {
        els.letteringStatus.textContent = '正在重新检测本机字体…';
        refreshFontStatus(true);
      });
    }
    if (els.btnFontList) {
      els.btnFontList.addEventListener('click', function () {
        var pre = els.letteringFontList;
        var hidden = pre.classList.contains('hidden');
        if (hidden) { pre.classList.remove('hidden'); els.btnFontList.textContent = '收起候选'; }
        else { pre.classList.add('hidden'); els.btnFontList.textContent = '查看全部候选'; }
      });
    }
  }

  /* ==================================================================== *
   * 09. 翻译任务
   * ==================================================================== */

  /** 开始翻译 */
  function handleStart() {
    if (!state.file || !state.file.file_id) {
      toast('warn', '请先上传一份 PDF 文件。', '无法开始');
      updateStartButton();
      return;
    }

    var cfg = collectConfig();

    if (providerNeedsKey() && !cfg.api_key) {
      toast('warn', '当前服务商需要 API Key，请填写后再开始。', '缺少 API Key');
      els.apiKey.focus();
      updateStartButton();
      return;
    }

    els.btnStart.disabled = true;
    els.btnStart.textContent = '正在启动…';

    var fileId = state.file.file_id;   // 固定下来，避免中途移除文件导致异常

    // 先把当前设置写入服务端配置，保证温度 / 字号缩放等高级参数生效
    api.saveConfig(cfg).catch(function (err) {
      toast('warn', '设置未能保存到服务端，将按默认配置继续：' + errText(err), '提示');
      return null;
    }).then(function () {
      return api.startTranslate({
        file_id: fileId,
        page_range: cfg.page_range,
        mode: cfg.mode,
        provider: cfg.provider,
        api_key: cfg.api_key,
        base_url: cfg.base_url,
        model: cfg.model,
        target_lang: cfg.target_lang,
        prompt: cfg.prompt,
        glossary: cfg.glossary,
        batch_size: cfg.batch_size,
        concurrency: cfg.concurrency,
        /* OCR 扫描件识别（关闭时后端不会走 OCR 分支） */
        ocr_mode: cfg.ocr_mode,
        ocr_lang: cfg.ocr_lang,
        ocr_dpi: cfg.ocr_dpi,
        ocr_min_confidence: cfg.ocr_min_confidence,
        ocr_det_version: cfg.ocr_det_version,
        ocr_use_cls: cfg.ocr_use_cls,
        export_pdf: true,
        export_markdown: true
      });
    }).then(function (data) {
      els.btnStart.textContent = '开始翻译';
      state.taskId = data.task_id;
      state.taskStatus = 'pending';
      state.taskError = null;
      state.result = null;
      state.logsSignature = '';
      state.pollFailed = 0;

      writeJSON(STORAGE_KEYS.taskId, state.taskId);
      els.taskIdTag.textContent = '任务 ' + state.taskId;
      hide(els.resultCard);
      resetLogs();

      appendLocalLog('任务已创建：共 ' + (data.paragraphs || 0) + ' 段，' + (data.batches || 0) + ' 批。');
      startPolling();
      toast('success', '共 ' + (data.paragraphs || 0) + ' 段，分 ' + (data.batches || 0) + ' 批处理。', '翻译已开始');
    }).catch(function (err) {
      els.btnStart.textContent = '开始翻译';
      updateStartButton();
      toast('error', errText(err), '启动翻译失败');
    });
  }

  /** 取消翻译 */
  function handleCancel() {
    if (!state.taskId) return;
    els.btnCancel.disabled = true;
    els.btnCancel.textContent = '正在取消…';
    api.cancelTranslate(state.taskId).then(function () {
      toast('warn', '已发送取消请求，等待当前批次收尾。', '正在取消');
    }).catch(function (err) {
      els.btnCancel.disabled = false;
      els.btnCancel.textContent = '取消翻译';
      toast('error', errText(err), '取消失败');
    });
  }

  /** 开始轮询进度 */
  function startPolling() {
    stopPolling();
    if (!state.taskId) return;
    state.polling = true;
    state.pollFailed = 0;
    updateStartButton();
    pollOnce();
  }

  /** 停止轮询 */
  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    state.polling = false;
    updateStartButton();
  }

  /** 单次轮询 */
  function pollOnce() {
    if (!state.taskId || !state.polling) return;
    var taskId = state.taskId;

    api.getProgress(taskId).then(function (p) {
      // 任务可能在轮询期间被切换
      if (state.taskId !== taskId) return;
      state.pollFailed = 0;
      renderProgress(p);

      if (TERMINAL_STATUS.indexOf(p.status) >= 0) {
        onTaskFinished(p);
        return;
      }
      state.pollTimer = setTimeout(pollOnce, POLL_INTERVAL);
    }).catch(function (err) {
      if (state.taskId !== taskId) return;
      state.pollFailed++;
      if (state.pollFailed >= POLL_MAX_FAILS) {
        stopPolling();
        appendLocalLog('进度获取连续失败：' + errText(err));
        toast('error', '连续 ' + POLL_MAX_FAILS + ' 次获取进度失败：' + errText(err) +
          ' 请检查本地服务后刷新页面。', '进度中断');
        return;
      }
      state.pollTimer = setTimeout(pollOnce, 2000);
    });
  }

  /** 任务进入终态 */
  function onTaskFinished(p) {
    stopPolling();
    state.taskStatus = p.status;

    els.btnCancel.disabled = true;
    els.btnCancel.textContent = '取消翻译';
    state.translating = false;

    if (p.status === 'done') {
      els.progressBar.classList.add('is-done');
      els.progressStage.textContent = STAGE_TEXT.finished;
      toast('success', '耗时 ' + formatDuration(p.elapsed) + '，可以下载产物了。', '翻译完成');
      // 完成后刷新段落译文、开放译文预览、拉取产物
      loadDocumentInfo();
      updatePreviewControls();
      loadResult();
    } else if (p.status === 'error') {
      els.progressBar.classList.add('is-error');
      els.progressStage.textContent = STATUS_TEXT.error;
      state.taskError = p.error || '翻译过程中出现错误。';
      els.progressMessage.textContent = state.taskError;
      toast('error', state.taskError, '翻译失败');
      appendLocalLog('任务失败：' + state.taskError);
    } else {
      els.progressStage.textContent = STATUS_TEXT.cancelled;
      els.progressMessage.textContent = '任务已取消。';
      toast('warn', '翻译任务已取消。', '已取消');
      appendLocalLog('任务已取消。');
    }
    updateStartButton();
  }

  /** 渲染进度信息 */
  function renderProgress(p) {
    state.taskStatus = p.status;

    var percent = clamp(p.percent, 0, 100);
    var display = Math.round(percent * 10) / 10;
    els.progressBar.style.width = percent + '%';
    els.progressPercent.textContent = display + '%';

    var stage = STAGE_TEXT[p.stage] || p.stage || STATUS_TEXT[p.status] || '处理中';
    if (p.status === 'running' || p.status === 'pending') {
      els.progressStage.textContent = stage;
    } else if (p.status === 'done') {
      els.progressStage.textContent = STAGE_TEXT.finished;
    } else if (p.status === 'error') {
      els.progressStage.textContent = STATUS_TEXT.error;
    } else if (p.status === 'cancelled') {
      els.progressStage.textContent = STATUS_TEXT.cancelled;
    }

    var msg = p.message || '';
    if (p.page_total) {
      msg += (msg ? '　' : '') + '（第 ' + (p.page_current || 0) + '/' + p.page_total + ' 页）';
    }
    els.progressMessage.textContent = msg || '处理中…';

    els.progressParas.textContent = (p.paragraph_done || 0) + ' / ' + (p.paragraph_total || 0);
    els.progressElapsed.textContent = formatDuration(p.elapsed);
    els.progressEta.textContent =
      (p.status === 'running' || p.status === 'pending') ? formatDuration(p.eta) : '--:--';

    renderLogs(p.logs);

    // 运行中：启动按钮禁用、取消按钮可用
    var busy = p.status === 'running' || p.status === 'pending';
    els.btnStart.textContent = busy ? '翻译进行中…' : '开始翻译';
    els.btnCancel.disabled = !busy;
    if (!busy) els.btnCancel.textContent = '取消翻译';
    state.translating = busy;
    updateStartButton();
  }

  /** 渲染日志面板 */
  function renderLogs(logs) {
    var list = Array.isArray(logs) ? logs : [];
    var signature = list.join('\u0000');
    if (signature === state.logsSignature) return;
    state.logsSignature = signature;

    if (!list.length) {
      els.logPanel.innerHTML = '<p class="log-empty">暂无日志</p>';
      return;
    }

    var wasNearBottom = isNearBottom(els.logPanel);
    var html = '';
    list.forEach(function (line) {
      html += '<span class="log-line ' + logLineClass(line) + '">' + escapeHtml(line) + '</span>';
    });
    els.logPanel.innerHTML = html;
    if (wasNearBottom) els.logPanel.scrollTop = els.logPanel.scrollHeight;
  }

  /** 判断日志类名 */
  function logLineClass(line) {
    var s = String(line);
    if (/错误|失败|异常|error|Error|ERROR/.test(s)) return 'is-err';
    if (/警告|warning|WARN/.test(s)) return 'is-warn';
    if (/完成|成功|done|OK/.test(s)) return 'is-ok';
    return '';
  }

  /** 面板是否停留在底部附近 */
  function isNearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  /** 往日志面板追加一条前端产生的日志 */
  function appendLocalLog(text) {
    var stamp = new Date().toTimeString().slice(0, 8);
    var line = '[' + stamp + '] ' + text;
    var empty = els.logPanel.querySelector('.log-empty');
    if (empty) els.logPanel.innerHTML = '';
    var span = document.createElement('span');
    span.className = 'log-line ' + logLineClass(text);
    span.textContent = line;
    els.logPanel.appendChild(span);
    els.logPanel.scrollTop = els.logPanel.scrollHeight;
    state.logsSignature = '';   // 让下一次服务端日志覆盖重绘
  }

  function resetLogs() {
    state.logsSignature = '';
    els.logPanel.innerHTML = '<p class="log-empty">暂无日志</p>';
  }

  /** 清空任务状态 */
  function clearTaskState(keepProgressText) {
    stopPolling();
    state.taskId = null;
    state.taskStatus = null;
    state.taskError = null;
    state.result = null;
    state.translating = false;
    writeJSON(STORAGE_KEYS.taskId, null);
    els.taskIdTag.textContent = '';
    hide(els.resultCard);
    els.btnCancel.disabled = true;
    els.btnCancel.textContent = '取消翻译';

    if (!keepProgressText) {
      els.progressBar.style.width = '0%';
      els.progressBar.classList.remove('is-done', 'is-error');
      els.progressStage.textContent = '等待开始';
      els.progressPercent.textContent = '0%';
      els.progressMessage.textContent = '尚未开始翻译';
      els.progressParas.textContent = '0 / 0';
      els.progressElapsed.textContent = '00:00';
      els.progressEta.textContent = '--:--';
      resetLogs();
    } else {
      hide(els.resultCard);
    }
    updatePreviewControls();
    updateStartButton();
  }

  /** 启动按钮与提示文案 */
  function updateStartButton() {
    if (!els.btnStart) return;
    var busy = state.polling;
    var keyOk = !providerNeedsKey() || !!els.apiKey.value.trim();
    var hasFile = !!(state.file && state.file.file_id);

    var disabled = true;
    var hint = '';
    var warn = false;

    if (!hasFile) {
      disabled = true;
      hint = '请先上传 PDF 文件';
    } else if (!keyOk) {
      disabled = true;
      warn = true;
      hint = '请填写 API Key 后开始翻译';
    } else if (busy) {
      disabled = true;
      hint = '翻译进行中，请稍候…';
    } else {
      disabled = false;
      hint = '准备就绪：' + (state.file.filename || '当前文档') +
        '（共 ' + (state.file.pages || '?') + ' 页）';
    }

    els.btnStart.disabled = disabled;
    els.startHint.textContent = hint;
    els.startHint.classList.toggle('is-warn', warn);
    updateOcrTestState();   // 试识别依赖已上传的文档
  }

  /* ==================================================================== *
   * 10. 原文对照视图
   * ==================================================================== */

  /** 加载文档结构化信息 */
  function loadDocumentInfo() {
    if (!state.file || !state.file.file_id) return Promise.resolve(null);
    var fileId = state.file.file_id;

    return api.getDocumentInfo(fileId).then(function (info) {
      if (!state.file || state.file.file_id !== fileId) return null;
      state.docInfo = info;
      state.paragraphs = Array.isArray(info.paragraphs) ? info.paragraphs : [];
      state.renderedCount = RENDER_STEP;

      renderDocBanner(info);
      renderParagraphs();
      updatePreviewControls();
      return info;
    }).catch(function (err) {
      if (!state.file || state.file.file_id !== fileId) return null;
      // 文件大概率已随服务重启丢失
      state.paragraphs = [];
      renderParagraphs();
      toast('warn', '无法读取文档信息：' + errText(err) + '　可能服务已重启，请重新上传。', '文档不可用');
      return null;
    });
  }

  /** 顶部文档信息条 */
  function renderDocBanner(info) {
    var file = state.file || {};
    var name = info.filename || file.filename || '当前文档';
    var pages = (info.pages && info.pages.length) || file.pages || 0;
    var translated = 0;
    state.paragraphs.forEach(function (p) { if (p.target) translated++; });

    var html = '<span class="doc-name" title="' + escapeHtml(name) + '">' + escapeHtml(name) + '</span>' +
      '<span class="sep">·</span><span>' + pages + ' 页</span>' +
      '<span class="sep">·</span><span>' + state.paragraphs.length + ' 段</span>' +
      '<span class="sep">·</span><span>' + formatBytes(file.size) + '</span>';
    if (translated > 0) {
      html += '<span class="sep">·</span><span>已翻译 ' + translated + ' 段</span>';
    }
    els.docBanner.innerHTML = html;
  }

  /** 过滤后的段落列表 */
  function filteredParagraphs() {
    var kw = state.filter.trim().toLowerCase();
    if (!kw) return state.paragraphs;
    return state.paragraphs.filter(function (p) {
      var src = (p.source || '').toLowerCase();
      var tgt = (p.target || '').toLowerCase();
      return src.indexOf(kw) >= 0 || tgt.indexOf(kw) >= 0;
    });
  }

  /** 渲染段落列表（分段渲染避免卡顿） */
  function renderParagraphs() {
    var list = filteredParagraphs();
    var keyword = state.filter.trim();

    if (!list.length) {
      els.paraList.innerHTML = '<p class="list-empty">' +
        (state.paragraphs.length ? '没有匹配「' + escapeHtml(keyword) + '」的段落。'
          : '还没有解析到段落，或文档为扫描件（无可提取文本）。') + '</p>';
      els.paraStats.textContent = state.paragraphs.length
        ? '匹配 0 / 共 ' + state.paragraphs.length + ' 段'
        : '共 0 段';
      hide(els.btnMoreParas);
      return;
    }

    var count = Math.min(state.renderedCount, list.length);
    var html = '';
    for (var i = 0; i < count; i++) {
      html += paragraphHtml(list[i], keyword);
    }
    els.paraList.innerHTML = html;

    if (list.length > count) {
      show(els.btnMoreParas);
      els.btnMoreParas.textContent = '显示更多段落（已显示 ' + count + ' / ' + list.length + '）';
    } else {
      hide(els.btnMoreParas);
    }

    els.paraStats.textContent = keyword
      ? '匹配 ' + list.length + ' / 共 ' + state.paragraphs.length + ' 段'
      : '共 ' + state.paragraphs.length + ' 段';
  }

  /** 单条段落的 HTML */
  function paragraphHtml(p, keyword) {
    var kindLabel = KIND_TEXT[p.kind] || '段落';
    var pageLabel = p.page != null ? '第 ' + p.page + ' 页' : '—';
    var hasTarget = !!(p.target && String(p.target).trim());

    var html = '<article class="para-item' + (hasTarget ? ' is-translated' : '') + '">' +
      '<div class="para-meta">' +
        '<span class="badge-page">' + escapeHtml(pageLabel) + '</span>' +
        '<span class="badge-kind">' + escapeHtml(kindLabel) + '</span>' +
        (hasTarget ? '<span class="badge-kind">已译</span>' : '<span class="badge-kind">未译</span>') +
      '</div>' +
      '<p class="para-source">' + (highlight(p.source || '（空段落）', keyword)) + '</p>';

    if (hasTarget) {
      html += '<div class="para-target">' +
        '<span class="para-target-label">译文</span>' +
        highlight(p.target, keyword) +
        '</div>';
    }
    html += '</article>';
    return html;
  }

  /* ==================================================================== *
   * 11. 页面预览
   * ==================================================================== */

  /** 总页数 */
  function totalPages() {
    if (state.docInfo && Array.isArray(state.docInfo.pages) && state.docInfo.pages.length) {
      return state.docInfo.pages.length;
    }
    if (state.file && state.file.pages) return state.file.pages;
    return 0;
  }

  /** 切换标签页 */
  function switchTab(name) {
    state.activeTab = name;
    var isOriginal = name === 'original';
    els.tabOriginal.classList.toggle('active', isOriginal);
    els.tabPreview.classList.toggle('active', !isOriginal);
    els.tabOriginal.setAttribute('aria-selected', String(isOriginal));
    els.tabPreview.setAttribute('aria-selected', String(!isOriginal));
    els.panelOriginal.classList.toggle('hidden', !isOriginal);
    els.panelPreview.classList.toggle('hidden', isOriginal);
    if (!isOriginal) renderPreview();
  }

  /** 更新预览控件（页码、译文开关） */
  function updatePreviewControls() {
    var total = totalPages();
    if (total > 0) state.previewPage = clamp(state.previewPage, 1, total);

    els.pageTotal.textContent = total || 1;
    els.pageInput.max = String(total || 1);
    els.pageInput.value = String(state.previewPage || 1);
    els.btnPrevPage.disabled = state.previewPage <= 1;
    els.btnNextPage.disabled = total > 0 && state.previewPage >= total;

    var done = state.taskStatus === 'done';
    var targetBtn = els.previewKindSeg.querySelector('[data-kind="target"]');
    if (targetBtn) targetBtn.disabled = !done;
    if (!done && state.previewKind === 'target') {
      state.previewKind = 'source';
    }
    Array.prototype.forEach.call(els.previewKindSeg.querySelectorAll('.seg-btn'), function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-kind') === state.previewKind);
    });
  }

  /** 渲染当前预览页 */
  function renderPreview() {
    if (!state.file || !state.file.file_id) return;
    var total = totalPages();
    if (total > 0) state.previewPage = clamp(state.previewPage, 1, total);

    updatePreviewControls();

    var fileId = state.file.file_id;
    var page = state.previewPage;
    var isTarget = state.previewKind === 'target';
    var mode = currentMode();

    var url = isTarget
      ? api.outPreviewUrl(fileId, page, mode, state.zoom)
      : api.previewUrl(fileId, page, state.zoom);

    var img = els.previewImg;
    show(els.previewLoading);
    hide(els.previewError);
    hide(img);

    // 令牌用于丢弃过期请求的回调，避免快速翻页时状态错乱
    var token = ++previewToken;

    img.onload = function () {
      if (token !== previewToken) return;
      hide(els.previewLoading);
      show(img);
    };
    img.onerror = function () {
      if (token !== previewToken) return;
      hide(els.previewLoading);
      hide(img);
      show(els.previewError);
      if (isTarget) {
        els.previewErrorText.textContent = '译文预览暂不可用';
        els.previewErrorSub.textContent =
          '第 ' + page + ' 页可能没有译文内容，或该模式下未生成预览。可切换到「原文」查看。';
      } else {
        els.previewErrorText.textContent = '第 ' + page + ' 页预览加载失败';
        els.previewErrorSub.textContent = '请确认本地服务仍在运行，然后点击重试。';
      }
    };

    img.alt = '第 ' + page + ' 页预览';

    if (url === lastPreviewUrl) {
      // 相同的地址（点「重试」）：先清空再重新赋值，强制浏览器重新请求
      img.removeAttribute('src');
      setTimeout(function () {
        if (token === previewToken) img.src = url;
      }, 30);
    } else {
      lastPreviewUrl = url;
      img.src = url;
    }
  }

  /** 跳转到指定页 */
  function gotoPage(page) {
    var total = totalPages();
    var target = clamp(page, 1, total || 1);
    if (target === state.previewPage) {
      updatePreviewControls();
      return;
    }
    state.previewPage = target;
    renderPreview();
  }

  /* ==================================================================== *
   * 12. 翻译产物
   * ==================================================================== */

  /** 拉取并渲染翻译产物 */
  function loadResult() {
    if (!state.taskId) return;
    var taskId = state.taskId;

    api.getResult(taskId).then(function (data) {
      if (state.taskId !== taskId) return;
      state.result = data;
      renderResult(data);
    }).catch(function (err) {
      if (state.taskId !== taskId) return;
      toast('error', '获取产物失败：' + errText(err), '提示');
    });
  }

  /** 渲染产物列表 */
  function renderResult(data) {
    var files = (data && Array.isArray(data.files)) ? data.files : [];
    show(els.resultCard);

    els.resultElapsed.textContent = data && data.elapsed != null
      ? '总耗时 ' + formatDuration(data.elapsed) : '';

    if (!files.length) {
      els.resultFiles.innerHTML = '<p class="stat">本次任务没有生成可下载的文件。</p>';
    } else {
      var html = '';
      files.forEach(function (f) {
        var meta = FILE_KIND_TEXT[f.kind] || { label: '输出文件', icon: 'FILE', cls: '' };
        html += '<div class="result-file">' +
          '<span class="rf-icon ' + meta.cls + '">' + escapeHtml(meta.icon) + '</span>' +
          '<div class="rf-meta">' +
            '<div class="rf-name" title="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) + '</div>' +
            '<div class="rf-sub">' + escapeHtml(meta.label) + ' · ' + formatBytes(f.size) + '</div>' +
          '</div>' +
          '<a class="btn btn-primary btn-sm" href="' + escapeHtml(f.url) + '" download="' +
            escapeHtml(f.name) + '">下载</a>' +
        '</div>';
      });
      els.resultFiles.innerHTML = html;
    }

    var stats = (data && data.stats) || {};
    var parts = [];
    if (stats.paragraphs != null) parts.push('段落 ' + stats.paragraphs);
    if (stats.translated != null) parts.push('已翻译 ' + stats.translated);
    if (stats.failed != null) parts.push('失败 ' + stats.failed);
    if (stats.tokens) parts.push('Token ' + stats.tokens);
    els.resultStats.textContent = parts.join('　·　');
  }

  /* ==================================================================== *
   * 13. 工作区与初始化
   * ==================================================================== */

  function showWorkArea() {
    hide(els.emptyState);
    show(els.workArea);
  }

  function hideWorkArea() {
    hide(els.workArea);
    show(els.emptyState);
  }

  /** 恢复上次的会话（file_id / task_id） */
  function resumeSession() {
    var savedFile = readJSON(STORAGE_KEYS.file);
    var savedTask = readJSON(STORAGE_KEYS.taskId);

    if (savedFile && savedFile.file_id) {
      state.file = savedFile;
      renderFileInfo(savedFile);
      showWorkArea();
      loadDocumentInfo();
    }

    if (savedTask && typeof savedTask === 'string') {
      state.taskId = savedTask;
      els.taskIdTag.textContent = '任务 ' + savedTask;
      resumeTask();
    }

    updateStartButton();
  }

  /** 刷新后继续跟踪未完成的任务 */
  function resumeTask() {
    var taskId = state.taskId;
    appendLocalLog('正在恢复任务 ' + taskId + ' 的进度…');

    api.getProgress(taskId).then(function (p) {
      if (state.taskId !== taskId) return;
      renderProgress(p);
      if (TERMINAL_STATUS.indexOf(p.status) >= 0) {
        onTaskFinished(p);
        toast('warn', '上次的任务已结束（' + (STATUS_TEXT[p.status] || p.status) + '）。', '任务已结束');
      } else {
        toast('warn', '正在继续跟踪上次的翻译任务。', '已恢复任务');
        startPolling();
      }
    }).catch(function (err) {
      if (state.taskId !== taskId) return;
      appendLocalLog('任务恢复失败：' + errText(err));
      toast('warn', '上次的翻译任务已失效（' + errText(err) + '），请重新开始。', '任务不可用');
      clearTaskState(false);
    });
  }

  /* ------------------------------ 事件绑定 ------------------------------ */

  function bindEvents() {
    /* --- 文件选择 / 拖拽 --- */
    els.btnPickFile.addEventListener('click', function (e) {
      e.stopPropagation();
      els.fileInput.click();
    });
    els.dropZone.addEventListener('click', function () { els.fileInput.click(); });
    els.dropZone.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        els.fileInput.click();
      }
    });
    els.fileInput.addEventListener('change', function () {
      var file = els.fileInput.files && els.fileInput.files[0];
      if (file) handleFile(file);
      els.fileInput.value = '';
    });

    ['dragenter', 'dragover'].forEach(function (type) {
      els.dropZone.addEventListener(type, function (e) {
        e.preventDefault();
        e.stopPropagation();
        els.dropZone.classList.add('is-drag');
      });
    });
    ['dragleave', 'drop'].forEach(function (type) {
      els.dropZone.addEventListener(type, function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (type === 'dragleave' && els.dropZone.contains(e.relatedTarget)) return;
        els.dropZone.classList.remove('is-drag');
      });
    });
    els.dropZone.addEventListener('drop', function (e) {
      var dt = e.dataTransfer;
      if (!dt || !dt.files || !dt.files.length) return;
      if (dt.files.length > 1) {
        toast('warn', '一次只能上传一个文件，已取第一个。', '提示');
      }
      handleFile(dt.files[0]);
    });
    // 阻止浏览器在页面其他地方打开被拖入的文件
    window.addEventListener('dragover', function (e) { e.preventDefault(); });
    window.addEventListener('drop', function (e) { e.preventDefault(); });

    els.btnClearFile.addEventListener('click', clearFile);

    /* --- 服务商 / 模型 --- */
    els.providerSelect.addEventListener('change', function () {
      applyProvider(els.providerSelect.value, false);
      state.pendingModel = '';
      persistDraft();
    });
    els.modelSelect.addEventListener('change', function () {
      if (els.modelSelect.value === '__custom__') {
        show(els.modelCustom);
        els.modelCustom.focus();
      } else {
        hide(els.modelCustom);
      }
      persistDraft();
    });

    /* --- API Key 显示 / 隐藏 --- */
    els.btnToggleKey.addEventListener('click', function () {
      var isPwd = els.apiKey.type === 'password';
      els.apiKey.type = isPwd ? 'text' : 'password';
      els.btnToggleKey.textContent = isPwd ? '隐藏' : '显示';
    });

    /* --- 设置项变化：同步标签 + 暂存草稿 + 刷新按钮状态 --- */
    ['concurrency', 'batchSize', 'temperature', 'fontScale'].forEach(function (key) {
      els[key].addEventListener('input', function () {
        syncRangeLabels();
        persistDraft();
      });
    });

    els.glossary.addEventListener('input', function () {
      updateGlossaryHint();
      persistDraft();
    });

    ['baseUrl', 'modelCustom', 'prompt', 'pageRange'].forEach(function (key) {
      els[key].addEventListener('input', persistDraft);
    });
    els.targetLang.addEventListener('change', persistDraft);
    els.modeGroup.addEventListener('change', persistDraft);

    els.apiKey.addEventListener('input', function () {
      persistDraft();
      updateStartButton();
    });

    els.btnSaveConfig.addEventListener('click', function () { saveConfigAsDefault(false); });
    els.btnTestConn.addEventListener('click', testConnection);

    /* --- OCR 扫描件识别 --- */
    bindOcrEvents();

    /* --- 嵌字字体匹配 --- */
    bindLetteringEvents();

    /* --- 翻译操作 --- */
    els.btnStart.addEventListener('click', handleStart);
    els.btnCancel.addEventListener('click', handleCancel);
    els.btnClearLogs.addEventListener('click', resetLogs);

    /* --- 标签页 --- */
    els.tabOriginal.addEventListener('click', function () { switchTab('original'); });
    els.tabPreview.addEventListener('click', function () { switchTab('preview'); });

    /* --- 段落搜索 / 加载更多 --- */
    els.paraSearch.addEventListener('input', debounce(function () {
      state.filter = els.paraSearch.value;
      state.renderedCount = RENDER_STEP;
      renderParagraphs();
    }, 200));

    els.btnMoreParas.addEventListener('click', function () {
      state.renderedCount += RENDER_STEP;
      renderParagraphs();
    });

    /* --- 预览操作 --- */
    els.btnPrevPage.addEventListener('click', function () { gotoPage(state.previewPage - 1); });
    els.btnNextPage.addEventListener('click', function () { gotoPage(state.previewPage + 1); });
    els.pageInput.addEventListener('change', function () {
      gotoPage(parseInt(els.pageInput.value, 10) || 1);
    });
    els.zoomSelect.addEventListener('change', function () {
      state.zoom = parseFloat(els.zoomSelect.value) || 1.4;
      renderPreview();
    });
    els.previewKindSeg.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.seg-btn') : null;
      if (!btn || btn.disabled) return;
      var kind = btn.getAttribute('data-kind');
      if (kind === state.previewKind) return;
      state.previewKind = kind;
      renderPreview();
    });
    els.btnPreviewRetry.addEventListener('click', renderPreview);

    /* --- 系统信息弹窗 --- */
    els.btnSysInfo.addEventListener('click', openSysModal);
    els.btnSysClose.addEventListener('click', closeSysModal);
    els.sysModal.addEventListener('click', function (e) {
      if (e.target === els.sysModal) closeSysModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !els.sysModal.classList.contains('hidden')) closeSysModal();
    });

    /* --- 关闭页面提醒 --- */
    window.addEventListener('beforeunload', function (e) {
      if (!state.polling) return;
      e.preventDefault();
      e.returnValue = '翻译正在进行中，离开页面会中断进度显示。';
      return e.returnValue;
    });
  }

  /* ------------------------------ 启动 ------------------------------ */

  function init() {
    cacheDom();
    bindEvents();
    syncRangeLabels();
    updatePreviewControls();
    updateStartButton();

    // 系统信息（顶部状态）
    loadSystemInfo().catch(function () { /* 已在内部提示 */ });

    // OCR 状态（引擎可用性 / 语言就绪情况），失败静默降级
    initOcr();

    // 本机中文字体（嵌字用），失败静默降级
    refreshFontStatus();

    // 服务商 → 配置。配置要在服务商之后应用，才能正确回填模型下拉
    initProviders().then(function () {
      return api.getConfig().then(function (data) {
        state.config = (data && data.config) || {};
        state.defaults = (data && data.defaults) || {};
        state.pendingModel = state.config.model || '';
        // 此时才知道配置里的服务商与语言，重新渲染一次下拉以便正确选中
        renderProviderOptions();
        renderLanguageOptions();
        applyConfigToForm(state.config);
        loadDraft();
        updateStartButton();
        // 配置里可能带了自定义字体路径 / 风格，重新匹配一次
        refreshFontStatus();
      });
    }).catch(function (err) {
      toast('error', '无法读取服务端配置：' + errText(err), '配置加载失败');
    });

    // 恢复上次会话
    resumeSession();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
