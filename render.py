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

# JavaScript dat in de geladen pagina draait en schone inhoud teruggeeft.
EXTRACT_JS = r"""
() => {
  const host = location.host;
  const root = document.body.cloneNode(true);
  const drop = 'script,style,noscript,template,iframe,svg,canvas,video,audio,' +
               'form,input,select,textarea,button,link,meta,' +
               '[aria-hidden="true"],[hidden],[role="dialog"],' +
               '[id*="cookie" i],[class*="cookie" i]';
  root.querySelectorAll(drop).forEach(e => e.remove());
  const keep = new Set(['href','src','alt','title','colspan','rowspan']);
  root.querySelectorAll('*').forEach(e => {
    for (const a of [...e.attributes]) if (!keep.has(a.name)) e.removeAttribute(a.name);
    if (e.tagName === 'A' && e.getAttribute('href')) {
      try {
        const u = new URL(e.getAttribute('href'), location.href);
        if (u.host === host && !u.search) e.setAttribute('href', u.pathname);
        else e.setAttribute('href', u.href);
      } catch (_) {}
    }
    if (e.tagName === 'IMG' && e.getAttribute('src')) {
      try { e.setAttribute('src', new URL(e.getAttribute('src'), location.href).href); } catch (_) {}
    }
  });
  // div/span/section zonder betekenis uitpakken, zodat er compacte HTML overblijft
  const unwrap = ['DIV','SPAN','SECTION','ARTICLE','HEADER','FOOTER','NAV','ASIDE','MAIN','FIGURE','PICTURE'];
  for (let pass = 0; pass < 3; pass++) {
    root.querySelectorAll(unwrap.join(',')).forEach(e => {
      if (!e.textContent.trim() && !e.querySelector('img')) { e.remove(); return; }
      const blocky = e.querySelector('p,h1,h2,h3,h4,h5,h6,ul,ol,table,div,section,article,blockquote');
      if (e.tagName === 'SPAN' || blocky) e.replaceWith(...e.childNodes);
      else { const p = document.createElement('p'); p.append(...e.childNodes); e.replaceWith(p); }
    });
  }
  const desc = document.querySelector('meta[name="description"]');
  const h1 = root.querySelector('h1');
  return {
    title: (h1 && h1.textContent.trim()) || document.title || '',
    docTitle: document.title || '',
    description: desc ? desc.content : '',
    body: root.innerHTML.replace(/\n\s*\n+/g, '\n'),
    text: root.textContent.replace(/\s+/g, ' ').trim()
  };
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
async def render_one(context, url, timeout_ms):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        # langzaam naar beneden scrollen zodat lui geladen blokken verschijnen
        await page.evaluate("""async () => {
            for (let y = 0; y < document.body.scrollHeight; y += 800) {
                window.scrollTo(0, y); await new Promise(r => setTimeout(r, 150));
            }}""")
        await page.wait_for_timeout(1000)
        return await page.evaluate(EXTRACT_JS)
    finally:
        await page.close()


async def render_all(urls, state, concurrency, delay, timeout_ms):
    stats = defaultdict(int)
    failed = []
    queue = asyncio.Queue()
    for u in urls:
        queue.put_nowait(u)
    total = len(urls)
    started = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(user_agent=USER_AGENT, locale="nl-NL")
        # afbeeldingen, lettertypen en video niet laden: scheelt veel tijd
        await context.route(re.compile(r".*\.(png|jpe?g|gif|webp|avif|svg|woff2?|ttf|mp4|webm)(\?.*)?$", re.I),
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
                        data = await render_one(context, url, timeout_ms)
                        if len(data["text"]) >= MIN_TEXT:
                            break
                        data = None
                    except Exception as e:
                        print(f"  fout ({attempt + 1}) {url}: {e.__class__.__name__}", flush=True)
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
    ap.add_argument("--mode", choices=["sample", "daily", "full"], default="daily")
    ap.add_argument("--limit", type=int, default=0, help="maximum aantal pagina's (0 = geen)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.0, help="pauze per tab na elke pagina (s)")
    ap.add_argument("--timeout", type=int, default=45, help="time-out per pagina (s)")
    ap.add_argument("--sitemap-file", help="lokale sitemap gebruiken in plaats van ophalen")
    a = ap.parse_args()
    if a.mode == "sample" and not a.limit:
        a.limit = 100

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    sitemap = fetch_sitemap(SITEMAP_URL, a.sitemap_file)
    print(f"Sitemap: {len(sitemap)} pagina's")

    # pagina's die niet meer in de sitemap staan opruimen (niet bij een proefrun)
    removed = 0
    if a.mode != "sample":
        for url in [u for u in state if u not in sitemap]:
            f = out_path(url)
            if f.exists():
                f.unlink()
            del state[url]
            removed += 1

    urls = choose(a.mode, sitemap, state, a.limit, seed=datetime.now().strftime("%Y%m%d"))
    print(f"Modus {a.mode}: {len(urls)} pagina's uitlezen, {removed} verwijderd")

    t0 = time.time()
    stats, failed = asyncio.run(render_all(urls, state, a.concurrency, a.delay, a.timeout * 1000))
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
