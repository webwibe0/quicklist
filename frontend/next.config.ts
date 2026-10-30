import type { NextConfig } from "next";
import { resolve } from "node:path";
import { loadEnvConfig } from "@next/env";

loadEnvConfig(resolve(process.cwd(), ".."));
const backend = process.env.BACKEND_URL || "http://127.0.0.1:8000";

const config: NextConfig = {
  poweredByHeader: false,
  async headers() {
    return [{source: '/:path*', headers: [
      {key: 'Referrer-Policy', value: 'no-referrer'},
      {key: 'X-Content-Type-Options', value: 'nosniff'},
      {key: 'X-Frame-Options', value: 'DENY'},
      {key: 'Permissions-Policy', value: 'camera=(), geolocation=(), microphone=(self)'},
    ]}];
  },
  async rewrites() {
    return [
      { source: "/api/v1/:path*", destination: `${backend}/api/v1/:path*` },
      { source: "/api/health", destination: `${backend}/health` },
    ];
  },
};
export default config;
