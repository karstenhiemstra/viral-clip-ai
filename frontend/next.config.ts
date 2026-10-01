import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for Docker (node .next/standalone/server.js)
  output: "standalone",
  poweredByHeader: false,
  images: {
    // YouTube thumbnails / channel avatars are shown with plain <img>; nothing to optimise server-side.
    unoptimized: true,
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" }, // the dashboard is never embedded elsewhere (clickjacking)
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
