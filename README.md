# hanime-proxy — hentaihaven.xxx streaming proxy for repeaks

Serverless proxy (Vercel Python runtime, Flask + curl_cffi) that turns
hentaihaven.xxx episodes into playable HLS streams for the repeaks 18+ category.

> curl_cffi impersonates Chrome's TLS fingerprint, which is required because
> hentaihaven.xxx sits behind a Cloudflare bot challenge that rejects plain
> Node/curl HTTP clients (Vercel's Node runtime would get "Just a moment...").

## Why this works

hentaihaven.xxx protects its video player with a token exchange, but every step
is plain server-side HTTP (no browser fingerprint required):

1. `GET /watch/<slug>/episode-N/` -> HTML with a `player.php?data=<blob>` iframe
2. `GET player.php?data=...` -> `<meta name="x-secure-token" content="sha512-...">`
3. Decode the token: strip `sha512-`, then 3x (ROT13 + base64-decode) -> JSON
   config containing `en` (encrypted payload) + `iv` + `uri`
4. `POST <uri>api.php` with `action=zarat_get_data_player_ajax&a=<en>&b=<iv>`
   -> `{ status: true, data: { sources: [{ src: "https://.../playlist.m3u8" }] } }`

The resulting m3u8 + segments are served from CDNs that send
`Access-Control-Allow-Origin: *`, so the browser plays them directly with
hls.js — no byte proxying needed.

## Endpoints

| Route | Params | Returns |
| --- | --- | --- |
| `/api/stream` | `slug` (watch slug), `ep` (episode, default 1) | `{ status, src, sources }` |
| `/api/search` | `q` (free-text title) | `{ status, found, slug, title, url }` |
| `/api/catalog` | `tag` (default `hanime`), `page` (default 1) | `{ status, tag, page, totalPages, count, titles[] }` |

All responses include `Access-Control-Allow-Origin: *`.

## Deploy

1. Push this folder to a GitHub repo (or use the `vercel` CLI):
   ```sh
   npm i -g vercel
   vercel        # from this directory
   vercel --prod
   ```
   Vercel auto-detects the Python runtime (`api/index.py` + `requirements.txt`).
2. Note the deployment URL, e.g. `https://hanime-proxy.vercel.app`.
3. In `client/src/pages/V6.tsx` set:
   ```ts
   const HENTAI_PROXY = 'https://hanime-proxy.vercel.app'; // your deployment URL
   ```

## Example

```sh
curl 'https://<your-proxy>/api/stream?slug=deco-x-deco-the-animation&ep=2'
# {"status":true,"src":"https://octopusmanifest.org/07d69b06-.../playlist.m3u8",...}
```

## Notes

- Vercel free tier is fine; each stream request is a few small HTTP calls.
- If a stream call fails, the client falls back to opening the video on
  hentaihaven.xxx / hanime.tv in a new tab.
- The token + stream URL are short-lived (~10 min), so always fetch fresh per
  play — never cache the `src`.
