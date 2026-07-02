#!/bin/bash
# IVD故障分析平台 - Docker一键部署脚本

set -e

echo "================================================"
echo "  IVD故障分析平台 - Docker部署"
echo "================================================"
echo ""

# 检查Docker是否安装
if ! command -v docker &> /dev/null; then
    echo "❌ Docker未安装，请先安装Docker"
    exit 1
fi

# 检查docker-compose命令（支持V1和V2）
if command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
elif docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
else
    echo "❌ docker-compose未安装，请先安装docker-compose"
    exit 1
fi

echo "✅ Docker环境检查通过 (使用: $COMPOSE_CMD)"
echo ""

# 停止并清理旧容器
echo "🧹 清理旧容器..."
$COMPOSE_CMD down -v 2>/dev/null || true

# 构建镜像
echo ""
echo "🔨 构建Docker镜像..."
$COMPOSE_CMD build --no-cache

# 启动服务
echo ""
echo "🚀 启动服务..."
$COMPOSE_CMD up -d

# 等待服务就绪
echo ""
echo "⏳ 等待服务就绪..."
sleep 10

# 检查服务状态
echo ""
echo "📊 服务状态:"
$COMPOSE_CMD ps

# 健康检查
echo ""
echo "🏥 健康检查:"

# 检查PostgreSQL
if $COMPOSE_CMD exec -T postgres pg_isready -U ivd_user &> /dev/null; then
    echo "  ✅ PostgreSQL: 正常"
else
    echo "  ⚠️  PostgreSQL: 启动中..."
fi

# 检查Redis
if $COMPOSE_CMD exec -T redis redis-cli ping &> /dev/null; then
    echo "  ✅ Redis: 正常"
else
    echo "  ⚠️  Redis: 启动中..."
fi

# 检查Go Parser
if curl -s http://localhost:8082/parse -X POST -H "Content-Type: application/json" -d '{"text":"test"}' &> /dev/null; then
    echo "  ✅ Go Parser: 正常"
else
    echo "  ⚠️  Go Parser: 启动中..."
fi

# 检查Web服务
if curl -s http://localhost:8081 &> /dev/null; then
    echo "  ✅ Web服务: 正常"
else
    echo "  ⚠️  Web服务: 启动中..."
fi

echo ""
echo "================================================"
echo "  🎉 部署完成！"
echo "================================================"
echo ""
echo "📌 访问地址:"
echo "   Web界面: http://localhost:8081"
echo "   Go解析器: http://localhost:8082/parse"
echo ""
echo "📌 查看日志:"
echo "   $COMPOSE_CMD logs -f web        # Web服务日志"
echo "   $COMPOSE_CMD logs -f worker     # Celery日志"
echo "   $COMPOSE_CMD logs -f go-parser  # Go解析器日志"
echo ""
echo "📌 停止服务:"
echo "   $COMPOSE_CMD down"
echo ""
echo "📌 重启服务:"
echo "   $COMPOSE_CMD restart"
echo ""