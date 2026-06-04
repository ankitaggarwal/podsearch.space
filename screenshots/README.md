# Screenshots & promo assets

Visuals for the README and the LinkedIn launch kit ([`../linkedin/`](../linkedin)).
Every card uses the site's logo (the eyes-headphones mark) for a consistent set.

| File | What it is |
|------|-----------|
| `podcast-search-demo.gif` | Live search end-to-end — ask a question, a cited answer renders. **README hero.** |
| `podcast-search-app.png` | The live homepage — the search-first product shot. |
| `podcast-search-architecture.png` | One-page architecture ("one engine, two surfaces"). Also embedded in the README. |
| `podcast-search-deck.png` | "The story, in 16 slides" — the LinkedIn "Presentation" media item. |
| `podcast-search-deck-cover.png` | The deck cover / title card. |
| `podcast-search-site.png` | "Live · try it" — the site-link thumbnail. |
| `podcast-search-github.png` | The repo card — the GitHub-link thumbnail. |

The designed cards (`architecture`, `github`, `site`, `deck`, `deck-cover`) are
rendered from matching HTML in [`../linkedin/`](../linkedin); `app` and `demo.gif`
are captured from the live site. To regenerate the cards:

```bash
cd ../podsearch-deck && node render-cards.js   # → ../screenshots/podcast-search-*.png
```
