#!/usr/bin/env python3
"""
Leest de sitemap van www.donateursbelangen.nl, opent elke pagina in een
headless browser (Playwright), laat Softr de inhoud laden en schrijft een
schone, statische HTML-versie naar de map site/.

Modi:
  sample  ongeveer 100 pagina's verspreid over alle paginatypen (proefrun)
  daily   nieuwe pagina's, pagina's met een nieuwere lastmod, en opruimen
          van pagina's die niet meer in de sitemap staan
  full    alle pagina's opnieuw

Alleen pagina's waarvan de inhoud echt veranderd is worden herschreven.
De status per URL staat in state/pages.json.
"""
import argparse
import asyncio
import hashlib
import html
import json
import random
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

WWW_HOST = "www.donateursbelangen.nl"
SITEMAP_URL = "https://www.donateursbelangen.nl/sitemap.xml"
SITE_DIR = Path("site")
STATE_FILE = Path("state/pages.json")
MIN_TEXT = 150            # minder tekst dan dit = pagina niet goed geladen
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 DonateursbelangenMirror/1.0")

# Technische pagina's die niet worden uitgelezen (zelfde lijst als generate_discovery.py)
EXCLUDE = {
    "login", "forgot-password", "reset-password", "link-expired", "404",
    "404-old", "401", "mollieredirect", "mailjet-test-form", "postfilter",
    "zoeken", "onderzoeksrapport-detail", "podcasts-details",
    "opzegservice-details", "goededoelenweek-plaats-detailinformatie",
    "in-de-schijnwerpers-organisatie", "anbi-organisatie",
}

CSS = """
:root{--bg:#fbfbf8;--fg:#1d2321;--muted:#5b6662;--line:#d9ddd6;--link:#1f5f8b;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#161a19;--fg:#e6e9e4;--muted:#9aa49f;--line:#2e3532;--link:#8cc4ea;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.6 Charter,"Bitstream Charter","Sitka Text",Cambria,Georgia,serif}
main{max-width:46rem;margin:0 auto;padding:2rem 1.25rem 4rem}
a{color:var(--link)}img{max-width:100%;height:auto}
table{border-collapse:collapse;display:block;overflow-x:auto}td,th{border:1px solid var(--line);padding:.3rem .5rem}
.bron{color:var(--muted);font-size:.9rem;border-bottom:1px solid var(--line);padding-bottom:.75rem;margin-bottom:1.5rem}
"""

