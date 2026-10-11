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
    // proxy.ts sees every request body first and cuts it at 10MB by
    // default. Research documents may be up to 20MB (core-service's limit);
    // they are uploaded through a route handler, not a Server Action.
    proxyClientMaxBodySize: "21mb",
  },
};

export default nextConfig;
