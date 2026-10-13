FROM node:24-alpine AS deps
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:24-alpine AS build
WORKDIR /web
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /web/node_modules ./node_modules
COPY web ./
RUN npm run build

FROM node:24-alpine AS run
WORKDIR /web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=build /web/.next/standalone ./
COPY --from=build /web/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
