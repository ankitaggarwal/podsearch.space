# Screenshots & promo assets

Visuals for the README and the LinkedIn launch kit ([`../linkedin/`](../linkedin)).
Capture site shots at 2× (retina); crop out anything that reveals production
hostnames or IPs; keep GIFs small enough that LinkedIn accepts them (~8 MB).

| File | What it is |
|------|-----------|
| `podcast-search-demo.gif` | Live search end-to-end — ask a question, a cited answer renders. **Used as the README hero.** |
| `podcast-search-architecture.png` | One-page architecture ("one engine, two surfaces"). Embedded in the README; rendered from `../podsearch-deck/architecture.html`. |
| `podcast-search-deck-cover.png` | The slide-deck cover, for the LinkedIn "Presentation" media item. |
| `01-homepage.png` | The search-first homepage. |
| `02-pipeline.png` | The live indexing / pipeline page. |
| `03-library.png` | The Shelf / library view. |
| `04-search.png` | A search with its answer + sources expanded. |
| `05-github.png` | The GitHub repo page (link thumbnail). |

`capture-live.js` is a small helper for grabbing fresh site shots after a deploy.

Regenerate the architecture diagram:

```bash
cd ../podsearch-deck && node render-architecture.js
```
