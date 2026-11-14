// Plays an HLS stream with hls.js in headless Chromium and reports stalls as JSON.
// Usage: PLAYWRIGHT_CORE=<dir> [CHROME=<bin>] node hls_play.mjs <proxy-port|direct> <page-url> <seconds>
// A stall is a `waiting` event after playback started, until the next `playing`.
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE);
const [proxyPort, url, seconds] = process.argv.slice(2);

const browser = await chromium.launch({
  executablePath: process.env.CHROME,
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required',
    ...(proxyPort === 'direct' ? [] : ['--proxy-bypass-list=<-loopback>'])],
  ...(proxyPort === 'direct' ? {} : { proxy: { server: `http://127.0.0.1:${proxyPort}`, bypass: '<-loopback>' } }),
});
const context = await browser.newContext({ ignoreHTTPSErrors: true });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));
await page.goto(url, { waitUntil: 'domcontentloaded' });
const limit = (Number(seconds) + 120) * 1000;
await page.waitForFunction(() => window.report && window.report.done, null, { timeout: limit, polling: 1000 });
const report = await page.evaluate(() => window.report);
await browser.close();
console.log(JSON.stringify({ ...report, errors }));
