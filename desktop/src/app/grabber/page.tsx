"use client";

import { Providers } from "@/components/providers";
import { VideoBar } from "@/components/video-bar";

export default function Page() {
  return (
    <Providers primary={false}>
      <VideoBar />
    </Providers>
  );
}
