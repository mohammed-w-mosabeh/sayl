export type Days = "every" | "mon_fri" | "sun_thu" | "custom";

const WEEKDAYS: Record<Exclude<Days, "custom">, string> = { every: "*", mon_fri: "1-5", sun_thu: "0-4" };

/** A daily cron at HH:MM on the chosen days; crontab counts Sunday as 0. */
export function cronFor(time: string, days: Days, custom = ""): string {
  if (days === "custom") return custom.trim();
  const [hour, minute] = time.split(":").map((part) => Number.parseInt(part, 10));
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) return "";
  return `${minute} ${hour} * * ${WEEKDAYS[days]}`;
}

/** The time and days back from a cron made by cronFor, or custom for anything else. */
export function parseCron(cron: string): { time: string; days: Days } {
  const match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5|0-4)$/.exec(cron.trim());
  if (!match) return { time: "", days: "custom" };
  const [, minute, hour, weekday] = match as unknown as [string, string, string, string];
  const days = (Object.keys(WEEKDAYS) as (keyof typeof WEEKDAYS)[]).find((key) => WEEKDAYS[key] === weekday);
  return { time: `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`, days: days ?? "custom" };
}
