#!/usr/bin/env python3
"""
Bouwt de vindbaarheidsbestanden voor https://ai-agents.donateursbelangen.nl
op basis van de sitemap van www.donateursbelangen.nl:

  llms.txt                     ingang voor AI-agents (llmstxt.org-formaat)
  index.html                   overzichtspagina met alle kernpagina's
  overzicht/<type>.html        per paginatype een lijst met alle pagina's
  sitemap.xml                  sitemap met de URL's op het subdomein
  robots.txt                   staat alle crawlers toe en wijst naar de sitemap

Gebruik:
  python generate_discovery.py --sitemap state/sitemap.xml --out site --rendered site

De homepage van www staat op het subdomein onder /home, omdat / het overzicht is.

Met --rendered leest het script titels uit de al gerenderde HTML-pagina's
(<h1> of <title>). Zonder die map wordt de titel uit de URL afgeleid.
"""
import argparse
import html
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Instellingen: deze teksten kunnen jullie zelf aanpassen.
# ---------------------------------------------------------------------------
SITE_NAME = "Stichting Donateursbelangen"
WWW = "https://www.donateursbelangen.nl"
AI = "https://ai-agents.donateursbelangen.nl"
SUMMARY = (
    "Stichting Donateursbelangen is een onafhankelijk platform dat opkomt voor "
    "de belangen van donateurs van goede doelen in Nederland. De site bevat "
    "informatie over goede doelen, opzeggen van donaties, dossiers, signalen, "
    "nieuws en onderzoek."
)
ABOUT_THIS_COPY = (
    "Dit subdomein bevat een statische, leesbare kopie van "
    "www.donateursbelangen.nl voor AI-agents en crawlers die geen JavaScript "
    "uitvoeren. Elke pagina heeft hetzelfde pad als op www en verwijst met een "
    "canonical-link naar de originele pagina. Citeer en verwijs bij voorkeur "
    "naar de www-URL."
)

# Paginatypen met een detailpagina per record: (titel, omschrijving)
TYPES = {
    "goede-doel": ("Goede doelen",
                   "Een profielpagina per goed doel."),
    "nonprofit": ("Nonprofitorganisaties",
                  "Een pagina per nonprofitorganisatie."),
    "opzeggen-goed-doel-per-email": ("Opzeggen per e-mail",
                                     "Per goed doel hulp bij het opzeggen van een donatie per e-mail."),
    "opzeggen-goed-doel-per-brief": ("Opzeggen per brief",
                                     "Per goed doel hulp bij het opzeggen van een donatie per brief."),
    "communicatievoorkeuren": ("Communicatievoorkeuren",
                               "Per organisatie informatie over het doorgeven van communicatievoorkeuren."),
    "nieuws-details": ("Nieuws", "Nieuwsberichten."),
    "blogitem": ("Blog", "Blogartikelen."),
    "signaal": ("Signalen", "Signalen over praktijken in de goededoelensector."),
    "stakeholder": ("Stakeholders", "Organisaties en partijen in de goededoelensector."),
}

# Losse pagina's die als eerste ingang worden uitgelicht (in deze volgorde).
FEATURED = [
    "", "over-ons", "welkom", "goede-doelen-lijst", "anbi-portaal", "dossiers",
    "kennisbank", "standpunten", "signalen-donateursbelangen", "laatste-nieuws",
    "opzeggen", "opzegservice", "onderzoeksrapporten", "woordenlijst", "faq",
]

# Technische of lege sjabloonpagina's die niet in de overzichten horen.
EXCLUDE = {
    "login", "forgot-password", "reset-password", "link-expired", "404",
    "404-old", "401", "mollieredirect", "mailjet-test-form", "postfilter",
    "zoeken", "onderzoeksrapport-detail", "podcasts-details",
    "opzegservice-details", "goededoelenweek-plaats-detailinformatie",
    "in-de-schijnwerpers-organisatie", "anbi-organisatie",
}

# Afwijkende titels voor losse pagina's waar de URL geen goede titel geeft.
TITLE_OVERRIDES = {
    "": "Home",
    "anbi": "ANBI",
    "anbi-portaal": "ANBI-portaal",
    "cbf": "CBF",
    "cbf-register": "CBF-register",
    "faq": "Veelgestelde vragen",
    "gdn": "GDN",
    "jaaroverzicht-2025": "Jaaroverzicht 2025",
    "whatsapp": "WhatsApp",
    "qr-code-doneren": "Doneren met een QR-code",
    "doneren-via-iban": "Doneren via IBAN",
    "tv-spots-maart-2024": "Tv-spots maart 2024",
    "gemeenten-apv": "Gemeenten en de APV",
    "privacy-versus-kyc": "Privacy versus KYC",
}


