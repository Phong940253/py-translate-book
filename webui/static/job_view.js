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
  var progEl = document.getElementById('progress');
  var barEl = document.getElementById('bar');
  var apiEl = document.getElementById('api');
  var subEl = document.getElementById('subprog');
  var structView = document.getElementById('structView');
  var diffView = document.getElementById('diffView');
  var diffWrap = document.getElementById('diffWrap');
  var diffSource = document.getElementById('diffSource');
  var diffTrans = document.getElementById('diffTrans');

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

  function renderProgress(p) {
    if (!p || !p.total_chapters) {
      if (progEl) progEl.textContent = i18n('job.view.initializing');
      return;
    }
    var cur = p.current_chapter || p.last_completed || '?';
    var total = p.total_chapters;
    var done = p.last_completed || 0;
    progEl.innerHTML = esc(i18n('job.view.chapter_word')) + ' <b>' + esc(cur) + '</b> / ' + esc(total) +
      (p.current_title ? ' — <i>' + esc(p.current_title) + '</i>' : '');
    var pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
    barEl.style.width = pct + '%';
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

  var VIEWS = { struct: 'structView', diff: 'diffView', side: 'diffWrap' };
  var BUTTONS = { struct: 'btnStruct', diff: 'btnDiff', side: 'btnSide' };

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
  }

  // Render current state first, then live-update via SSE (no flash on reload).
  if (data.progress) renderProgress(data.progress);

  if (data.streamUrl) {
    var es = new EventSource(data.streamUrl);
    es.onmessage = function (e) {
      var msg = JSON.parse(e.data);
      if (msg.type === 'log') {
        if (logEl) {
          logEl.textContent += (msg.line || '') + '\n';
          logEl.scrollTop = logEl.scrollHeight;
        }
      } else if (msg.type === 'progress') {
        renderProgress(msg.progress);
        if (['done', 'error', 'stopped', 'interrupted'].indexOf(msg.status) !== -1) {
          if (progEl) {
            var badge = document.createElement('span');
            badge.className = 'badge ' + msg.status;
            var d = document.createElement('span');
            d.className = 'status-dot';
            badge.appendChild(d);
            badge.appendChild(document.createTextNode(' ' + i18n('job.status.' + msg.status)));
            if (msg.error) logEl.textContent += '\n' + i18n('job.view.error_line') + msg.error + '\n';
            progEl.appendChild(document.createTextNode(' — '));
            progEl.appendChild(badge);
          }
        }
      } else if (msg.type === 'end') {
        es.close();
      }
    };
    es.onerror = function () {
      if (progEl) progEl.textContent = i18n('job.view.stream_lost');
    };
  }
})();