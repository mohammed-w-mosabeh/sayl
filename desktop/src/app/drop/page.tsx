"use client";

import { DropTarget } from "@/components/drop-target";
import { Providers } from "@/components/providers";

export default function Page() {
  return (
    <Providers primary={false}>
      <DropTarget />
    </Providers>
  );
}