# JavaScript dat in de volledig geladen pagina draait. Het loopt door de
# zichtbare pagina (ook door iframes heen), neemt alleen zichtbare tekst mee,
# zet grafieken om naar tabellen en laat menu, footer, kaarten en plaatjes weg.
EXTRACT_JS = r"""
() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IMG','PICTURE','VIDEO','AUDIO',
    'INPUT','SELECT','TEXTAREA','BUTTON','LINK','META','HEADER','FOOTER','NAV','SVG','svg','OBJECT','EMBED',
    'FORM','LABEL']);
  const KEEP = new Set(['H1','H2','H3','H4','H5','H6','P','UL','OL','LI','TABLE','THEAD','TBODY','TFOOT',
    'TR','TD','TH','CAPTION','A','STRONG','B','EM','I','BLOCKQUOTE','BR','DL','DT','DD','SUP','SUB']);
  const SKIP_RE = /leaflet|navbar|cookie|apexcharts-toolbar|highcharts-credits|modal|popup|lightbox/i;
  const host = location.host;
  const esc = t => t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const attr = t => esc(String(t)).replace(/"/g,'&quot;');
  const fmt = v => (v && typeof v === 'object') ? (v.y ?? v.value ?? v.v ?? JSON.stringify(v)) : (v ?? '');
  const diag = {iframes: [], charts: {}, canvasesZonderData: 0, shadowRoots: 0, weggelaten: []};
  const note = (el, reden) => {
    let t = 0; try { t = (el.innerText || el.textContent || '').trim().length; } catch (e) {}
    if (t >= 40 && diag.weggelaten.length < 60) diag.weggelaten.push({reden, tag: el.tagName,
      id: (el.id || '').slice(0, 60), klasse: (typeof el.className === 'string' ? el.className : '').slice(0, 80),
      tekst: t, begin: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60)});
  };

  function table(title, labels, series) {
    if (!series.length) return '';
    let h = '<table>' + (title ? '<caption>' + esc(String(title)) + '</caption>' : '');
    h += '<tr><th></th>' + series.map(s => '<th>' + esc(String(s.name || '')) + '</th>').join('') + '</tr>';
    const n = Math.max(labels.length, ...series.map(s => s.data.length));
    for (let i = 0; i < n; i++) {
      h += '<tr><th>' + esc(String(labels[i] ?? '')) + '</th>' +
           series.map(s => '<td>' + esc(String(fmt(s.data[i]))) + '</td>').join('') + '</tr>';
    }
    return h + '</table>';
  }
  const count = k => diag.charts[k] = (diag.charts[k] || 0) + 1;

  function chartFromCanvas(c, win) {
    try {
      let ch = null;
      if (win.Chart) {
        if (win.Chart.getChart) ch = win.Chart.getChart(c);
        if (!ch && win.Chart.instances) ch = Object.values(win.Chart.instances).find(x => x.canvas === c || (x.chart && x.chart.canvas === c));
      }
      if (ch && ch.data) {
        count('chartjs');
        const o = ch.options || {};
        const t = (o.plugins && o.plugins.title && o.plugins.title.text) || (o.title && o.title.text) || '';
        return table(Array.isArray(t) ? t.join(' ') : t, ch.data.labels || [],
          (ch.data.datasets || []).map(d => ({name: d.label, data: d.data || []})));
      }
    } catch (e) {}
    return null;
  }
  function chartFromContainer(el, win) {
    try {
      if (win.echarts && el.getAttribute && el.getAttribute('_echarts_instance_')) {
        const inst = win.echarts.getInstanceByDom(el);
        if (inst) {
          count('echarts');
          const o = inst.getOption();
          const x = (o.xAxis && o.xAxis[0] && o.xAxis[0].data) || (o.yAxis && o.yAxis[0] && o.yAxis[0].data) || [];
          const ser = (o.series || []);
          if (!x.length && ser[0] && ser[0].data && ser[0].data[0] && ser[0].data[0].name !== undefined) {
            return table(o.title && o.title[0] && o.title[0].text, ser[0].data.map(d => d.name),
              ser.map(s => ({name: s.name, data: s.data})));
          }
          return table(o.title && o.title[0] && o.title[0].text, x, ser.map(s => ({name: s.name, data: s.data || []})));
        }
      }
      if (win.Highcharts && el.getAttribute && el.getAttribute('data-highcharts-chart') !== null) {
        const ch = win.Highcharts.charts[+el.getAttribute('data-highcharts-chart')];
        if (ch) {
          count('highcharts');
          const s0 = ch.series[0];
          const labels = s0 ? s0.points.map(p => p.category ?? p.name) : [];
          return table(ch.title && ch.title.textStr, labels,
            ch.series.map(s => ({name: s.name, data: s.points.map(p => p.y)})));
        }
      }
      if (win.Apex && win.Apex._chartInstances && el.classList && el.classList.contains('apexcharts-canvas')) {
        const inst = win.Apex._chartInstances.find(i => i.chart && i.chart.el && i.chart.el.contains(el));
        if (inst) {
          count('apexcharts');
          const cfg = inst.chart.w.config;
          let series = cfg.series || [];
          const labels = (cfg.labels && cfg.labels.length ? cfg.labels : (cfg.xaxis && cfg.xaxis.categories)) || [];
          series = typeof series[0] === 'number' ? [{name: '', data: series}] : series;
          return table(cfg.title && cfg.title.text, labels, series.map(s => ({name: s.name, data: s.data || []})));
        }
      }
    } catch (e) {}
    return null;
  }

  function visible(el) {
    try {
      if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
      const r = el.getBoundingClientRect();
      return !(r.width === 0 && r.height === 0 && el.tagName !== 'BR');
    } catch (e) { return true; }
  }

  // Verborgen Softr-gegevensblok (id list-details...) omzetten naar een
  // nette tabel met jaarcijfers en een lijst met overige gegevens.
  const LABEL = /^[A-Za-z0-9][A-Za-z0-9_:\-]{1,59}$/;
  const OVERSLAAN = /^(random|recordid|nonprofits_sd_recordid|seo:|slug_|latitude|longitude|longtitude|logo|dataverrijking|status-aanmelding|link_parent|url_nonprofit_recordid)/i;
  function dataBlock(el) {
    const pairs = [], used = new Set();
    for (const e of el.querySelectorAll('*')) {
      if (used.has(e) || e.children.length < 2) continue;
      const first = (e.children[0].textContent || '').trim();
      if (!LABEL.test(first)) continue;
      const rest = Array.from(e.children).slice(1).map(c => (c.textContent || '').replace(/\s+/g, ' ').trim())
                        .filter(t => t && t !== 'Loading...').join(', ');
      pairs.push([first, rest || '-']);
      e.querySelectorAll('*').forEach(d => used.add(d));
    }
    diag.gegevensvelden = pairs.length;
    if (pairs.length < 5) return '';
    const years = ((pairs.find(p => p[0] === 'jaarcijfers_jaartal') || [])[1] || '').split(/,\s*/).filter(Boolean);
    const rows = [], items = [];
    for (const [k, v] of pairs) {
      if (OVERSLAAN.test(k) || k === 'jaarcijfers_jaartal' || v === '-') continue;
      const parts = v.split(/,\s*/);
      const naam = esc(k.replace(/[_:]+/g, ' '));
      if (years.length > 1 && parts.length === years.length && parts.every(x => /^-?\d+(\.\d+)?$/.test(x)))
        rows.push('<tr><th>' + naam + '</th>' + parts.map(x => '<td>' + esc(x) + '</td>').join('') + '</tr>');
      else items.push('<dt>' + naam + '</dt><dd>' + esc(v) + '</dd>');
    }
    let h = '\n<h2>Gegevens en jaarcijfers</h2>';
    if (rows.length) h += '<table><tr><th></th>' + years.map(y => '<th>' + esc(y) + '</th>').join('') + '</tr>' + rows.join('') + '</table>';
    if (items.length) h += '<dl>' + items.join('') + '</dl>';
    return h;
  }

  function children(el) {
    if (el.tagName === 'SLOT') return el.assignedNodes({flatten: true});
    if (el.shadowRoot) { diag.shadowRoots++; return el.shadowRoot.childNodes; }
    return el.childNodes;
  }

  function walk(node, win, force) {
    if (node.nodeType === 3) return esc(node.textContent.replace(/\s+/g, ' '));
    if (node.nodeType === 11) { let o = ''; for (const c of node.childNodes) o += walk(c, win, force); return o; }
    if (node.nodeType !== 1) return '';
    const el = node, tag = el.tagName;
    if (tag === 'IFRAME') {
      if (!visible(el)) { note(el, 'iframe onzichtbaar'); return ''; }
      let doc = null;
      try { doc = el.contentDocument; } catch (e) {}
      diag.iframes.push({src: (el.getAttribute('src') || '').slice(0, 120), srcdoc: !!el.getAttribute('srcdoc'), leesbaar: !!(doc && doc.body)});
      return (doc && doc.body) ? walk(doc.body, el.contentWindow) : '';
    }
    if (SKIP.has(tag)) { if (!['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','META','LINK'].includes(tag)) note(el, 'tag ' + tag); return ''; }
    if (SKIP_RE.test((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : ''))) { note(el, 'naam'); return ''; }
    if (tag === 'CANVAS') {
      const t = chartFromCanvas(el, win);
      if (t === null) diag.canvasesZonderData++;
      return t || '';
    }
    const chart = chartFromContainer(el, win);
    if (chart) return chart;
    if (!force && !visible(el)) {
      if (/^list-details/i.test(el.id || '')) return dataBlock(el);
      if (/collapse|accordion/i.test(typeof el.className === 'string' ? el.className : '')) force = true;
      else { note(el, 'onzichtbaar'); return ''; }
    }
    const tc = el.textContent || '';
    if (tag !== 'BODY' && tag !== 'MAIN' && tc.length < 3000 && tc.includes('Alle rechten voorbehouden') && /©\s*20\d\d/.test(tc)) { note(el, 'footer-tekst'); return ''; }
    let inner = '';
    for (const ch of children(el)) inner += walk(ch, win, force);
    if (KEEP.has(tag)) {
      if (tag === 'BR') return '<br>';
      if (!inner.trim()) return '';
      let a = '';
      if (tag === 'A' && el.getAttribute('href')) {
        try {
          const u = new URL(el.getAttribute('href'), el.ownerDocument.baseURI);
          a = ' href="' + attr(u.host === host && !u.search ? u.pathname : u.href) + '"';
        } catch (e) {}
      }
      if ((tag === 'TD' || tag === 'TH') && el.getAttribute('colspan')) a = ' colspan="' + attr(el.getAttribute('colspan')) + '"';
      const t = tag.toLowerCase();
      return '<' + t + a + '>' + inner.trim() + '</' + t + '>';
    }
    if (!inner.trim()) return '';
    let block = false;
    try { block = !/^inline/.test(win.getComputedStyle(el).display); } catch (e) {}
    if (block && !/<(p|h[1-6]|ul|ol|table|dl|blockquote)[ >]/.test(inner)) return '\n<p>' + inner.trim() + '</p>';
    return block ? '\n' + inner : inner + ' ';
  }

  let body = walk(document.body, window, false)
    .replace(/<p>\s*(Loading\.\.\.|0\.00)\s*<\/p>/gi, '')
    .replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n');
  const desc = document.querySelector('meta[name="description"]');
  const tmp = document.createElement('div'); tmp.innerHTML = body;
  const h1 = tmp.querySelector('h1');
  return {
    title: (h1 && h1.textContent.trim()) || document.title || '',
    docTitle: document.title || '',
    description: desc ? desc.content : '',
    body: body,
    text: tmp.textContent.replace(/\s+/g, ' ').trim(),
    diag: diag
  };
}
"""

