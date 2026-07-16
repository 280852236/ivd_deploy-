#!/bin/bash
# 测试Docker构建（不启动服务）

set -e

echo "================================================"
echo "  测试Docker镜像构建"
echo "================================================"
echo ""

# 检查docker-compose命令
if command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
elif docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
else
    echo "❌ docker-compose未安装"
    exit 1
fi

echo "✅ 使用: $COMPOSE_CMD"
echo ""

# 测试构建Go Parser
echo "🔨 测试构建Go Parser镜像..."
$COMPOSE_CMD build go-parser

echo ""
echo "✅ Go Parser镜像构建成功"
echo ""

# 测试构建Web
echo "🔨 测试构建Web镜像..."
$COMPOSE_CMD build web

echo ""
echo "✅ Web镜像构建成功"
echo ""

# 显示镜像信息
echo "📦 构建的镜像:"
docker images | grep -E "ivd_deploy|REPOSITORY"

echo ""
echo "================================================"
echo "  ✅ 测试完成"
echo "================================================"