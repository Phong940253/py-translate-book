/* Translate Book Manager — client app (library, preview, theme, lightbox, i18n). */
(function () {
  'use strict';

  var APP = window.APP || {};

  function i18n(key) {
    return (APP.i18n && APP.i18n[key]) || key;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function coverSrc(e) {
    return APP.coverUrl + '?path=' + encodeURIComponent(e.path);
  }

  function coverImg(e, alt) {
    var src = e ? coverSrc(e) : APP.coverPlaceholder;
    var ph = APP.coverPlaceholder || '';
    return '<img src="' + esc(src) + '" alt="' + esc(alt || '') + '" loading="lazy"' +
      ' onerror="this.onerror=null;this.src=\'' + ph + '\'">';
  }

  function metaOf(e) {
    return e && e.meta ? e.meta : {};
  }

  function statusBadge(status, label) {
    return '<span class="badge status-' + esc(status) + '"><span class="status-dot"></span>' +
      esc(label) + '</span>';
  }

  /* ------------------------------------------------------------ toast */
  function toast(message, type) {
    var root = document.getElementById('toast-root');
    if (!root) return;
    var el = document.createElement('div');
    el.className = 'toast' + (type ? ' toast-' + type : '');
    el.textContent = message;
    root.appendChild(el);
    setTimeout(function () {
      el.classList.add('toast-out');
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 220);
    }, 4000);
  }

  /* ------------------------------------------------------- requestJSON */
  async function requestJSON(url) {
    var res = await fetch(url);
    var raw = await res.text();
    var data = null;
    try { data = JSON.parse(raw); } catch (e) { /* non-JSON response */ }
    if (!res.ok) {
      throw new Error((data && data.error) || ('HTTP ' + res.status));
    }
    return data;
  }

  /* ------------------------------------------------------------ preview */
  var _pv = { path: null, chapter: 1 };

  function pvEl(id) {
    return document.getElementById(id);
  }

  function openPreviewPanel() {
    var box = pvEl('preview-box');
    if (!box) return;
    var prompt = pvEl('pv-prompt');
    var text = pvEl('pv-text');
    var ch = pvEl('pv-ch');
    var input = pvEl('pv-input');
    if (prompt) prompt.hidden = false;
    if (text) { text.textContent = ''; text.hidden = true; }
    if (ch) ch.textContent = '';
    if (input) input.value = _pv.chapter || 1;
    box.hidden = false;
    if (input) input.focus();
  }

  window.preview = function (btn) {
    var path = btn && btn.dataset && btn.dataset.path;
    if (!path) return;
    _pv.path = path;
    openPreviewPanel();
  };

  async function fetchPreview() {
    if (!_pv.path) return;
    var input = pvEl('pv-input');
    var n = parseInt((input && input.value) || '1', 10);
    if (!n || n < 1) {
      toast(i18n('books.preview_hint'), 'warn');
      return;
    }
    _pv.chapter = n;
    var url = APP.previewUrl + '?path=' + encodeURIComponent(_pv.path) +
      '&chapter=' + encodeURIComponent(n);
    try {
      var data = await requestJSON(url);
      var prompt = pvEl('pv-prompt');
      var text = pvEl('pv-text');
      var ch = pvEl('pv-ch');
      if (prompt) prompt.hidden = true;
      if (text) { text.textContent = data.text || ''; text.hidden = false; }
      if (ch) ch.textContent = data.chapter + ' / ' + data.total;
      var box = pvEl('preview-box');
      if (box) box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
      toast(i18n('books.error_prefix') + ' ' + (e && e.message ? e.message : String(e)), 'err');
    }
  }

  function closePreview() {
    var box = pvEl('preview-box');
    if (box) box.hidden = true;
    _pv.path = null;
  }

  /* -------------------------------------------------- theme (global, single
     source of truth). The whole chrome — and the reader's page theme, which
     listens for `themechange` — is driven from here, so a page can never
     disagree with the app about light/dark/paper/black/custom. */
  var THEME_NAMES = ['light', 'sepia', 'dark', 'black', 'system', 'custom'];

  function themePref() {
    var t = null;
    try { t = localStorage.getItem('theme'); } catch (e) {}
    return THEME_NAMES.indexOf(t) >= 0 ? t : 'system';
  }
  window.themePref = themePref;

  function prefersLight() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
  }

  function resolveTheme(t) {
    if (t === 'system' || THEME_NAMES.indexOf(t) < 0) return prefersLight() ? 'light' : 'dark';
    return t;
  }

  function storedColors() {
    var bg = null, fg = null;
    try { bg = localStorage.getItem('theme.bg'); fg = localStorage.getItem('theme.fg'); } catch (e) {}
    return { bg: bg || '#101418', fg: fg || '#d7dbe0' };
  }

  function lum(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return 0;
    var v = parseInt(m[1], 16);
    return (0.2126 * ((v >> 16) & 255) + 0.7152 * ((v >> 8) & 255) + 0.0722 * (v & 255)) / 255;
  }

  function each(sel, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(sel), fn);
  }

  function applyTheme() {
    var root = document.documentElement;
    var name = themePref();
    var resolved = resolveTheme(name);
    var scheme;
    if (name === 'custom') {
      var c = storedColors();
      root.style.setProperty('--custom-bg', c.bg);
      root.style.setProperty('--custom-fg', c.fg);
      scheme = lum(c.bg) < 0.5 ? 'dark' : 'light';
      root.style.colorScheme = scheme;          // CSS has no custom rule for it
    } else {
      root.style.removeProperty('--custom-bg');
      root.style.removeProperty('--custom-fg');
      root.style.removeProperty('color-scheme'); // let the theme rules own it
      scheme = (resolved === 'light' || resolved === 'sepia') ? 'light' : 'dark';
    }
    root.setAttribute('data-theme', resolved);
    root.setAttribute('data-scheme', scheme);

    each('.theme-opt', function (b) {
      b.setAttribute('aria-checked', String(b.dataset.gtheme === name));
    });
    var row = document.getElementById('theme-custom-row');
    if (row) row.hidden = name !== 'custom';
    if (name === 'custom') {
      var cc = storedColors();
      var bi = document.getElementById('theme-bg'), fi = document.getElementById('theme-fg');
      if (bi && bi.value !== cc.bg) bi.value = cc.bg;
      if (fi && fi.value !== cc.fg) fi.value = cc.fg;
    }

    // PWA address-bar tint follows the active scheme.
    each('meta[name="theme-color"]', function (m) {
      var dark = (m.getAttribute('media') || '').indexOf('dark') >= 0;
      if (dark === (scheme === 'dark')) {
        m.setAttribute('content', getComputedStyle(root).getPropertyValue('--bg').trim() || m.content);
      }
    });

    try {
      document.dispatchEvent(new CustomEvent('themechange', {
        detail: { name: name, theme: resolved, scheme: scheme }
      }));
    } catch (e) { /* CustomEvent unsupported */ }
  }
  window.applyTheme = applyTheme;

  function setTheme(name) {
    if (THEME_NAMES.indexOf(name) < 0) return;
    try { localStorage.setItem('theme', name); } catch (e) {}
    applyTheme();
    openThemeMenu(false);
  }
  window.setTheme = setTheme;

  /* Reader (and the menu itself) write the custom pair through here. */
  function setThemeColors(bg, fg) {
    try {
      if (bg) localStorage.setItem('theme.bg', bg);
      if (fg) localStorage.setItem('theme.fg', fg);
    } catch (e) {}
    applyTheme();
  }
  window.setThemeColors = setThemeColors;

  function openThemeMenu(open) {
    var pop = document.getElementById('theme-pop');
    if (!pop) return;
    pop.hidden = !open;
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.setAttribute('aria-expanded', String(!!open));
    document.body.classList.toggle('theme-open', !!open);
  }

  function toggleThemeMenu(ev) {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    var pop = document.getElementById('theme-pop');
    openThemeMenu(!!(pop && pop.hidden));
  }
  window.toggleThemeMenu = toggleThemeMenu;

  /* Legacy quick flip: light <-> dark (kept for callers/tests). */
  function toggleTheme() {
    setTheme(resolveTheme(themePref()) === 'light' ? 'dark' : 'light');
  }
  window.toggleTheme = toggleTheme;

  function wireThemeMenu() {
    applyTheme();
    var pop = document.getElementById('theme-pop');
    if (!pop) return;

    pop.addEventListener('click', function (ev) {
      var b = ev.target.closest('.theme-opt');
      if (b && b.dataset.gtheme) setTheme(b.dataset.gtheme);
    });

    var bi = document.getElementById('theme-bg');
    var fi = document.getElementById('theme-fg');
    var onColor = function () {
      setThemeColors(bi && bi.value, fi && fi.value);
      if (themePref() !== 'custom') setTheme('custom');
    };
    if (bi) bi.addEventListener('input', onColor);
    if (fi) fi.addEventListener('input', onColor);

    document.addEventListener('click', function (ev) {
      var menu = document.getElementById('theme-menu');
      if (menu && !menu.contains(ev.target)) openThemeMenu(false);
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') openThemeMenu(false);
    });
    // other tabs change the theme
    window.addEventListener('storage', function (e) {
      if (e.key === 'theme' || e.key === 'theme.bg' || e.key === 'theme.fg') applyTheme();
    });
    // OS light/dark flips while we follow the system
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: light)');
      var onMq = function () { if (themePref() === 'system') applyTheme(); };
      if (mq.addEventListener) mq.addEventListener('change', onMq);
      else if (mq.addListener) mq.addListener(onMq);
    }
  }

  /* ------------------------------------------------------ library render */
  var LIB = {
    el: null,
    compact: false,
    chapters: false,
    data: null,
    filter: APP.initialFilter || '',
    query: ''
  };
  window.LIB = LIB;

  function matchGroup(g) {
    if (LIB.filter && g.rank !== LIB.filter) return false;
    var q = LIB.query.trim().toLowerCase();
    if (!q) return true;
    var hay = (g.title || '') + ' ' + (g.base_name || '') + ' ' + metaOf(g.source).creator;
    return hay.toLowerCase().indexOf(q) !== -1;
  }

  function progressHtml(e) {
    if (!(e.status === 'partial' && e.progress)) return '';
    var p = e.progress;
    return '<span class="progress-mini"><span style="width:' + esc(p.pct) + '%"></span></span>' +
      '<span class="small muted">' + esc(p.chapter) + '/' + esc(p.total) + '</span>';
  }

  function entryActions(e) {
    var a = [];
    if (e.kind === 'source') {
      a.push('<a class="btn btn-primary btn-sm" href="' + APP.jobNewUrl + '?input=' +
        encodeURIComponent(e.path) + '">' + esc(i18n('books.dich')) + '</a>');
    } else if (e.kind === 'translated') {
      if (e.status === 'partial') {
        a.push('<a class="btn btn-primary btn-sm" href="' + APP.jobNewUrl + '?input=' +
          encodeURIComponent(e.path) + '">' + esc(i18n('books.resume')) + '</a>');
      } else if (e.status === 'done') {
        a.push('<a class="btn btn-primary btn-sm" href="' + APP.downloadUrl + '?path=' +
          encodeURIComponent(e.path) + '">' + esc(i18n('books.download')) + '</a>');
      }
    }
    // `data-path` (not a JS string literal) keeps Windows backslashes intact.
    a.push('<button type="button" class="btn btn-ghost btn-sm" data-path="' + esc(e.path) +
      '" onclick="preview(this)">' + esc(i18n('books.preview')) + '</button>');
    a.push('<a class="btn btn-ghost btn-sm" href="' + APP.readUrl + '?path=' +
      encodeURIComponent(e.path) + '">' + esc(i18n('books.read')) + '</a>');
    return a.join('');
  }

  function entryHtml(e) {
    return '<div class="ver-row">' +
      statusBadge(e.status, e.label) +
      '<span class="mono small ver-name" title="' + esc(e.path) + '">' + esc(e.name) + '</span>' +
      progressHtml(e) +
      '<span class="ver-actions">' + entryActions(e) + '</span>' +
      '</div>';
  }

  function groupHtml(g) {
    var src = g.source;
    var head;
    if (src) {
      var m = metaOf(src);
      var sub = esc(m.creator || '—');
      if (m.language) sub += ' · ' + esc(m.language);
      if (src.chapters) sub += ' · ' + esc(src.chapters) + ' ' + esc(i18n('books.chapters'));
      head = '<div class="muted small">' + sub + '</div>';
    } else {
      head = '<div class="muted small">' + esc(g.translations.length) + ' ' + esc(i18n('books.translations')) + '</div>';
    }
    var rank = statusBadge(g.rank, g.rank_label);
    var btn = src
      ? '<a class="btn btn-primary btn-sm" href="' + APP.jobNewUrl + '?input=' +
        encodeURIComponent(src.path) + '">' + esc(i18n('books.dich_nguon')) + '</a>'
      : '';
    return '<div class="lib-group card">' +
      '<div class="lib-cover">' + coverImg(src, g.title) + '</div>' +
      '<div class="lib-body">' +
      '<div class="lib-head"><h2>' + esc(g.title) + '</h2>' + rank + btn + '</div>' +
      head +
      '<div class="ver-list">' + g.entries.map(entryHtml).join('') + '</div>' +
      '</div>' +
      '</div>';
  }

  function miniHtml(g) {
    var src = g.source;
    var size = src ? esc(src.size_mb) + ' MB · ' : '';
    var act = src
      ? '<a class="btn btn-primary btn-sm" href="' + APP.jobNewUrl + '?input=' +
        encodeURIComponent(src.path) + '">' + esc(i18n('books.dich')) + '</a>'
      : '';
    return '<div class="book-card">' +
      '<div class="book-cover">' + coverImg(src, g.title) + '</div>' +
      '<div class="book-info">' +
      '<div class="book-title" title="' + esc(g.title) + '">' + esc(g.title) + '</div>' +
      '<div class="muted small">' + size + esc(g.translations.length) + ' ' + esc(i18n('books.translations')) + '</div>' +
      '<div>' + statusBadge(g.rank, g.rank_label) + '</div>' +
      '<div class="book-actions">' + act + '</div>' +
      '</div>' +
      '</div>';
  }

  function updateStats() {
    var el = document.getElementById('lib-stats');
    if (!el || !LIB.data || !LIB.data.stats) return;
    var s = LIB.data.stats;
    el.textContent = s.total + ' ' + i18n('books.titles_unit') + ' / ' + s.files + ' ' + i18n('books.files_unit') +
      ' · ' + s.untranslated + ' ' + i18n('status.untranslated') +
      ' · ' + s.partial + ' ' + i18n('status.partial') +
      ' · ' + s.done + ' ' + i18n('status.done') +
      ' · ' + s.assumed + ' ' + i18n('status.assumed');
  }

  function render() {
    if (!LIB.el || !LIB.data) return;
    var groups = LIB.data.groups.filter(matchGroup);
    if (LIB.compact) {
      LIB.el.innerHTML = groups.length
        ? '<div class="book-grid compact">' + groups.map(miniHtml).join('') + '</div>'
        : '<div class="lib-empty">' + esc(LIB.data.groups.length ? i18n('books.no_match') : i18n('books.none')) + '</div>';
    } else {
      if (!groups.length) {
        LIB.el.innerHTML = '<div class="lib-empty">' +
          esc(LIB.data.groups.length ? i18n('books.no_match') : i18n('books.none')) + '</div>';
      } else {
        LIB.el.innerHTML = groups.map(groupHtml).join('');
      }
      updateStats();
    }
  }

  async function load(force) {
    if (!LIB.el) return;
    LIB.el.classList.add('lib-loading');
    var url = APP.libUrl;
    var params = [];
    if (LIB.chapters) params.push('chapters=1');
    if (force) params.push('refresh=1');
    if (params.length) url += '?' + params.join('&');
    try {
      LIB.data = await requestJSON(url);
      render();
    } catch (e) {
      LIB.el.innerHTML = '<div class="lib-empty error" role="alert">' +
        esc(i18n('toast.network')) + ' — ' + esc((e && e.message) || String(e)) + '</div>' +
        '<div class="form-actions"><button type="button" class="btn btn-ghost btn-sm" data-retry="1">' +
        esc(i18n('books.refresh')) + '</button></div>';
    } finally {
      LIB.el.classList.remove('lib-loading');
    }
  }

  /* ------------------------------------------------------ page wiring */
  document.addEventListener('DOMContentLoaded', function () {
    wireThemeMenu();

    // Library list (books page) and mini-grid (dashboard).
    var el = document.getElementById('lib');
    if (el) { LIB.el = el; LIB.chapters = true; load(false); }
    var mini = document.getElementById('lib-mini');
    if (mini) { LIB.el = mini; LIB.compact = true; load(false); }

    // Filter chips → re-render only (data already client-side).
    document.addEventListener('click', function (ev) {
      var chip = ev.target.closest('.filter-chip');
      if (!chip) return;
      LIB.filter = chip.dataset.filter || '';
      document.querySelectorAll('.filter-chip').forEach(function (c) {
        c.classList.toggle('btn-primary', c.dataset.filter === LIB.filter);
        c.classList.toggle('btn-ghost', c.dataset.filter !== LIB.filter);
      });
      render();
    });

    // Toolbar refresh button + error-state retry.
    document.addEventListener('click', function (ev) {
      if (ev.target.closest('#lib-refresh') || ev.target.closest('[data-retry]')) {
        LIB.data = null;
        load(true);
      }
    });

    // Client-side search.
    var searchEl = document.getElementById('lib-search');
    if (searchEl) {
      searchEl.addEventListener('input', function () { LIB.query = searchEl.value; render(); });
    }

    // Preview popover (chapter input replaces the old prompt()).
    var pvGo = document.getElementById('pv-go');
    var pvCancel = document.getElementById('pv-cancel');
    var pvClose = document.getElementById('pv-close');
    var pvInput = document.getElementById('pv-input');
    if (pvGo) pvGo.addEventListener('click', fetchPreview);
    if (pvCancel) pvCancel.addEventListener('click', closePreview);
    if (pvClose) pvClose.addEventListener('click', closePreview);
    if (pvInput) {
      pvInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') fetchPreview();
      });
    }

    // Lang switch: append ?next= so the current page is preserved.
    document.querySelectorAll('.lang-btn[data-lang]').forEach(function (a) {
      a.addEventListener('click', function () {
        var sep = a.href.indexOf('?') === -1 ? '?' : '&';
        a.href = a.href + sep + 'next=' + encodeURIComponent(location.pathname + location.search);
      });
    });

    // Mobile nav menu: hamburger opens the topnav as a dropdown panel below
    // the sticky topbar (style.css shows the button only at <=1000px).
    var navBtn = document.getElementById('nav-toggle');
    var navEl = document.getElementById('topnav');
    function setNavOpen(open) {
      if (!navEl || !navBtn) return;
      navEl.classList.toggle('is-open', open);
      navBtn.setAttribute('aria-expanded', String(open));
      // raise .topbar above sticky page chrome (reader toolbar) while open
      document.body.classList.toggle('nav-open', open);
    }
    if (navBtn && navEl) {
      navBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        setNavOpen(!navEl.classList.contains('is-open'));
      });
      document.addEventListener('click', function (ev) {
        if (!navEl.classList.contains('is-open')) return;
        if (navEl.contains(ev.target) || navBtn.contains(ev.target)) return;
        setNavOpen(false);
      });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') setNavOpen(false);
      });
      window.addEventListener('resize', function () {
        if (window.innerWidth > 1000) setNavOpen(false);
      });
    }

    // Lightbox (illustrations page; delegated, no inline handlers).
    var lightbox = document.getElementById('lightbox');
    var lbImg = document.getElementById('lb-img');
    var lbClose = document.getElementById('lb-close');
    if (lightbox) {
      document.addEventListener('click', function (ev) {
        var thumb = ev.target.closest('.thumb[data-src]');
        if (thumb) {
          lbImg.src = thumb.getAttribute('data-src');
          lbImg.alt = thumb.getAttribute('data-alt') || '';
          lightbox.classList.add('open');
          return;
        }
        if (ev.target === lightbox || (lbClose && lbClose.contains(ev.target))) {
          lightbox.classList.remove('open');
          lbImg.src = '';
        }
      });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') lightbox.classList.remove('open');
      });
    }

    // Config page: Discord webhook test.
    var discordBtn = document.getElementById('btn-discord');
    var discordResult = document.getElementById('discord-result');
    if (discordBtn && discordResult) {
      discordBtn.addEventListener('click', function () {
        discordResult.textContent = i18n('config.discord_testing');
        fetch('/config/test-discord', { method: 'POST' })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            discordResult.textContent = d.success
              ? i18n('config.discord_success')
              : i18n('config.discord_fail_log');
          })
          .catch(function () {
            discordResult.textContent = i18n('config.discord_fail_net');
          });
      });
    }
  });
})();