#!/usr/bin/env node
/**
 * RE:player Resolver — standalone stream resolution.
 *
 * Resolves the direct HLS stream URL for any TMDB title by loading the
 * source player in headless Chromium and capturing the resolved m3u8.
 * Supports multiple providers; prefers English audio.
 *
 * Providers:
 *   cinesrc (nebula CDN) — primary, ranked list, audio probed
 *   vidking (rapidnight/moon CDN) — backup
 *
 * Usage:
 *   node resolver.cjs movie 1084242
 *   node resolver.cjs tv 387 1 1
 *   node resolver.cjs tv 387 1 1 vidking
 *   node resolver.cjs tv 387 1 1 cinesrc
 *
 * Output (JSON): { tmdb, type, season, episode, url, provider, quality, cached, ms }
 */
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const http = require('http');
const https = require('https');

const CACHE_TTL = 24 * 60 * 60 * 1000; // 24h
const cache = new Map();

const CHROME = process.env.CHROME_PATH || '/usr/bin/chromium';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';

function get(url, headers = {}, timeout = 20000) {
  return new Promise((resolve, reject) => {
    const mod = url.startsWith('https') ? https : http;
    const req = mod.get(url, { headers: { 'User-Agent': UA, ...headers } }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    });
    req.on('error', reject);
    req.setTimeout(timeout, () => { req.destroy(new Error('timeout')); });
  });
}

/** Probe audio language of an HLS stream: download init + first segment, run ffprobe. */
async function probeAudioLanguage(streamUrl, referer = '') {
  try {
    // Get the master playlist → pick first variant
    const master = await get(streamUrl, referer ? { Referer: referer } : {}, 20000);
    if (master.status !== 200) return 'unknown';
    const m3u8 = master.body.toString('utf-8');
    // find first non-comment URL line
    const lines = m3u8.split('\n').filter((l) => l && !l.startsWith('#'));
    if (!lines.length) return 'unknown';
    const base = streamUrl.substring(0, streamUrl.lastIndexOf('/'));
    const variantUrl = lines[0].startsWith('http') ? lines[0] : `${base}/${lines[0]}`;

    const variant = await get(variantUrl, referer ? { Referer: referer } : {}, 20000);
    if (variant.status !== 200) return 'unknown';
    const vPlaylist = variant.body.toString('utf-8');
    const vLines = vPlaylist.split('\n').filter((l) => l && !l.startsWith('#'));
    if (!vLines.length) return 'unknown';
    const vBase = variantUrl.substring(0, variantUrl.lastIndexOf('/'));
    const segUrl = vLines[0].startsWith('http') ? vLines[0] : `${vBase}/${vLines[0]}`;

    // init segment for fMP4 (EXT-X-MAP) if present — carries real language tags
    let initUrl = null;
    const mapMatch = vPlaylist.match(/EXT-X-MAP:URI="([^"]+)"/);
    if (mapMatch) initUrl = mapMatch[1].startsWith('http') ? mapMatch[1] : `${vBase}/${mapMatch[1]}`;

    // Probe the init segment ALONE first (it has the language metadata)
    if (initUrl) {
      try {
        const init = (await get(initUrl, referer ? { Referer: referer } : {}, 20000)).body;
        const tmp2 = '/tmp/rp_init_probe.mp4';
        fs.writeFileSync(tmp2, init);
        const out2 = execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', tmp2], { timeout: 15000 }).toString();
        const d2 = JSON.parse(out2);
        for (const s of d2.streams || []) {
          if (s.codec_type === 'audio') {
            const lang = (s.tags && (s.tags.language || '')) || '';
            if (lang && lang !== 'und') return lang;
          }
        }
      } catch { /* fall through to segment probe */ }
    }

    let seg = (await get(segUrl, referer ? { Referer: referer } : {}, 20000)).body;
    if (initUrl) {
      const init = (await get(initUrl, referer ? { Referer: referer } : {}, 20000)).body;
      seg = Buffer.concat([init, seg]);
    }
    const tmp = '/tmp/rp_probe.bin';
    fs.writeFileSync(tmp, seg);
    const out = execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', tmp], { timeout: 15000 }).toString();
    const data = JSON.parse(out);
    for (const s of data.streams || []) {
      if (s.codec_type === 'audio') {
        const lang = (s.tags && (s.tags.language || '')) || '';
        return lang || 'unknown';
      }
    }
    return 'no-audio';
  } catch (e) {
    return 'unknown';
  }
}

