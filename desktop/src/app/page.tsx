"use client";

import { Providers } from "@/components/providers";
import { MainWindow } from "@/components/main-window";

export default function Page() {
  return (
    <Providers>
      <MainWindow />
    </Providers>
  );
}
