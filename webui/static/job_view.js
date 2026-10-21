/* Job view client: SSE progress stream + diff/structure panes.
   Bootstrapped from window.APP.jobData (set by job_view.html) and localizes
   dynamic text through window.APP.i18n (the full token table for the locale). */
(function () {
  'use strict';

  var APP = window.APP || {};
  var data = APP.jobData || {};

  function i18n(key) {
    return (APP.i18n && APP.i18n[key]) || key;
  }

  function fmt(key, vars) {
    var s = i18n(key);
    vars = vars || {};
    Object.keys(vars).forEach(function (k) {
      s = s.split('{' + k + '}').join(String(vars[k]));
    });
    return s;
  }

  var logEl = document.getElementById('log');
  // Display cap: the <pre> keeps only the newest lines (the on-disk .log file
  // stays complete; the stream also sends just the tail on connect).
  var MAX_LOG_LINES = 1000;
  var logLines = [];

  var logDirty = false;
  var logRaf = null;

  function flushLog() {
    if (!logEl) { logDirty = false; return; }
    logEl.textContent = logLines.join('\n') + '\n';
    logEl.scrollTop = logEl.scrollHeight;
    logDirty = false;
  }

  function appendLog(text) {
    if (!logEl) return;
    var parts = String(text == null ? '' : text).split('\n');
    for (var i = 0; i < parts.length; i++) logLines.push(parts[i]);
    if (logLines.length > MAX_LOG_LINES) {
      logLines.splice(0, logLines.length - MAX_LOG_LINES);
    }
    if (!logDirty) {
      logDirty = true;
      logRaf = requestAnimationFrame(flushLog);
    }
  }
  var progEl = document.getElementById('progress');
  var connEl = document.getElementById('conn');
  // Older cached job_view.html templates lack these two elements. Create/find
  // them so the reconnect notice and live status badge still work after a
  // no-restart static-asset update.
  if (!connEl && progEl && progEl.parentNode) {
    connEl = document.createElement('p');
    connEl.id = 'conn';
    connEl.className = 'muted small';
    connEl.setAttribute('role', 'status');
    connEl.setAttribute('aria-live', 'polite');
    connEl.hidden = true;
    progEl.parentNode.insertBefore(connEl, progEl.nextSibling);
  }
  var statusEl = document.getElementById('jobStatusBadge') ||
    document.querySelector('.page-title .badge');
  var barEl = document.getElementById('bar');
  var apiEl = document.getElementById('api');
  var subEl = document.getElementById('subprog');
  var structView = document.getElementById('structView');
  var diffView = document.getElementById('diffView');
  var diffWrap = document.getElementById('diffWrap');
  var diffSource = document.getElementById('diffSource');
  var diffTrans = document.getElementById('diffTrans');
  var TERMINAL = ['done', 'error', 'stopped', 'interrupted'];
  var lastProgress = null;
  var lastError = null;
  var lastRulesCompleted = null;
  var pollTimer = null;
  var pollDelay = 2000;
  var es = null;
  var currentStatus = data.status || null;
  var lastMessageAt = Date.now();

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function dot(cls) {
    var d = document.createElement('span');
    d.className = 'k-dot ' + cls;
    return d;
  }

  function isTerminal(status) {
    return TERMINAL.indexOf(status) !== -1;
  }

  function setConn(text) {
    if (!connEl) return;
    connEl.textContent = text || '';
    connEl.hidden = !text;
  }

  function setStatusBadge(status) {
    if (!statusEl || !status) return;
    statusEl.className = 'badge ' + status;
    statusEl.textContent = '';
    var d = document.createElement('span');
    d.className = 'status-dot';
    statusEl.appendChild(d);
    statusEl.appendChild(document.createTextNode(' ' + i18n('job.status.' + status)));
  }

  function renderProgress(p) {
    if (!p || !p.total_chapters) {
      // A resume briefly clears progress; keep the last known values visible
      // instead of flashing "Initializing..." over a working monitor.
      if (progEl && !lastProgress) progEl.textContent = i18n('job.view.initializing');
      return;
    }
    lastProgress = p;
    var cur = p.current_chapter || p.last_completed || '?';
    var total = p.total_chapters;
    var done = p.last_completed || 0;
    progEl.innerHTML = esc(i18n('job.view.chapter_word')) + ' <b>' + esc(cur) + '</b> / ' + esc(total) +
      (p.current_title ? ' — <i>' + esc(p.current_title) + '</i>' : '');
    var pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
    barEl.style.width = pct + '%';
    var barBox = barEl.parentNode;
    if (barBox) barBox.setAttribute('aria-valuenow', String(pct));
    renderMonitor(p);
  }

  function renderMonitor(p) {
    var api = p && p.api;
    if (api && apiEl) {
      apiEl.textContent = fmt('job.view.api_stats', {
        calls: api.calls,
        total: (api.total_ms / 1000).toFixed(1),
        last: Math.round(api.last_ms),
        avg: Math.round(api.avg_ms)
      });
    }
    if (p && p.chunk_total && subEl) {
      subEl.textContent = fmt('job.view.chunk_prog', {
        index: p.chunk_index || '?',
        total: p.chunk_total,
        chapter: p.current_chapter || '?'
      });
      var cc0 = p.current_chunk;
      if (cc0 && cc0.status === 'retry') {
        subEl.appendChild(dot('warn'));
        subEl.appendChild(document.createTextNode(fmt('job.view.retry', { attempt: cc0.attempt, error: cc0.error })));
      } else if (cc0 && cc0.status === 'failed') {
        subEl.appendChild(dot('err'));
        subEl.appendChild(document.createTextNode(i18n('job.view.failed_fallback')));
      }
    }
    var cc = p && p.current_chunk;
    if (cc && diffSource) diffSource.textContent = cc.source || '';
    if (cc && diffTrans) diffTrans.textContent = cc.translated || '';
    renderDiff(p);
    renderStruct(p);
  }

  function renderDiff(p) {
    var cc = p && p.current_chunk;
    if (!cc || !cc.diff || !diffView) return;
    var d = cc.diff;
    var view = diffView;
    view.textContent = '';
    var stats = document.createElement('div');
    stats.className = 'diff-key';
    stats.appendChild(document.createTextNode(fmt('job.view.diff_stats', {
      add: d.added_words, removed: d.removed_words
    })));
    stats.appendChild(dot('ok'));
    stats.appendChild(document.createTextNode(i18n('job.view.diff_add')));
    stats.appendChild(dot('err'));
    stats.appendChild(document.createTextNode(i18n('job.view.diff_rem')));
    stats.appendChild(dot('eq'));
    stats.appendChild(document.createTextNode(i18n('job.view.diff_keep')));
    view.appendChild(stats);
    for (var i = 0; i < d.lines.length; i++) {
      var line = d.lines[i];
      var row = document.createElement('div');
      row.className = line.kind; // equal | del | ins | change
      var parts = line.parts || [];
      for (var j = 0; j < parts.length; j++) {
        var part = parts[j];
        var span = document.createElement('span');
        span.className = part.op; // eq | del | ins
        span.textContent = part.text;
        row.appendChild(span);
      }
      view.appendChild(row);
    }
    // Auto-scroll only if the user is already near the bottom, so inspecting
    // history with the scrollbar isn't yanked back down.
    var nearBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
    if (nearBottom) view.scrollTop = view.scrollHeight;
  }

  function renderStruct(p) {
    var cc = p && p.current_chunk;
    var st = cc && cc.structure;
    if (!structView) return;
    if (!st) { structView.textContent = ''; return; }
    var view = structView;
    view.textContent = '';
    var summary = document.createElement('div');
    summary.className = st.same ? 'ok-line' : 'warn-line';
    summary.appendChild(dot(st.same ? 'ok' : 'err'));
    summary.appendChild(document.createTextNode(st.same
      ? fmt('job.view.struct_ok', { src_tags: st.source_tags, spans: st.coverage.total_source })
      : fmt('job.view.struct_diff', {
          src_tags: st.source_tags,
          src_spans: st.coverage.total_source,
          tr_tags: st.translated_tags,
          tr_spans: st.coverage.total_translated
        })));
    view.appendChild(summary);
    if (cc && cc.status === 'retry') {
      var r = document.createElement('div');
      r.className = 'yellow';
      r.appendChild(dot('warn'));
      r.appendChild(document.createTextNode(fmt('job.view.retry', { attempt: cc.attempt, error: cc.error })));
      view.appendChild(r);
    }
    if (cc && cc.status === 'failed') {
      var f = document.createElement('div');
      f.className = 'warn-line';
      f.appendChild(dot('err'));
      f.appendChild(document.createTextNode(fmt('job.view.failed_giveup', { attempt: cc.attempt, error: cc.error })));
      view.appendChild(f);
    }
    if (st.coverage.missing.length) {
      var m = document.createElement('div');
      m.className = 'warn-line';
      m.appendChild(dot('err'));
      m.appendChild(document.createTextNode(fmt('job.view.missing_span', { ids: st.coverage.missing.join(', ') })));
      view.appendChild(m);
    }
    if (st.coverage.extra.length) {
      var e = document.createElement('div');
      e.className = 'yellow';
      e.appendChild(dot('warn'));
      e.appendChild(document.createTextNode(fmt('job.view.extra_span', { ids: st.coverage.extra.join(', ') })));
      view.appendChild(e);
    }
    var tagDiff = st.tag_diff || [];
    for (var i = 0; i < tagDiff.length; i++) {
      var td = tagDiff[i];
      var row = document.createElement('div');
      row.className = td.op; // del -> red (tag dropped), ins -> green (tag added)
      row.appendChild(dot(td.op === 'del' ? 'err' : 'ok'));
      row.appendChild(document.createTextNode(' ' + td.tag));
      view.appendChild(row);
    }
    var nearBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
    if (nearBottom) view.scrollTop = view.scrollHeight;
  }

  // ── Rules view ─────────────────────────────────────────────────
  var rulesContent = document.getElementById('rulesContent');
  var rulesMeta = document.getElementById('rulesMeta');
  var rulesLoaded = false;
  var rulesChapter = null;   // null → every chapter (payload can be 1MB+)

  function fetchRules() {
    if (!data.id || rulesLoaded) return;
    var url = '/api/jobs/' + data.id + '/rules';
    if (rulesChapter !== null) url += '?chapter=' + encodeURIComponent(rulesChapter);
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var rules = JSON.parse(xhr.responseText);
          renderRules(rules);
          rulesLoaded = true;
        } catch (e) {}
      }
    };
    xhr.send();
  }

  // Re-fetch with (or without) a chapter scope: characters that chapter's
  // analysis named + that one analysis, instead of the whole dictionary.
  function setRulesChapter(chapter) {
    rulesChapter = chapter;
    rulesLoaded = false;
    var input = document.getElementById('rulesChapter');
    if (input) input.value = chapter === null ? '' : String(chapter);
    fetchRules();
  }

  function renderRules(rules) {
    if (!rulesContent) return;
    rulesContent.textContent = '';

    var chars = rules.characters || {};
    var narration = rules.narration_defaults || {};
    var fallback = rules.fallback || '';
    var analyses = rules.analyses || {};
    var meta = rules.meta || {};

    if (rulesMeta) {
      var bits = [];
      if (meta.chapter !== null && meta.chapter !== undefined) {
        bits.push('chapter ' + meta.chapter);
      }
      if (meta.returned_characters !== undefined) {
        bits.push(meta.returned_characters + '/' + meta.total_characters + ' chars');
      }
      if (meta.chapter && meta.has_analysis === false) bits.push('no analysis yet');
      rulesMeta.textContent = bits.join(' · ');
    }

    // Empty state
    if (Object.keys(chars).length === 0) {
      var empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = (rulesChapter !== null)
        ? 'No rules recorded for chapter ' + rulesChapter + '.'
        : 'No rules yet — rules are generated as chapters are translated.';
      rulesContent.appendChild(empty);
      return;
    }

    // Characters section
    var charsTitle = document.createElement('h3');
    charsTitle.textContent = 'Characters';
    rulesContent.appendChild(charsTitle);

    var charList = document.createElement('div');
    charList.className = 'rules-chars';
    Object.keys(chars).forEach(function (name) {
      var entry = chars[name];
      var card = document.createElement('div');
      card.className = 'rules-char-card';

      var nameEl = document.createElement('div');
      nameEl.className = 'rules-char-name';
      nameEl.textContent = name;
      card.appendChild(nameEl);

      if (entry.names && entry.names.length > 1) {
        var aliases = document.createElement('div');
        aliases.className = 'muted small';
        aliases.textContent = 'Aliases: ' + entry.names.join(', ');
        card.appendChild(aliases);
      }

      if (entry.calls && Object.keys(entry.calls).length > 0) {
        var callsEl = document.createElement('div');
        callsEl.className = 'rules-calls';
        Object.keys(entry.calls).forEach(function (target) {
          var row = document.createElement('div');
          row.className = 'rules-call-row';
          row.innerHTML = esc(name) + ' → <b>' + esc(target) + '</b>: ' + esc(entry.calls[target]);
          callsEl.appendChild(row);
        });
        card.appendChild(callsEl);
      }

      charList.appendChild(card);
    });
    rulesContent.appendChild(charList);

    // Narration defaults
    if (Object.keys(narration).length > 0) {
      var narrTitle = document.createElement('h3');
      narrTitle.textContent = 'Narration Defaults';
      rulesContent.appendChild(narrTitle);

      var narrList = document.createElement('div');
      narrList.className = 'rules-narr';
      Object.keys(narration).forEach(function (ctx) {
        var row = document.createElement('div');
        row.className = 'rules-narr-row';
        row.innerHTML = esc(ctx) + ' → ' + esc(narration[ctx]);
        narrList.appendChild(row);
      });
      rulesContent.appendChild(narrList);
    }

    // Fallback
    if (fallback) {
      var fbTitle = document.createElement('h3');
      fbTitle.textContent = 'Default Fallback';
      rulesContent.appendChild(fbTitle);
      var fbEl = document.createElement('div');
      fbEl.className = 'rules-fallback';
      fbEl.textContent = fallback;
      rulesContent.appendChild(fbEl);
    }

    // Chapter analyses (collapsible)
    var chapterKeys = Object.keys(analyses).sort(function (a, b) { return Number(a) - Number(b); });
    if (chapterKeys.length > 0) {
      var analTitle = document.createElement('h3');
      analTitle.textContent = 'Chapter Analyses (' + chapterKeys.length + ')';
      rulesContent.appendChild(analTitle);

      chapterKeys.forEach(function (ch) {
        var details = document.createElement('details');
        details.className = 'rules-analysis';

        var summary = document.createElement('summary');
        summary.textContent = 'Chapter ' + ch;
        details.appendChild(summary);

        var pre = document.createElement('pre');
        pre.className = 'log';
        pre.textContent = analyses[ch];
        details.appendChild(pre);

        rulesContent.appendChild(details);
      });
    }
  }

  var VIEWS = { struct: 'structView', diff: 'diffView', side: 'diffWrap', rules: 'rulesView' };
  var BUTTONS = { struct: 'btnStruct', diff: 'btnDiff', side: 'btnSide', rules: 'btnRules' };

  function show(name) {
    Object.keys(VIEWS).forEach(function (viewName) {
      var el = document.getElementById(VIEWS[viewName]);
      if (el) el.hidden = viewName !== name;
    });
    Object.keys(BUTTONS).forEach(function (btnName) {
      var el = document.getElementById(BUTTONS[btnName]);
      if (el) el.setAttribute('aria-pressed', String(btnName === name));
    });
  }

  if (document.getElementById('btnStruct')) {
    document.getElementById('btnStruct').addEventListener('click', function () { show('struct'); });
    document.getElementById('btnDiff').addEventListener('click', function () { show('diff'); });
    document.getElementById('btnSide').addEventListener('click', function () { show('side'); });
    document.getElementById('btnRules').addEventListener('click', function () {
      show('rules');
      fetchRules();
    });
  }

  if (document.getElementById('rulesFilter')) {
    var rulesChapterInput = document.getElementById('rulesChapter');
    document.getElementById('rulesFilter').addEventListener('click', function () {
      var value = parseInt(rulesChapterInput.value, 10);
      setRulesChapter(isNaN(value) || value < 1 ? null : value);
    });
    document.getElementById('rulesAll').addEventListener('click', function () {
      setRulesChapter(null);
    });
    rulesChapterInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') document.getElementById('rulesFilter').click();
    });
  }

  function applyProgress(msg) {
    if (!msg) return;
    if (msg.progress) renderProgress(msg.progress);
    if (msg.status) {
      currentStatus = msg.status;
      setStatusBadge(msg.status);
    }

    // Refresh rules only when a new chapter actually completes; the SSE stream
    // emits progress every tick, and refetching the (possibly 1MB) rules table
    // on every tick would compete with the stream itself.
    var p = msg.progress || {};
    if (p.last_completed && p.last_completed !== lastRulesCompleted) {
      lastRulesCompleted = p.last_completed;
      rulesLoaded = false;
      var rulesView = document.getElementById('rulesView');
      if (rulesView && !rulesView.hidden) fetchRules();
    }

    if (isTerminal(msg.status)) {
      if (msg.error && msg.error !== lastError) {
        lastError = msg.error;
        appendLog('\n' + i18n('job.view.error_line') + msg.error + '\n');
      }
      stopPolling();
      setConn(null);
    }
  }

  function schedulePoll(delay) {
    if (pollTimer || !data.id) return;
    pollTimer = setTimeout(function () {
      pollTimer = null;
      fetchState();
    }, delay);
  }

  function fetchStateFromStream() {
    // One-shot read of the SSE endpoint for older servers that predate
    // /api/jobs/<id>/state. Abort as soon as the first progress event arrives;
    // this is a short request, not a permanently held connection.
    if (!data.streamUrl) return Promise.reject(new Error('no stream'));
    return fetch(data.streamUrl, { cache: 'no-store' })
      .then(function (resp) {
        if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buf = '';
        function finish(result) {
          try { reader.cancel(); } catch (e) { /* already closed */ }
          return result;
        }
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) throw new Error('stream ended');
            buf += decoder.decode(chunk.value, { stream: true });
            var idx;
            while ((idx = buf.indexOf('\n\n')) !== -1) {
              var block = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              var payload = null;
              var rows = block.split('\n');
              for (var i = 0; i < rows.length; i++) {
                if (rows[i].indexOf('data:') !== 0) continue;
                var line = rows[i].slice(5).replace(/^ /, '');
                payload = payload === null ? line : payload + '\n' + line;
              }
              if (payload === null) continue;
              var msg;
              try { msg = JSON.parse(payload); } catch (e) { continue; }
              if (msg.type === 'progress' || msg.type === 'end') {
                return finish({ progress: msg.progress || {}, status: msg.status });
              }
            }
            return pump();
          });
        }
        return pump();
      });
  }

  function fetchState() {
    fetch('/api/jobs/' + encodeURIComponent(data.id) + '/state', { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .catch(fetchStateFromStream)
      .then(function (msg) {
        pollDelay = 2000;
        applyProgress(msg);
        if (!isTerminal(msg.status)) schedulePoll(pollDelay);
      })
      .catch(function () {
        // Fallback polling is best-effort; keep the last progress visible and
        // back off while the server is unreachable.
        pollDelay = Math.min(10000, Math.round(pollDelay * 1.5));
        schedulePoll(pollDelay);
      });
  }

  function startPolling() {
    if (pollTimer || !data.id) return;
    pollDelay = 2000;
    schedulePoll(pollDelay);
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function connectStream() {
    if (!data.streamUrl) return null;
    if (es) {
      try { es.close(); } catch (e) { /* already closed */ }
    }
    var src = new EventSource(data.streamUrl);
    src.onopen = function () {
      if (es !== src) { src.close(); return; }
      lastMessageAt = Date.now();
      stopPolling();
      setConn(null);
    };
    src.onmessage = function (e) {
      lastMessageAt = Date.now();
      var msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      if (msg.type === 'log_batch' && Array.isArray(msg.lines)) {
        for (var i = 0; i < msg.lines.length; i++) {
          appendLog(msg.lines[i] + '\n');
        }
      } else if (msg.type === 'log') {
        appendLog((msg.line || '') + '\n');
      } else if (msg.type === 'progress') {
        applyProgress(msg);
        if (isTerminal(msg.status)) {
          // The server also sends "end", but closing here avoids a spurious
          // onerror if the last event is lost while the socket closes.
          src.close();
          if (es === src) es = null;
        }
      } else if (msg.type === 'end') {
        src.close();
        stopPolling();
        if (es === src) es = null;
      }
    };
    src.onerror = function () {
      if (es !== src) return;
      // Keep the last progress visible; surface the reconnect attempt in a
      // separate status line instead of replacing the progress text.
      setConn(i18n('job.view.reconnecting'));
      startPolling();
      // EventSource retries by itself, but mobile browsers can leave it
      // permanently CLOSED after a background freeze. Reconnect manually.
      if (src.readyState === 2) {
        setTimeout(function () {
          if (es === src && src.readyState === 2) es = connectStream();
        }, 5000);
      }
    };
    es = src;
    return src;
  }

  // Render current state first, then live-update via SSE (no flash on reload).
  if (data.progress) renderProgress(data.progress);
  if (data.status) setStatusBadge(data.status);
  if (data.streamUrl) es = connectStream();

  // A dead TCP connection doesn't always fire EventSource.onerror promptly
  // (mobile browsers can sit on it for minutes). If no event — progress, log,
  // or server ping — arrives for 30s while the job is live, rebuild the stream.
  setInterval(function () {
    if (!es || es.readyState !== 1 || isTerminal(currentStatus)) return;
    if (Date.now() - lastMessageAt < 30000) return;
    setConn(i18n('job.view.reconnecting'));
    startPolling();
    es = connectStream();
  }, 10000);

  document.addEventListener('visibilitychange', function () {
    if (document.hidden || !data.streamUrl) return;
    if (!es || es.readyState === 2 || (connEl && !connEl.hidden)) {
      es = connectStream();
    }
  });
})();