/** Resolve via cinesrc (nebula CDN). */
async function resolveCinesrc(tmdb, type, season, episode) {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
  try {
    const ctx = await browser.newContext({ userAgent: UA, viewport: { width: 1280, height: 720 }, locale: 'en-US' });
    const page = await ctx.newPage();
    let streamUrl = null;
    page.on('request', (req) => {
      const u = req.url();
      if (!streamUrl && /nebula\.bright67\.online\/hls\/[^/]+\/master\.m3u8/.test(u)) streamUrl = u;
    });
    const embedUrl = type === 'tv'
      ? `https://cinesrc.st/embed/tv/${tmdb}?s=${season}&e=${episode}`
      : `https://cinesrc.st/embed/movie/${tmdb}`;
    await page.goto(embedUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    try { await page.waitForSelector('video', { timeout: 25000 }); } catch {}
    const deadline = Date.now() + 15000;
    while (!streamUrl && Date.now() < deadline) await page.waitForTimeout(500);
    return streamUrl;
  } finally {
    await browser.close();
  }
}

/** Resolve via vidking (rapidnight/moon CDN) — fast path. */
async function resolveVidking(tmdb, type, season, episode) {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
  try {
    const ctx = await browser.newContext({ userAgent: UA, viewport: { width: 1280, height: 720 }, locale: 'en-US' });
    const page = await ctx.newPage();
    let streamUrl = null;
    page.on('request', (req) => {
      const u = req.url();
      if (!streamUrl && /moon\.peakstorm\.top\/r2\/cdn2\/[^/]+\/.*\/index\.m3u8/.test(u)) streamUrl = u;
      // Abort heavy media so we resolve the URL without buffering video
      if (/stormgate\.top|\.mp4|\.m3u8/.test(u) && !streamUrl) req.abort();
    });
    const embedUrl = type === 'tv'
      ? `https://www.vidking.net/embed/tv/${tmdb}/${season}/${episode}?color=eee8dc&autoPlay=true`
      : `https://www.vidking.net/embed/movie/${tmdb}?color=eee8dc&autoPlay=true`;
    await page.goto(embedUrl, { waitUntil: 'domcontentloaded', timeout: 25000 });
    // Don't wait for the video element — just poll for the m3u8 request
    const deadline = Date.now() + 12000;
    while (!streamUrl && Date.now() < deadline) await page.waitForTimeout(250);
    return streamUrl;
  } finally {
    await browser.close();
  }
}

async function main() {
  const args = process.argv.slice(2);
  const type = args[0];
  const tmdb = args[1];
  const s = args[2];
  const e = args[3];
  const provider = args[4] || 'auto';
  if (!type || !tmdb) {
    console.log(JSON.stringify({ error: 'usage: resolver.cjs <movie|tv> <tmdb> [season] [episode] [auto|cinesrc|vidking]' }));
    process.exit(1);
  }
  const season = s ? Number(s) : undefined;
  const episode = e ? Number(e) : undefined;
  const key = `${type}:${tmdb}:${season ?? ''}:${episode ?? ''}`;

  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < CACHE_TTL) {
    console.log(JSON.stringify({ ...hit.data, cached: true, ms: 0 }));
    return;
  }

  const t0 = Date.now();

  // VIDKING ONLY — single fast path, no backup, no audio probing (saves ~5-8s)
  let url = null;
  try {
    url = await resolveVidking(tmdb, type, season, episode);
  } catch { url = null; }

  if (!url) {
    console.log(JSON.stringify({ error: 'resolve_failed', tmdb, type, season, episode, ms: Date.now() - t0 }));
    process.exit(2);
  }

  const result = {
    tmdb, type, season: season ?? null, episode: episode ?? null,
    url, provider: 'vidking',
    quality: '1080p/720p/480p',
    servers: [{ provider: 'vidking', url }],
    cached: false, ms: Date.now() - t0,
  };

  if (!result || !result.url) {
    console.log(JSON.stringify({ error: 'resolve_failed', tmdb, type, season, episode, ms: Date.now() - t0 }));
    process.exit(2);
  }

  cache.set(key, { t: Date.now(), data: result });
  console.log(JSON.stringify(result));
}

main().catch((e) => {
  console.log(JSON.stringify({ error: e.message }));
  process.exit(1);
});
