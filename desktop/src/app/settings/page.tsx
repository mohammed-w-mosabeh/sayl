"use client";

import { Providers } from "@/components/providers";
import { SettingsView } from "@/components/settings-view";

export default function Page() {
  return (
    <Providers>
      <SettingsView />
    </Providers>
  );
}