# Meet hoeveel tekst er zichtbaar is (ook in iframes) en of er nog "Loading..." staat.
PROGRESS_JS = r"""
() => {
  let len = 0, loading = 0;
  const docs = [document];
  document.querySelectorAll('iframe').forEach(f => { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} });
  const roots = [];
  const collect = r => { roots.push(r); r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) collect(e.shadowRoot); }); };
  docs.forEach(d => d.body && collect(d.body));
  for (const r of roots) {
    const t = r === r.ownerDocument?.body ? (r.innerText || '') : Array.from(r.childNodes).map(n => n.innerText || n.textContent || '').join(' ');
    len += t.length;
    loading += (t.match(/Loading\.\.\./g) || []).length;
  }
  return {len, loading, height: document.body.scrollHeight};
}
"""

EXPAND_JS = r"""
() => {
  // ingeklapte secties openen: <details>, elementen met aria-expanded="false"
  // en knoppen met een 'oogje' (eye) die een sectie tonen
  const roots = [];
  const collect = r => { roots.push(r); r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) collect(e.shadowRoot); }); };
  collect(document);
  document.querySelectorAll('iframe').forEach(f => { try { if (f.contentDocument) collect(f.contentDocument); } catch (e) {} });
  let n = 0;
  const kandidaten = [];
  for (const r of roots) {
    r.querySelectorAll('details:not([open])').forEach(d => { d.open = true; n++; });
    r.querySelectorAll('[aria-expanded="false"]').forEach(e => {
      if (e.tagName === 'A' && e.getAttribute('href') && !e.getAttribute('href').startsWith('#')) return;
      if (e.closest('header, nav')) return;
      try { e.click(); n++; } catch (x) {}
    });
    r.querySelectorAll('button, [role="button"]').forEach(b => {
      const h = (b.outerHTML || '').slice(0, 400);
      if (kandidaten.length < 15 && !b.closest('header, nav') && !(b.innerText || '').trim())
        kandidaten.push(h.slice(0, 200));
      if (/eye(?![-_]?slash|[-_]?off)/i.test(h) && !/eye[-_]?(slash|off)/i.test(h) && !b.closest('header, nav')) {
        try { b.click(); n++; } catch (x) {}
      }
    });
  }
  return {geklikt: n, kandidaten};
}
"""

