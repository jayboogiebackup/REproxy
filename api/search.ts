import { corsHeaders, json, error, fetchHtml } from './_lib';

/**
 * GET /api/search?q=<title>
 *
 * Resolves a free-text title to a hentaihaven.xxx watch slug via WordPress search.
 * Used to map the browsing catalog titles to playable slugs.
 */
export default async function handler(req: Request) {
  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: corsHeaders() });

  const url = new URL(req.url);
  const q = (url.searchParams.get('q') || '').trim();
  if (!q) return error('Missing "q" query param', 400);

  const html = await fetchHtml(`https://hentaihaven.xxx/?s=${encodeURIComponent(q)}`);
  if (!html) return error('Search request failed', 502);

  // first watch link with its title attribute
  const m = html.match(/href="https:\/\/hentaihaven\.xxx\/watch\/([^"/]+)\/"[^>]*title="([^"]+)"/);
  if (!m) return json({ status: false, found: false });

  return json({ status: true, found: true, slug: m[1], title: m[2], url: `https://hentaihaven.xxx/watch/${m[1]}/` });
}
