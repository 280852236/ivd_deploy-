#!/bin/bash
# IVD平台 数据库自动备份脚本
# 用法: 每日凌晨2点通过cron执行
# 0 2 * * * /path/to/scripts/db-backup.sh

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-ivd_user}"
DB_PASSWORD="${DB_PASSWORD:-ivd_pass}"
DB_NAME="${DB_NAME:-ivd_fault_db}"
KEEP_DAYS="${KEEP_DAYS:-7}"

mkdir -p "$BACKUP_DIR"
export PGPASSWORD="$DB_PASSWORD"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql.gz"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始备份 $DB_NAME ..."

pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --no-owner \
    --no-privileges \
    --format=plain \
    | gzip > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
FILE_SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || echo 0)
if [ "$FILE_SIZE" -lt 1024 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 警告: 备份文件异常小(${FILE_SIZE}B)，可能备份失败，删除"
    rm -f "$BACKUP_FILE"
    exit 1
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份完成: $BACKUP_FILE ($SIZE)"

# 清理过期备份
find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime +$KEEP_DAYS -delete
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已清理 ${KEEP_DAYS} 天前的备份"