SCROLL_JS = r"""
async () => {
  const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
  for (let y = 0; y < document.body.scrollHeight; y += step) {
    window.scrollTo(0, y);
    document.querySelectorAll('iframe').forEach(f => { try { f.contentWindow.scrollTo(0, y); } catch (e) {} });
    await new Promise(r => setTimeout(r, 250));
  }
  window.scrollTo(0, 0);
}
"""


# ---------------------------------------------------------------------------
def fetch_sitemap(url, local=None):
    if local:
        data = Path(local).read_bytes()
    else:
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                break
            except Exception as e:          # Softr laadt de sitemap traag
                last = e
                time.sleep(30 * (attempt + 1))
        else:
            raise SystemExit(f"Sitemap niet op te halen: {last}")
    Path("state").mkdir(exist_ok=True)
    Path("state/sitemap.xml").write_bytes(data)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out = {}
    for u in ET.fromstring(data).findall("s:url", ns):
        loc = (u.findtext("s:loc", default="", namespaces=ns) or "").strip()
        lm = (u.findtext("s:lastmod", default="", namespaces=ns) or "").strip()
        p = urlparse(loc)
        if not loc or p.query or p.netloc != WWW_HOST:
            continue
        parts = [x for x in p.path.split("/") if x]
        if len(parts) <= 1 and (parts[0] if parts else "") in EXCLUDE:
            continue
        out[loc] = lm
    return out


