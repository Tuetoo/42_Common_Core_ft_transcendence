import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `next build` also writes .next/standalone: a minimal server.js plus only
  // the node_modules files it actually uses. The Docker image ships just
  // that, instead of the whole dependency tree.
  output: "standalone",
  experimental: {
    serverActions: {
      // Avatars may be up to 5MB (the backend's limit); Server Action
      // requests are capped at 1MB by default. The extra headroom covers
      // the multipart encoding around the file.
      bodySizeLimit: "6mb",
    },
  },
};

export default nextConfig;
