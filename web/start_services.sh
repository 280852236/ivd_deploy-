#!/bin/bash
# IVD 服务启动脚本（内存优化版）

cd /home/ivduser/ivd_analyzer
source .venv_wsl/bin/activate

mkdir -p logs

# 启动 Redis
sudo service redis-server start

# 等待 Redis 就绪
echo "⏳ 等待 Redis 就绪..."
until redis-cli -h 127.0.0.1 ping > /dev/null 2>&1; do
    sleep 1
done
echo "✅ Redis 已就绪"

# 启动 Celery Worker
echo "🚀 启动 Celery Worker..."
nohup celery -A celery_app worker --loglevel=info --concurrency=4 > logs/worker.log 2>&1 &

# 启动 Gunicorn（2 个 worker + 内存释放）
echo "🚀 启动 Gunicorn Web 服务..."
nohup gunicorn -w 2 -b 0.0.0.0:8081 \
    --timeout 600 \
    --graceful-timeout 30 \
    --max-requests 100 \
    --max-requests-jitter 20 \
    --limit-request-line 8190 \
    --limit-request-field_size 8190 \
    --limit-request-fields 100 \
    app:app \
    --access-logfile logs/access.log \
    --error-logfile logs/error.log > /dev/null 2>&1 &

echo ""
echo "✅ 所有服务已启动！"
echo "📌 查看日志："
echo "   Worker 日志：tail -f logs/worker.log"
echo "   访问日志：tail -f logs/access.log"
echo "   错误日志：tail -f logs/error.log"
echo ""
echo "📌 停止服务：pkill -f celery && pkill -f gunicorn && sudo service redis-server stop"
