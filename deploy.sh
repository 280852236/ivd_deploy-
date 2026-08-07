#!/bin/bash
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
ENV_FILE=".env"; SSL_DIR="ssl"; DEPLOY_LOG="deploy.log"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
GRAFANA_ADMIN_PASSWORD=""

check_prerequisites() {
    info "检查部署环境..."
    command -v docker &>/dev/null || { fail "Docker未安装"; exit 1; }
    ok "Docker: $(docker --version)"
    docker compose version &>/dev/null || { fail "docker compose未安装"; exit 1; }
    ok "Compose: $(docker compose version --short)"
    AVAILABLE_GB=$(df -BG "$SCRIPT_DIR" | tail -1 | awk '{print $4}' | tr -d 'G')
    [ "$AVAILABLE_GB" -lt 5 ] && { fail "磁盘不足: ${AVAILABLE_GB}GB (需5GB+)"; exit 1; }
    ok "磁盘: ${AVAILABLE_GB}GB可用"
    TOTAL_MEM_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo 0)
    [ "$TOTAL_MEM_MB" -lt 2048 ] && warn "内存较低: ${TOTAL_MEM_MB}MB" || ok "内存: ${TOTAL_MEM_MB}MB"
    for port in 8081 8443 3000 9090; do
        ss -tlnp 2>/dev/null | grep -q ":${port} " && warn "端口 ${port} 被占用" || true
    done
}

generate_env() {
    if [ -f "$ENV_FILE" ]; then warn ".env已存在，跳过"; return 0; fi
    info "生成.env配置..."
    SK=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p)
    DBP=$(openssl rand -base64 16 2>/dev/null | tr -dc 'a-zA-Z0-9' | head -c 20)
    GAP=$(openssl rand -base64 12 2>/dev/null | tr -dc 'a-zA-Z0-9' | head -c 16)
    GRAFANA_ADMIN_PASSWORD="$GAP"
    cat > "$ENV_FILE" << EOF
# IVD智能故障分析平台 - 环境配置 (自动生成 ${TIMESTAMP})
# 警告：含敏感信息，勿提交到版本控制

# 数据库
DB_HOST=postgres
DB_PORT=5432
DB_USER=ivd_user
DB_PASSWORD=${DBP}
DB_NAME=ivd_fault_db

# Redis
REDIS_URL=redis://redis:6379/0

# Go解析器
GO_PARSER_URL=http://go-parser:8082/parse

# Flask应用
SECRET_KEY=${SK}
ADMIN_PASSWORD=admin123
SUPER_ADMIN_PASSWORD=super2026

# 分析配置
ANALYSIS_TTL_HOURS=2
MAX_CONTENT_LENGTH=209715200
UPLOAD_DIR=/app/uploads

# Grafana
GRAFANA_ADMIN_PASSWORD=${GAP}

# 日志级别
LOG_LEVEL=INFO
EOF
    chmod 600 "$ENV_FILE"
    ok ".env已生成 (权限600)"
    info "Grafana密码: ${GAP}"
    info "数据库密码: ${DBP}"
}

generate_ssl() {
    [ -f "${SSL_DIR}/ivd.crt" ] && [ -f "${SSL_DIR}/ivd.key" ] && { ok "SSL证书已存在"; return 0; }
    info "生成自签名SSL证书..."
    mkdir -p "$SSL_DIR"
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "${SSL_DIR}/ivd.key" -out "${SSL_DIR}/ivd.crt" -days 365 -subj "/C=CN/O=IVD/CN=ivd-localhost" 2>/dev/null
    chmod 600 "${SSL_DIR}/ivd.key"
    ok "SSL证书已生成 (365天)"
}

stop_old_services() {
    local running; running=$(docker compose ps -q 2>/dev/null | wc -l)
    if [ "$running" -gt 0 ]; then
        info "停止${running}个旧容器..."
        docker compose down --remove-orphans 2>/dev/null || true
        ok "旧容器已停止"
    fi
}

build_images() {
    info "构建Docker镜像..."
    echo "构建: $(date)" > "$DEPLOY_LOG"
    docker compose build 2>&1 | tee -a "$DEPLOY_LOG" || { fail "构建失败"; exit 1; }
    ok "镜像构建完成"
}

start_services() {
    info "启动全部服务..."
    docker compose up -d 2>&1 | tee -a "$DEPLOY_LOG" || { fail "启动失败"; rollback; exit 1; }
    ok "启动命令已执行"
}

