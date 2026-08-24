/**
 * Shared helpers for the hentaihaven.xxx proxy.
 *
 * Flow (all server-side, no browser fingerprint needed):
 *   watch page HTML -> player.php iframe (data blob) -> player.php (x-secure-token)
 *   -> decode token (ROT13 + base64, x3) -> en/iv -> POST api.php -> stream URL
 */
const BASE = 'https://hentaihaven.xxx';
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

export function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    'Cache-Control': 'no-store',
  };
}

export function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...corsHeaders() },
  });
}

export function error(message: string, status = 500) {
  return json({ status: false, error: message }, status);
}

export async function fetchHtml(url: string): Promise<string | null> {
  try {
    const res = await fetch(url, {
      headers: {
        'User-Agent': UA,
        Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
      },
      redirect: 'follow',
    });
    if (!res.ok) return null;
    return await res.text();
  } catch {
    return null;
  }
}

function rot13(s: string): string {
  return s.replace(/[a-zA-Z]/g, (c) => {
    const base = c <= 'Z' ? 65 : 97;
    return String.fromCharCode(((c.charCodeAt(0) - base + 13) % 26) + base);
  });
}

function b64d(s: string): string {
  return Buffer.from(s, 'base64').toString('utf-8');
}

/** Decode the x-secure-token meta content ("sha512-..." blob) into its JSON config. */
export function decodeSecureToken(content: string): Record<string, any> | null {
  try {
    let data = content.replace(/^sha512-/, '');
    for (let i = 0; i < 3; i++) {
      data = rot13(data);
      data = b64d(data);
    }
    return JSON.parse(data);
  } catch {
    return null;
  }
}

/** Get the player config (en/iv/uri) for a watch URL. */
export async function getPlayerConfig(watchUrl: string): Promise<{ en: string; iv: string; uri: string } | null> {
  const html = await fetchHtml(watchUrl);
  if (!html) return null;

  const iframe = html.match(/src="([^"]*player\.php\?data=[^"]+)"/);
  if (!iframe) return null;
  const playerUrl = iframe[1].startsWith('http') ? iframe[1] : BASE + iframe[1];

  const playerHtml = await fetchHtml(playerUrl);
  if (!playerHtml) return null;

  const meta = playerHtml.match(/name="x-secure-token" content="([^"]+)"/);
  if (!meta) return null;

  const cfg = decodeSecureToken(meta[1]);
  if (!cfg || !cfg.en || !cfg.iv) return null;

  const uri = cfg.uri.startsWith('http') ? cfg.uri : 'https:' + cfg.uri;
  return { en: cfg.en, iv: cfg.iv, uri };
}

/** Exchange en/iv for the stream URL via api.php. */
export async function getStreamData(
  uri: string,
  en: string,
  iv: string
): Promise<{ src?: string; sources?: { src: string; type?: string; label?: string }[]; title?: string } | null> {
  try {
    const body = new URLSearchParams();
    body.append('action', 'zarat_get_data_player_ajax');
    body.append('a', en);
    body.append('b', iv);

    const res = await fetch(uri.replace(/\/?$/, '/') + 'api.php', {
      method: 'POST',
      headers: {
        'User-Agent': UA,
        'X-Requested-With': 'XMLHttpRequest',
        Origin: BASE,
        Referer: BASE + '/',
      },
      body,
    });
    if (!res.ok) return null;
    const data: any = await res.json();
    if (!data?.status || !data?.data?.sources) return null;
    const sources = data.data.sources;
    const src = Array.isArray(sources) ? sources[0]?.src : null;
    if (!src) return null;
    return { src, sources, title: data.data?.title };
  } catch {
    return null;
  }
}

/** Resolve a free-text query to a hentaihaven watch slug via WordPress search. */
export async function searchSlug(query: string): Promise<{ slug: string; url: string; title: string } | null> {
  const html = await fetchHtml(`${BASE}/?s=${encodeURIComponent(query)}`);
  if (!html) return null;
  const m = html.match(/href="https:\/\/hentaihaven\.xxx\/watch\/([^"/]+)\/"[^>]*title="([^"]+)"/);
  if (!m) return null;
  return { slug: m[1], url: `${BASE}/watch/${m[1]}/`, title: m[2] };
}