# ---------------------------------------------------------------------------
def read_sitemap(path):
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.parse(path).getroot()
    out = []
    for url in root.findall("s:url", ns):
        loc = url.findtext("s:loc", default="", namespaces=ns).strip()
        lastmod = url.findtext("s:lastmod", default="", namespaces=ns).strip()
        if loc:
            out.append((loc, lastmod))
    return out


def slug_title(slug):
    if slug in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[slug]
    text = re.sub(r"[-_]+", " ", slug).strip()
    return text[:1].upper() + text[1:] if text else slug


def rendered_file(rendered_dir, path):
    if not rendered_dir:
        return None
    rel = path.strip("/") or "home"
    for cand in (rendered_dir / f"{rel}.html", rendered_dir / rel / "index.html"):
        if cand.exists():
            return cand
    return None


def title_from_html(file):
    text = file.read_text(encoding="utf-8", errors="ignore")
    for pattern in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        m = re.search(pattern, text, re.S | re.I)
        if m:
            t = html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip()
            if t:
                return t
    return None


def classify(entries, rendered_dir):
    detail = defaultdict(list)
    single = {}
    skipped = []
    for loc, lastmod in entries:
        p = urlparse(loc)
        if p.query:                      # bv. nieuws-details?recordId=...
            skipped.append(loc)
            continue
        parts = p.path.strip("/").split("/") if p.path.strip("/") else []
        if len(parts) >= 2 and parts[0] in TYPES:
            f = rendered_file(rendered_dir, p.path)
            title = (f and title_from_html(f)) or slug_title(parts[1])
            detail[parts[0]].append((title, p.path, lastmod))
        elif len(parts) <= 1:
            slug = parts[0] if parts else ""
            if slug in EXCLUDE:
                skipped.append(loc)
                continue
            f = rendered_file(rendered_dir, "/" + (slug or "home"))
            title = (f and title_from_html(f)) or slug_title(slug)
            single[slug] = (title, "/" + (slug or "home"), lastmod)
        else:
            skipped.append(loc)
    for k in detail:
        detail[k].sort(key=lambda x: x[0].lower())
    return detail, single, skipped


# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#fbfbf8;--fg:#1d2321;--muted:#5b6662;--line:#d9ddd6;--link:#1f5f8b;
color-scheme:light;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);
padding-bottom:env(safe-area-inset-bottom,0px)}
@media (prefers-color-scheme:dark){:root{--bg:#161a19;--fg:#e6e9e4;--muted:#9aa49f;
--line:#2e3532;--link:#8cc4ea;color-scheme:dark}}
*,*::before,*::after{box-sizing:inherit}
body{margin:0;background:var(--bg);color:var(--fg);
font:17px/1.6 Charter,"Bitstream Charter","Sitka Text",Cambria,Georgia,serif}
main{max-width:44rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}
h1{font-size:1.9rem;line-height:1.2;margin:0 0 .75rem}
h2{font-size:1.25rem;margin:2.5rem 0 .5rem;padding-top:1rem;border-top:1px solid var(--line)}
p{margin:.5rem 0}.note{color:var(--muted);font-size:.95rem}
a{color:var(--link)}a:focus-visible{outline:2px solid var(--link);outline-offset:2px}
ul{padding-left:1.2rem}li{margin:.2rem 0}
dl{margin:0}dt{font-weight:600;margin-top:1rem}dd{margin:.1rem 0 0}
.count{color:var(--muted)}
"""


def page(title, body, canonical):
    return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)}</title>
<link rel="canonical" href="{canonical}">
<style>{CSS}</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def li(title, path, base=""):
    return f'<li><a href="{base}{path}">{html.escape(title)}</a></li>'


def build_index(detail, single):
    featured = [single[s] for s in FEATURED if s in single]
    rest = sorted((v for k, v in single.items() if k not in FEATURED),
                  key=lambda x: x[0].lower())
    body = [f"<h1>{html.escape(SITE_NAME)}</h1>",
            f"<p>{html.escape(SUMMARY)}</p>",
            f'<p class="note">{html.escape(ABOUT_THIS_COPY)} '
            f'Een beknopte index voor taalmodellen staat in <a href="/llms.txt">llms.txt</a>.</p>',
            "<h2>Belangrijkste pagina's</h2><ul>",
            *[li(t, p) for t, p, _ in featured], "</ul>",
            "<h2>Overzichten per paginatype</h2><dl>"]
    for key, (name, desc) in TYPES.items():
        n = len(detail.get(key, []))
        if n:
            body.append(f'<dt><a href="/overzicht/{key}">{html.escape(name)}</a> '
                        f'<span class="count">({n})</span></dt><dd>{html.escape(desc)}</dd>')
    body += ["</dl>", "<h2>Overige pagina's</h2><ul>",
             *[li(t, p) for t, p, _ in rest], "</ul>",
             f'<p class="note">Bijgewerkt op {date.today().isoformat()}.</p>']
    return page(SITE_NAME + " – overzicht voor AI-agents", "\n".join(body), WWW + "/")


def build_type_page(key, items):
    name, desc = TYPES[key]
    body = [f'<p class="note"><a href="/">Terug naar het overzicht</a></p>',
            f"<h1>{html.escape(name)}</h1>",
            f"<p>{html.escape(desc)} {len(items)} pagina's, alfabetisch.</p>",
            "<ul>", *[li(t, p) for t, p, _ in items], "</ul>"]
    return page(f"{name} – {SITE_NAME}", "\n".join(body), f"{AI}/overzicht/{key}")


def build_llms(detail, single):
    featured = [single[s] for s in FEATURED if s in single]
    rest = sorted((v for k, v in single.items() if k not in FEATURED),
                  key=lambda x: x[0].lower())
    lines = [f"# {SITE_NAME}", "", f"> {SUMMARY}", "", ABOUT_THIS_COPY, "",
             "## Belangrijkste pagina's", ""]
    lines += [f"- [{t}]({AI}{p})" for t, p, _ in featured]
    lines += ["", "## Overzichten per paginatype", ""]
    for key, (name, desc) in TYPES.items():
        n = len(detail.get(key, []))
        if n:
            lines.append(f"- [{name}]({AI}/overzicht/{key}): {desc} {n} pagina's.")
    lines += ["", "## Optional", ""]
    lines += [f"- [{t}]({AI}{p})" for t, p, _ in rest]
    return "\n".join(lines) + "\n"


def build_sitemap(detail, single):
    urls = [(AI + "/", "")] + [(f"{AI}/overzicht/{k}", "") for k in TYPES if detail.get(k)]
    urls += [(AI + p, lm) for _, p, lm in single.values()]
    for items in detail.values():
        urls += [(AI + p, lm) for _, p, lm in items]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u, lm in urls:
        out.append(f"<url><loc>{html.escape(u)}</loc>"
                   + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n", len(urls)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sitemap", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rendered", help="map met gerenderde pagina's (optioneel)")
    a = ap.parse_args()

    out = Path(a.out)
    (out / "overzicht").mkdir(parents=True, exist_ok=True)
    rendered = Path(a.rendered) if a.rendered else None

    detail, single, skipped = classify(read_sitemap(a.sitemap), rendered)

    (out / "index.html").write_text(build_index(detail, single), encoding="utf-8")
    for key, items in detail.items():
        (out / "overzicht" / f"{key}.html").write_text(build_type_page(key, items), encoding="utf-8")
    (out / "llms.txt").write_text(build_llms(detail, single), encoding="utf-8")
    (out / "404.html").write_text(page(
        "Pagina niet gevonden – " + SITE_NAME,
        '<h1>Deze pagina is (nog) niet beschikbaar</h1>'
        '<p>Van deze pagina is nog geen leesbare kopie gemaakt. Bekijk het origineel op '
        f'<a href="{WWW}">www.donateursbelangen.nl</a> of ga naar het <a href="/">overzicht</a>.</p>',
        WWW + "/"), encoding="utf-8")
    sm, n = build_sitemap(detail, single)
    (out / "sitemap.xml").write_text(sm, encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {AI}/sitemap.xml\n",
                                    encoding="utf-8")

    print(f"Losse pagina's: {len(single)}")
    for k, v in detail.items():
        print(f"  {k}: {len(v)}")
    print(f"Overgeslagen: {len(skipped)}")
    print(f"URL's in sitemap subdomein: {n}")


if __name__ == "__main__":
    main()
