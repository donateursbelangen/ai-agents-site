# ai-agents-site

Statische, leesbare kopie van www.donateursbelangen.nl voor AI-agents en crawlers
die geen JavaScript uitvoeren. Gepubliceerd op https://ai-agents.donateursbelangen.nl.

## Hoe het werkt

1. `render.py` leest de sitemap van www.donateursbelangen.nl, opent elke pagina in een
   headless browser en schrijft een schone HTML-versie naar `site/`, met een canonical-link
   naar de originele pagina.
2. `generate_discovery.py` maakt het overzicht (`index.html`), `llms.txt`, de overzichten
   per paginatype, `sitemap.xml` en `robots.txt`.
3. De workflow in `.github/workflows/update-site.yml` doet dit automatisch en publiceert
   naar Cloudflare Pages (project `ai-agents-donateursbelangen`).

## Schema

- Maandag t/m zaterdag 03:00 UTC: nieuwe en gewijzigde pagina's (`daily`).
- Zondag 01:00 UTC: alle pagina's opnieuw (`full`).
- Handmatig via Actions > Leesbare site bijwerken > Run workflow (`sample`, `daily` of `full`).

De homepage van www staat op het subdomein onder `/home`; `/` is het overzicht.

## Geheimen (Settings > Secrets and variables > Actions)

- `CLOUDFLARE_API_TOKEN`: token met recht Cloudflare Pages: Edit
- `CLOUDFLARE_ACCOUNT_ID`: Account ID van de stichting

## Status

`state/last-run.json` toont de laatste run: duur, aantallen en mislukte pagina's.
