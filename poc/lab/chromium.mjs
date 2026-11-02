// Drives Playwright's Chromium through the proxy in a throwaway profile.
// Usage: PLAYWRIGHT_CORE=<dir> CHROME=<bin> node chromium.mjs <proxy-port|direct> <steps.json>
// "direct" skips the proxy: a site that fails there too is not a capture failure.
// Each step: {"goto": url} | {"click": selector} | {"wait": ms} | {"name": label}
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE);
const [proxyPort, stepsPath] = process.argv.slice(2);
const steps = JSON.parse(readFileSync(stepsPath, 'utf8'));

const browser = await chromium.launch({
  executablePath: process.env.CHROME,
  headless: true,
  ...(proxyPort === 'direct'
    ? {}
    : {
        proxy: { server: `http://127.0.0.1:${proxyPort}`, bypass: '<-loopback>' },
        args: ['--proxy-bypass-list=<-loopback>'],
      }),
});
const context = await browser.newContext({ ignoreHTTPSErrors: true, acceptDownloads: true });
const page = await context.newPage();
const failures = [];
const downloads = [];
page.on('download', (d) => downloads.push(d.suggestedFilename()));
page.on('requestfailed', (r) => failures.push(`${r.failure()?.errorText} ${r.url().split('?')[0]}`));

for (const step of steps) {
  const started = Date.now();
  let note = '';
  try {
    if (step.goto) {
      const response = await page.goto(step.goto, { timeout: 60000, waitUntil: 'domcontentloaded' });
      note = `status=${response?.status()} title=${JSON.stringify(await page.title())}`;
    } else if (step.click) {
      await page.click(step.click, { timeout: 20000, noWaitAfter: true });
    } else if (step.script) {
      await page.evaluate(step.script);
    } else if (step.quit) {
      await browser.close();
      console.log(JSON.stringify({ step, note: 'browser closed' }));
      process.exit(0);
    } else if (step.wait) {
      await page.waitForTimeout(step.wait);
    }
  } catch (error) {
    note = `ERROR ${String(error.message).split('\n')[0]}`;
  }
  console.log(JSON.stringify({ step: step.name ?? step, ms: Date.now() - started, note, url: page.url().split('?')[0] }));
}
console.log(JSON.stringify({ browserDownloads: downloads, requestFailures: failures.slice(0, 10) }));
await browser.close();