def out_path(url):
    path = urlparse(url).path.strip("/")
    return SITE_DIR / f"{path or 'home'}.html"


def page_type(url):
    parts = [x for x in urlparse(url).path.split("/") if x]
    return parts[0] if len(parts) > 1 else "_los"


def build_html(url, data):
    title = data["title"] or data["docTitle"]
    head_title = data["docTitle"] or title
    desc = f'<meta name="description" content="{html.escape(data["description"])}">\n' if data["description"] else ""
    return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(head_title)}</title>
{desc}<link rel="canonical" href="{html.escape(url)}">
<style>{CSS}</style>
</head>
<body>
<main>
<p class="bron">Leesbare kopie van <a href="{html.escape(url)}">{html.escape(url)}</a>, gemaakt voor AI-agents en crawlers. <a href="/">Overzicht</a></p>
{data["body"]}
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
async def render_one(context, url, timeout_ms, debug_dir=None):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="load", timeout=timeout_ms)
        deadline = time.time() + timeout_ms / 1000
        # scrollen zodat lui geladen blokken starten, daarna wachten tot de
        # hoeveelheid tekst 3 seconden gelijk blijft en "Loading..." weg is
        await page.evaluate(SCROLL_JS)
        last, stable, scrolled_again, prog, expand = -1, 0, False, {}, {}
        while time.time() < deadline:
            await page.wait_for_timeout(700)
            prog = await page.evaluate(PROGRESS_JS)
            stable = stable + 1 if prog["len"] == last else 0
            last = prog["len"]
            if stable >= 3 and prog["loading"] == 0 and scrolled_again:
                break
            if stable >= 2 and not scrolled_again:
                expand = await page.evaluate(EXPAND_JS)
                await page.evaluate(SCROLL_JS)
                scrolled_again, stable = True, 0
        data = await page.evaluate(EXTRACT_JS)
        if debug_dir:
            name = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path).strip("_") or "home"
            debug_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
            (debug_dir / f"{name}.json").write_text(json.dumps(
                {"url": url, "tekst_lengte": len(data["text"]), "diag": data["diag"],
                 "voortgang": prog, "uitklappen": expand}, indent=1, ensure_ascii=False))
        return data
    finally:
        await page.close()


