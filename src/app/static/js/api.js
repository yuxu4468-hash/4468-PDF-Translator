/* ==========================================================================
 * api.js —— 后端 HTTP 接口统一封装
 * --------------------------------------------------------------------------
 * 契约来源：docs/API.md
 * 统一约定：
 *   - 所有接口返回 { ok: true, data: {...} } 或 { ok: false, error: "..." }
 *   - 本模块把 data 直接 resolve 出去；ok=false / HTTP 异常统一抛出 ApiError
 *   - 所有路径都使用相对根路径（同源），因此 base 为空字符串
 * 对外暴露：window.api，以及 window.ApiError
 * ========================================================================== */
(function (global) {
  'use strict';

  /** 请求超时（毫秒）。上传与翻译任务另有处理。 */
  var DEFAULT_TIMEOUT = 30000;

  /** 统一的接口错误对象，message 为可读中文。 */
  function ApiError(message, detail) {
    var err = Error.call(this, message);
    this.name = 'ApiError';
    this.message = message || '请求失败';
    this.detail = detail || null;
    this.stack = err.stack;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  /** 把异常的消息拼成可读文本。 */
  function readableMessage(err) {
    if (!err) return '未知错误';
    if (typeof err === 'string') return err;
    if (err.message) return err.message;
    return String(err);
  }

  /**
   * 统一的 fetch 封装：解析统一响应结构，失败时抛 ApiError。
   * @param {string} path    例如 '/api/config'
   * @param {object} [options] { method, body, timeout, headers }
   * @returns {Promise<any>} resolve 响应里的 data 字段
   */
  function request(path, options) {
    var opts = options || {};
    var method = opts.method || 'GET';
    var timeout = typeof opts.timeout === 'number' ? opts.timeout : DEFAULT_TIMEOUT;

    var init = {
      method: method,
      headers: Object.assign({ 'Accept': 'application/json' }, opts.headers || {}),
      cache: 'no-store'
    };

    if (opts.body !== undefined && opts.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    var controller = null;
    var timer = null;
    if (timeout > 0 && typeof AbortController !== 'undefined') {
      controller = new AbortController();
      init.signal = controller.signal;
      timer = setTimeout(function () { controller.abort(); }, timeout);
    }

    return fetch(path, init)
      .catch(function (err) {
        // 网络层失败 / 超时：给出更友好的中文提示
        if (err && err.name === 'AbortError') {
          throw new ApiError('请求超时（' + Math.round(timeout / 1000) + ' 秒），请检查本地服务或网络。');
        }
        throw new ApiError('无法连接本地服务，请确认后端已启动。', readableMessage(err));
      })
      .then(function (resp) {
        if (timer) { clearTimeout(timer); timer = null; }

        return resp.text().then(function (text) {
          var payload = null;
          if (text) {
            try { payload = JSON.parse(text); } catch (e) { payload = null; }
          }

          if (!payload) {
            if (!resp.ok) {
              throw new ApiError('服务返回异常状态（HTTP ' + resp.status + '）。');
            }
            throw new ApiError('服务返回了非 JSON 数据，无法解析。');
          }

          if (payload.ok === false) {
            throw new ApiError(payload.error || '操作失败（HTTP ' + resp.status + '）。');
          }
          if (!resp.ok) {
            throw new ApiError('操作失败（HTTP ' + resp.status + '）。');
          }
          return payload.data;
        });
      })
      .catch(function (err) {
        if (timer) { clearTimeout(timer); timer = null; }
        throw (err instanceof ApiError) ? err : new ApiError(readableMessage(err));
      });
  }

  /* ------------------------------------------------------------------ *
   * 配置 / 服务商
   * ------------------------------------------------------------------ */

  /** GET /api/config → { config, defaults } */
  function getConfig() {
    return request('/api/config');
  }

  /** POST /api/config —— 传配置字段子集，返回 { config, ... } */
  function saveConfig(partial) {
    return request('/api/config', { method: 'POST', body: partial || {} });
  }

  /** GET /api/providers → { providers: [...], languages: [...] } */
  function getProviders() {
    return request('/api/providers');
  }

  /** POST /api/test-connection → { latency_ms, reply, model } */
  function testConnection(payload) {
    return request('/api/test-connection', {
      method: 'POST',
      body: payload || {},
      timeout: 60000
    });
  }

  /* ------------------------------------------------------------------ *
   * 文档：上传 / 信息 / 预览图
   * ------------------------------------------------------------------ */

  /**
   * POST /api/upload —— 使用 XHR 以便拿到真实上传进度。
   * @param {File} file
   * @param {function} [onProgress] 回调 ({ loaded, total, percent })
   * @returns {Promise<object>} { file_id, filename, pages, size, encrypted, text_pages, message }
   */
  function upload(file, onProgress) {
    return new Promise(function (resolve, reject) {
      if (!file) {
        reject(new ApiError('请先选择 PDF 文件。'));
        return;
      }

      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload', true);
      xhr.responseType = 'text';

      xhr.upload.onprogress = function (e) {
        if (typeof onProgress !== 'function') return;
        if (e.lengthComputable) {
          var percent = e.total > 0 ? Math.round((e.loaded / e.total) * 100) : 0;
          onProgress({ loaded: e.loaded, total: e.total, percent: percent, done: false });
        } else {
          onProgress({ loaded: e.loaded, total: 0, percent: 0, done: false });
        }
      };

      xhr.upload.onload = function () {
        if (typeof onProgress === 'function') {
          onProgress({ loaded: 0, total: 0, percent: 100, done: true });
        }
      };

      xhr.onload = function () {
        var payload = null;
        try { payload = JSON.parse(xhr.responseText); } catch (e) { payload = null; }

        if (!payload) {
          reject(new ApiError('上传失败：服务返回了非 JSON 数据（HTTP ' + xhr.status + '）。'));
          return;
        }
        if (payload.ok === false) {
          reject(new ApiError(payload.error || '上传失败。'));
          return;
        }
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new ApiError('上传失败（HTTP ' + xhr.status + '）。'));
          return;
        }
        resolve(payload.data);
      };

      xhr.onerror = function () {
        reject(new ApiError('上传失败：无法连接本地服务。'));
      };
      xhr.ontimeout = function () {
        reject(new ApiError('上传超时，请重试。'));
      };
      xhr.onabort = function () {
        reject(new ApiError('上传已取消。'));
      };

      var form = new FormData();
      form.append('file', file, file.name);
      xhr.send(form);
    });
  }

  /** GET /api/document/<file_id>/info → 文档结构化信息 */
  function getDocumentInfo(fileId) {
    return request('/api/document/' + encodeURIComponent(fileId) + '/info');
  }

  /** 原始页面预览图地址（page 从 1 开始，zoom 0.5~3.0） */
  function previewUrl(fileId, page, zoom) {
    var z = typeof zoom === 'number' ? zoom : 1.4;
    return '/api/preview/' + encodeURIComponent(fileId) + '/' + encodeURIComponent(page) +
      '?zoom=' + encodeURIComponent(z);
  }

  /** 译后页面预览图地址（kind: 'mono' | 'dual'） */
  function outPreviewUrl(fileId, page, kind, zoom) {
    var z = typeof zoom === 'number' ? zoom : 1.4;
    var url = '/api/outpreview/' + encodeURIComponent(fileId) + '/' + encodeURIComponent(page) +
      '?zoom=' + encodeURIComponent(z);
    if (kind) { url += '&kind=' + encodeURIComponent(kind); }
    return url;
  }

  /* ------------------------------------------------------------------ *
   * 翻译任务
   * ------------------------------------------------------------------ */

  /** POST /api/translate/start → { task_id, paragraphs, batches } */
  function startTranslate(payload) {
    return request('/api/translate/start', {
      method: 'POST',
      body: payload || {},
      timeout: 120000
    });
  }

  /** GET /api/translate/progress/<task_id> → 进度对象 */
  function getProgress(taskId) {
    return request('/api/translate/progress/' + encodeURIComponent(taskId), { timeout: 15000 });
  }

  /** POST /api/translate/cancel/<task_id> → { status: 'cancelling' } */
  function cancelTranslate(taskId) {
    return request('/api/translate/cancel/' + encodeURIComponent(taskId), { method: 'POST' });
  }

  /** DELETE /api/translate/task/<task_id> —— 删除任务与产物 */
  function deleteTask(taskId) {
    return request('/api/translate/task/' + encodeURIComponent(taskId), { method: 'DELETE' });
  }

  /** GET /api/translate/result/<task_id> → { status, elapsed, files, stats } */
  function getResult(taskId) {
    return request('/api/translate/result/' + encodeURIComponent(taskId));
  }

  /* ------------------------------------------------------------------ *
   * 系统与历史
   * ------------------------------------------------------------------ */

  /** GET /api/system/info → 运行时环境信息 */
  function getSystemInfo() {
    return request('/api/system/info', { timeout: 15000 });
  }

  /** GET /api/history?limit=n → { items: [...] } */
  function getHistory(limit) {
    return request('/api/history?limit=' + encodeURIComponent(limit || 20));
  }

  /** GET /api/logs?lines=n → { text } */
  function getLogs(lines) {
    return request('/api/logs?lines=' + encodeURIComponent(lines || 200));
  }

  /* ------------------------------------------------------------------ *
   * 导出
   * ------------------------------------------------------------------ */
  global.ApiError = ApiError;
  global.api = {
    request: request,
    // 配置 / 服务商
    getConfig: getConfig,
    saveConfig: saveConfig,
    getProviders: getProviders,
    testConnection: testConnection,
    // 文档
    upload: upload,
    getDocumentInfo: getDocumentInfo,
    previewUrl: previewUrl,
    outPreviewUrl: outPreviewUrl,
    // 翻译任务
    startTranslate: startTranslate,
    getProgress: getProgress,
    cancelTranslate: cancelTranslate,
    deleteTask: deleteTask,
    getResult: getResult,
    // 系统
    getSystemInfo: getSystemInfo,
    getHistory: getHistory,
    getLogs: getLogs
  };
})(window);
