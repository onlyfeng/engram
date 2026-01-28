# syntax=docker/dockerfile:1.4
# ===== BUILD STAGE =====
FROM node:20-alpine AS builder

WORKDIR /app

# Install system dependencies required for native module compilation
RUN apk add --no-cache python3 make g++

# ----- Dependency Install Layer -----
# Copy only manifests and lockfile first (before src) to optimize layer caching.
# Changes to src/ will NOT invalidate the npm install layer.
COPY package.json package-lock.json ./

# Install all dependencies (including devDependencies) using npm ci
# --mount=type=cache caches npm downloads across builds (BuildKit)
# npm ci uses package-lock.json for reproducible builds
RUN --mount=type=cache,target=/root/.npm \
    npm ci

# ----- Build Layer -----
# Copy source code and build the application
COPY src/ ./src/
COPY tsconfig.json ./
RUN npm run build

# Remove devDependencies to reduce image size
RUN npm prune --omit=dev


# ===== PRODUCTION STAGE =====
FROM node:20-alpine AS production

WORKDIR /app

# Install timezone data for proper TZ support
RUN apk add --no-cache tzdata

# Create a dedicated non-root user for security
RUN addgroup -g 1001 -S appgroup \
 && adduser -u 1001 -S appuser -G appgroup

# Copy only production artifacts from the builder stage
COPY --from=builder /app/node_modules ./node_modules/
COPY --from=builder /app/dist ./dist/
COPY package.json ./

# Create a directory for persistent data and secure file permissions
RUN mkdir -p /data \
 && chown -R appuser:appgroup /data /app \
 && chmod -R go-w /app

# Switch to non-root user
USER appuser

# Expose the application port
EXPOSE 8080

# Define a lightweight health check that verifies the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD node -e "require('http').get('http://localhost:8080/health', (res) => process.exit(res.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))"

# Start the application using npm
ENTRYPOINT ["npm", "start"]