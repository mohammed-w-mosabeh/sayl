"use client";

import { Providers } from "@/components/providers";
import { StatsView } from "@/components/stats-view";

export default function Page() {
  return (
    <Providers>
      <StatsView />
    </Providers>
  );
}
