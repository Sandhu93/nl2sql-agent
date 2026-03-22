# syntax=docker/dockerfile:1

# ---- deps stage ----
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
# Use npm install (not ci) since no lockfile exists yet; generates one inside the image.
RUN npm install --legacy-peer-deps

# ---- builder stage ----
FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* vars are inlined into the client-side JS bundle at build time.
# Both must be passed as build args so they are correct for the target environment.
# NEXT_PUBLIC_BACKEND_URL — backend API base URL (required)
# NEXT_PUBLIC_API_KEY     — API key for /api/query auth; empty = auth disabled
ARG NEXT_PUBLIC_BACKEND_URL=http://localhost:8086
ARG NEXT_PUBLIC_API_KEY=
ENV NEXT_PUBLIC_BACKEND_URL=${NEXT_PUBLIC_BACKEND_URL}
ENV NEXT_PUBLIC_API_KEY=${NEXT_PUBLIC_API_KEY}
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---- runner stage ----
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

COPY --from=builder /app/public* ./public/
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 8085
ENV PORT=8085
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
