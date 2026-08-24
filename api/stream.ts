import { corsHeaders, json, error, fetchHtml } from './_lib';

/**
 * GET /api/stream?slug=<watch-slug>&ep=<episode-number>
 *
 * Returns the HLS stream URL for a hentaihaven.xxx episode.
 * Example: /api/stream?slug=deco-x-deco-the-animation&ep=2
 */
export default async function handler(req: Request) {
  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: corsHeaders() });

  const url = new URL(req.url);
  const slug = (url.searchParams.get('slug') || '').trim();
  const ep = (url.searchParams.get('ep') || '1').trim();

  if (!slug) return error('Missing "slug" query param', 400);

  const watchUrl = `https://hentaihaven.xxx/watch/${encodeURIComponent(slug)}/${ep && ep !== '1' ? `episode-${ep}/` : ''}`;

  const cfg = await getConfig(watchUrl, slug);
  if (!cfg) return error('Could not resolve player config for ' + slug, 404);

  const stream = await getStreamData(cfg.uri, cfg.en, cfg.iv);
  if (!stream?.src) return error('Could not fetch stream URL', 502);

  return json({ status: true, src: stream.src, sources: stream.sources, slug, episode: ep });
}

import { getPlayerConfig, getStreamData } from './_lib';

async function getConfig(watchUrl: string, slug: string) {
  let cfg = await getPlayerConfig(watchUrl);
  if (cfg) return cfg;
  // fallback: try without episode suffix
  return getPlayerConfig(`https://hentaihaven.xxx/watch/${encodeURIComponent(slug)}/`);
}
