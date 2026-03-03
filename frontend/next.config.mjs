/** @type {import('next').NextConfig} */
const nextConfig = {
  // Produces a self-contained build that the multi-stage Dockerfile can copy.
  output: "standalone",

  // TODO: If you add API routes that proxy to the backend, configure
  //       rewrites here to avoid CORS issues in production:
  //
  // async rewrites() {
  //   return [
  //     {
  //       source: "/api/:path*",
  //       destination: `${process.env.NEXT_PUBLIC_BACKEND_URL}/api/:path*`,
  //     },
  //   ];
  // },
};

export default nextConfig;
