# Production image for the Next.js frontend, served through the gateway.
# Three stages: only the last one ends up in the image, so it carries the
# standalone server and static files, not the source, the compiler or the
# development packages.

# ---- 1. Install dependencies -------------------------------------------
# Copying only the package files first lets Docker reuse this layer until
# they change, so a code edit doesn't trigger a fresh npm install.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- 2. Build ----------------------------------------------------------
FROM node:24-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* values are baked in here. None are set on purpose: the
# browser then uses its own origin (the gateway) for WebSockets and avatars.
RUN npm run build

# ---- 3. Run ------------------------------------------------------------
FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    # Listen on every interface, or only the container itself could connect.
    HOSTNAME=0.0.0.0

# Run as an unprivileged user rather than root.
RUN addgroup -S nextjs && adduser -S nextjs -G nextjs

# The standalone server does not include static files; they go next to it.
COPY --from=build --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nextjs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nextjs /app/public ./public

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
