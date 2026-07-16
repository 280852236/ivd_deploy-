#!/bin/bash
# 停止所有服务

# 检查docker-compose命令
if command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
elif docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
else
    echo "❌ docker-compose未安装"
    exit 1
fi

echo "🛑 停止IVD服务..."
$COMPOSE_CMD down

echo ""
echo "✅ 服务已停止"
