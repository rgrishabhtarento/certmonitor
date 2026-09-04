# Frontend image: build the SPA, then serve it from nginx.
#
# nginx also reverse-proxies /api, /health and /ready to the backend, so the
# browser only ever talks to one origin. That removes CORS from the picture
# entirely and matches how the app is expected to sit behind an ingress.

# ---------------------------------------------------------------- builder
FROM node:20-alpine AS builder

WORKDIR /app

# Copy manifests first so `npm ci` is cached until dependencies actually
# change.
COPY package.json package-lock.json* ./

# `npm ci` requires a lockfile; fall back to `npm install` so a fresh clone
# without one still builds.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

RUN npm run build

# ---------------------------------------------------------------- runtime
FROM nginx:1.27-alpine AS runtime

LABEL org.opencontainers.image.title="InfraSight Frontend" \
      org.opencontainers.image.description="Dashboard for endpoint and SSL monitoring"

RUN apk add --no-cache curl

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# nginx:alpine ships an unprivileged `nginx` user, but the stock image needs
# to bind :80 and write its pid as root. Running the master as root and the
# workers as nginx is the image's own default and is the least surprising
# choice here.

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
