/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_VOICE_AGENT_BACKEND:
      process.env.NEXT_PUBLIC_VOICE_AGENT_BACKEND || "http://localhost:5050",
  },
};

module.exports = nextConfig;
