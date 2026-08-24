import { corsHeaders, json, error, fetchHtml } from './_lib';

/**
 * GET /api/catalog?tag=hanime&page=1
 *
 * Parses a hentaihaven.xxx tag page into a title list.
 * The default tag "hanime" mirrors hanime.tv content.
 * Other tags work too (creampie, uncensored, ahegao, ...).
 */
export default async function handler(req: Request) {
  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: corsHeaders() });

  const url = new URL(req.url);
  const tag = (url.searchParams.get('tag') || 'hanime').trim().toLowerCase();
  const page = Math.max(1, parseInt(url.searchParams.get('page') || '1', 10) || 1);

  const pageUrl =
    page === 1
      ? `https://hentaihaven.xxx/tag/${encodeURIComponent(tag)}/`
      : `https://hentaihaven.xxx/tag/${encodeURIComponent(tag)}/page/${page}/`;

  const html = await fetchHtml(pageUrl);
  if (!html) return error('Failed to fetch tag page', 502);

  const titles: any[] = [];
  const seen = new Set<string>();

  // Each series card: <div id="manga-item-<id>" ...><a href=".../watch/<slug>/" title="Title"><img src="poster"></a> ... episode links
  const cardRe = /id="manga-item-(\d+)"[\s\S]*?href="https:\/\/hentaihaven\.xxx\/watch\/([^"/]+)\/"[^>]*title="([^"]+)"[\s\S]*?<img[^>]*src="([^"]+)"/g;
  let m: RegExpExecArray | null;
  while ((m = cardRe.exec(html))) {
    const [, id, slug, title, poster] = m;
    if (seen.has(slug)) continue;
    seen.add(slug);

    // count episodes in the same card block (episode-N links)
    const cardStart = m.index;
    const cardEnd = html.indexOf('</div>', cardStart) + 6;
    const cardBlock = html.slice(cardStart, cardStart + 6000);
    const eps = new Set<string>();
    const epRe = /watch\/[^"/]+\/episode-(\d+)/g;
    let em: RegExpExecArray | null;
    while ((em = epRe.exec(cardBlock))) eps.add(em[1]);

    titles.push({
      id: Number(id),
      title: title.trim(),
      slug,
      poster,
      url: `https://hentaihaven.xxx/watch/${slug}/`,
      episodes: eps.size,
      episodeNumbers: Array.from(eps).map(Number).sort((a, b) => a - b),
    });
  }

  if (titles.length === 0) return json({ status: false, error: 'No entries found (tag may not exist)' }, 404);

  // pagination info
  const lastPage = html.match(/page\/(\d+)\/"\s*class="[^"]*last[^"]*"/) || html.match(/page\/(\d+)\/"[^>]*>\s*»/);
  const totalPages = lastPage ? parseInt(lastPage[1], 10) : page;

  return json({ status: true, tag, page, totalPages, count: titles.length, titles });
}
