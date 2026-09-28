/* EPUB reader client (P1): sandboxed iframe rendering, nested TOC, CFI-lite
   positions, CSS-columns pagination, appearance settings, footnote popups and
   read-aloud (local TTS via /api/tts).

   Bootstrapped from window.APP.reader (set by reader.html); UI strings come
   from window.APP.i18n. The chapter document is fetched from /api/reader/doc
   (sanitized XHTML) and injected via srcdoc — the iframe runs sandboxed
   without scripts, so all interaction (links, highlight, pagination) happens
   from this parent document through the same-origin contentDocument.

   Position format v2: {v:2, ch, path, off, item, sc, pg} — `path` is a
   CFI-lite element path inside the chapter body ("2/5/1"), `off` the saved
   viewport offset of that element, `sc` the scroll ratio, `pg` the page index
   (page mode). Legacy {ch, i} positions migrate on restore via the item index.
   State lives in localStorage and in the sidecar <book>.reader.json (PUT/GET
   /api/reader/state). */
(function () {
  'use strict';

  var APP = window.APP || {};
  var R = APP.reader || {};
  var path = R.path || '';

  function i18n(key) { return (APP.i18n && APP.i18n[key]) || key; }
  function fmt(key, vars) {
    var s = i18n(key);
    vars = vars || {};
    Object.keys(vars).forEach(function (k) { s = s.split('{' + k + '}').join(String(vars[k])); });
    return s;
  }
  function el(id) { return document.getElementById(id); }
  function noop() {}
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function getJSON(url) {
    return fetch(url).then(function (res) {
      return res.text().then(function (raw) {
        var data = null;
        try { data = JSON.parse(raw); } catch (e) { /* non-JSON */ }
        if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
        return data;
      });
    });
  }

  function loadJSON(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }
  function saveJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* quota */ }
  }

  /* -------------------------------------------------------------- state */
  var DEFAULTS = {
    family: 'serif', size: 18, lh: 1.75, width: 760, ls: 0, margin: 24,
    align: 'left', breakWords: false, overrideFont: false,
    theme: 'dark', customBg: '#101418', customFg: '#d7dbe0',
    mode: 'scroll', cols: 1
  };
  var state = {
    meta: null, toc: null, chapter: null, items: [],
    ch: 1, cur: 0, nodes: [], nodeOf: [], curNode: null,
    frameReady: false, token: 0, pos: { v: 2, ch: 1, item: 0 },
    fnAnchor: null
  };
  var sessionPos = {};              // per-chapter positions remembered this visit
  var bootPos = null;
  var pending = null;               // in-flight chapter load: {token, html, pos, frag, item}
  var fdoc = null;                  // current iframe document
  var frame = null;
  var fontBuf = null;               // uploaded font (session only)
  var pgState = { step: 0, total: 1, cols: 1, pgw: 0 };
  var settings = Object.assign({}, DEFAULTS);
  var ttsCfg = { rate: 1, voice: null };

  var posKey = 'rd.pos:' + path;
  var setKey = 'rd.set:' + path;
  var ttsKey = 'rd.tts';

  /* ------------------------------------------------- resources / caching */
  var docCache = {};                // n -> sanitized html string (LRU-ish)
  var chapCache = {};               // n -> /api/reader/chapter payload

  function withQuery(url, extra) {
    var q = ['path=' + encodeURIComponent(path)];
    Object.keys(extra || {}).forEach(function (k) {
      q.push(k + '=' + encodeURIComponent(extra[k]));
    });
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + q.join('&');
  }
  function docUrl(n) { return withQuery(R.docUrl, { n: n }); }
  function chapterUrl(n) { return withQuery(R.chapterUrl, { n: n }); }
  function bookUrl(base) { return withQuery(base, {}); }

  function evict(cache, max) {
    var keys = Object.keys(cache);
    while (keys.length > max) delete cache[keys.shift()];
  }
  function touch(cache, key) {
    var v = cache[key];
    delete cache[key];
    cache[key] = v;
  }

  function htmlMessage(raw) {
    try {
      var d = new DOMParser().parseFromString(raw, 'text/html');
      var p = d.querySelector('p');
      return p ? p.textContent : '';
    } catch (e) { return ''; }
  }

  function fetchDoc(n) {
    if (docCache[n] != null) { touch(docCache, n); return Promise.resolve(docCache[n]); }
    return fetch(docUrl(n)).then(function (res) {
      return res.text().then(function (raw) {
        if (!res.ok) throw new Error(htmlMessage(raw) || ('HTTP ' + res.status));
        docCache[n] = raw;
        evict(docCache, 4);
        return raw;
      });
    });
  }

  function fetchChap(n) {
    if (chapCache[n]) { touch(chapCache, n); return Promise.resolve(chapCache[n]); }
    return getJSON(chapterUrl(n)).then(function (ch) {
      chapCache[n] = ch;
      evict(chapCache, 6);
      return ch;
    });
  }

  /* ------------------------------------------------ appearance / theming */
  var THEMES = {
    light: { fg: '#1b1e22', bg: '#ffffff', scheme: 'light' },
    sepia: { fg: '#3d2f1c', bg: '#f7ecd7', scheme: 'light' },
    dark: { fg: '#d7dbe0', bg: '#14171c', scheme: 'dark' },
    black: { fg: '#cfcfcf', bg: '#000000', scheme: 'dark' }
  };

  function themeColors() {
    var k = settings.theme;
    if (k === 'system') {
      var dark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
      k = dark ? 'dark' : 'light';
    }
    if (k === 'custom') {
      var bg = settings.customBg || '#101418';
      var fg = settings.customFg || '#d7dbe0';
      var lum = function (hex) {
        var m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
        if (!m) return 0;
        var v = parseInt(m[1], 16);
        return (0.2126 * ((v >> 16) & 255) + 0.7152 * ((v >> 8) & 255) + 0.0722 * (v & 255)) / 255;
      };
      return { fg: fg, bg: bg, scheme: lum(bg) < 0.5 ? 'dark' : 'light' };
    }
    return THEMES[k] || THEMES.dark;
  }

  function familyValue() {
    switch (settings.family) {
      case 'sans': return "'Helvetica Neue', Arial, 'Segoe UI', sans-serif";
      case 'mono': return "ui-monospace, Consolas, 'Courier New', monospace";
      case 'system': return "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
      case 'dyslexic': return "'OpenDyslexic', 'Comic Sans MS', Verdana, sans-serif";
      case 'custom': return "'RDUserFont', Georgia, serif";
      default: return "Georgia, 'Times New Roman', serif";
    }
  }

  function pushVars(doc) {
    if (!doc || !doc.documentElement) return;
    var s = doc.documentElement.style;
    var t = themeColors();
    s.setProperty('--rd-fg', t.fg);
    s.setProperty('--rd-bg', t.bg);
    s.setProperty('--rd-size', settings.size + 'px');
    s.setProperty('--rd-lh', String(settings.lh));
    s.setProperty('--rd-fam', familyValue());
    s.setProperty('--rd-w', settings.width + 'px');
    s.setProperty('--rd-ls', settings.ls + 'px');
    s.setProperty('--rd-padx', settings.margin + 'px');
    s.setProperty('--rd-pad-y', Math.max(settings.margin, 16) + 'px');
    s.setProperty('--rd-align', settings.align);
    s.setProperty('--rd-ow', settings.breakWords ? 'break-word' : 'normal');
    try { doc.documentElement.style.colorScheme = t.scheme; } catch (e) { /* older */ }
    doc.documentElement.classList.toggle('rd-force', !!settings.overrideFont);
  }

  function pressGroup(sel, key, value) {
    qsa(sel).forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset[key] === value)); });
  }
  function setVal(id, v) { var n = el(id); if (n) n.value = v; }
  function setLabel(id, v) { var n = el(id); if (n) n.textContent = v; }

  function applySettings() {
    setVal('rd-set-family', settings.family);
    setVal('rd-set-size', settings.size);
    setLabel('rd-set-size-v', settings.size + 'px');
    setVal('rd-set-lh', settings.lh);
    setLabel('rd-set-lh-v', Number(settings.lh).toFixed(2));
    setVal('rd-set-width', settings.width);
    setLabel('rd-set-width-v', settings.width + 'px');
    setVal('rd-set-ls', settings.ls);
    setLabel('rd-set-ls-v', settings.ls + 'px');
    setVal('rd-set-margin', settings.margin);
    setLabel('rd-set-margin-v', settings.margin + 'px');
    var ow = el('rd-set-ow'); if (ow) ow.checked = !!settings.breakWords;
    var ov = el('rd-set-override'); if (ov) ov.checked = !!settings.overrideFont;
    pressGroup('.rd-theme', 'theme', settings.theme);
    pressGroup('.rd-align', 'align', settings.align);
    pressGroup('.rd-mode', 'mode', settings.mode);
    pressGroup('.rd-cols', 'cols', String(settings.cols));
    var cc = el('rd-custom-colors'); if (cc) cc.hidden = settings.theme !== 'custom';
    setVal('rd-set-bg', settings.customBg);
    setVal('rd-set-fg', settings.customFg);

    var rd = el('rd');
    if (rd) {
      rd.setAttribute('data-mode', settings.mode);
      rd.style.setProperty('--rd-width', settings.width + 'px');
      rd.style.setProperty('--rd-size', settings.size + 'px');
      rd.style.setProperty('--rd-lh', String(settings.lh));
    }
    var pn = el('rd-page-nav');
    if (pn) pn.hidden = settings.mode !== 'page';
    var stage = el('rd-stage');
    if (stage) {
      stage.setAttribute('data-theme', settings.theme);
      stage.style.background = settings.theme === 'custom' ? (settings.customBg || '') : '';
    }
    if (fdoc) pushVars(fdoc);
    if (state.frameReady) {
      if (settings.mode === 'page') scheduleLayout();
      else clearPageLayout();
    }
    renderPosition();
  }

  function saveSettings() {
    saveJSON(setKey, settings);
    scheduleSidecar();
  }

  /* --------------------------------------------------- state persistence */
  var saveT = 0;

  function scheduleSidecar() {
    if (saveT) clearTimeout(saveT);
    saveT = setTimeout(function () { saveT = 0; flushSidecar(false); }, 800);
  }

  function flushSidecar(keepalive) {
    if (saveT) { clearTimeout(saveT); saveT = 0; }
    if (!state.meta) return;
    capturePos();
    saveJSON(posKey, state.pos);
    var body;
    try { body = JSON.stringify({ path: path, state: { pos: state.pos, settings: settings } }); }
    catch (e) { return; }
    try {
      fetch(R.stateUrl, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: body,
        keepalive: !!keepalive
      }).catch(noop);
    } catch (e) { /* offline */ }
  }

  function capturePos() {
    if (!state.frameReady || !fdoc) return state.pos;
    var node = (state.curNode && fdoc.contains(state.curNode)) ? state.curNode : null;
    var pth = node ? pathOf(node) : null;
    var off = node ? Math.round(node.getBoundingClientRect().top) : 0;
    var scRatio = 0, pg = 0;
    if (settings.mode === 'page') {
      pg = currentPage();
    } else {
      var sc = fdoc.scrollingElement || fdoc.documentElement;
      var max = sc.scrollHeight - sc.clientHeight;
      scRatio = max > 4 ? sc.scrollTop / max : 0;
    }
    state.pos = { v: 2, ch: state.ch, path: pth, off: off, item: state.cur, sc: scRatio, pg: pg };
    sessionPos[state.ch] = state.pos;
    return state.pos;
  }

  /* ------------------------------------------------------- CFI-lite paths */
  function pathOf(node) {
    if (!fdoc || !node) return null;
    var body = fdoc.body;
    var chain = [];
    var cur = node;
    while (cur && cur !== body) {
      if (cur.nodeType === 1) chain.unshift(cur);
      cur = cur.parentNode;
    }
    if (cur !== body) return null;
    var parts = chain.map(function (e) {
      var parent = e.parentNode;
      if (!parent) return -1;
      var i = 1, sib = parent.firstElementChild;
      while (sib && sib !== e) { sib = sib.nextElementSibling; i++; }
      return sib ? i : -1;
    });
    if (parts.indexOf(-1) >= 0) return null;
    return '/' + parts.join('/');
  }

  function nodeOf(pth) {
    if (!fdoc || !pth || pth.charAt(0) !== '/') return null;
    var cur = fdoc.body;
    var parts = pth.split('/').filter(Boolean);
    for (var i = 0; i < parts.length; i++) {
      var want = parseInt(parts[i], 10);
      if (!want || want < 1) return null;
      var e = cur.firstElementChild, idx = 1;
      while (e && idx < want) { e = e.nextElementSibling; idx++; }
      if (!e || idx !== want) return null;
      cur = e;
    }
    return cur === fdoc.body ? null : cur;
  }

  function absTop(node) {
    var y = 0, cur = node;
    while (cur && cur !== fdoc.body) {
      y += cur.offsetTop;
      cur = cur.offsetParent;
      if (!cur) break;
    }
    return y;
  }

  /* -------------------------------------------------- chapter documents */
  function showLoading(on, isError) {
    var n = el('rd-loading');
    if (!n) return;
    n.classList.toggle('is-error', !!isError);
    if (on) {
      n.hidden = false;
      if (!isError) n.textContent = i18n('read.loading');
    } else {
      n.hidden = true;
    }
  }

  function showError(e) {
    var n = el('rd-loading');
    if (!n) return;
    n.hidden = false;
    n.classList.add('is-error');
    n.textContent = i18n('read.error') + (e && e.message ? ' — ' + e.message : '');
  }

  function loadChapter(n, opts) {
    opts = opts || {};
    if (!state.meta) return Promise.reject(new Error('no meta'));
    n = clamp(Number(n) || 1, 1, state.meta.total);
    var token = ++state.token;
    pending = {
      token: token, html: null, pos: opts.pos || null,
      frag: opts.frag || '', item: (opts.item != null ? opts.item : null),
      scroll: opts.scroll !== false
    };
    showLoading(true);
    return Promise.all([fetchDoc(n), fetchChap(n)]).then(function (r) {
      if (token !== state.token) return null;
      pending.html = r[0];
      state.ch = n;
      state.chapter = r[1];
      state.items = (r[1] && r[1].items) || [];
      state.cur = 0;
      state.curNode = null;
      state.nodes = [];
      state.nodeOf = [];
      updateChapterUI();
      state.frameReady = false;
      fdoc = null;
      frame.srcdoc = r[0];
      if (opts.push !== false) {
        try { history.pushState({ rdCh: n }, ''); } catch (e) { /* file:// etc */ }
      }
      prefetchNearby(n);
      return r[1];
    }).catch(function (e) {
      if (token === state.token) showError(e);
      return null;
    });
  }

  function onFrameLoad() {
    if (!pending || !frame || frame.srcdoc !== pending.html) return;
    var p = pending;
    pending = null;
    var doc;
    try { doc = frame.contentDocument; } catch (e) { showError(e); return; }
    if (!doc || !doc.body) return;

    fdoc = doc;
    state.frameReady = true;
    pushVars(doc);
    if (settings.mode === 'page') layoutPages(false);
    buildItems(doc);
    bindFrame(doc);

    // restore reading position (TTS > saved CFI > legacy item > top)
    if (speak.on) {
      var si = (speak.ch === state.ch) ? speak.i : 0;
      setCurrent(si, { scroll: true });
      markSpeaking();
    } else if (p.pos) {
      restorePos(p.pos);
    } else if (p.frag) {
      jumpToId(p.frag);
    } else if (p.item != null) {
      setCurrent(p.item, { scroll: p.scroll });
    } else {
      setCurrent(0, { scroll: false });
    }

    showLoading(false);
    renderPosition();
    qsa('.rd-content img', doc).forEach(function (im) {
      if (!im.complete) im.addEventListener('load', function () { scheduleLayout(); }, { once: true });
    });
    injectFont(doc).then(function () {
      if (settings.mode === 'page' && state.frameReady) layoutPages(true);
    });
    if (doc.fonts && doc.fonts.ready && doc.fonts.ready.then) {
      doc.fonts.ready.then(function () { scheduleLayout(); }).catch(noop);
    }
  }

  function restorePos(pos) {
    if (!pos) { setCurrent(0, { scroll: false }); return; }
    var idx = (pos.item != null) ? pos.item : (pos.i != null ? pos.i : 0);
    var node = pos.path ? nodeOf(pos.path) : null;
    if (node) {
      if (node.__rdIdx != null) setCurrent(node.__rdIdx, { scroll: false });
      else scrollToNode(node);
      if (settings.mode === 'page') {
        setPage(pageOfNode(node));
      } else if (typeof pos.off === 'number') {
        var sc = fdoc.scrollingElement || fdoc.documentElement;
        sc.scrollTop = Math.max(0, absTop(node) - pos.off);
      }
      return;
    }
    if (settings.mode === 'page' && pos.pg) { setPage(pos.pg); return; }
    if (settings.mode === 'scroll' && pos.sc) {
      var sc2 = fdoc.scrollingElement || fdoc.documentElement;
      var max = sc2.scrollHeight - sc2.clientHeight;
      if (max > 4) sc2.scrollTop = pos.sc * max;
      setCurrent(idx, { scroll: false });
      return;
    }
    setCurrent(idx, { scroll: idx > 0 });
  }

  function prefetchNearby(n) {
    if (state.meta && n < state.meta.total) {
      fetchChap(n + 1).catch(noop);
      fetchDoc(n + 1).catch(noop);
    }
    if (n > 1) fetchDoc(n - 1).catch(noop);
  }

  /* --------------------------------------------------- item ↔ DOM mapping */
  var SKIP_TAGS = { script: 1, style: 1, noscript: 1, nav: 1, head: 1, title: 1, meta: 1, link: 1 };
  var BLOCK_TAGS = { h1: 1, h2: 1, h3: 1, h4: 1, h5: 1, h6: 1, p: 1, blockquote: 1, pre: 1, li: 1 };

  function textOf(node) {
    // mirror of Python get_text(" ", strip=True) + whitespace collapse
    var parts = [];
    var walker;
    try { walker = fdoc.createTreeWalker(node, 4 /* SHOW_TEXT */, null); }
    catch (e) { return (node.textContent || '').replace(/\s+/g, ' ').trim(); }
    var t;
    while ((t = walker.nextNode())) {
      var s = (t.nodeValue || '').replace(/\s+/g, ' ').trim();
      if (s) parts.push(s);
    }
    return parts.join(' ');
  }

  function resRef(src) {
    if (!src || src.indexOf('/api/reader/res?') !== 0) return '';
    var m = src.match(/[?&]ref=([^&]+)/);
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  function walkItems(doc) {
    var out = [];
    var body = doc.body;

    function hasBlock(el) { return !!el.querySelector('h1,h2,h3,h4,h5,h6,p,blockquote,pre,li'); }
    function hasImg(el) { return !!el.querySelector('img'); }
    function collectImgs(el) {
      Array.prototype.forEach.call(el.querySelectorAll('img'), function (im) {
        var r = resRef(im.getAttribute('src') || '');
        if (r) out.push({ type: 'img', src: r, node: im });
      });
    }
    function walk(node) {
      Array.prototype.forEach.call(node.children, function (child) {
        var name = (child.localName || '').toLowerCase();
        if (SKIP_TAGS[name]) return;
        if (name === 'img') {
          var r = resRef(child.getAttribute('src') || '');
          if (r) out.push({ type: 'img', src: r, node: child });
          return;
        }
        if (BLOCK_TAGS[name]) {
          var text = textOf(child);
          if (text) out.push({ type: 'p', text: text, node: child });
          collectImgs(child);
          return;  // nested blocks belong to the parent
        }
        if (!hasBlock(child) && !hasImg(child)) {
          var t2 = textOf(child);
          if (t2) out.push({ type: 'p', text: t2, node: child });
          return;
        }
        walk(child);
      });
    }
    if (body) walk(body);
    return out;
  }

  function mapByText(items, nodes) {
    // `nodes` are walkItems wrappers {type, text|src, node} — bucket by their
    // precomputed text/src and map each server item to a DOM element.
    var buckets = {};
    nodes.forEach(function (nd) {
      var key = nd.type === 'img' ? '@' + (nd.src || '') : '!' + (nd.text || '');
      (buckets[key] = buckets[key] || []).push(nd.node);
    });
    return items.map(function (it) {
      var key = it.type === 'img' ? '@' + (it.src || '') : '!' + (it.text || '');
      var arr = buckets[key];
      return (arr && arr.length) ? arr.shift() : null;
    });
  }

  function buildItems(doc) {
    var nodes = walkItems(doc);
    var items = state.items || [];
    var map;
    if (nodes.length === items.length) {
      var ok = true;
      for (var i = 0; i < items.length; i++) {
        var a = items[i], b = nodes[i];
        if (a.type !== b.type) { ok = false; break; }
        if (a.type === 'img' ? (a.src || '') !== (b.src || '')
                             : (a.text || '') !== (b.text || '')) { ok = false; break; }
      }
      map = ok ? nodes.map(function (nd) { return nd.node; }) : mapByText(items, nodes);
    } else {
      map = mapByText(items, nodes);
    }
    state.nodes = nodes;
    state.nodeOf = map;
    map.forEach(function (nd, i) {
      if (nd) nd.__rdIdx = i;
    });
  }

  function climbToItem(target) {
    var cur = target;
    while (cur && cur !== fdoc.body) {
      if (cur.__rdIdx != null) return cur;
      cur = cur.parentNode;
    }
    return null;
  }

  /* ------------------------------------------------- position / highlight */
  function scroller() {
    if (!fdoc) return document.scrollingElement || document.documentElement;
    if (settings.mode === 'page') return fdoc.body;
    return fdoc.scrollingElement || fdoc.documentElement;
  }

  function scrollToNode(node) {
    if (!node || !fdoc) return;
    if (settings.mode === 'page') { setPage(pageOfNode(node)); return; }
    var sc = fdoc.scrollingElement || fdoc.documentElement;
    sc.scrollTop = Math.max(0, absTop(node) - sc.clientHeight * 0.25);
  }

  function setCurrent(i, opts) {
    opts = opts || {};
    if (!state.items.length) { state.cur = 0; return; }
    i = clamp(Number(i) || 0, 0, state.items.length - 1);
    if (state.curNode) state.curNode.classList.remove('rd-cur');
    state.cur = i;
    var node = (state.nodeOf && state.nodeOf[i]) || null;
    state.curNode = node;
    if (node) {
      node.classList.add('rd-cur');
      if (opts.scroll !== false) scrollToNode(node);
    }
    if (speak.on) markSpeaking();
    renderPosition();
    if (opts.save !== false) scheduleSidecar();
  }

  function markSpeaking() {
    if (!fdoc) return;
    qsa('.rd-speaking', fdoc).forEach(function (n) { n.classList.remove('rd-speaking'); });
    if (speak.on && state.curNode) state.curNode.classList.add('rd-speaking');
  }

  function chapterFraction() {
    if (!state.meta) return 0;
    var within = 0;
    if (settings.mode === 'page' && pgState.step) {
      within = pgState.total ? (currentPage() + 1) / pgState.total : 0;
    } else if (fdoc && state.frameReady) {
      var sc = fdoc.scrollingElement || fdoc.documentElement;
      var max = sc.scrollHeight - sc.clientHeight;
      if (max > 4) within = sc.scrollTop / max;
      else if (state.items.length) within = (state.cur + 1) / state.items.length;
    } else if (state.items.length) {
      within = (state.cur + 1) / state.items.length;
    }
    within = clamp(within, 0, 1);
    return clamp(((state.ch - 1) + within) / (state.meta.total || 1), 0, 1);
  }

  function etaText(f) {
    var chars = (state.meta && state.meta.chars) || 0;
    var mins = Math.round(Math.max(0, chars * (1 - f)) / 1300);
    if (mins < 1) return '';
    var time = mins < 60
      ? fmt('read.time_m', { m: mins })
      : fmt('read.time_hm', { h: Math.floor(mins / 60), m: mins % 60 });
    return fmt('read.eta', { time: time });
  }

  function renderPosition() {
    if (!state.meta) return;
    var total = state.meta.total || 1;
    setLabel('rd-pos', fmt('read.position', { ch: state.ch, total: total }));
    setLabel('rd-p-pos', fmt('read.position_item', { i: state.cur + 1, total: state.items.length || 0 }));
    var f = chapterFraction();
    var bar = el('rd-progress-bar');
    if (bar) bar.style.width = clamp(f * 100, 0, 100).toFixed(2) + '%';
    var prog = qs('.rd-progress');
    if (prog) prog.setAttribute('aria-valuenow', String(Math.round(f * 100)));
    setLabel('rd-eta', etaText(f));
    setLabel('rd-pageinfo',
      settings.mode === 'page'
        ? fmt('read.page_of', { p: currentPage() + 1, total: Math.max(1, pgState.total) })
        : '');
  }

  /* ----------------------------------------------------------- pagination */
  var layoutT = 0;

  function scheduleLayout() {
    if (layoutT) clearTimeout(layoutT);
    layoutT = setTimeout(function () {
      layoutT = 0;
      if (state.frameReady && settings.mode === 'page') { layoutPages(true); renderPosition(); }
    }, 140);
  }

  function clearPageLayout() {
    pgState = { step: 0, total: 1, cols: settings.cols, pgw: 0 };
    if (!fdoc) return;
    fdoc.documentElement.classList.remove('pg');
    var content = fdoc.querySelector('.rd-content');
    if (content) {
      content.style.width = '';
      content.style.height = '';
      content.style.removeProperty('column-width');
      content.style.removeProperty('column-gap');
    }
  }

  function layoutPages(keepPos) {
    if (!state.frameReady || !fdoc || settings.mode !== 'page') return;
    var root = fdoc.documentElement;
    var content = fdoc.querySelector('.rd-content');
    if (!content) return;

    var node = (keepPos && state.curNode && fdoc.contains(state.curNode)) ? state.curNode : null;
    var vw = root.clientWidth || window.innerWidth;
    var vh = root.clientHeight || window.innerHeight;
    var padx = settings.margin;
    var padY = Math.max(settings.margin, 16);
    var gap = 32;
    // two-column spreads are unreadable on phones — force a single column
    var cols = (settings.cols === 2 && vw >= 700) ? 2 : 1;
    var pgw = Math.max(160, Math.floor((vw - 2 * padx - gap * (cols - 1)) / cols));

    root.classList.add('pg');
    // measure the natural single-column height at this column width
    content.style.height = 'auto';
    content.style.width = (pgw + 2 * padx) + 'px';
    content.style.setProperty('column-width', pgw + 'px');
    content.style.setProperty('column-gap', gap + 'px');
    var natural = content.offsetHeight;
    var usable = Math.max(80, vh - 2 * padY);
    var n = Math.max(1, Math.ceil(Math.max(natural - 2 * padY, 1) / usable));
    // strip = columns rounded up to whole spreads so the last page can align
    var stripCols = Math.ceil(n / cols) * cols;
    var step = pgw + gap;
    content.style.width = (stripCols * step - gap + 2 * padx) + 'px';
    content.style.height = '';           // CSS: height 100vh
    pgState = { step: step, total: n, cols: cols, pgw: pgw };

    if (node) setPage(pageOfNode(node));
    else if (state.pos && state.pos.ch === state.ch && state.pos.pg) setPage(state.pos.pg);
  }

  function pageOfNode(node) {
    if (!pgState.step || !fdoc || !node) return 0;
    var content = fdoc.querySelector('.rd-content');
    if (!content) return 0;
    var rel = node.getBoundingClientRect().left - content.getBoundingClientRect().left;
    var col = Math.max(0, Math.floor(rel / pgState.step + 0.001));
    var page = Math.floor(col / pgState.cols);
    return clamp(page, 0, Math.max(0, pgState.total - 1));
  }

  function currentPage() {
    if (!pgState.step || !fdoc || settings.mode !== 'page') return 0;
    var p = Math.round(fdoc.body.scrollLeft / (pgState.step * pgState.cols));
    return clamp(p, 0, Math.max(0, pgState.total - 1));
  }

  function setPage(p) {
    if (!state.frameReady || !pgState.step) return;
    p = clamp(Number(p) || 0, 0, Math.max(0, pgState.total - 1));
    var left = p * pgState.step * pgState.cols;
    try { fdoc.body.scrollTo({ left: left, behavior: 'auto' }); }
    catch (e) { fdoc.body.scrollLeft = left; }
    renderPosition();
    scheduleSidecar();
  }

  function flipPage(dir) {
    if (settings.mode === 'page') { setPage(currentPage() + dir); return; }
    var sc = scroller();
    var d = (sc.clientHeight || 600) * 0.92 * dir;
    if (sc.scrollBy) sc.scrollBy({ top: d, behavior: 'smooth' });
    else sc.scrollTop += d;
  }

  /* ------------------------------------------------------- chapter nav */
  function updateChapterUI() {
    var sel = el('rd-chapter');
    if (sel) sel.value = String(state.ch);
    var pv = el('rd-prev-ch'), nx = el('rd-next-ch');
    if (pv) pv.disabled = state.ch <= 1;
    if (nx) nx.disabled = !state.meta || state.ch >= state.meta.total;
    markToc();
  }

  function goChapter(n, opts) {
    opts = opts || {};
    if (!state.meta) return Promise.resolve(null);
    var target = clamp(Number(n) || 1, 1, state.meta.total);
    stopForNavigation();
    flushSidecar(false);
    if (target === state.ch && state.frameReady && !opts.frag && opts.item == null) return Promise.resolve(state.chapter);
    return loadChapter(target, opts);
  }

  function stepChapter(dir) {
    if (!state.meta) return;
    if (state.ch + dir < 1 || state.ch + dir > state.meta.total) return;
    goChapter(state.ch + dir, {});
  }

  function stepItem(delta) {
    if (!state.items.length) return;
    var i = state.cur + delta;
    if (i < 0) {
      if (state.chapter && state.chapter.prev) goChapter(state.chapter.prev, { item: 1e9 });
      return;
    }
    if (i >= state.items.length) {
      if (state.chapter && state.chapter.next) goChapter(state.chapter.next, { item: 0 });
      return;
    }
    setCurrent(i, { scroll: true });
  }

  function jumpToId(id) {
    closeFootnote();
    if (!fdoc || !id) return;
    var t = fdoc.getElementById(id);
    if (!t && fdoc.getElementsByName) {
      var nl = fdoc.getElementsByName(id);
      if (nl && nl.length) t = nl[0];
    }
    if (!t) return;
    var item = climbToItem(t);
    if (item && item.__rdIdx != null) setCurrent(item.__rdIdx, { scroll: true });
    else scrollToNode(t);
  }

  /* --------------------------------------------------------------- TOC */
  function chapterLabel(c) {
    var title = c.title || '';
    if (title.length > 70) title = title.slice(0, 67) + '…';
    return c.n + '. ' + title;
  }

  function buildSelect() {
    var sel = el('rd-chapter');
    if (!sel || !state.meta) return;
    sel.textContent = '';
    state.meta.chapters.forEach(function (c) {
      var opt = document.createElement('option');
      opt.value = c.n;
      opt.textContent = chapterLabel(c);
      sel.appendChild(opt);
    });
  }

  function renderUl(nodes) {
    var ul = document.createElement('ul');
    nodes.forEach(function (nd) {
      var li = document.createElement('li');
      li.dataset.n = nd.n != null ? String(nd.n) : '';
      var row = document.createElement('div');
      row.className = 'rd-node';

      var kids = nd.children || [];
      var tog = document.createElement('button');
      tog.type = 'button';
      tog.className = 'rd-node-toggle' + (kids.length ? '' : ' is-empty');
      tog.textContent = kids.length ? '▾' : '·';
      tog.setAttribute('aria-expanded', 'true');
      if (kids.length) {
        tog.addEventListener('click', function () {
          var collapsed = li.classList.toggle('is-collapsed');
          tog.textContent = collapsed ? '▸' : '▾';
          tog.setAttribute('aria-expanded', String(!collapsed));
        });
      }

      var go = document.createElement('button');
      go.type = 'button';
      go.className = 'rd-node-go';
      var title = nd.title || (nd.n ? chapterLabel({ n: nd.n, title: '' }) : '');
      go.textContent = title;
      go.title = title;
      if (nd.n == null) { go.disabled = true; go.classList.add('is-muted'); }
      else {
        go.addEventListener('click', function () {
          var frag = nd.frag || '';
          closeFootnote();
          goChapter(nd.n, frag ? { frag: frag } : {});
          togglePanel('toc');
        });
      }

      row.appendChild(tog);
      row.appendChild(go);
      li.appendChild(row);
      if (kids.length) li.appendChild(renderUl(kids));
      ul.appendChild(li);
    });
    return ul;
  }

  function buildToc() {
    var tree = el('rd-toc-tree');
    if (!tree || !state.meta) return;
    tree.textContent = '';
    var roots = null;
    if (state.toc && state.toc.toc && state.toc.toc.length) roots = state.toc.toc;
    else roots = state.meta.chapters.map(function (c) { return { title: c.title, n: c.n, children: [] }; });
    tree.appendChild(renderUl(roots));
    setLabel('rd-toc-count', '(' + state.meta.total + ')');
    buildLandmarks();
    markToc();
  }

  function buildLandmarks() {
    var box = el('rd-toc-landmarks');
    if (!box) return;
    box.textContent = '';
    var entries = [];
    if (state.toc && state.toc.landmarks) entries = entries.concat(state.toc.landmarks);
    if (state.toc && state.toc.pages && state.toc.pages.length <= 30) {
      entries = entries.concat(state.toc.pages);
    }
    entries = entries.filter(function (e) { return e && e.n != null; });
    box.hidden = !entries.length;
    if (!entries.length) return;
    var head = document.createElement('span');
    head.className = 'muted small';
    head.textContent = i18n('read.landmarks');
    box.appendChild(head);
    entries.forEach(function (e) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = e.title || e.type || String(e.n);
      b.addEventListener('click', function () {
        closeFootnote();
        goChapter(e.n, e.frag ? { frag: e.frag } : {});
        togglePanel('toc');
      });
      box.appendChild(b);
    });
  }

  function markToc() {
    var sel = el('rd-chapter');
    if (sel && document.activeElement !== sel) sel.value = String(state.ch);
    qsa('#rd-toc-tree .rd-node-go').forEach(function (b) { b.removeAttribute('aria-current'); });
    var li = qs('#rd-toc-tree li[data-n="' + state.ch + '"]');
    if (!li) return;
    var go = qs('.rd-node-go', li);
    if (go) {
      go.setAttribute('aria-current', 'true');
      var tree = el('rd-toc-tree');
      if (tree && !tree.hidden && el('rd') && el('rd').dataset.panel === 'toc') {
        try { go.scrollIntoView({ block: 'nearest' }); } catch (e) { /* noop */ }
      }
    }
    // expand ancestors
    var cur = li.parentNode;
    while (cur && cur !== el('rd-toc-tree')) {
      if (cur.tagName === 'LI') {
        cur.classList.remove('is-collapsed');
        var t = qs('.rd-node-toggle', cur);
        if (t) { t.textContent = '▾'; t.setAttribute('aria-expanded', 'true'); }
      }
      cur = cur.parentNode;
    }
  }

  /* ------------------------------------------------- links & footnotes */
  function closeFootnote() {
    var fn = el('rd-fn');
    if (fn) fn.hidden = true;
    if (state.fnAnchor && state.fnAnchor.classList) state.fnAnchor.classList.remove('rd-fnmark');
    state.fnAnchor = null;
  }

  function isFootnote(a, target, id) {
    var at = (a.getAttribute('epub:type') || a.getAttribute('type') || '').toLowerCase();
    if (at.indexOf('noteref') >= 0) return true;
    if (/^page/i.test(id)) return false;
    if (target.closest('aside, footer, [role="doc-footnote"]')) return true;
    var tt = (target.getAttribute('epub:type') || target.getAttribute('type') || '').toLowerCase();
    if (tt.indexOf('footnote') >= 0 || tt.indexOf('endnote') >= 0) return true;
    if (/^(fn|fnote|foot|note|endnote|noteref)/i.test(id)) return true;
    if (/^\d+(_\d+)?$/.test(id)) return true;
    return false;
  }

  function showFootnote(anchor, target) {
    var fn = el('rd-fn');
    var body = el('rd-fn-body');
    if (!fn || !body) return;
    closeFootnote();
    body.textContent = '';
    var clone = target.cloneNode(true);
    var text = (target.textContent || '').replace(/\s+/g, ' ').trim();
    if (text.length > 4000) clone.textContent = text.slice(0, 4000) + '…';
    body.appendChild(clone);
    fn.hidden = false;

    var rd = el('rd');
    var rdRect = rd.getBoundingClientRect();
    var aRect = anchor.getBoundingClientRect();
    var left = aRect.left - rdRect.left;
    var top = aRect.bottom - rdRect.top + 8;
    var w = fn.offsetWidth, h = fn.offsetHeight;
    left = clamp(left, 8, Math.max(8, rdRect.width - w - 8));
    if (top + h > rdRect.height - 8) top = Math.max(8, aRect.top - rdRect.top - h - 8);
    fn.style.left = left + 'px';
    fn.style.top = top + 'px';

    if (anchor.classList) anchor.classList.add('rd-fnmark');
    state.fnAnchor = anchor;
    var close = el('rd-fn-close');
    if (close) close.focus();
  }

  function handleFrag(a, rawId) {
    var id = rawId;
    try { id = decodeURIComponent(rawId); } catch (e) { /* keep raw */ }
    if (!fdoc || !id) return;
    var target = fdoc.getElementById(id);
    if (!target && fdoc.getElementsByName && fdoc.getElementsByName(id).length) {
      target = fdoc.getElementsByName(id)[0];
    }
    if (!target) return;
    if (isFootnote(a, target, id)) showFootnote(a, target);
    else jumpToId(id);
  }

  function handleLink(a) {
    var href = a.getAttribute('href') || '';
    if (!href) return;
    if (href.charAt(0) === '#') { handleFrag(a, href.slice(1)); return; }
    if (/^(https?:|mailto:)/i.test(href)) {
      if (window.confirm(i18n('read.external_link'))) window.open(href, '_blank', 'noopener');
      return;
    }
    var m = href.match(/[?&]n=(\d+)/);
    if (m && href.indexOf('/api/reader/doc') >= 0) {
      var n = Number(m[1]);
      var hash = href.indexOf('#');
      var frag = hash >= 0 ? href.slice(hash + 1) : '';
      if (n === state.ch && frag) { jumpToId(frag); return; }
      if (n === state.ch && !frag) return;
      stopForNavigation();
      flushSidecar(false);
      closeFootnote();
      loadChapter(n, { frag: frag, push: true });
      return;
    }
    // any other relative URL died during sanitizing — ignore it
  }

  /* -------------------------------------------------------- iframe wiring */
  var wheelAcc = 0, wheelLock = 0;
  var scrollT = 0;
  var touchXY = null;

  function onFrameClick(ev) {
    wakeBar();
    var t = ev.target;
    if (!t || !t.closest) return;
    var a = t.closest ? t.closest('a[href]') : null;
    if (a) {
      ev.preventDefault();
      handleLink(a);
      return;
    }
    if (el('rd-fn') && !el('rd-fn').hidden) closeFootnote();
    var item = climbToItem(t);
    if (item && item.__rdIdx != null) {
      if (speak.on) stopForNavigation();
      setCurrent(item.__rdIdx, { scroll: false });
    }
  }

  function onFrameScroll() {
    wakeBar();
    if (scrollT) return;
    scrollT = setTimeout(function () {
      scrollT = 0;
      renderPosition();
      scheduleSidecar();
    }, 400);
  }

  function onWheel(ev) {
    if (settings.mode !== 'page') return;
    ev.preventDefault();
    var now = Date.now();
    if (now < wheelLock) { wheelAcc = 0; return; }
    var d = Math.abs(ev.deltaY) >= Math.abs(ev.deltaX) ? ev.deltaY : ev.deltaX;
    if (ev.deltaMode === 1) d *= 16;        // lines → px
    else if (ev.deltaMode === 2) d *= window.innerHeight;
    wheelAcc += d;
    if (Math.abs(wheelAcc) > 50) {
      flipPage(wheelAcc > 0 ? 1 : -1);
      wheelAcc = 0;
      wheelLock = now + 260;
    }
  }

  function onTouchStart(ev) {
    if (!ev.touches || !ev.touches.length) return;
    touchXY = { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
  }

  function onTouchEnd(ev) {
    if (!touchXY || !ev.changedTouches || !ev.changedTouches.length) return;
    var dx = ev.changedTouches[0].clientX - touchXY.x;
    var dy = ev.changedTouches[0].clientY - touchXY.y;
    touchXY = null;
    if (Math.abs(dx) < 60 || Math.abs(dy) > 48) return;
    if (settings.mode === 'page') { flipPage(dx < 0 ? 1 : -1); return; }
    if (dx < 0) stepChapter(1); else stepChapter(-1);
  }

  function bindFrame(doc) {
    doc.addEventListener('click', onFrameClick, true);
    doc.addEventListener('keydown', onKeydown);
    doc.addEventListener('scroll', onFrameScroll, true);
    doc.addEventListener('wheel', onWheel, { passive: false });
    doc.addEventListener('touchstart', onTouchStart, { passive: true });
    doc.addEventListener('touchend', onTouchEnd, { passive: true });
    ['mousemove', 'keydown', 'touchstart'].forEach(function (evt) {
      doc.addEventListener(evt, wakeBar, { passive: true });
    });
  }

  /* ------------------------------------------------------------ panels */
  function togglePanel(name) {
    var rd = el('rd');
    if (!rd) return;
    var open = rd.dataset.panel === name ? '' : (name || '');
    rd.dataset.panel = open;
    ['toc', 'font', 'tts'].forEach(function (n) {
      var panel = el('rd-panel-' + n);
      var btn = el('rd-open-' + n);
      var show = n === open;
      if (panel) panel.hidden = !show;
      if (btn) btn.setAttribute('aria-pressed', String(show));
    });
    if (open === 'tts') warmVoices();
    if (open === 'toc') markToc();
    resizeStage();
    requestAnimationFrame(resizeStage);
  }

  /* --------------------------------------------------------------- TTS */
  var tts = { state: 'unknown', voices: [], def: null, poll: 0, tries: 0 };
  var speak = { on: false, paused: false, i: 0, ch: 0, token: 0, audio: null, cache: {}, inflight: {} };

  function ttsStatus(text, cls) {
    var n = el('rd-tts-status');
    if (!n) return;
    n.textContent = text || '';
    n.className = 'muted small' + (cls ? ' ' + cls : '');
  }

  function warmVoices() {
    getJSON(R.voicesUrl).then(function (data) {
      tts.state = data.state;
      if (data.state === 'ready') {
        tts.voices = data.voices || [];
        tts.def = data.default;
        renderVoices();
        ttsStatus(fmt('read.tts_ready', { n: tts.voices.length, sec: data.load_seconds || 0 }));
        if (tts.poll) { clearTimeout(tts.poll); tts.poll = 0; }
      } else if (data.state === 'error') {
        ttsStatus(i18n('read.tts_error') + ' ' + (data.error || ''), 'error');
        if (tts.poll) { clearTimeout(tts.poll); tts.poll = 0; }
      } else {
        ttsStatus(i18n('read.tts_loading'));
        if (!tts.poll && tts.tries < 90) {
          tts.tries++;
          tts.poll = setTimeout(warmVoices, 2000);
        }
      }
    }).catch(function (e) {
      ttsStatus(i18n('read.tts_error') + ' ' + e.message, 'error');
    });
  }

  function renderVoices() {
    var sel = el('rd-tts-voice');
    if (!sel) return;
    sel.textContent = '';
    tts.voices.forEach(function (v) {
      var opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = v.label;
      if (v.desc) opt.title = v.desc;
      sel.appendChild(opt);
    });
    var want = ttsCfg.voice || tts.def;
    if (want && tts.voices.some(function (v) { return v.id === want; })) sel.value = want;
    ttsCfg.voice = sel.value;
    sel.disabled = !tts.voices.length;
    var rate = el('rd-tts-rate'), rateV = el('rd-tts-rate-v');
    if (rate) rate.value = ttsCfg.rate;
    if (rateV) rateV.textContent = 'x' + Number(ttsCfg.rate).toFixed(2);
  }

  function ttsPost(text) {
    return fetch(R.ttsUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, voice: ttsCfg.voice })
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (raw) {
          var data = null;
          try { data = JSON.parse(raw); } catch (e) { /* ignore */ }
          throw new Error((data && data.error) || ('HTTP ' + res.status));
        });
      }
      return res.blob();
    });
  }

  function cacheKey(ch, i) { return ch + ':' + i; }

  function chapterItems(n) {
    if (state.chapter && state.chapter.n === n) return Promise.resolve(state.items);
    return fetchChap(n).then(function (ch) { return ch.items; });
  }

  /* Audio blob for (chapter, item) — deduped, cached, one request in flight. */
  function audioFor(ch, i) {
    var k = cacheKey(ch, i);
    if (speak.cache[k]) return Promise.resolve(speak.cache[k]);
    if (speak.inflight[k]) return speak.inflight[k];

    var p = chapterItems(ch).then(function (items) {
      var it = items[i];
      if (!it || it.type !== 'p') return null;
      return ttsPost(it.text).then(function (blob) {
        var keys = Object.keys(speak.cache);
        if (keys.length > 6) delete speak.cache[keys[0]];
        speak.cache[k] = blob;
        return blob;
      });
    }).then(function (blob) {
      delete speak.inflight[k];
      return blob;
    }, function (e) {
      delete speak.inflight[k];
      throw e;
    });
    speak.inflight[k] = p;
    return p;
  }

  /* One-deep lookahead: VieNeu synthesizes at ~0.45x real time, so a single
     request in flight already keeps playback gapless — queueing two would just
     make the second wait behind the first on the server's synthesis lock. */
  function prefetchCurrent(i) {
    if (i + 1 < state.items.length) audioFor(state.ch, i + 1).catch(noop);
    if (i + 1 >= state.items.length && state.chapter && state.chapter.next) {
      audioFor(state.chapter.next, 0).catch(noop);
    }
  }

  function speakNote(text) {
    var n = el('rd-tts-note');
    if (n) n.textContent = text || '';
  }

  function playBlob(blob, token) {
    if (token !== speak.token) return;
    var url = URL.createObjectURL(blob);
    if (speak.audio) {
      speak.audio.pause();
      try { URL.revokeObjectURL(speak.audio.src); } catch (e) { /* already revoked */ }
    }
    var audio = new Audio(url);
    speak.audio = audio;
    audio.playbackRate = ttsCfg.rate;
    audio.onended = function () {
      try { URL.revokeObjectURL(url); } catch (e) { /* already revoked */ }
      speakNext();
    };
    audio.onerror = function () { speakFail(i18n('read.tts_play_fail')); };
    audio.play().catch(function (e) { speakFail(e && e.message ? e.message : String(e)); });
  }

  function speakFail(message) {
    speakStop(false);
    speakNote(message);
    ttsStatus(message, 'error');
  }

  function speakStep() {
    if (!speak.on || speak.paused) return;
    var token = speak.token;

    if (speak.i >= state.items.length) {
      var next = state.chapter && state.chapter.next;
      if (!next) { speakDone(); return; }
      // keep reading: advance chapters without stopping the session
      loadChapter(next, { item: 0, push: true }).then(function () {
        if (token !== speak.token || !speak.on) return;
        speak.i = 0;
        speakStep();
      });
      return;
    }

    var it = state.items[speak.i];
    if (!it || it.type !== 'p') { speak.i++; speakStep(); return; }

    speak.ch = state.ch;
    setCurrent(speak.i, { scroll: true });
    markSpeaking();
    speakNote(fmt('read.tts_speaking', { ch: state.ch, i: speak.i + 1, total: state.items.length }));
    ttsStatus(i18n('read.tts_synthesizing'));

    var idx = speak.i;
    audioFor(state.ch, idx).then(function (blob) {
      if (token !== speak.token) return;
      if (!blob) { speak.i++; speakStep(); return; }
      ttsStatus(i18n('read.tts_playing'));
      playBlob(blob, token);
      prefetchCurrent(idx);
    }).catch(function (e) {
      if (token !== speak.token) return;
      speakFail(e && e.message ? e.message : String(e));
    });
  }

  function speakNext() {
    if (!speak.on || speak.paused) return;
    speak.i++;
    speakStep();
  }

  function speakStart() {
    if (!state.chapter) return;
    speak.on = true;
    speak.paused = false;
    speak.token++;
    speak.i = state.cur;
    speak.ch = state.ch;
    var play = el('rd-tts-play'), pause = el('rd-tts-pause'), stop = el('rd-tts-stop');
    if (play) play.hidden = true;
    if (pause) pause.hidden = false;
    if (stop) stop.hidden = false;
    markSpeaking();
    speakStep();
  }

  function speakPause() {
    if (!speak.on) return;
    speak.paused = true;
    if (speak.audio) speak.audio.pause();
    var pause = el('rd-tts-pause'), play = el('rd-tts-play');
    if (pause) pause.hidden = true;
    if (play) play.hidden = false;
    ttsStatus(i18n('read.tts_paused'));
    speakNote(i18n('read.tts_paused_note'));
  }

  function speakResume() {
    if (!speak.on) return;
    speak.paused = false;
    var play = el('rd-tts-play'), pause = el('rd-tts-pause');
    if (play) play.hidden = true;
    if (pause) pause.hidden = false;
    if (speak.audio && speak.audio.paused && speak.audio.src) {
      ttsStatus(i18n('read.tts_playing'));
      speak.audio.play().catch(function () { speakStep(); });
    } else {
      speakStep();
    }
  }

  function speakStop(updateNote) {
    speak.on = false;
    speak.paused = false;
    speak.token++;
    if (speak.audio) {
      speak.audio.pause();
      try { URL.revokeObjectURL(speak.audio.src); } catch (e) { /* already revoked */ }
      speak.audio = null;
    }
    var play = el('rd-tts-play'), pause = el('rd-tts-pause'), stop = el('rd-tts-stop');
    if (play) play.hidden = false;
    if (pause) pause.hidden = true;
    if (stop) stop.hidden = true;
    ttsStatus(tts.state === 'ready' ? fmt('read.tts_ready_n', { n: tts.voices.length }) : ttsStatusText());
    if (updateNote !== false) speakNote('');
    markSpeaking();
  }

  function ttsStatusText() {
    return tts.state === 'ready' ? '' : i18n('read.tts_loading');
  }

  function speakDone() {
    speakStop(false);
    speakNote(i18n('read.tts_finished'));
    ttsStatus(fmt('read.tts_ready_n', { n: tts.voices.length }));
  }

  function stopForNavigation() {
    if (speak.on) speakStop(true);
  }

  /* ------------------------------------------- fullscreen / chrome hide */
  var hideT = 0;

  function wakeBar() {
    var bar = el('rd-bar');
    if (!bar) return;
    bar.classList.remove('is-hidden');
    if (hideT) clearTimeout(hideT);
    hideT = 0;
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      hideT = setTimeout(function () {
        hideT = 0;
        var b = el('rd-bar');
        if (b) b.classList.add('is-hidden');
      }, 2600);
    }
  }

  function toggleFullscreen() {
    try {
      if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        var d = document.documentElement;
        (d.requestFullscreen || d.webkitRequestFullscreen).call(d);
      } else {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      }
    } catch (e) { /* unsupported */ }
  }

  /* ---------------------------------------------------------- stage size */
  var resizeRaf = 0;

  function resizeStage() {
    var stage = el('rd-stage');
    if (!stage) return;
    var rect = stage.getBoundingClientRect();
    var jump = el('rd-jump');
    var jumpH = jump ? jump.offsetHeight + 8 : 0;
    var h = Math.round(window.innerHeight - rect.top - jumpH - 8);
    if (h < 320) h = 320;
    if (Math.abs(h - stage.offsetHeight) > 3) stage.style.height = h + 'px';
    scheduleLayout();
  }

  function resizeSoon() {
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(function () { resizeRaf = 0; resizeStage(); });
  }

  /* ------------------------------------------------------------ keyboard */
  function onKeydown(ev) {
    if (ev.defaultPrevented) return;
    var t = ev.target;
    var tag = ((t && t.tagName) || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || (t && t.isContentEditable)) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    var k = ev.key;

    if (k === 'Escape' || k === 'Esc') {
      var fn = el('rd-fn');
      if (fn && !fn.hidden) { closeFootnote(); return; }
      if (document.fullscreenElement || document.webkitFullscreenElement) { toggleFullscreen(); return; }
      var rd = el('rd');
      if (rd && rd.dataset.panel) togglePanel(rd.dataset.panel);
      return;
    }

    switch (k) {
      case 'ArrowLeft':
        stopForNavigation(); stepChapter(-1); wakeBar(); return;
      case 'ArrowRight':
        stopForNavigation(); stepChapter(1); wakeBar(); return;
      case 'PageDown':
        ev.preventDefault();
        if (settings.mode === 'page') flipPage(1); else stepItem(1);
        return;
      case 'PageUp':
        ev.preventDefault();
        if (settings.mode === 'page') flipPage(-1); else stepItem(-1);
        return;
      case ' ':
      case 'Spacebar':
        if (tag === 'button' || tag === 'a' || tag === 'label') return;
        ev.preventDefault();
        flipPage(1);
        return;
      case 'Home':
        ev.preventDefault();
        setCurrent(0, { scroll: true });
        if (settings.mode === 'page') setPage(0);
        return;
      case 'End':
        ev.preventDefault();
        setCurrent(state.items.length - 1, { scroll: true });
        return;
      case '+':
      case '=':
        settings.size = clamp(settings.size + 1, 14, 30);
        saveSettings(); applySettings(); return;
      case '-':
      case '_':
        settings.size = clamp(settings.size - 1, 14, 30);
        saveSettings(); applySettings(); return;
      case 't': case 'T': togglePanel('toc'); return;
      case 'f': case 'F': togglePanel('font'); return;
      case 'r': case 'R': togglePanel('tts'); return;
      default: return;
    }
  }

  /* ------------------------------------------------------ font upload */
  function handleFontFile(file) {
    if (!file) return;
    if (!file.arrayBuffer) return;
    file.arrayBuffer().then(function (buf) {
      fontBuf = buf;
      settings.family = 'custom';
      var sel = el('rd-set-family');
      if (sel && !qs('option[value="custom"]', sel)) {
        var opt = document.createElement('option');
        opt.value = 'custom';
        opt.textContent = file.name;
        sel.appendChild(opt);
      } else if (sel) {
        var existing = qs('option[value="custom"]', sel);
        if (existing) existing.textContent = file.name;
      }
      saveSettings();
      applySettings();
    }).catch(function () { /* unreadable font file */ });
  }

  function injectFont(doc) {
    if (settings.family !== 'custom' || !fontBuf || !doc.fonts || typeof FontFace === 'undefined') {
      return Promise.resolve();
    }
    try {
      var ff = new FontFace('RDUserFont', fontBuf.slice(0));
      return ff.load().then(function (loaded) {
        doc.fonts.add(loaded);
        scheduleLayout();
      }).catch(function () { /* fall back to serif */ });
    } catch (e) { return Promise.resolve(); }
  }

  /* ------------------------------------------------------------- wiring */
  function bindStatic() {
    // panels
    ['toc', 'font', 'tts'].forEach(function (n) {
      var btn = el('rd-open-' + n);
      if (btn) btn.addEventListener('click', function () { togglePanel(n); });
    });
    var fnClose = el('rd-fn-close');
    if (fnClose) fnClose.addEventListener('click', closeFootnote);

    // chapter nav
    var prevCh = el('rd-prev-ch'), nextCh = el('rd-next-ch'), sel = el('rd-chapter');
    if (prevCh) prevCh.addEventListener('click', function () { stepChapter(-1); });
    if (nextCh) nextCh.addEventListener('click', function () { stepChapter(1); });
    if (sel) sel.addEventListener('change', function () {
      stopForNavigation();
      flushSidecar(false);
      loadChapter(Number(this.value), {});
    });

    // paragraph nav + pages
    var prevP = el('rd-prev-p'), nextP = el('rd-next-p');
    if (prevP) prevP.addEventListener('click', function () { stepItem(-1); });
    if (nextP) nextP.addEventListener('click', function () { stepItem(1); });
    var prevPage = el('rd-prev-page'), nextPage = el('rd-next-page');
    if (prevPage) prevPage.addEventListener('click', function () { flipPage(-1); });
    if (nextPage) nextPage.addEventListener('click', function () { flipPage(1); });

    // fullscreen
    var fs = el('rd-fullscreen');
    if (fs) fs.addEventListener('click', toggleFullscreen);

    // reading mode / columns / themes / alignment
    qsa('.rd-mode').forEach(function (b) {
      b.addEventListener('click', function () { setMode(b.dataset.mode); });
    });
    qsa('.rd-cols').forEach(function (b) {
      b.addEventListener('click', function () {
        settings.cols = Number(b.dataset.cols) === 2 ? 2 : 1;
        saveSettings(); applySettings();
      });
    });
    qsa('.rd-theme').forEach(function (b) {
      b.addEventListener('click', function () {
        settings.theme = b.dataset.theme;
        saveSettings(); applySettings();
      });
    });
    qsa('.rd-align').forEach(function (b) {
      b.addEventListener('click', function () {
        settings.align = b.dataset.align === 'justify' ? 'justify' : 'left';
        saveSettings(); applySettings();
      });
    });

    // appearance controls
    var bindRange = function (id, key, after) {
      var n = el(id);
      if (!n) return;
      n.addEventListener('input', function () {
        settings[key] = Number(this.value);
        saveSettings();
        applySettings();
        if (after) after();
      });
    };
    var fam = el('rd-set-family');
    if (fam) fam.addEventListener('change', function () {
      if (this.value === 'custom' && !fontBuf) { this.value = settings.family; return; }
      settings.family = this.value;
      saveSettings(); applySettings();
    });
    var fontfile = el('rd-set-fontfile');
    if (fontfile) fontfile.addEventListener('change', function () { handleFontFile(this.files && this.files[0]); });
    bindRange('rd-set-size', 'size');
    bindRange('rd-set-lh', 'lh');
    bindRange('rd-set-width', 'width');
    bindRange('rd-set-ls', 'ls');
    bindRange('rd-set-margin', 'margin');
    var ow = el('rd-set-ow');
    if (ow) ow.addEventListener('change', function () { settings.breakWords = this.checked; saveSettings(); applySettings(); });
    var ov = el('rd-set-override');
    if (ov) ov.addEventListener('change', function () { settings.overrideFont = this.checked; saveSettings(); applySettings(); });
    var bg = el('rd-set-bg'), fg = el('rd-set-fg');
    if (bg) bg.addEventListener('input', function () { settings.customBg = this.value; saveSettings(); applySettings(); });
    if (fg) fg.addEventListener('input', function () { settings.customFg = this.value; saveSettings(); applySettings(); });
    var reset = el('rd-set-reset');
    if (reset) reset.addEventListener('click', function () {
      settings = Object.assign({}, DEFAULTS);
      saveSettings(); applySettings();
    });

    // TTS controls
    var rate = el('rd-tts-rate');
    if (rate) rate.addEventListener('input', function () {
      ttsCfg.rate = Number(this.value);
      var v = el('rd-tts-rate-v');
      if (v) v.textContent = 'x' + ttsCfg.rate.toFixed(2);
      if (speak.audio) speak.audio.playbackRate = ttsCfg.rate;
      saveJSON(ttsKey, ttsCfg);
    });
    var voice = el('rd-tts-voice');
    if (voice) voice.addEventListener('change', function () {
      ttsCfg.voice = this.value;
      saveJSON(ttsKey, ttsCfg);
    });
    var play = el('rd-tts-play');
    if (play) play.addEventListener('click', function () {
      if (speak.on && speak.paused) { speakResume(); return; }
      if (!tts.voices.length) { warmVoices(); speakNote(i18n('read.tts_loading')); return; }
      speakStart();
    });
    var pause = el('rd-tts-pause');
    if (pause) pause.addEventListener('click', speakPause);
    var stop = el('rd-tts-stop');
    if (stop) stop.addEventListener('click', function () { speakStop(true); });
    var test = el('rd-tts-test');
    if (test) test.addEventListener('click', function () {
      if (!tts.voices.length) { warmVoices(); return; }
      ttsStatus(i18n('read.tts_synthesizing'));
      ttsPost(i18n('read.test_text')).then(function (blob) {
        ttsStatus(i18n('read.tts_playing'));
        var audio = new Audio(URL.createObjectURL(blob));
        audio.playbackRate = ttsCfg.rate;
        audio.onended = function () { ttsStatus(fmt('read.tts_ready_n', { n: tts.voices.length })); };
        audio.play();
      }).catch(function (e) {
        ttsStatus(i18n('read.tts_error') + ' ' + e.message, 'error');
      });
    });

    // parent-level input
    document.addEventListener('keydown', onKeydown);
    document.addEventListener('mousemove', wakeBar, { passive: true });
    window.addEventListener('resize', resizeSoon);
    window.addEventListener('scroll', resizeSoon, { passive: true });
    document.addEventListener('mousedown', function (ev) {
      var fn = el('rd-fn');
      if (!fn || fn.hidden) return;
      if (fn.contains(ev.target)) return;
      closeFootnote();
    });
    var stage = el('rd-stage');
    if (stage) {
      stage.addEventListener('touchstart', onTouchStart, { passive: true });
      stage.addEventListener('touchend', onTouchEnd, { passive: true });
    }
    document.addEventListener('fullscreenchange', onFsChange);
    document.addEventListener('webkitfullscreenchange', onFsChange);
    window.addEventListener('pagehide', function () { flushSidecar(true); });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') flushSidecar(true);
    });
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onMq = function () { if (settings.theme === 'system') applySettings(); };
      if (mq.addEventListener) mq.addEventListener('change', onMq);
      else if (mq.addListener) mq.addListener(onMq);
    }
    window.addEventListener('popstate', function (ev) {
      var n = ev.state && ev.state.rdCh;
      if (!n || !state.meta) return;
      stopForNavigation();
      flushSidecar(false);
      var pos = sessionPos[n] || (bootPos && bootPos.ch === n ? bootPos : null);
      loadChapter(n, { pos: pos, push: false });
    });
  }

  function onFsChange() {
    var on = !!(document.fullscreenElement || document.webkitFullscreenElement);
    var b = el('rd-fullscreen');
    if (b) b.setAttribute('aria-pressed', String(on));
    if (!on && hideT) { clearTimeout(hideT); hideT = 0; }
    wakeBar();
    resizeStage();
    requestAnimationFrame(resizeStage);
  }

  function setMode(mode) {
    var m = mode === 'page' ? 'page' : 'scroll';
    if (m === settings.mode) return;
    // remember where we were, flip layout, come back to the same paragraph
    capturePos();
    settings.mode = m;
    saveSettings();
    applySettings();
    if (state.frameReady) {
      if (m === 'page') layoutPages(false);
      else clearPageLayout();
      var node = (state.curNode && fdoc.contains(state.curNode)) ? state.curNode : null;
      if (node) {
        if (m === 'page') setPage(pageOfNode(node));
        else scrollToNode(node);
      } else if (state.pos) {
        restorePos(state.pos);
      }
      renderPosition();
    }
  }

  /* --------------------------------------------------------------- boot */
  document.addEventListener('DOMContentLoaded', function () {
    frame = el('rd-frame');
    if (!frame) return;

    var localSet = loadJSON(setKey, null);
    if (localSet) settings = Object.assign({}, settings, localSet);
    var localPos = loadJSON(posKey, null);
    var savedTts = loadJSON(ttsKey, null);
    if (savedTts) ttsCfg = Object.assign(ttsCfg, savedTts);
    if (settings.family === 'custom') settings.family = 'serif';  // font is session-only

    applySettings();
    bindStatic();
    frame.addEventListener('load', onFrameLoad);
    resizeStage();
    showLoading(true);

    Promise.all([
      getJSON(bookUrl(R.metaUrl)),
      getJSON(bookUrl(R.stateUrl)).catch(function () { return null; }),
      getJSON(bookUrl(R.tocUrl)).catch(function () { return null; })
    ]).then(function (r) {
      var meta = r[0], remote = r[1], toc = r[2];
      state.meta = meta;
      state.toc = toc;
      if (remote && remote.settings) {
        settings = Object.assign({}, settings, remote.settings);
        if (settings.family === 'custom') settings.family = 'serif';
        saveJSON(setKey, settings);
      }
      applySettings();
      buildSelect();
      buildToc();
      var pos = (remote && remote.pos) || localPos || { ch: 1 };
      bootPos = pos;
      sessionPos[pos.ch || 1] = pos;
      try { history.replaceState({ rdCh: pos.ch || 1 }, ''); } catch (e) { /* noop */ }
      return loadChapter(pos.ch || 1, { pos: pos, push: false });
    }).catch(function (e) {
      showError(e);
    });

    warmVoices();
  });
})();