wait_for_health() {
    info "等待服务就绪..."
    local services=("postgres:PostgreSQL:30" "redis:Redis:15" "go-parser:Go解析器:30" "web:Web服务:60" "worker:Worker:60" "nginx:Nginx:15" "loki:Loki:20" "promtail:Promtail:20" "prometheus:Prometheus:20" "grafana:Grafana:30" "node-exporter:NodeExporter:15" "db-backup:DB备份:15")
    for svc in "${services[@]}"; do
        IFS=':' read -r name label timeout <<< "$svc"
        local elapsed=0 interval=3
        printf "  %-14s " "$label"
        while [ $elapsed -lt $timeout ]; do
            local health; health=$(docker inspect --format='{{.State.Health.Status}}' "ivd_deploy-${name}-1" 2>/dev/null || echo "")
            local state; state=$(docker inspect --format='{{.State.Status}}' "ivd_deploy-${name}-1" 2>/dev/null || echo "")
            if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ -z "$health" ]; }; then
                echo -e "${GREEN}✅${NC} (${elapsed}s)"; break
            fi
            printf "."; sleep $interval; elapsed=$((elapsed + interval))
        done
        [ $elapsed -ge $timeout ] && echo -e "${RED}❌超时${NC} (${timeout}s)"
    done
}

verify_deployment() {
    info "验证部署..."
    local pass=0 total=0
    total=$((total+1)); docker compose exec -T web curl -sf http://localhost:8081/api/health &>/dev/null && { ok "Web健康检查"; pass=$((pass+1)); } || fail "Web健康检查失败"
    total=$((total+1)); docker compose exec -T postgres pg_isready -U ivd_user -d ivd_fault_db &>/dev/null && { ok "PostgreSQL连接"; pass=$((pass+1)); } || fail "PostgreSQL连接失败"
    total=$((total+1)); docker compose exec -T redis redis-cli ping &>/dev/null && { ok "Redis连接"; pass=$((pass+1)); } || fail "Redis连接失败"
    total=$((total+1)); docker compose exec -T nginx nginx -t &>/dev/null && { ok "Nginx配置"; pass=$((pass+1)); } || fail "Nginx配置异常"
    total=$((total+1)); docker compose exec -T go-parser wget -qO- http://localhost:8082/health &>/dev/null && { ok "GoParser健康"; pass=$((pass+1)); } || fail "GoParser健康失败"
    echo ""; info "验证: ${pass}/${total} 通过"
}

rollback() {
    fail "执行回滚..."
    docker compose down --remove-orphans 2>/dev/null || true
    fail "回滚完成。检查 ${DEPLOY_LOG}"
}

print_info() {
    local HOST_IP; HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    local GAP_DISPLAY="${GRAFANA_ADMIN_PASSWORD:-admin}"
    [ -f .env ] && GAP_DISPLAY=$(grep GRAFANA_ADMIN_PASSWORD .env | cut -d= -f2)
    echo ""
    echo "================================================================"
    echo -e "${GREEN}  🎉 IVD智能故障分析平台部署完成！${NC}"
    echo "================================================================"
    echo ""
    echo -e "${BLUE}📌 访问地址:${NC}"
    echo "   Web界面:    http://${HOST_IP}:8081  或  https://${HOST_IP}:8443"
    echo "   监控仪表板:  http://${HOST_IP}:3000  (admin/${GAP_DISPLAY})"
    echo "   Prometheus: http://${HOST_IP}:9090"
    echo ""
    echo -e "${BLUE}📌 常用命令:${NC}"
    echo "   ./deploy.sh --stop    停止服务"
    echo "   ./deploy.sh --status  查看状态"
    echo "   ./deploy.sh --logs web  查看日志"
    echo "   ./deploy.sh --clean   清理全部数据"
    echo "================================================================"
}

case "${1:-}" in
    --clean)  docker compose down -v --remove-orphans 2>/dev/null; docker image prune -f 2>/dev/null; rm -f .env deploy.log; ok "清理完成" ;;
    --stop)   docker compose down --remove-orphans; ok "已停止" ;;
    --logs)   shift; docker compose logs -f "$@" ;;
    --status) docker compose ps ;;
    --help|-h) echo "用法: ./deploy.sh [部署|--stop|--clean|--logs <svc>|--status|--help]" ;;
    *)
        echo ""; echo "================================================================"
        echo -e "${BLUE}  IVD智能故障分析平台 - 一键部署${NC}"
        echo "  $(date '+%Y-%m-%d %H:%M:%S')"; echo "================================================================"; echo ""
        check_prerequisites; echo ""
        generate_env; echo ""
        generate_ssl; echo ""
        stop_old_services; echo ""
        build_images; echo ""
        start_services; echo ""
        wait_for_health; echo ""
        verify_deployment
        print_info
        ;;
esac
