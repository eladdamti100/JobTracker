import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Allow API URL to be injected at build time via NEXT_PUBLIC_API_URL
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:5001",
  },
};

export default nextConfig;
