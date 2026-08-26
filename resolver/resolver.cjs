#!/usr/bin/env node
/**
 * RE:player Resolver — standalone stream resolution.
 *
 * Resolves the direct HLS stream URL for any TMDB title by loading the
 * source player in headless Chromium and capturing the resolved m3u8.
 * URLs are deterministic per title → cache aggressively (24h).
 *
 * Usage:
 *   node resolver.js movie 1084242
 *   node resolver.js tv 387 1 1
 *
 * Output (JSON): { tmdb, type, season, episode, url, quality, cached, ms }
 */
const { chromium } = require('playwright');

const CACHE_TTL = 24 * 60 * 60 * 1000; // 24h
const cache = new Map(); // in-memory; deploy with a KV/Redis for persistence

async function resolvePage(tmdb, type, season, episode) {
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || '/usr/bin/chromium',
    headless: true,
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  });
  try {
    const ctx = await browser.newContext({
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
      viewport: { width: 1280, height: 720 },
      locale: 'en-US',
    });
    const page = await ctx.newPage();

    let streamUrl = null;
    page.on('request', (req) => {
      const u = req.url();
      if (!streamUrl && /nebula\.bright67\.online\/hls\/[^/]+\/master\.m3u8/.test(u)) {
        streamUrl = u;
      }
    });

    const embedUrl = type === 'tv'
      ? `https://cinesrc.st/embed/tv/${tmdb}?s=${season}&e=${episode}`
      : `https://cinesrc.st/embed/movie/${tmdb}`;

    await page.goto(embedUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Wait for the video element (challenge solves itself)
    try {
      await page.waitForSelector('video', { timeout: 25000 });
    } catch { /* video may not appear; rely on request capture */ }

    // Give the player a moment to fire the m3u8 request
    const deadline = Date.now() + 15000;
    while (!streamUrl && Date.now() < deadline) {
      await page.waitForTimeout(500);
    }

    return streamUrl;
  } finally {
    await browser.close();
  }
}

async function main() {
  const [type, tmdb, s, e] = process.argv.slice(2);
  if (!type || !tmdb) {
    console.log(JSON.stringify({ error: 'usage: resolver.js <movie|tv> <tmdb> [season] [episode]' }));
    process.exit(1);
  }
  const season = s ? Number(s) : undefined;
  const episode = e ? Number(e) : undefined;
  const key = `${type}:${tmdb}:${season ?? ''}:${episode ?? ''}`;

  // Check cache
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < CACHE_TTL) {
    console.log(JSON.stringify({ ...hit.data, cached: true, ms: 0 }));
    return;
  }

  const t0 = Date.now();
  const url = await resolvePage(tmdb, type, season, episode);
  const ms = Date.now() - t0;

  if (!url) {
    console.log(JSON.stringify({ error: 'resolve_failed', tmdb, type, season, episode, ms }));
    process.exit(2);
  }

  const data = { tmdb, type, season: season ?? null, episode: episode ?? null, url, quality: '1080p/720p/480p', cached: false, ms };
  cache.set(key, { t: Date.now(), data });
  console.log(JSON.stringify(data));
}

main().catch((e) => {
  console.log(JSON.stringify({ error: e.message }));
  process.exit(1);
});
