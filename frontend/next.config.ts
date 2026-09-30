import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for Docker (node .next/standalone/server.js)
  output: "standalone",
  poweredByHeader: false,
  images: {
    // YouTube thumbnails / channel avatars are shown with plain <img>; nothing to optimise server-side.
    unoptimized: true,
  },
};

export default nextConfig;
