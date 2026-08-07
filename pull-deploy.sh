#!/bin/bash
# 服务器端：拉取最新代码并重新部署
set -e
cd "$(dirname "$0")"

echo "📋 拉取最新代码..."
git pull origin main

echo "🔨 重新构建镜像..."
docker compose build

echo "🚀 重启服务..."
docker compose up -d

echo "⏳ 等待服务就绪..."
sleep 10

echo "📊 服务状态:"
docker compose ps --format "table {{.Name}}\t{{.Status}}"

echo ""
echo "✅ 部署完成"
