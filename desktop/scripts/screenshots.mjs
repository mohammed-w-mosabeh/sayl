// Driven by ui_check.py. Prints a JSON report on stdout; screenshots go to SAYL_OUT.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE);
const { SAYL_URL: url, SAYL_API: api, SAYL_TOKEN: token, SAYL_OUT: out } = process.env;

const RUNS = [
  { name: "en-light", language: "en", theme: "light", dialog: true, settings: true },
  { name: "ar-light", language: "ar", theme: "light", dialog: true, settings: true },
  { name: "ar-dark", language: "ar", theme: "dark" },
];
const ENGINE = { en: "aria2 (segmented)", ar: "aria2 (مقسّم)" };
const NAME_HEADER = { en: "Name", ar: "الاسم" };
const BAR_QUALITY = "1080p";
const STATS_DONE = { en: "Completed", ar: "المكتملة" };

const browser = await chromium.launch({ executablePath: process.env.CHROME });
const runs = [];
for (const run of RUNS) {
  const problems = [];
  const context = await browser.newContext({ viewport: { width: 1200, height: 720 } });
  await context.addInitScript(
    ([connection, prefs]) => {
      sessionStorage.setItem("sayl.dev-connection", JSON.stringify(connection));
      localStorage.setItem("sayl.prefs", JSON.stringify(prefs));
    },
    [{ baseUrl: api, token }, { language: run.language, theme: run.theme, notifications: true }],
  );
  const page = await context.newPage();
  page.on("console", (message) => message.type() === "error" && problems.push(`console: ${message.text()}`));
  page.on("pageerror", (error) => problems.push(`page: ${error.message}`));
  try {
    await page.goto(url);
    await page.getByText(NAME_HEADER[run.language], { exact: true }).waitFor({ timeout: 15000 });
    await page.getByText("ubuntu-24.04-desktop-amd64.iso").first().click();
    await page.locator("[role=status] .bg-success").waitFor({ timeout: 10000 });
    await page.waitForTimeout(1200);
    const dir = await page.evaluate(() => document.documentElement.dir);
    if (dir !== (run.language === "ar" ? "rtl" : "ltr")) problems.push(`dir is ${dir}`);
    const dark = await page.evaluate(() => document.documentElement.classList.contains("dark"));
    if (dark !== (run.theme === "dark")) problems.push(`dark class is ${dark}`);
    await page.screenshot({ path: `${out}/main-${run.name}.png` });
    if (run.dialog) {
      await page.keyboard.press("Control+n");
      await page.locator("#add-url").fill(url.replace(/:\d+\/$/, ":3297/archive.tar.gz"));
      await page.getByRole("dialog").getByText(ENGINE[run.language]).waitFor({ timeout: 15000 });
      await page.screenshot({ path: `${out}/add-${run.name}.png` });
      await page.keyboard.press("Escape");
      for (const [label, name] of [
        [run.language === "ar" ? "دفعة" : "Batch", "batch"],
        [run.language === "ar" ? "جلب موقع" : "Grab site", "grab"],
      ]) {
        await page.getByRole("button", { name: label, exact: true }).click();
        await page.getByRole("dialog").waitFor();
        await page.screenshot({ path: `${out}/${name}-${run.name}.png` });
        await page.keyboard.press("Escape");
      }
      const stop = run.language === "ar" ? "إيقاف قائمة الانتظار" : "Stop queue";
      const start = run.language === "ar" ? "تشغيل قائمة الانتظار" : "Start queue";
      await page.getByRole("button", { name: stop, exact: true }).click();
      await page.getByRole("status").filter({ hasText: run.language === "ar" ? "متوقفة" : "stopped" }).waitFor();
      await page.screenshot({ path: `${out}/queue-stopped-${run.name}.png` });
      await page.getByRole("button", { name: start, exact: true }).first().click();
    }
    if (run.settings) {
      await page.goto(`${url}settings/`);
      await page.locator("#language").waitFor({ timeout: 15000 });
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${out}/settings-${run.name}.png` });
      await page.locator("#clip-ignore").scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${out}/settings-clipboard-${run.name}.png` });
      await page.locator("#sched-name").scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${out}/settings-schedules-${run.name}.png` });
      await page.locator("#proxy-url").scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${out}/settings-proxy-${run.name}.png` });
      await page.locator("#login-host").scrollIntoViewIfNeeded();
      await page.getByText("files.example.com", { exact: true }).waitFor({ timeout: 10000 });
      await page.screenshot({ path: `${out}/settings-logins-${run.name}.png` });
    }
    await page.goto(`${url}stats/`);
    await page.getByText(STATS_DONE[run.language], { exact: true }).first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${out}/stats-${run.name}.png` });
    const bar = await context.newPage();
    bar.on("pageerror", (error) => problems.push(`bar: ${error.message}`));
    await bar.setViewportSize({ width: 480, height: 64 });
    await bar.goto(`${url}grabber/`);
    await bar.locator("select option", { hasText: BAR_QUALITY }).waitFor({ state: "attached", timeout: 30000 });
    await bar.screenshot({ path: `${out}/bar-${run.name}.png` });
  } catch (error) {
    problems.push(String(error).split("\n")[0]);
  }
  runs.push({ name: run.name, problems });
  await context.close();
}
await browser.close();
process.stdout.write(JSON.stringify({ runs }));