async def render_all(urls, state, concurrency, delay, timeout_ms, debug_dir=None):
    stats = defaultdict(int)
    failed = []
    queue = asyncio.Queue()
    for u in urls:
        queue.put_nowait(u)
    total = len(urls)
    started = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(user_agent=USER_AGENT, locale="nl-NL",
                                            viewport={"width": 1280, "height": 900})
        # foto's en video niet laden: scheelt tijd (in debugmodus wel, voor de schermafbeelding)
        if not debug_dir:
            await context.route(re.compile(r".*\.(png|jpe?g|gif|webp|avif|mp4|webm)(\?.*)?$", re.I),
                                lambda route: route.abort())

        async def worker():
            while True:
                try:
                    url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                data = None
                for attempt in range(2):
                    try:
                        data = await render_one(context, url, timeout_ms, debug_dir)
                        if len(data["text"]) >= MIN_TEXT:
                            break
                        data = None
                    except Exception as e:
                        print(f"  fout ({attempt + 1}) {url}: {e.__class__.__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}", flush=True)
                    await asyncio.sleep(3)
                if data is None:
                    stats["mislukt"] += 1
                    failed.append(url)
                else:
                    doc = build_html(url, data)
                    digest = hashlib.sha256(data["body"].encode()).hexdigest()
                    entry = state.get(url, {})
                    f = out_path(url)
                    if entry.get("hash") == digest and f.exists():
                        stats["ongewijzigd"] += 1
                    else:
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text(doc, encoding="utf-8")
                        stats["bijgewerkt"] += 1
                        entry["hash"] = digest
                        entry["changed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    entry["rendered"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    state[url] = entry
                done = sum(stats.values())
                if done % 50 == 0 or done == total:
                    rate = done / max(time.time() - started, 1)
                    print(f"{done}/{total} klaar ({rate:.2f} p/s) {dict(stats)}", flush=True)
                await asyncio.sleep(delay)

        await asyncio.gather(*[worker() for _ in range(concurrency)])
        await browser.close()
    return stats, failed


def choose(mode, sitemap, state, limit, seed):
    if mode == "full":
        urls = list(sitemap)
    elif mode == "daily":
        urls = [u for u, lm in sitemap.items()
                if u not in state or (lm and lm != state[u].get("lastmod"))
                or not out_path(u).exists()]
    else:  # sample: verspreid over alle paginatypen
        rnd = random.Random(seed)
        groups = defaultdict(list)
        for u in sitemap:
            groups[page_type(u)].append(u)
        per = max(1, limit // max(len(groups), 1))
        urls = []
        for g in groups.values():
            rnd.shuffle(g)
            urls += g[:per]
        urls = urls[:limit]
    if limit and mode != "sample":
        urls = urls[:limit]
    return urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["test", "sample", "daily", "full"], default="daily")
    ap.add_argument("--urls", default="", help="komma-gescheiden URL's voor modus test")
    ap.add_argument("--debug", action="store_true", help="schermafbeelding en diagnose per pagina in debug/")
    ap.add_argument("--limit", type=int, default=0, help="maximum aantal pagina's (0 = geen)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.0, help="pauze per tab na elke pagina (s)")
    ap.add_argument("--timeout", type=int, default=60, help="time-out per pagina (s)")
    ap.add_argument("--sitemap-file", help="lokale sitemap gebruiken in plaats van ophalen")
    a = ap.parse_args()
    if a.mode == "sample" and not a.limit:
        a.limit = 100

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if a.mode == "test" and not a.sitemap_file and Path("state/sitemap.xml").exists():
        a.sitemap_file = "state/sitemap.xml"
    print("Sitemap ophalen...", flush=True)
    sitemap = fetch_sitemap(SITEMAP_URL, a.sitemap_file)
    print(f"Sitemap: {len(sitemap)} pagina's")

    # pagina's die niet meer in de sitemap staan opruimen (niet bij een proefrun)
    removed = 0
    if a.mode in ("daily", "full"):
        for url in [u for u in state if u not in sitemap]:
            f = out_path(url)
            if f.exists():
                f.unlink()
            del state[url]
            removed += 1

    if a.mode == "test":
        urls = [u.strip() for u in a.urls.split(",") if u.strip()]
    else:
        urls = choose(a.mode, sitemap, state, a.limit, seed=datetime.now().strftime("%Y%m%d"))
    print(f"Modus {a.mode}: {len(urls)} pagina's uitlezen, {removed} verwijderd")

    t0 = time.time()
    debug_dir = Path("debug") if (a.debug or a.mode == "test") else None
    stats, failed = asyncio.run(render_all(urls, state, a.concurrency, a.delay, a.timeout * 1000, debug_dir))
    for u in urls:
        if u in state:
            state[u]["lastmod"] = sitemap.get(u, "")

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))
    Path("state/last-run.json").write_text(json.dumps({
        "mode": a.mode, "datum": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duur_min": round((time.time() - t0) / 60, 1), "pagina's": len(urls),
        "resultaat": stats, "verwijderd": removed, "mislukt": failed[:200]}, indent=1))

    mins = (time.time() - t0) / 60
    print(f"Klaar in {mins:.1f} min: {dict(stats)}, verwijderd {removed}")
    if urls and stats["mislukt"] > 0.2 * len(urls):
        print("Meer dan 20% mislukt; controleer de log.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
