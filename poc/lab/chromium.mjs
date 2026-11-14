// Drives Playwright's Chromium through the proxy in a throwaway profile.
// Usage: PLAYWRIGHT_CORE=<dir> [CHROME=<bin>] node chromium.mjs <proxy-port|direct> <steps.json>
// "direct" skips the proxy: a site that fails there too is not a capture failure.
// Each step: {"goto": url} | {"click": selector, "answered"?: path} | {"wait": ms} | {"name": label}
// "answered" waits until the proxy has answered that path: the click's request may still be
// waiting on a TLS retry, and navigating away first would cancel it before it is sent.
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
      // Chromium answers a Basic challenge from URL credentials only sometimes, and otherwise
      // waits on a prompt headless cannot show. Send them up front, as a logged-in browser does.
      const target = new URL(step.goto);
      if (target.username) {
        const user = `${decodeURIComponent(target.username)}:${decodeURIComponent(target.password)}`;
        await page.setExtraHTTPHeaders({ authorization: `Basic ${Buffer.from(user).toString('base64')}` });
        target.username = '';
        target.password = '';
      }
      const response = await page.goto(target.href, { timeout: 60000, waitUntil: 'domcontentloaded' });
      note = `status=${response?.status()} title=${JSON.stringify(await page.title())}`;
    } else if (step.click) {
      const answered = step.answered
        ? page.waitForResponse((r) => new URL(r.url()).pathname === step.answered, { timeout: 20000 })
        : null;
      await page.click(step.click, { timeout: 20000, noWaitAfter: true });
      await answered;
    } else if (step.script) {
      await page.evaluate(step.script);
    } else if (step.quit) {
      await browser.close();
      console.log(JSON.stringify({ step, note: 'browser closed' }));
      console.log(JSON.stringify({ browserDownloads: downloads }));
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
