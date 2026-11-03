import { cn } from "@/lib/utils";

/** Fills from the start edge, so it runs right to left in Arabic. */
export function Progress({
  value,
  tone = "primary",
  className,
}: {
  value: number | null;
  tone?: "primary" | "success" | "warning" | "danger" | "muted";
  className?: string;
}) {
  const colors = {
    primary: "bg-primary",
    success: "bg-success",
    warning: "bg-warning",
    danger: "bg-danger",
    muted: "bg-muted-foreground/50",
  };
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value === null ? undefined : Math.round(value)}
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-muted", className)}
    >
      <div
        className={cn(
          "h-full rounded-full transition-[width] duration-300",
          colors[tone],
          value === null && "w-1/3 animate-pulse",
        )}
        style={value === null ? undefined : { width: `${value}%` }}
      />
    </div>
  );
}
