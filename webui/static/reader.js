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
    setChromeAway(false);   // fresh chapter → toolbar/header back in view
    chromeReset();
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
    chromeReset();   // the previous document's scroll position means nothing here
    state.frameReady = true;
    pushVars(doc);
    if (settings.mode === 'page') layoutPages(false);
    buildItems(doc);
    bindFrame(doc);
    clearSent();   // sentence marks from the previous document died with it
    markChapterVisited(state.ch);

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

    // P2: re-apply highlights for this chapter (fresh srcdoc each load)
    applyAnnMarks();
    if (srch.pending) {
      applyPendingSearch();
    } else if (srch.q && srch.scope === 'chapter' &&
               el('rd-panel-search') && !el('rd-panel-search').hidden) {
      runChapterSearch(srch.q);
    }
    if (wantAnn) {
      var wa = null;
      anns.forEach(function (a) { if (a.id === wantAnn) wa = a; });
      wantAnn = null;
      if (wa) focusAnnMark(wa);
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
    chromeReset();   // programmatic jump — don't read it as a scroll gesture
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
    /* mousedown→mouseup over a selection still dispatches a click on the
       common ancestor. Hiding unconditionally here raced the 90ms
       selectionchange debounce: the popup appears mid-drag, then the
       trailing click killed it the moment the mouse was released (text
       stayed highlighted, popup gone). Keep it while a real selection
       exists — clicking elsewhere collapses the selection first. */
    var sel = null;
    try { sel = fdoc && fdoc.getSelection(); } catch (e) {}
    if (!sel || sel.isCollapsed || !sel.rangeCount) hideSelPop();
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
    chromeFrom('f', frameScrollY());
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
    doc.addEventListener('selectionchange', onSelChangeSoon);
    ['mousemove', 'keydown', 'touchstart'].forEach(function (evt) {
      doc.addEventListener(evt, wakeBar, { passive: true });   // rearm only
    });
    // the parent document never sees iframe events: a tap on the chapter text
    // (click) or a keypress inside the frame hands an idle-hidden bar back
    doc.addEventListener('click', idleReveal, { passive: true });
    doc.addEventListener('keydown', idleReveal, { passive: true });
  }

  /* --------------------------------------------- text match + marks */
  var WS_RX = /\s/;
  var ORIG_SEQ = 0;   // id generator: pieces split from one node share an id

  function textNodesOf(root) {
    var out = [], w, n;
    if (!root) return out;
    try { w = fdoc.createTreeWalker(root, 4 /* SHOW_TEXT */, null); }
    catch (e) { return out; }
    while ((n = w.nextNode())) out.push(n);
    return out;
  }

  /* Normalized text of one text node: whitespace runs → single space, ends
     trimmed — mirrors Python `_collapse` per node (textOf joins with " ").
     Map entries are {node, idx, end}: the raw span of each normalized char. */
  function normNode(node) {
    var data = node.data || '', text = '', map = [];
    var i = 0, L = data.length;
    while (i < L && WS_RX.test(data.charAt(i))) i++;
    while (i < L) {
      if (WS_RX.test(data.charAt(i))) {
        var run = i;
        while (i < L && WS_RX.test(data.charAt(i))) i++;
        if (i >= L) break;
        text += ' '; map.push({ node: node, idx: run, end: i });
      } else {
        text += data.charAt(i); map.push({ node: node, idx: i, end: i + 1 }); i++;
      }
    }
    return { text: text, map: map };
  }

  /* Marks split text nodes (splitText) — normalizing each piece separately
     would drop/insert boundary whitespace ("bold " + "SPANS" → "boldSPANS")
     and shift every later offset. Pieces split from ONE original node carry
     the same __rdOrig id and are normalized jointly here. */
  function normGroup(nodes) {
    var text = '', map = [], started = false, run = null;
    for (var k = 0; k < nodes.length; k++) {
      var node = nodes[k], data = node.data || '', L = data.length;
      for (var i = 0; i < L; i++) {
        if (WS_RX.test(data.charAt(i))) {
          if (!started) continue;                       // leading ws
          if (!run) run = { node: node, idx: i, end: i + 1 };
          else if (run.node === node) run.end = i + 1;  // same-node run grows
          // run crossing into another piece keeps the start node's span
        } else {
          if (!started) started = true;
          else if (run) { text += ' '; map.push(run); run = null; }  // join space
          text += data.charAt(i); map.push({ node: node, idx: i, end: i + 1 });
        }
      }
    }
    return { text: text, map: map };   // trailing ws run stays unemitted
  }

  /* Full normalized text of an element + segment table for offset mapping.
     One segment per "textual unit": an untouched original text node, or the
     group of pieces a mark split from one — so text is identical whether or
     not highlights are present. Between segments sits the single " " that
     textOf's join (and the server's get_text) produces. */
  function normFull(root) {
    var groups = [];
    textNodesOf(root).forEach(function (nd) {
      var oid = nd.__rdOrig, last = groups[groups.length - 1];
      if (oid != null && last && last.oid === oid) last.nodes.push(nd);
      else groups.push({ oid: (oid == null ? null : oid), nodes: [nd] });
    });
    var segs = [];
    groups.forEach(function (g) {
      var p = (g.nodes.length > 1) ? normGroup(g.nodes) : normNode(g.nodes[0]);
      if (p.text) segs.push(p);
    });
    var full = '', starts = [];
    segs.forEach(function (p, i) {
      starts.push(full.length + (i ? 1 : 0));
      if (i) full += ' ';
      full += p.text;
    });
    return { segs: segs, starts: starts, text: full };
  }

  /* Raw [from,to) runs per text node for the full-text span [a,b). */
  function rangeSegments(nf, a, b) {
    var out = [];
    for (var i = 0; i < nf.segs.length; i++) {
      var p = nf.segs[i], s = nf.starts[i], e = s + p.text.length;
      var oa = Math.max(a, s), ob = Math.min(b, e);
      if (oa >= ob) continue;
      var run = null;
      for (var j = oa - s; j < ob - s; j++) {
        var en = p.map[j];
        if (!en) continue;
        if (run && run.node === en.node) { run.to = en.end; continue; }
        if (run) out.push(run);
        run = { node: en.node, from: en.idx, to: en.end };
      }
      if (run) out.push(run);
    }
    return out;
  }

  function findRanges(nf, query, all) {
    var hay = nf.text.toLowerCase(), needle = String(query || '').toLowerCase();
    if (!needle) return [];
    var out = [], start = 0, cap = all ? 400 : 1;
    while (out.length < cap) {
      var i = hay.indexOf(needle, start);
      if (i < 0) break;
      out.push([i, i + needle.length]);
      start = i + Math.max(1, needle.length);
    }
    return out;
  }

  /* Wrap one full-text span → one <mark> per touched text node (they may sit
     in different parents, e.g. across an <em> boundary). Splits tag both
     halves with the original node's __rdOrig id so normFull still treats
     them as one textual unit. */
  function splitTagged(node, at) {
    if (node.__rdOrig == null) node.__rdOrig = ++ORIG_SEQ;
    var tail = node.splitText(at);
    tail.__rdOrig = node.__rdOrig;
    return tail;
  }

  function wrapRange(nf, a, b, cls, attrs) {
    var segs = rangeSegments(nf, a, b), marks = [];
    for (var s = segs.length - 1; s >= 0; s--) {
      var sg = segs[s], node = sg.node;
      if (!node || !node.parentNode) continue;
      var from = sg.from, to = Math.min(sg.to, node.data.length);
      if (from >= to) continue;
      if (to < node.data.length) splitTagged(node, to);
      var mid = node;
      if (from > 0) mid = splitTagged(node, from);
      var mk = fdoc.createElement('mark');
      mk.className = cls;
      if (attrs) Object.keys(attrs).forEach(function (k) { mk.setAttribute(k, attrs[k]); });
      mid.parentNode.insertBefore(mk, mid);
      mk.appendChild(mid);
      marks.push(mk);
    }
    return marks.reverse();
  }

  /* Mark every occurrence of `query` under `root` (case-insensitive).
     Matches wrap last-to-first so earlier raw offsets stay valid. */
  function markText(root, query, cls) {
    var nf = normFull(root);
    var ranges = findRanges(nf, query, true);
    var out = [];
    for (var r = ranges.length - 1; r >= 0; r--) {
      var marks = wrapRange(nf, ranges[r][0], ranges[r][1], cls || 'rd-qhit');
      out.push({ marks: marks, a: ranges[r][0], b: ranges[r][1] });
    }
    return out.reverse();
  }

  function unwrapMarks(marks) {
    (marks || []).forEach(function (m) {
      if (!m || !m.parentNode) return;
      var p = m.parentNode;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      if (p.normalize) p.normalize();
    });
  }

  /* ------------------------------------------------------- annotations */
  var anns = [];
  var selPend = null;      // selection snapshot awaiting a color/note pick
  var selLock = 0;         // mousedown inside the popup must not self-hide
  var wantAnn = null;      // annotation to focus after a chapter load

  function annSorted() {
    return anns.slice().sort(function (a, b) {
      return (a.ch - b.ch) || String(a.id).localeCompare(String(b.id));
    });
  }

  function annChapLabel(c) {
    var meta = state.meta;
    var ch = meta && meta.chapters && meta.chapters[c - 1];
    if (ch) return chapterLabel(ch);
    return String(c);
  }

  function annStatus(msg, isErr) {
    var n = el('rd-ann-status');
    if (!n) return;
    n.textContent = msg || '';
    n.className = 'muted small' + (isErr ? ' error' : '');
  }

  function smallBtn(label, title, fn) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-ghost btn-sm';
    b.textContent = label;
    if (title) { b.title = title; b.setAttribute('aria-label', title); }
    b.addEventListener('click', fn);
    return b;
  }

  function renderAnns() {
    var list = el('rd-ann-list'), cnt = el('rd-ann-count');
    if (cnt) cnt.textContent = anns.length
      ? fmt('read.ann_count', { n: anns.length }) : '';
    if (!list) return;
    list.textContent = '';
    if (!anns.length) {
      var empty = document.createElement('div');
      empty.className = 'muted small';
      empty.textContent = i18n('read.ann_none');
      list.appendChild(empty);
      return;
    }
    var chap = null;
    annSorted().forEach(function (a) {
      if (a.ch !== chap) {
        chap = a.ch;
        var h = document.createElement('div');
        h.className = 'rd-ann-chap';
        h.textContent = annChapLabel(chap);
        list.appendChild(h);
      }
      list.appendChild(annRow(a));
    });
  }

  function annRow(a) {
    var row = document.createElement('div');
    row.className = 'rd-ann-row';
    row.dataset.id = a.id;

    var dot = document.createElement('span');
    dot.className = 'rd-dot rd-dot-' + a.color + ' rd-dot-static';
    row.appendChild(dot);

    var body = document.createElement('button');
    body.type = 'button';
    body.className = 'rd-ann-body';
    var q = document.createElement('span');
    q.className = 'rd-ann-quote';
    q.textContent = a.quote.length > 140 ? a.quote.slice(0, 140) + '…' : a.quote;
    body.appendChild(q);
    if (a.note) {
      var nt = document.createElement('span');
      nt.className = 'rd-ann-note-txt';
      nt.textContent = a.note;
      body.appendChild(nt);
    }
    body.addEventListener('click', function () { jumpToAnn(a); });
    row.appendChild(body);

    var tools = document.createElement('span');
    tools.className = 'rd-ann-tools';
    tools.appendChild(smallBtn('✎', i18n('read.ann_note_ph'),
      function () { editAnnNote(row, a); }));
    tools.appendChild(smallBtn('✕', i18n('read.close'),
      function () { deleteAnn(a); }));
    row.appendChild(tools);
    return row;
  }

  function editAnnNote(row, a) {
    var exist = row.querySelector('.rd-ann-edit textarea');
    if (exist) { exist.focus(); return; }
    var wrap = document.createElement('div');
    wrap.className = 'rd-ann-edit';
    var ta = document.createElement('textarea');
    ta.className = 'rd-input';
    ta.rows = 2;
    ta.placeholder = i18n('read.ann_note_ph');
    ta.value = a.note || '';
    var bar = document.createElement('div');
    bar.className = 'rd-ann-editbar';
    bar.appendChild(smallBtn('✓', i18n('read.ann_saved'), function () {
      persistAnn(a, { note: ta.value.slice(0, 2000) });
    }));
    bar.appendChild(smallBtn('✕', i18n('read.close'), function () { renderAnns(); }));
    wrap.appendChild(ta);
    wrap.appendChild(bar);
    row.appendChild(wrap);
    ta.focus();
  }

  function annRequest(method, url) {
    return fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path, ann: arguments[2] })
    }).then(function (res) {
      return res.text().then(function (raw) {
        var data = null;
        try { data = JSON.parse(raw); } catch (e) { /* non-JSON */ }
        if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
        return data;
      });
    });
  }

  function persistAnn(a, patch) {
    return annRequest('PUT', R.annUrl, Object.assign({}, a, patch)).then(function (saved) {
      var i = -1;
      anns.forEach(function (x, k) { if (x.id === saved.id) i = k; });
      if (i >= 0) anns[i] = saved; else anns.push(saved);
      renderAnns();
      annStatus(i18n('read.ann_saved'));
      if (patch.color) refreshAnnMark(saved);
      return saved;
    }).catch(function (e) {
      annStatus(i18n('read.ann_failed') + ' ' + (e.message || ''), true);
      return null;
    });
  }

  function deleteAnn(a) {
    if (!window.confirm(i18n('read.ann_delete'))) return;
    fetch(withQuery(R.annUrl, { id: a.id }), { method: 'DELETE' })
      .then(function (res) {
        if (!res.ok && res.status !== 404) throw new Error('HTTP ' + res.status);
      })
      .then(function () {
        anns = anns.filter(function (x) { return x.id !== a.id; });
        clearAnnMarks(a.id);
        renderAnns();
        annStatus('');
      })
      .catch(function (e) {
        annStatus(i18n('read.ann_failed') + ' ' + (e.message || ''), true);
      });
  }

  function addAnn(color, note) {
    var p = selPend;
    if (!p) return Promise.resolve(null);
    var ann = {
      ch: p.ch, item: p.item, quote: p.quote, pre: p.pre, post: p.post,
      color: color, note: note || ''
    };
    return annRequest('POST', R.annUrl, ann).then(function (created) {
      anns.push(created);
      hideSelPop();
      renderAnns();
      annStatus(i18n('read.ann_saved'));
      markQuote(created);
      return created;
    }).catch(function (e) {
      annStatus(i18n('read.ann_failed') + ' ' + (e.message || ''), true);
      return null;
    });
  }

  function annMarks(id) {
    if (!fdoc) return [];
    return qsa('[data-ann="' + id + '"]', fdoc);
  }

  function clearAnnMarks(id) { unwrapMarks(annMarks(id)); }

  function refreshAnnMark(a) {
    if (!fdoc || a.ch !== state.ch) return;
    clearAnnMarks(a.id);
    markQuote(a);
  }

  /* Locate an annotation's quote in the chapter (stored item first, then the
     whole chapter; stored pre/post context disambiguates repeats). */
  function markQuote(a) {
    if (!fdoc || a.ch !== state.ch || !state.frameReady) return false;
    var first = (a.item != null && state.nodeOf[a.item]) || null;
    var order = [];
    if (first) order.push(first);
    (state.nodeOf || []).forEach(function (nd) {
      if (nd && nd !== first) order.push(nd);
    });
    for (var i = 0; i < order.length; i++) {
      var nf = normFull(order[i]);
      var ranges = findRanges(nf, a.quote, true);
      if (!ranges.length) continue;
      var pick = ranges[0];
      if (a.pre || a.post) {
        for (var r = 0; r < ranges.length; r++) {
          var s = ranges[r][0], e = ranges[r][1];
          var okPre = !a.pre ||
            nf.text.slice(Math.max(0, s - a.pre.length), s) === a.pre;
          var okPost = !a.post || nf.text.slice(e, e + a.post.length) === a.post;
          if (okPre && okPost) { pick = ranges[r]; break; }
        }
      }
      var marks = wrapRange(nf, pick[0], pick[1],
        'rd-hl rd-hl-' + a.color, { 'data-ann': a.id });
      if (marks.length) return true;
    }
    return false;
  }

  function applyAnnMarks() {
    var missing = false;
    anns.forEach(function (a) {
      if (a.ch === state.ch && !markQuote(a)) missing = true;
    });
    if (missing) annStatus(i18n('read.ann_missing'));
  }

  function focusAnnMark(a) {
    var marks = annMarks(a.id);
    if (!marks.length) return false;
    var mk = marks[0];
    var it = climbToItem(mk);
    if (it) setCurrent(it.__rdIdx, { scroll: false });
    scrollToNode(mk);
    return true;
  }

  function jumpToAnn(a) {
    hideSelPop();
    if (a.ch === state.ch) {
      if (!focusAnnMark(a) && !markQuote(a)) {
        annStatus(i18n('read.ann_missing'));
      } else {
        focusAnnMark(a);
      }
      return;
    }
    stopForNavigation();
    flushSidecar(false);
    wantAnn = a.id;
    loadChapter(a.ch, { item: a.item != null ? a.item : 0, push: true });
  }

  /* ------------------------------------------------- selection popup */
  /* Map a selection range into the normalized text (nf) of an item so the
     quote we store is *literally a substring of nf.text*. range.toString()
     concatenates raw node data, which diverges from textOf's per-node join
     at no-whitespace tag boundaries ("world<em>,x" → raw "world,x" vs nf
     "world ,x") and would then fail to be found again on restore. */
  function edgeTextNode(node, offset, isEnd) {
    if (!node) return null;
    if (node.nodeType === 3) return { node: node, off: offset };
    var kids = node.childNodes;
    if (!kids || !kids.length) return null;
    try {
      if (!isEnd) {
        var kid = kids[offset] || kids[kids.length - 1];
        if (!kid) return null;
        if (kid.nodeType === 3) return { node: kid, off: 0 };
        var w = fdoc.createTreeWalker(kid, 4, null);
        var first = w.nextNode();
        return first ? { node: first, off: 0 } : null;
      }
      var kid2 = kids[Math.max(0, offset - 1)];
      if (!kid2) return null;
      if (kid2.nodeType === 3) return { node: kid2, off: kid2.data.length };
      var w2 = fdoc.createTreeWalker(kid2, 4, null), last = null, t;
      while ((t = w2.nextNode())) last = t;
      return last ? { node: last, off: last.data.length } : null;
    } catch (e) { return null; }
  }

  /* raw index in a segment's node → index in nf.text. The node's entries
     form one contiguous block in the group map: every entry before the block
     belongs to earlier pieces of the same unit and counts as well. Returns
     -1 when the node carries no normalized text (all whitespace). */
  function normPos(nf, node, rawOff) {
    for (var i = 0; i < nf.segs.length; i++) {
      var p = nf.segs[i], f0 = -1, f1 = -1;
      for (var j = 0; j < p.map.length; j++) {
        if (p.map[j].node !== node) continue;
        if (f0 < 0) f0 = j;
        f1 = j + 1;
      }
      if (f0 < 0) continue;
      var cnt = f0;                       // chars of preceding pieces
      for (var k = f0; k < f1; k++) {
        if (p.map[k].idx < rawOff) cnt++; else break;
      }
      return nf.starts[i] + cnt;
    }
    return -1;
  }

  function quoteFromRange(nf, range) {
    var s = edgeTextNode(range.startContainer, range.startOffset, false);
    var e = edgeTextNode(range.endContainer, range.endOffset, true);
    if (!s || !e) return null;
    var a = normPos(nf, s.node, s.off);
    var b = normPos(nf, e.node, e.off);
    if (a < 0 || b < 0 || a >= b) return null;
    return { a: a, b: b, quote: nf.text.slice(a, b) };
  }

  function hideSelPop() {
    var p = el('rd-selpop');
    if (p) p.hidden = true;
    selPend = null;
  }

  function showSelPop(range) {
    hideLookup();       // a new selection makes any open AI card stale
    var pop = el('rd-selpop');
    if (!pop || !frame) return;
    pop.hidden = false;
    var rect = range.getBoundingClientRect();
    var fr = frame.getBoundingClientRect();
    var pw = pop.offsetWidth, ph = pop.offsetHeight;
    var x = fr.left + rect.left + rect.width / 2 - pw / 2;
    var y = fr.top + rect.bottom + 8;
    x = Math.max(8, Math.min(x, window.innerWidth - pw - 8));
    if (y + ph > window.innerHeight - 8) y = fr.top + rect.top - ph - 8;
    pop.style.left = x + 'px';
    pop.style.top = Math.max(8, y) + 'px';
  }

  /* --------------------------------------------------- AI lookup popover */
  function hideLookup() {
    var n = el('rd-lkp');
    if (n) n.hidden = true;
  }

  function runLookup() {
    if (!selPend || !R.lookupUrl) return;
    var box = el('rd-lkp'), body = el('rd-lkp-body'), pop = el('rd-selpop');
    if (!box || !body) return;
    if (pop && !pop.hidden) {            // anchor where the selection popup was
      box.style.left = pop.style.left;
      box.style.top = pop.style.top;
    }
    box.hidden = false;
    var x = parseFloat(box.style.left) || 8;
    box.style.left = Math.max(8, Math.min(x, window.innerWidth - box.offsetWidth - 8)) + 'px';
    body.textContent = i18n('read.lookup_loading');
    var pend = selPend;
    hideSelPop();
    fetch(R.lookupUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: pend.quote,
        ctx: (pend.pre || '') + pend.quote + (pend.post || '')
      })
    }).then(function (res) {
      return res.json().then(function (j) {
        if (!res.ok || j.error) {
          throw new Error(j.error || res.statusText || ('HTTP ' + res.status));
        }
        body.textContent = j.result || '';
      });
    }).catch(function (e) {
      body.textContent = i18n('read.lookup_fail') + ': ' + ((e && e.message) || '');
    });
  }

  var selT = 0;
  function onSelChangeSoon() {
    if (selT) clearTimeout(selT);
    selT = setTimeout(function () { selT = 0; onSelChange(); }, 90);
  }

  function onSelChange() {
    if (selLock || !fdoc || !state.frameReady) return;
    var so = null;
    try { so = fdoc.getSelection(); } catch (e) { return; }
    if (!so || so.isCollapsed || !so.rangeCount) { hideSelPop(); return; }
    var range = so.getRangeAt(0);
    var item = climbToItem(range.commonAncestorContainer);
    if (!item || item.__rdIdx == null) { hideSelPop(); return; }
    var nf = normFull(item);
    var q = quoteFromRange(nf, range);
    var quote = q ? q.quote : String(range.toString() || '').replace(/\s+/g, ' ').trim();
    var lead = quote.length - quote.replace(/^\s+/, '').length;
    quote = quote.replace(/^\s+|\s+$/g, '').slice(0, 500);
    if (quote.length < 2) { hideSelPop(); return; }
    var i = q ? (q.a + lead) : nf.text.indexOf(quote);
    selPend = {
      ch: state.ch,
      item: item.__rdIdx,
      quote: quote,
      pre: i >= 0 ? nf.text.slice(Math.max(0, i - 20), i) : '',
      post: i >= 0 ? nf.text.slice(i + quote.length, i + quote.length + 20) : ''
    };
    showSelPop(range);
  }

  /* ------------------------------------------------------------- search */
  var srch = {
    scope: 'book', q: '', matches: [], cur: -1, token: 0, pending: null, t: 0
  };

  function searchStatus(msg, cls) {
    var n = el('rd-search-status');
    if (!n) return;
    n.textContent = msg || '';
    n.className = 'muted small' + (cls ? ' ' + cls : '');
  }

  function showQNav(on) {
    var nav = el('rd-search-nav');
    if (nav) nav.hidden = !on;
    renderQIdx();
  }

  function renderQIdx() {
    var n = el('rd-q-idx');
    if (!n) return;
    n.textContent = (srch.cur >= 0 && srch.matches.length)
      ? fmt('read.search_match', { i: srch.cur + 1, n: srch.matches.length }) : '';
  }

  function setScope(scope) {
    srch.scope = scope === 'chapter' ? 'chapter' : 'book';
    qsa('.rd-scope').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.scope === srch.scope));
    });
    runSearch();
  }

  function runSearch() {
    var qi = el('rd-search-q');
    if (qi) srch.q = qi.value;
    clearSearch(true);
    var q = String(srch.q || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    srch.q = q;
    if (!q) { searchStatus(''); return; }
    if (srch.scope === 'chapter') runChapterSearch(q);
    else runBookSearch(q);
  }

  function clearSearch(keepInput) {
    clearQMarks();
    showQNav(false);
    var res = el('rd-search-results');
    if (res) res.textContent = '';
    if (!keepInput) {
      var qi = el('rd-search-q');
      if (qi) qi.value = '';
      srch.q = '';
    }
    srch.pending = null;
  }

  function clearQMarks() {
    (srch.matches || []).forEach(function (m) { unwrapMarks(m.marks); });
    srch.matches = [];
    srch.cur = -1;
  }

  function runChapterSearch(q) {
    if (!fdoc || !state.frameReady) return;
    var root = qs('.rd-content', fdoc) || fdoc.body;
    srch.matches = markText(root, q, 'rd-qhit');
    if (!srch.matches.length) {
      searchStatus(i18n('read.search_none'));
      showQNav(false);
      return;
    }
    searchStatus(fmt('read.search_results', { n: srch.matches.length }));
    showQNav(true);
    focusQ(0);
  }

  function focusQ(i) {
    var m = srch.matches;
    if (!m.length) return;
    var prev = m[srch.cur];
    if (prev) prev.marks.forEach(function (x) { x.classList.remove('rd-qcur'); });
    srch.cur = ((i % m.length) + m.length) % m.length;
    m[srch.cur].marks.forEach(function (x) { x.classList.add('rd-qcur'); });
    var mk = m[srch.cur].marks[0];
    if (mk) scrollToNode(mk);
    renderQIdx();
  }

  function stepQ(d) {
    if (!srch.matches.length) return;
    focusQ(srch.cur + d);
  }

  function runBookSearch(q) {
    var token = ++srch.token;
    searchStatus(i18n('read.search_scanning'));
    var res = el('rd-search-results');
    if (res) res.textContent = '';
    getJSON(withQuery(R.searchUrl, { q: q, max: 300 })).then(function (data) {
      if (token !== srch.token) return;
      searchStatus('');
      renderBookResults(q, data);
    }).catch(function (e) {
      if (token !== srch.token) return;
      searchStatus(i18n('read.search_none') + ' — ' + (e.message || ''), 'error');
    });
  }

  function renderBookResults(q, data) {
    var res = el('rd-search-results');
    if (!res) return;
    res.textContent = '';
    var rows = (data && data.chapters) || [];
    if (!rows.length) { searchStatus(i18n('read.search_none')); return; }
    var status = fmt('read.search_results', { n: data.total });
    if (data.truncated) status += ' · ' + fmt('read.search_trunc', { n: data.total });
    searchStatus(status);
    rows.forEach(function (row) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'rd-res';
      var head = document.createElement('span');
      head.className = 'rd-res-head';
      head.textContent = chapterLabel(row);
      var cnt = document.createElement('span');
      cnt.className = 'rd-res-count';
      cnt.textContent = row.count;
      head.appendChild(cnt);
      b.appendChild(head);
      var snip = document.createElement('span');
      snip.className = 'rd-res-snip';
      snip.textContent = (row.hits && row.hits[0] && row.hits[0].snip) || '';
      b.appendChild(snip);
      b.addEventListener('click', function () { openSearchChapter(q, row); });
      res.appendChild(b);
    });
  }

  function openSearchChapter(q, row) {
    stopForNavigation();
    flushSidecar(false);
    srch.pending = { q: q, n: row.n };
    if (state.ch === row.n) applyPendingSearch();
    else loadChapter(row.n, { push: true });
  }

  /* Mark the pending search hits once the target chapter is in the DOM. */
  function applyPendingSearch() {
    var p = srch.pending;
    if (!p || !fdoc || !state.frameReady) return;
    srch.pending = null;
    var root = qs('.rd-content', fdoc) || fdoc.body;
    srch.matches = markText(root, p.q, 'rd-qhit');
    if (!srch.matches.length) {
      searchStatus(i18n('read.search_none'));
      showQNav(false);
      return;
    }
    searchStatus(fmt('read.search_results', { n: srch.matches.length }));
    showQNav(true);
    focusQ(0);
  }

  /* ------------------------------------------------------------ panels */
  function togglePanel(name) {
    var rd = el('rd');
    if (!rd) return;
    var prev = rd.dataset.panel;
    var open = rd.dataset.panel === name ? '' : (name || '');
    rd.dataset.panel = open;
    // phone: panels are modal — nothing may scroll behind the backdrop
    document.body.style.overflow = (open && chromeOn()) ? 'hidden' : '';
    if (open) setChromeAway(false);   // the panel toggles live in the bar
    ['toc', 'font', 'tts', 'search', 'ann'].forEach(function (n) {
      var panel = el('rd-panel-' + n);
      var btn = el('rd-open-' + n);
      var show = n === open;
      if (panel) panel.hidden = !show;
      if (btn) btn.setAttribute('aria-pressed', String(show));
    });
    // focus: opening moves into the panel, closing returns to its toggle
    if (open && open !== 'search') {
      var np = el('rd-panel-' + open);
      var first = np && np.querySelector('button:not([disabled]), select, input:not([disabled])');
      if (first) { try { first.focus(); } catch (e) { /* inert */ } }
    } else if (!open && prev) {
      var pb = el('rd-open-' + prev);
      if (pb) { try { pb.focus(); } catch (e) { /* inert */ } }
    }
    if (open === 'tts') { warmVoices(); bgmList(); }
    if (open === 'toc') { markToc(); renderStats(); }
    if (open === 'search') {
      var qi = el('rd-search-q');
      if (qi) { qi.value = srch.q || ''; qi.focus(); qi.select(); }
    }
    if (open === 'ann') renderAnns();
    resizeStage();
    requestAnimationFrame(resizeStage);
  }

  /* ------------------------------------------------------- reading stats */
  /* Per-book minutes + visited chapters, kept in localStorage (client-only).
     A 15s tick counts while the page is visible OR TTS is speaking, so
     screen-off listening still accumulates. */
  var stats = null;

  function statsKey() { return 'rdStats:' + (R.path || ''); }

  function loadStats() {
    if (stats) return stats;
    stats = { total: 0, days: {}, chapters: {} };
    var ls = null;
    try { ls = window.localStorage; } catch (e) { /* no storage */ }
    if (!ls) return stats;
    try {
      var p = JSON.parse(ls.getItem(statsKey()) || 'null');
      if (p && typeof p === 'object') {
        stats.total = Number(p.total) || 0;
        stats.days = (p.days && typeof p.days === 'object') ? p.days : {};
        stats.chapters = (p.chapters && typeof p.chapters === 'object') ? p.chapters : {};
      }
    } catch (e) { /* corrupt → fresh */ }
    return stats;
  }

  function saveStats() {
    var ls = null;
    try { ls = window.localStorage; } catch (e) { /* no storage */ }
    if (!ls) return;
    try { ls.setItem(statsKey(), JSON.stringify(stats)); } catch (e) { /* quota */ }
  }

  function todayKey() {
    var d = new Date();
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) +
      '-' + ('0' + d.getDate()).slice(-2);
  }

  function statsTick() {
    if (document.visibilityState === 'hidden' && !(speak.on && !speak.paused)) return;
    var s = loadStats();
    s.total += 15;
    var k = todayKey();
    s.days[k] = (Number(s.days[k]) || 0) + 15;
    var ks = Object.keys(s.days).sort();
    while (ks.length > 60) { delete s.days[ks.shift()]; }
    saveStats();
    var rd = el('rd');
    if (rd && rd.dataset.panel === 'toc') renderStats();
  }

  function markChapterVisited(n) {
    var s = loadStats();
    if (s.chapters[n]) return;
    s.chapters[n] = 1;
    saveStats();
  }

  function renderStats() {
    var n = el('rd-toc-stats');
    if (!n) return;
    var s = loadStats();
    n.textContent = fmt('read.stats', {
      today: Math.round((Number(s.days[todayKey()]) || 0) / 60),
      total: Math.round(s.total / 60),
      chapters: Object.keys(s.chapters).length
    });
  }

  /* --------------------------------------------------------------- TTS */
  var tts = { state: 'unknown', voices: [], def: null, poll: 0, tries: 0 };
  var speak = { on: false, paused: false, i: 0, ch: 0, token: 0, groups: {}, inflight: {},
    sent: null, sentIdx: -1, gen: 0, pfGen: 0, fails: 0, ac: null };

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
      var rawHdr = res.headers.get('X-TTS-Segs') || '';
      return res.blob().then(function (blob) {
        var segs = null;
        if (rawHdr) { try { segs = JSON.parse(rawHdr); } catch (e) { segs = null; } }
        return { blob: blob, segs: (segs && segs.length) ? segs : null };
      });
    });
  }

  function cacheKey(ch, i) { return ch + ':' + i; }

  /* Recent non-current chapters' items (queue walks past the current chapter) */
  var chapItems = {};

  function chapterItems(n) {
    if (state.chapter && state.chapter.n === n) return Promise.resolve(state.items);
    if (chapItems[n]) return Promise.resolve(chapItems[n]);
    return fetchChap(n).then(function (ch) {
      var ks = Object.keys(chapItems);
      if (ks.length > 3) delete chapItems[ks[0]];
      chapItems[n] = ch.items;
      return ch.items;
    });
  }

  /* ------------------------------------------------- streaming TTS pieces
     Frame format: uint32be hdrLen | JSON header | uint32be wavLen | wav. */

  function frameParser(onPiece) {
    var acc = new Uint8Array(0), hdr = null, dec = new TextDecoder('utf8');
    function u32(u8) { return new DataView(u8.buffer, u8.byteOffset, u8.length).getUint32(0); }
    return function feed(chunk) {
      var merged = new Uint8Array(acc.length + chunk.length);
      merged.set(acc);
      merged.set(chunk, acc.length);
      acc = merged;
      for (;;) {
        if (!hdr) {
          if (acc.length < 4) return;
          var hl = u32(acc);
          if (acc.length < 4 + hl) return;
          hdr = JSON.parse(dec.decode(acc.subarray(4, 4 + hl)));
          acc = acc.subarray(4 + hl);
        }
        if (acc.length < 4) return;
        var wl = u32(acc);
        if (acc.length < 4 + wl) return;
        var wav = acc.slice(4, 4 + wl);       // copy: decodeAudioData detaches
        acc = acc.subarray(4 + wl);
        var h = hdr;
        hdr = null;
        if (h.error) throw new Error(h.error);  // mid-stream error frame
        onPiece(h, wav);
      }
    };
  }

  function ttsFrames(text, signal, onPiece) {
    return fetch(R.ttsStreamUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, voice: ttsCfg.voice }),
      signal: signal
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (raw) {
          var data = null;
          try { data = JSON.parse(raw); } catch (e) { /* ignore */ }
          throw new Error((data && data.error) || ('HTTP ' + res.status));
        });
      }
      if (!res.body || !res.body.getReader) throw new Error('no stream support');
      var rdr = res.body.getReader();
      var feed = frameParser(onPiece);
      function pump() {
        return rdr.read().then(function (r) {
          if (r.done) return;
          feed(r.value);
          return pump();
        });
      }
      return pump();
    });
  }

  function decodePiece(bytes) {
    var ctx = playerCtx();
    return new Promise(function (resolve, reject) {
      ctx.decodeAudioData(bytes.buffer, resolve, reject);
    });
  }

  /* ---------------------------------------------- gapless Web Audio player
     Decoded pieces are scheduled on an AudioContext with exact start times —
     no element swap, so sentences and paragraphs join with a true 0ms seam.
     Pause = ctx.suspend() keeps the whole schedule; resume continues exactly
     where it left off. */

  var player = {
    ctx: null, gain: null, timer: 0, rate: 1,
    q: [],        // decoded, not yet scheduled: {buf, s, e, final}
    wins: [],     // scheduled windows: {src, buf, s, e, final, t0, t1, n}
    nextT: 0,     // ctx time where the next piece starts
    wseq: 0
  };

  function playerCtx() {
    if (player.ctx) return player.ctx;
    var AC = window.AudioContext || window.webkitAudioContext;
    player.ctx = new AC();
    player.gain = player.ctx.createGain();
    player.gain.connect(player.ctx.destination);
    player.rate = ttsCfg.rate;
    return player.ctx;
  }

  /* ------------------------------------------ pitch-preserving time stretch
     Web Audio's playbackRate shifts pitch with speed (chipmunk at x1.5,
     growl at x0.75) — the voice must NOT change with the speed slider.
     Buffers are time-stretched here with WSOLA and scheduled at rate 1.0,
     so the speaker keeps their voice at every speed and seams stay 0ms. */

  function wsola(x, rate, sr) {
    var len = x.length;
    var target = Math.max(1, Math.round(len / rate));
    var F = Math.min(Math.round(0.04 * sr), Math.floor(len / 3));
    if (F < 240) {        // a sliver (< ~5ms frame): linear resample is fine
      var out0 = new Float32Array(target);
      for (var j = 0; j < target; j++) {
        var pp = Math.min(len - 1, Math.max(0, (j + 0.5) * rate - 0.5));
        var i0 = Math.floor(pp), fr = pp - i0;
        out0[j] = x[i0] * (1 - fr) + x[Math.min(len - 1, i0 + 1)] * fr;
      }
      return out0;
    }
    var Hs = Math.max(1, Math.round(F / 2));   // synthesis hop (50% overlap)
    var X = F - Hs;                            // crossfade overlap
    var search = Math.min(Math.round(0.006 * sr), F);   // ±6ms to re-align
    var D = 4;                                 // decimated correlation step
    var out = new Float32Array(target + F);
    var aPrev = 0;

    function corr(xi, yi) {                    // normalized, decimated
      var sxy = 0, sxx = 0, syy = 0;
      for (var i = 0; i < X; i += D) {
        var a = x[xi + i], b = x[yi + i];
        sxy += a * b; sxx += a * a; syy += b * b;
      }
      var e = Math.sqrt(sxx * syy);
      return e < 1e-12 ? -Infinity : sxy / e;   // silence: no preference
    }

    var steps = Math.ceil(target / Hs);
    for (var k = 0; k < steps; k++) {
      var s = k * Hs;
      if (s >= target) break;
      var nominal = Math.round(s * rate);       // exact time map, no drift
      var a = nominal;
      if (k > 0) {
        // WSOLA: pick the candidate whose head lines up with the previous
        // segment's tail, so repositioned segments stay phase-continuous.
        var tgt = aPrev + Hs, best = -Infinity, bestO = 0;
        for (var o = -search; o <= search; o += D) {
          var c = nominal + o;
          if (c < 0 || c + X > len) continue;
          var sc = corr(c, tgt);
          if (sc > best) { best = sc; bestO = o; }
        }
        if (best > -Infinity) a = nominal + bestO;
      }
      if (a < 0) a = 0;
      if (a + F > len) a = Math.max(0, len - F);
      aPrev = a;
      if (k === 0) {
        out.set(x.subarray(a, Math.min(len, a + F)), 0);
      } else {
        for (var i = 0; i < X; i++) {           // linear crossfade over X
          var g = (i + 1) / (X + 1);
          out[s + i] = out[s + i] * (1 - g) + x[a + i] * g;
        }
        var end = Math.min(len, a + F);
        for (var i2 = X; a + i2 < end; i2++) out[s + i2] = x[a + i2];
      }
    }
    return out.subarray(0, target);
  }

  /* ``buf`` stretched for ``rate`` (memoized on the buffer: one copy per
     speed). Rate 1 → the original buffer, zero DSP cost. */
  function stretched(buf, rate) {
    if (!buf || Math.abs(rate - 1) < 0.005) return buf;
    if (!buf.getChannelData) return buf;              // non-audio fixture
    var cache = buf.__rdSt || (buf.__rdSt = {});
    var key = rate.toFixed(3);
    if (cache[key]) return cache[key];
    var sr = buf.sampleRate || 48000;
    var out = wsola(buf.getChannelData(0), rate, sr);
    var nb = player.ctx.createBuffer(1, Math.max(1, out.length), sr);
    nb.getChannelData(0).set(out);
    cache[key] = nb;
    return nb;
  }

  function playerSchedule() {
    var ctx = player.ctx;
    if (!ctx) return;
    var now = ctx.currentTime;
    if (!player.nextT) player.nextT = now + 0.15;     // cold start: absorb the stretch
    else if (player.nextT < now) player.nextT = now;  // gap behind us: now
    while (player.q.length && player.nextT < now + 1.0) {
      var p = player.q.shift();
      var buf = stretched(p.buf, player.rate);        // stretch before schedule
      var src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(player.gain);
      var t0 = player.nextT;
      src.start(t0);
      player.wins.push({ src: src, buf: p.buf, s: p.s, e: p.e, final: p.final,
                         t0: t0, t1: t0 + buf.duration, n: ++player.wseq });
      player.nextT = t0 + buf.duration;
    }
  }

  function playerPush(p) {
    if (!player.ctx) return;   // no context yet — nothing can play
    player.q.push(p);
    playerSchedule();
  }

  function playerReset() {
    player.q.length = 0;
    player.wins.forEach(function (w) {
      try { w.src.stop(0); } catch (e) { /* not started */ }
      try { w.src.disconnect(); } catch (e) { /* already gone */ }
    });
    player.wins.length = 0;
    player.nextT = 0;
  }

  function playerSetRate(rate) {
    if (Math.abs(rate - player.rate) < 0.001) return;
    player.rate = rate;
    var ctx = player.ctx;
    if (!ctx) return;
    onSpeakTime();                // shift windows that already ended
    var now = ctx.currentTime;
    var act = null, requeue = [], gapT = null;
    player.wins.forEach(function (w) {
      if (act === null && w.t0 <= now) {   // playing: finishes at its own
        act = w;                           // tempo — pitch NEVER changes
        return;
      }
      // waiting behind the active one: re-queue its ORIGINAL buffer — it is
      // re-stretched for the new speed when it is (re)scheduled
      if (gapT === null) gapT = w.t0;
      requeue.push({ buf: w.buf, s: w.s, e: w.e, final: w.final });
      try { w.src.stop(0); } catch (e) { /* not started */ }
      try { w.src.disconnect(); } catch (e) { /* already gone */ }
    });
    player.wins = act ? [act] : [];
    if (act) player.nextT = act.t1;                 // seam after the active one
    else if (gapT !== null) player.nextT = gapT;    // mid-gap: keep the anchor
    if (requeue.length) player.q = requeue.concat(player.q);
    playerSchedule();
  }

  /* Test hook: read-aloud internals for the browser e2e (state, clock). */
  if (typeof window !== 'undefined') {
    window.__rd = { speak: speak, tts: tts, player: player, state: state, hl: highlightSeg };
  }

  /* Prefetched decoded group for (chapter, item) — filled while the current
     paragraph plays so paragraph transitions start instantly. Deduped. */
  function audioFor(ch, i) {
    var k = cacheKey(ch, i);
    if (speak.groups[k]) return Promise.resolve(speak.groups[k]);
    if (speak.inflight[k]) return speak.inflight[k];

    var gen = speak.gen;
    var p = chapterItems(ch).then(function (items) {
      var it = items[i];
      if (!it || it.type !== 'p') return null;
      var pieces = [], chain = Promise.resolve();
      return ttsFrames(it.text, null, function (h, wav) {
        chain = chain.then(function () { return decodePiece(wav); }).then(function (buf) {
          pieces.push({ buf: buf, s: h.s, e: h.e, final: !!h.final });
        });
      }).then(function () { return chain; }).then(function () {
        if (gen === speak.gen && pieces.length) {   // voice changed mid-flight → drop
          var keys = Object.keys(speak.groups);
          if (keys.length > 16) delete speak.groups[keys[0]];
          speak.groups[k] = pieces;
        }
        return pieces;
      });
    }).then(function (g) {
      delete speak.inflight[k];
      return g;
    }, function (e) {
      delete speak.inflight[k];
      throw e;
    });
    speak.inflight[k] = p;
    return p;
  }

  /* Stream paragraph (ch, i) straight into the player: the first piece
     speaks as soon as it is synthesized — no waiting for the whole file.
     onFirst fires after the first decoded piece. */
  function streamLive(ch, i, token, onFirst) {
    var ac = new AbortController();
    speak.ac = ac;
    var chain = Promise.resolve();
    var first = false;
    return chapterItems(ch).then(function (items) {
      var it = items[i];
      if (!it || it.type !== 'p') return null;
      return ttsFrames(it.text, ac.signal, function (h, wav) {
        chain = chain.then(function () { return decodePiece(wav); }).then(function (buf) {
          if (token !== speak.token || !speak.on) return;
          if (!first) { first = true; if (onFirst) onFirst(); }
          playerPush({ buf: buf, s: h.s, e: h.e, final: !!h.final });
        });
      }).then(function () { return chain; });
    }).then(function (r) {
      if (speak.ac === ac) speak.ac = null;
      return r;
    }, function (e) {
      if (speak.ac === ac) speak.ac = null;
      throw e;
    });
  }

  /* Lookahead queue: synthesize the next TTS_QUEUE paragraphs while the
     current one plays, so paragraph transitions and jump buttons never wait
     for synthesis. Requests are chained one at a time — the server
     serializes synthesis anyway, and one in-flight fetch keeps the UI
     (state saves, chapter loads) responsive. A new prefetch call or a stop
     invalidates older chains via pfGen. */
  var TTS_QUEUE = 10;

  function prefetchCurrent(i) {
    var gen = ++speak.pfGen;
    var ch = state.ch, idx = i + 1, left = TTS_QUEUE;
    var total = (state.meta && state.meta.total) || 0;
    var its = state.items;

    function step() {
      if (gen !== speak.pfGen || !speak.on || left <= 0) return;
      if (ch === state.ch) its = state.items;
      while (idx < its.length && (!its[idx] || its[idx].type !== 'p')) idx++;
      if (idx < its.length) {
        var j = idx++;
        left--;
        audioFor(ch, j).then(step, step);   // one failure must not kill the queue
        return;
      }
      if (total && ch >= total) return;
      var nch = ch + 1;
      chapterItems(nch).then(function (next) {
        if (gen !== speak.pfGen || !speak.on || left <= 0) return;
        if (!next || !next.length) return;
        ch = nch; its = next; idx = 0;
        step();
      }, noop);
    }
    step();
  }

  function speakNote(text) {
    var n = el('rd-tts-note');
    if (n) n.textContent = text || '';
  }

  /* Sentence-level highlight: the server times each synthesized piece
     (X-TTS-Segs → [[start_c, end_c, t_seconds], …] over the collapsed item
     text = nf.text); wrap the piece covering the playhead in .rd-sent. */
  function clearSent() {
    if (speak.sent && speak.sent.length) unwrapMarks(speak.sent);
    speak.sent = null;
    speak.sentIdx = -1;
  }

  function highlightSeg(seg) {
    clearSent();
    var it = state.items[speak.i];
    if (!seg || !it || it.type !== 'p') return;
    // items come from server JSON — the rendered node lives in nodeOf
    var node = it.node || (state.nodeOf && state.nodeOf[speak.i]);
    if (!node) return;
    var nf = normFull(node);
    var a = Math.max(0, Math.min(seg[0], nf.text.length));
    var b = Math.max(a, Math.min(seg[1], nf.text.length));
    if (b - a < 1) return;
    speak.sent = wrapRange(nf, a, b, 'rd-sent');
  }

  /* Ticker: schedule upcoming pieces, highlight the piece under the
     AudioContext clock; when a paragraph's final window ends, reset the
     failure streak and advance (even while paused — speakStep() itself
     no-ops until resume). */
  function onSpeakTime() {
    if (!player.ctx || !speak.on) return;
    playerSchedule();
    var now = player.ctx.currentTime;
    while (player.wins.length && player.wins[0].t1 <= now) {
      var w = player.wins.shift();
      try { w.src.disconnect(); } catch (e) { /* already gone */ }
      if (w.final) {                 // paragraph boundary → next paragraph
        speak.fails = 0;             // it played: clear the failure streak
        speak.i++;
        speakStep();                 // no-op while paused; resume re-runs it
        break;
      }
    }
    var cur = null;
    for (var i = 0; i < player.wins.length; i++) {
      if (player.wins[i].t0 <= now) cur = player.wins[i]; else break;
    }
    if (!cur || cur.n === speak.sentIdx) return;
    highlightSeg([cur.s, cur.e]);
    speak.sentIdx = cur.n;   // AFTER highlightSeg: clearSent() inside resets it to -1
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
    clearSent();
    speakNote(fmt('read.tts_speaking', { ch: state.ch, i: speak.i + 1, total: state.items.length }));
    ttsStatus(i18n('read.tts_synthesizing'));

    var idx = speak.i, k = cacheKey(state.ch, idx);
    var cached = speak.groups[k];
    if (cached && cached.length) {
      delete speak.groups[k];               // consume: each group plays once
      ttsStatus(i18n('read.tts_playing'));
      cached.forEach(playerPush);           // already decoded → 0ms handoff
      prefetchCurrent(idx);
      return;
    }
    streamLive(state.ch, idx, token, function () {
      ttsStatus(i18n('read.tts_playing'));
      prefetchCurrent(idx);                 // next paragraphs while this one speaks
    }).then(function (item) {
      if (token !== speak.token || !speak.on) return;
      if (item === null) { speak.i++; speakStep(); return; }   // not a paragraph
    }).catch(function (e) {
      if (token !== speak.token) return;
      if (e && e.name === 'AbortError') return;
      // A broken paragraph must not kill the session: skip it. Only a
      // pile-up of consecutive failures is worth stopping for.
      speak.fails++;
      if (speak.fails > 3) {
        speakFail(e && e.message ? e.message : i18n('read.tts_play_fail'));
        return;
      }
      speak.i++;
      speakStep();
    });
  }

  /* Bottom-bar transport: pause/resume + stop while reading aloud. */
  function jumpTransport() {
    var wrap = el('rd-tts-jump'), tog = el('rd-tts-jump-toggle');
    if (!wrap) return;
    wrap.hidden = !speak.on;
    if (tog) {
      tog.textContent = speak.paused ? '▶' : '⏸';
      tog.setAttribute('aria-label', i18n(speak.paused ? 'read.resume' : 'read.pause'));
      tog.title = i18n(speak.paused ? 'read.resume' : 'read.pause');
    }
  }

  /* Move the reading position (bottom jump buttons while reading aloud):
     abort the live stream, reschedule the player, continue at item i. */
  function speakSeek(i) {
    if (!speak.on) return;
    speak.i = i;
    speak.token++;
    if (speak.ac) { try { speak.ac.abort(); } catch (e) { /* closed */ } speak.ac = null; }
    playerReset();
    speakStep();
  }

  function stepItemTts(delta) {
    if (!speak.on) { stepItem(delta); return; }
    var i = state.cur + delta;
    if (i < 0 || i >= state.items.length) { stepItem(delta); return; }  // chapter edge
    setCurrent(i, { scroll: true });
    speakSeek(i);
  }

  function speakStart() {
    if (!state.chapter) return;
    var ctx = playerCtx();          // called from a click → context allowed
    if (ctx.state === 'suspended') { var r = ctx.resume(); if (r && r.catch) r.catch(noop); }
    playerReset();
    speak.on = true;
    speak.paused = false;
    speak.token++;
    speak.fails = 0;
    speak.i = state.cur;
    speak.ch = state.ch;
    var play = el('rd-tts-play'), pause = el('rd-tts-pause'), stop = el('rd-tts-stop');
    if (play) play.hidden = true;
    if (pause) pause.hidden = false;
    if (stop) stop.hidden = false;
    jumpTransport();
    if (!player.timer) player.timer = setInterval(onSpeakTime, 80);
    markSpeaking();
    speakStep();
  }

  function speakPause() {
    if (!speak.on) return;
    speak.paused = true;
    if (player.ctx) { var r = player.ctx.suspend(); if (r && r.catch) r.catch(noop); }
    var pause = el('rd-tts-pause'), play = el('rd-tts-play');
    if (pause) pause.hidden = true;
    if (play) play.hidden = false;
    jumpTransport();
    ttsStatus(i18n('read.tts_paused'));
    speakNote(i18n('read.tts_paused_note'));
  }

  function speakResume() {
    if (!speak.on) return;
    speak.paused = false;
    var play = el('rd-tts-play'), pause = el('rd-tts-pause');
    if (play) play.hidden = true;
    if (pause) pause.hidden = false;
    jumpTransport();
    speakNote(fmt('read.tts_speaking',
      { ch: state.ch, i: speak.i + 1, total: state.items.length }));  // not the pause note anymore
    if (player.ctx && (player.q.length || player.wins.length || speak.ac)) {
      // pieces are queued or a stream is still in flight — the suspended
      // context resumes exactly where it stopped (0ms)
      var r = player.ctx.resume();
      if (r && r.catch) r.catch(noop);
      ttsStatus(i18n('read.tts_playing'));
      return;
    }
    // nothing buffered (or the stream died while paused) → fetch current item
    speakStep();
  }

  function speakStop(updateNote) {
    speak.on = false;
    speak.paused = false;
    speak.token++;
    if (speak.ac) { try { speak.ac.abort(); } catch (e) { /* closed */ } speak.ac = null; }
    playerReset();
    if (player.timer) { clearInterval(player.timer); player.timer = 0; }
    clearSent();
    var play = el('rd-tts-play'), pause = el('rd-tts-pause'), stop = el('rd-tts-stop');
    if (play) play.hidden = false;
    if (pause) pause.hidden = true;
    if (stop) stop.hidden = true;
    jumpTransport();
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

  /* ------------------------------------------------------ background music
     The soundtrack runs on its OWN AudioContext (never the TTS player's):
     pause / stop / speed changes of the read-aloud cannot touch it, so on a
     phone that cannot play the story and a music app at the same time one
     continuous background track plays under the reading. Volume sits on a
     gain node below the voice (default 25%) and the loop is scheduled at
     playbackRate 1.0 — the read-speed slider never changes its tempo. */
  var bgmKey = 'rd.bgm';
  var bgm = { ctx: null, gain: null, src: null, buf: null, bufFile: null,
    on: false, want: false, loading: false, file: null, vol: 25, files: [] };

  function bgmSave() { saveJSON(bgmKey, { file: bgm.file, vol: bgm.vol }); }

  function bgmNote(text, cls) {
    var n = el('rd-music-note');
    if (!n) return;
    n.textContent = text || '';
    n.className = 'muted small' + (cls ? ' ' + cls : '');
  }

  function bgmRender() {
    var btn = el('rd-music-play'), lab = el('rd-music-play-l');
    if (btn) btn.setAttribute('aria-pressed', String(bgm.on || bgm.want));
    if (lab) lab.textContent = i18n((bgm.on || bgm.want) ? 'read.music_stop' : 'read.music_play');
  }

  function bgmCtx() {
    if (bgm.ctx) return bgm.ctx;
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    bgm.ctx = new AC();
    bgm.gain = bgm.ctx.createGain();
    bgm.gain.gain.value = bgm.vol / 100;
    bgm.gain.connect(bgm.ctx.destination);
    return bgm.ctx;
  }

  function bgmUrl(name) {
    var hit = bgm.files.filter(function (f) { return f.name === name; })[0];
    return hit ? hit.url : null;
  }

  function bgmList() {
    if (!R.musicUrl) return;
    getJSON(R.musicUrl).then(function (data) {
      bgm.files = (data && data.files) || [];
      var sel = el('rd-music-file');
      if (!sel) return;
      sel.textContent = '';
      if (!bgm.files.length) {
        var none = document.createElement('option');
        none.value = '';
        none.textContent = i18n('read.music_none');
        sel.appendChild(none);
        sel.disabled = true;
        bgmNote(i18n('read.music_empty'));
        return;
      }
      sel.disabled = false;
      bgm.files.forEach(function (f) {
        var o = document.createElement('option');
        o.value = f.name;
        o.textContent = f.name;
        sel.appendChild(o);
      });
      if (!bgm.file || !bgm.files.some(function (f) { return f.name === bgm.file; })) {
        bgm.file = bgm.files[0].name;
      }
      sel.value = bgm.file;
      if (bgm.on && bgm.bufFile !== bgm.file) {   // selection changed elsewhere
        bgmStop();
        bgm.buf = null;
        bgmPlay();
      }
    }).catch(function (e) {
      bgmNote(i18n('read.music_err') + ' ' + e.message, 'error');
    });
  }

  function bgmSchedule() {
    var ctx = bgm.ctx;
    if (!ctx || !bgm.buf) return;
    if (bgm.src) { try { bgm.src.stop(0); } catch (e) { /* already ended */ } }
    var src = ctx.createBufferSource();
    src.buffer = bgm.buf;
    src.loop = true;
    src.connect(bgm.gain);
    src.start(0);
    bgm.src = src;
    bgm.on = true;
    bgm.loading = false;
    bgmRender();
    bgmNote('');
  }

  function bgmPlay() {
    var ctx = bgmCtx();
    if (!ctx) { bgmNote(i18n('read.music_err') + ' AudioContext', 'error'); return; }
    if (!bgm.file || !bgmUrl(bgm.file)) { bgmNote(i18n('read.music_none')); return; }
    if (ctx.state === 'suspended') { var r = ctx.resume(); if (r && r.catch) r.catch(noop); }
    bgm.want = true;
    bgmRender();
    if (bgm.buf && bgm.bufFile === bgm.file) { bgmSchedule(); return; }
    if (bgm.loading) return;
    bgm.loading = true;
    bgmNote(i18n('read.music_loading'));
    var url = bgmUrl(bgm.file), wantFile = bgm.file;
    fetch(url).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.arrayBuffer();
    }).then(function (ab) {
      return new Promise(function (resolve, reject) {
        ctx.decodeAudioData(ab, resolve, reject);   // promise-less on old WebKit
      });
    }).then(function (buf) {
      bgm.loading = false;
      if (!bgm.want || bgm.file !== wantFile) return;   // stopped/switched meanwhile
      bgm.buf = buf;
      bgm.bufFile = wantFile;
      bgmSchedule();
    }).catch(function (e) {
      bgm.loading = false;
      bgm.want = false;
      bgmRender();
      bgmNote(i18n('read.music_err') + ' ' + e.message, 'error');
    });
  }

  function bgmStop() {
    bgm.want = false;
    if (bgm.src) { try { bgm.src.stop(0); } catch (e) { /* already ended */ } bgm.src = null; }
    bgm.on = false;
    bgmRender();
  }

  function bgmToggle() {
    if (bgm.on || bgm.want) bgmStop();
    else bgmPlay();
  }

  function bgmSetVol(v) {
    bgm.vol = clamp(Math.round(Number(v) || 0), 0, 100);
    var lab = el('rd-music-vol-v');
    if (lab) lab.textContent = bgm.vol + '%';
    if (bgm.gain) bgm.gain.gain.value = bgm.vol / 100;
    bgmSave();
  }

  window.__rd.bgm = bgm;   // test hook (declared after the module initializes)

  /* ------------------------------------------- fullscreen / chrome hide */
  var hideT = 0;

  function wakeBar() {
    idleArm();   // any input re-arms the idle hide — revealing is deliberate
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

  /* ------------------------------------------ scroll-direction chrome hide */
  /* Phone: scrolling down slides the reader toolbar + page header up and out
     (negative top margins — only JS knows the pixel heights — pull the stage
     into the freed space); scrolling up, a keypress, opening a panel or a
     chapter change brings them back. The CSS half of the effect lives under a
     (max-width: 760px), (pointer: coarse) query, and chromeOn() mirrors that
     query here, so desktop never arms it. */
  var chrome = { away: false, fY: null, wY: null, acc: 0, follow: 0, followUntil: 0, quiet: 0, lastLayout: 0 };
  var CHROME_PX = 16;         // down-drift before the bars retract
  var CHROME_UP_PX = 64;      // up-drift before they come back — a phone user
                              // steering a scroll often tugs the thumb up a few
                              // pixels first, so a deliberate 16px is way too
                              // twitchy to mean "show the toolbar"
  var CHROME_JUMP = 400;   // bigger jumps are programmatic — re-baseline only
  var chromeMq = window.matchMedia
    ? window.matchMedia('(max-width: 760px), (pointer: coarse)') : null;

  function chromeOn() { return !!(chromeMq && chromeMq.matches); }

  function chromeBlocked() {
    var rd = el('rd');
    return document.body.classList.contains('nav-open') || !!(rd && rd.dataset.panel);
  }

  function chromeReset() { chrome.fY = null; chrome.wY = null; chrome.acc = 0; }

  function frameScrollY() {
    if (!fdoc) return null;
    var sc = fdoc.scrollingElement || fdoc.documentElement;
    return (sc && sc.scrollTop) || 0;
  }

  /* rect.top moves every frame while the margins animate, so keep resizing the
     stage until the transition is done — one shot at either end would leave it
     the wrong height. */
  function followStage() {
    chrome.followUntil = Date.now() + 420;
    if (chrome.follow) return;
    function step() {
      resizeStage();
      if (Date.now() < chrome.followUntil) chrome.follow = requestAnimationFrame(step);
      else { chrome.follow = 0; resizeStage(); }
    }
    chrome.follow = requestAnimationFrame(step);
  }

  function setChromeAway(on) {
    on = !!on;
    if (!on) idle.away = false;   // whatever brings the bars back ends the idle
                                  // state (flag lives here so every reveal path
                                  // — scroll, key, tap, panel, mq — clears it)
    if (on && (!chromeOn() || chromeBlocked())) return;
    if (on === chrome.away) return;
    chrome.away = on;
    chrome.acc = 0;
    document.body.classList.toggle('chrome-away', on);
    [document.querySelector('.topbar'), el('rd-bar')].forEach(function (bar) {
      if (!bar) return;
      if (on) {
        var h = bar.offsetHeight;
        if (h) bar.style.marginTop = -h + 'px';
      } else {
        bar.style.marginTop = '';
      }
    });
    followStage();
    // shrinking/growing the stage changes the document height and can clamp
    // the parent page's scroll — swallow that self-inflicted scroll until the
    // transition has settled (frame-side clamps are filtered by lastLayout)
    chrome.quiet = chrome.followUntil;
  }

  function chromeFrom(src, y) {
    if (y == null) return;
    var key = src === 'f' ? 'fY' : 'wY';
    var prev = chrome[key];
    chrome[key] = y;
    if (prev == null) return;
    var d = y - prev;
    if (!d || !chromeOn()) return;
    if (Math.abs(d) > CHROME_JUMP) { chrome.acc = 0; return; }   // jumpTo/restore
    // while our own stage resize is animating the iframe, Blink re-anchors the
    // frame's scroll (clamped at the chapter end, anchored mid-content) — those
    // events must not read as user scrolling or state flips into a resize loop
    if (src === 'f' && Date.now() - chrome.lastLayout < 120) return;
    if (src !== 'f' && Date.now() < chrome.quiet) return;
    chrome.acc = (chrome.acc && (d > 0) === (chrome.acc > 0)) ? chrome.acc + d : d;
    if (chrome.acc >= CHROME_PX) setChromeAway(true);
    else if (chrome.acc <= -CHROME_UP_PX) setChromeAway(false);
  }

  /* ------------------------------------------ idle auto-hide (phone) */
  /* No touch for 10s → the header + reader toolbar retract exactly like a
     scroll-down does. Touches only RE-ARM the timer (wakeBar → idleArm); the
     bars come back only on a deliberate act: a tap (click — no click ever fires
     from a drag, so steering a scroll never pops them up) or a pull-up of
     CHROME_UP_PX, or a keypress. A bar that a scroll hid stays a scroll-bar
     until 10s pass without a touch — only then does a tap start to reveal it
     (idle.away). Desktop never arms: the media query in idleArm mirrors the one
     setChromeAway guards. */
  var idle = { t: 0, away: false };
  var IDLE_MS = 10000;

  function idleArm() {
    if (idle.t) clearTimeout(idle.t);
    idle.t = 0;
    if (!chromeOn()) return;
    idle.t = setTimeout(function () {
      idle.t = 0;
      if (!chromeOn() || chromeBlocked()) return;   // panel/nav open → keep it
      idle.away = true;
      setChromeAway(true);
    }, IDLE_MS);
  }

  function idleReveal() {
    if (idle.away) setChromeAway(false);   // deliberate act gives them back
    idleArm();
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
    if (Math.abs(h - stage.offsetHeight) > 3) {
      stage.style.height = h + 'px';
      chrome.lastLayout = Date.now();   // frame scroll events right now are ours
    }
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
    setChromeAway(false);   // deliberate keypress → chrome back on screen
    var k = ev.key;

    if (k === 'Escape' || k === 'Esc') {
      var pop = el('rd-selpop');
      if (pop && !pop.hidden) { hideSelPop(); return; }
      var lk = el('rd-lkp');
      if (lk && !lk.hidden) { hideLookup(); return; }
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
      case 's': case 'S': togglePanel('search'); return;
      case 'n': case 'N': togglePanel('ann'); return;
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
    ['toc', 'font', 'tts', 'search', 'ann'].forEach(function (n) {
      var btn = el('rd-open-' + n);
      if (btn) btn.addEventListener('click', function () { togglePanel(n); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('.rd-panel-close'),
      function (b) { b.addEventListener('click', function () { togglePanel(''); }); });
    var fnClose = el('rd-fn-close');
    if (fnClose) fnClose.addEventListener('click', closeFootnote);

    // selection popup → create annotation
    var pop = el('rd-selpop');
    if (pop) {
      pop.addEventListener('mousedown', function () { selLock = 1; });
      pop.addEventListener('touchstart', function () { selLock = 1; }, { passive: true });
      pop.addEventListener('mouseup', function () {
        setTimeout(function () { selLock = 0; }, 140);
      });
      pop.addEventListener('touchend', function () {
        setTimeout(function () { selLock = 0; }, 140);
      });
      qsa('.rd-dot[data-color]', pop).forEach(function (b) {
        b.addEventListener('click', function () { addAnn(b.dataset.color, ''); });
      });
      var selNote = el('rd-sel-note');
      if (selNote) selNote.addEventListener('click', function () {
        addAnn('yellow', '').then(function (created) {
          if (!created) return;
          togglePanel('ann');
          var row = qs('.rd-ann-row[data-id="' + created.id + '"]');
          if (row) editAnnNote(row, created);
        });
      });
      var selAi = el('rd-sel-ai');
      if (selAi) selAi.addEventListener('click', runLookup);
    }
    var lkpClose = el('rd-lkp-close');
    if (lkpClose) lkpClose.addEventListener('click', hideLookup);
    // click anywhere else in the parent document dismisses the popups
    document.addEventListener('mousedown', function (ev) {
      var lkp = el('rd-lkp');
      if (lkp && !lkp.hidden && !lkp.contains(ev.target)) hideLookup();
      var p = el('rd-selpop');
      if (!p || p.hidden) return;
      if (p.contains(ev.target)) return;
      hideSelPop();
    });

    // search panel
    var searchQ = el('rd-search-q');
    if (searchQ) {
      searchQ.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); runSearch(); return; }
        if (ev.key === 'ArrowDown' && srch.matches.length) {
          ev.preventDefault(); stepQ(1); return;
        }
        if (ev.key === 'ArrowUp' && srch.matches.length) {
          ev.preventDefault(); stepQ(-1); return;
        }
      });
      searchQ.addEventListener('input', function () {
        srch.q = this.value;
        if (srch.scope !== 'chapter') return;
        if (srch.t) clearTimeout(srch.t);
        srch.t = setTimeout(function () { srch.t = 0; runSearch(); }, 220);
      });
    }
    var searchGo = el('rd-search-go');
    if (searchGo) searchGo.addEventListener('click', runSearch);
    qsa('.rd-scope').forEach(function (b) {
      b.addEventListener('click', function () { setScope(b.dataset.scope); });
    });
    var qPrev = el('rd-q-prev'), qNext = el('rd-q-next'), qClear = el('rd-q-clear');
    if (qPrev) qPrev.addEventListener('click', function () { stepQ(-1); });
    if (qNext) qNext.addEventListener('click', function () { stepQ(1); });
    if (qClear) qClear.addEventListener('click', function () {
      clearSearch(false);
      searchStatus('');
    });

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
    if (prevP) prevP.addEventListener('click', function () { stepItemTts(-1); });
    if (nextP) nextP.addEventListener('click', function () { stepItemTts(1); });
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
      playerSetRate(ttsCfg.rate);
      saveJSON(ttsKey, ttsCfg);
    });
    var voice = el('rd-tts-voice');
    if (voice) voice.addEventListener('change', function () {
      ttsCfg.voice = this.value;
      speak.groups = {};        // old audio belongs to the previous voice
      speak.inflight = {};
      speak.gen++;
      saveJSON(ttsKey, ttsCfg);
    });
    // background music: own toggle + own volume (never follows the read rate)
    var mvol = el('rd-music-vol');
    if (mvol) {
      mvol.value = bgm.vol;
      var mvolV = el('rd-music-vol-v');
      if (mvolV) mvolV.textContent = bgm.vol + '%';
      mvol.addEventListener('input', function () { bgmSetVol(this.value); });
    }
    var mplay = el('rd-music-play');
    if (mplay) mplay.addEventListener('click', bgmToggle);
    var mfile = el('rd-music-file');
    if (mfile) mfile.addEventListener('change', function () {
      var wasOn = bgm.on || bgm.want;
      bgm.file = this.value;
      bgm.buf = null;
      bgm.bufFile = null;
      bgmSave();
      if (wasOn) { bgmStop(); bgmPlay(); }   // switch track live
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
    var jtog = el('rd-tts-jump-toggle');
    if (jtog) jtog.addEventListener('click', function () {
      if (!speak.on) return;
      if (speak.paused) speakResume(); else speakPause();
    });
    var jstop = el('rd-tts-jump-stop');
    if (jstop) jstop.addEventListener('click', function () { speakStop(true); });
    var test = el('rd-tts-test');
    if (test) test.addEventListener('click', function () {
      if (!tts.voices.length) { warmVoices(); return; }
      ttsStatus(i18n('read.tts_synthesizing'));
      ttsPost(i18n('read.test_text')).then(function (r) {
        ttsStatus(i18n('read.tts_playing'));
        var audio = new Audio(URL.createObjectURL(r.blob));
        audio.playbackRate = ttsCfg.rate;
        audio.onended = function () { ttsStatus(fmt('read.tts_ready_n', { n: tts.voices.length })); };
        audio.play();
      }).catch(function (e) {
        ttsStatus(i18n('read.tts_error') + ' ' + e.message, 'error');
      });
    });

    // parent-level input
    document.addEventListener('keydown', onKeydown);
    setInterval(statsTick, 15000);   // reading-time counter (visibility/TTS aware)
    document.addEventListener('mousemove', wakeBar, { passive: true });
    ['pointerdown', 'touchstart', 'keydown'].forEach(function (evt) {
      document.addEventListener(evt, wakeBar, { passive: true });
    });
    // phone modal: tapping the dimmed backdrop (#rd::before) closes the panel
    document.addEventListener('click', function (ev) {
      var rd = el('rd');
      if (!rd || !rd.dataset.panel || !chromeOn()) return;
      if (ev.target === rd) togglePanel('');
    });
    // a real tap gives an idle-hidden bar back (a drag never fires click, so
    // touching down to steer a scroll only re-arms the timer)
    document.addEventListener('click', idleReveal, { passive: true });
    idleArm();   // phone: the bar retracts on its own after 10s of no touch
    window.addEventListener('resize', resizeSoon);
    window.addEventListener('scroll', resizeSoon, { passive: true });
    // phone: page scroll direction retracts/restores header + reader toolbar
    window.addEventListener('scroll', function () {
      chromeFrom('w', window.pageYOffset || (document.documentElement || {}).scrollTop || 0);
    }, { passive: true });
    if (chromeMq) {
      var onChromeMq = function () {
        if (!chromeOn()) {
          setChromeAway(false);
          document.body.style.overflow = '';   // desktop → inline panels again
        } else if (el('rd') && el('rd').dataset.panel) {
          document.body.style.overflow = 'hidden';   // reopened as a modal
        }
        idleArm();
      };
      if (chromeMq.addEventListener) chromeMq.addEventListener('change', onChromeMq);
      else if (chromeMq.addListener) chromeMq.addListener(onChromeMq);
    }
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
    setChromeAway(false);
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
    var savedBgm = loadJSON(bgmKey, null);
    if (savedBgm) {
      if (typeof savedBgm.file === 'string') bgm.file = savedBgm.file;
      if (savedBgm.vol !== undefined && !isNaN(Number(savedBgm.vol))) bgm.vol = clamp(Number(savedBgm.vol), 0, 100);
    }
    if (settings.family === 'custom') settings.family = 'serif';  // font is session-only

    applySettings();
    bindStatic();
    frame.addEventListener('load', onFrameLoad);
    resizeStage();
    showLoading(true);

    Promise.all([
      getJSON(bookUrl(R.metaUrl)),
      getJSON(bookUrl(R.stateUrl)).catch(function () { return null; }),
      getJSON(bookUrl(R.tocUrl)).catch(function () { return null; }),
      getJSON(bookUrl(R.annUrl)).catch(function () { return null; })
    ]).then(function (r) {
      var meta = r[0], remote = r[1], toc = r[2];
      state.meta = meta;
      state.toc = toc;
      anns = (r[3] && r[3].anns) || [];
      if (remote && remote.settings) {
        settings = Object.assign({}, settings, remote.settings);
        if (settings.family === 'custom') settings.family = 'serif';
        saveJSON(setKey, settings);
      }
      applySettings();
      buildSelect();
      buildToc();
      renderAnns();
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
