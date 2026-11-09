# syntax=docker/dockerfile:1.7

# Stage 1: build the SPA.
FROM --platform=$BUILDPLATFORM node:22-alpine AS builder
WORKDIR /app

ARG VITE_APP_VERSION=dev
ENV VITE_APP_VERSION=${VITE_APP_VERSION}

# CE release fallback edition. Edition is normally decided at runtime by the
# backend (GET /api/v1/config); VITE_EDITION only takes effect when that fetch
# fails, so baking =ce makes this CE image fail safe to CE (no EE login wall)
# instead of the EE default. See src/lib/runtime-config.ts fallbackRuntimeConfig().
ARG VITE_EDITION=ce
ENV VITE_EDITION=${VITE_EDITION}

# Instance-level build-time config. Every value here is inlined into the bundle,
# so it MUST arrive through a build arg: `.dockerignore` drops `.env` / `.env.*`,
# which means `vite build --mode ce` cannot read `.env.ce.local` inside the build
# context even though a local `npm run build:ce` does. Empty (the default) keeps
# the shipped upstream behavior. See src/lib/ui-hide-flags.ts, src/lib/app-mode.ts
# and docs/agents/web-hotpatch-deploy.md §1/§6 — a bundle built without these was
# the 2026-11-06 incident (customer studio links started asking for the internal
# console key). `docker-compose.yml` forwards the matching host `.env` values.
ARG VITE_HIDE_RELEASE_POPUP=""
ENV VITE_HIDE_RELEASE_POPUP=${VITE_HIDE_RELEASE_POPUP}
ARG VITE_HIDE_PIKO_ENTRY=""
ENV VITE_HIDE_PIKO_ENTRY=${VITE_HIDE_PIKO_ENTRY}
ARG VITE_HIDE_OFFICIAL_CHANNELS=""
ENV VITE_HIDE_OFFICIAL_CHANNELS=${VITE_HIDE_OFFICIAL_CHANNELS}
ARG VITE_HIDE_NOTIFICATION_CENTER=""
ENV VITE_HIDE_NOTIFICATION_CENTER=${VITE_HIDE_NOTIFICATION_CENTER}
ARG VITE_HIDE_MODEL_GUIDE=""
ENV VITE_HIDE_MODEL_GUIDE=${VITE_HIDE_MODEL_GUIDE}
ARG VITE_HIDE_GITHUB_STARS=""
ENV VITE_HIDE_GITHUB_STARS=${VITE_HIDE_GITHUB_STARS}
ARG VITE_HIDE_HEADER_SETTINGS=""
ENV VITE_HIDE_HEADER_SETTINGS=${VITE_HIDE_HEADER_SETTINGS}
ARG VITE_LOGIN_ANNOUNCEMENTS_URL=""
ENV VITE_LOGIN_ANNOUNCEMENTS_URL=${VITE_LOGIN_ANNOUNCEMENTS_URL}
ARG VITE_INTERNAL_ENTRY_PATH=""
ENV VITE_INTERNAL_ENTRY_PATH=${VITE_INTERNAL_ENTRY_PATH}
ARG VITE_BRAND_ATTRIBUTION=""
ENV VITE_BRAND_ATTRIBUTION=${VITE_BRAND_ATTRIBUTION}

# Opt-in strict build-time check (see scripts/verify-ce-bundle.mjs): when "1",
# `pnpm verify:ce` fails unless every instance value above is non-empty, so a
# line lost from the host `.env` can never ship a degraded bundle again
# (single-entry studio link, re-exposed header settings). Generic CE/upstream
# builds leave it unset. `docker-compose.yml` forwards the host `.env` value.
ARG VITE_REQUIRE_INSTANCE_CONFIG=""
ENV VITE_REQUIRE_INSTANCE_CONFIG=${VITE_REQUIRE_INSTANCE_CONFIG}

# Use pnpm (matches local dev) instead of npm — a single lockfile as source of
# truth, which removes the npm/pnpm dual-lockfile drift that made `npm ci` fail.
# corepack picks the version pinned in package.json's "packageManager" field.
# pnpm-workspace.yaml is required here: it carries the allowBuilds approvals that
# keep `pnpm install` from erroring on skipped (unneeded) dependency build scripts.
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# 可选镜像源：默认官方 registry，行为与现在完全一致。构建时传
# --build-arg NPM_REGISTRY=https://registry.npmmirror.com 可显著加快 pnpm install。
ARG NPM_REGISTRY=""
RUN pnpm install --frozen-lockfile --registry="${NPM_REGISTRY:-https://registry.npmjs.org}"

COPY . .
# `build:ce`, not `build`: the plain production-mode build drops `.env.ce` and
# every instance value declared above. `verify:ce` then fails the image build if
# the produced bundle does not actually carry those values.
RUN pnpm build:ce && pnpm verify:ce

# Parse the rendered runtime template in CI without building the SPA image.
FROM nginx:1.27-alpine AS nginx-config-test
ENV BACKEND_HOST=127.0.0.1 \
    BACKEND_PORT=8780 \
    CSP_PAYMENT_FORM_ORIGINS="" \
    NGINX_ENVSUBST_FILTER="^(BACKEND_HOST|BACKEND_PORT|CSP_PAYMENT_FORM_ORIGINS)$"
COPY --chmod=755 docker/19-payment-form-csp.envsh /docker-entrypoint.d/19-payment-form-csp.envsh
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
RUN /docker-entrypoint.sh nginx -t \
    && CSP_PAYMENT_FORM_ORIGINS="https://pay.example.test" /docker-entrypoint.sh nginx -t

# Stage 2: serve via nginx + reverse-proxy /api /static to the backend Service.
FROM nginx:1.27-alpine AS runtime

# Defaults overridable per environment via Helm values -> Deployment env.
ENV BACKEND_HOST=novelvideo-ui-staging \
    BACKEND_PORT=8780 \
    CSP_PAYMENT_FORM_ORIGINS="" \
    NGINX_ENVSUBST_FILTER="^(BACKEND_HOST|BACKEND_PORT|CSP_PAYMENT_FORM_ORIGINS)$"

COPY --from=builder /app/dist /usr/share/nginx/html
COPY --chmod=755 docker/19-payment-form-csp.envsh /docker-entrypoint.d/19-payment-form-csp.envsh
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 80
