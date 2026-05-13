/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_VOICE_AGENT_BACKEND:
      process.env.NEXT_PUBLIC_VOICE_AGENT_BACKEND || "http://localhost:5000",
  },
};

module.exports = nextConfig;
