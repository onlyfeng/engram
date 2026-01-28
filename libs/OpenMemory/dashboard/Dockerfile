# ===== BUILD STAGE =====
FROM node:20-alpine AS builder

WORKDIR /app

# 1. 优先复制依赖清单文件（利用 Docker 缓存层）
COPY package.json ./
COPY package-lock.json* ./

# 2. 安装依赖（优先使用 npm ci 若存在 lockfile）
RUN if [ -f package-lock.json ]; then \
      npm ci --legacy-peer-deps; \
    else \
      npm install; \
    fi

# 3. 复制构建所需的配置和源代码
#    注意：根据项目实际结构调整以下 COPY 命令
#    若某目录不存在，需删除或注释对应行
COPY next.config.* tsconfig*.json ./
COPY tailwind.config.* postcss.config.* ./
COPY app/ ./app/
COPY src/ ./src/
COPY public/ ./public/
COPY components/ ./components/
COPY lib/ ./lib/

# 4. 构建 Next.js 应用
ENV NODE_ENV=production
RUN npm run build

# ===== PRODUCTION STAGE =====
FROM node:20-alpine AS production

WORKDIR /app

# 1. 复制依赖清单
COPY package.json ./
COPY package-lock.json* ./

# 2. 仅安装生产依赖
RUN if [ -f package-lock.json ]; then \
      npm ci --omit=dev --legacy-peer-deps; \
    else \
      npm install --omit=dev; \
    fi

# 3. 仅复制运行所需的构建产物
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
# 复制 next.config（根据项目使用 .ts 或 .js 选择对应行）
COPY --from=builder /app/next.config.* ./

# 4. 创建非 root 用户（安全最佳实践）
RUN addgroup -g 1001 -S nodejs \
 && adduser -u 1001 -S nextjs -G nodejs \
 && chown -R nextjs:nodejs /app

USER nextjs

# 暴露应用端口
EXPOSE 3000

# 设置生产环境
ENV NODE_ENV=production

# 启动 Next.js 应用
CMD ["npm", "start"]
