# IVD智能故障分析平台 - 架构文档

> 更新时间: 2026-08-11 | 版本: v3.0 | 提交: 952e4d0

---

## 一、系统架构

### 1.1 整体架构图

```
                    ┌─────────────────────────────────────────────────┐
                    │                  Nginx (8081/8443)                │
                    │         SSL终止 / 静态文件 / 图片缓存 / gzip      │
                    └────────────────────┬────────────────────────────┘
                                         │
                    ┌────────────────────┴────────────────────────────┐
                    │              Web (Gunicorn 4w × gevent)           │
                    │         Flask API / CSRF / 限流 / API缓存         │
                    └───┬──────────────┬──────────────┬───────────────┘
                        │              │              │
              ┌─────────┴──┐  ┌───────┴──────┐  ┌───┴────────────┐
              │  PgBouncer  │  │    Redis     │  │  Go Parser     │
              │  (6432)     │  │   (6379)     │  │  (8082)        │
              │  连接池化    │  │  缓存/队列    │  │  PDF解析       │
              └──────┬──────┘  └──────────────┘  └────────────────┘
                     │
              ┌──────┴──────┐
              │ PostgreSQL  │
              │   (5432)    │
              │  数据持久化  │
              └─────────────┘

         ┌─────────────────────────────────────────────────┐
         │              Celery Worker (gevent ×4)           │
         │              Celery Beat (定时调度)               │
         └─────────────────────────────────────────────────┘

         ┌─────────────────────────────────────────────────┐
         │    监控: Prometheus + Grafana + Loki + Exporters │
         │    运维: db-backup (crond)                        │
         └─────────────────────────────────────────────────┘
```

### 1.2 容器清单 (16个)

| 容器 | 镜像 | 角色 | 端口 | 内存限制 |
|------|------|------|------|----------|
| nginx | nginx:1.25 | 反向代理/SSL/静态 | 8081, 8443 | 256M |
| web | python:3.12-slim | Flask API | 8081(内部) | 2G |
| worker | python:3.12-slim | Celery异步任务 | - | 1G |
| beat | python:3.12-slim | Celery定时调度 | - | 256M |
| pgbouncer | alpine:3.18 | DB连接池 | 6432(内部) | 128M |
| postgres | postgres:15 | 数据库 | 5432 | 1G |
| redis | redis:7-alpine | 缓存/消息队列 | 6379(内部) | 256M |
| go-parser | go:alpine | PDF解析 | 8082(内部) | 512M |
| prometheus | prom/prometheus | 指标采集 | 9090 | 512M |
| grafana | grafana/grafana | 可视化 | 3000 | 512M |
| loki | grafana/loki | 日志聚合 | 3100 | 512M |
| promtail | grafana/promtail | 日志采集 | - | 256M |
| node-exporter | prom/node-exporter | 主机指标 | 9100(内部) | 128M |
| postgres-exporter | prometheuscommunity | PG指标 | 9187(内部) | 128M |
| redis-exporter | oliver006 | Redis指标 | 9121(内部) | 64M |
| db-backup | postgres:15-alpine | 备份/清理 | - | 256M |

### 1.3 网络拓扑

- **网络**: ivd_net (bridge, 172.28.0.0/16)
- **固定IP**: 每个容器分配静态IP，支持服务名+IP双连接
- **数据流**: Nginx → Web → PgBouncer → PostgreSQL / Redis / Go-Parser

---

## 二、功能模块

### 2.1 核心业务功能

| 模块 | 功能 | API端点 |
|------|------|---------|
| 故障分析 | PDF上传→解析→诊断→结果存储 | /api/analysis/* |
| 硬件故障 | 故障记录CRUD+图片管理+搜索 | /api/hardware-failures/* |
| 软件Bug | Bug记录CRUD+图片管理+搜索 | /api/bugs/* |
| 电机状态 | 加注针监测+异常检测+状态查询 | /api/motor_status/* |
| 板卡兼容 | PCBA/Bootloader兼容性查询 | /api/board-compat/* |
| LIS协议 | 协议模板管理+下发 | /api/lis/* |
| 规则管理 | 诊断规则CRUD+版本历史 | /api/rules/* |
| 用户管理 | 用户CRUD+权限+密码重置 | /api/users/* |
| 管理后台 | PDF导入+型号管理+系统配置 | /admin/* |

### 2.2 安全机制

| 机制 | 实现 | 说明 |
|------|------|------|
| HTTPS | Nginx自签名证书 | TLS 1.2/1.3 |
| 认证 | Session+Cookie | @login_required装饰器 |
| CSRF | Token验证 | POST/PUT/DELETE校验X-CSRFToken |
| 限流 | Redis滑动窗口 | 60次/分钟/IP |
| SQL注入防护 | 参数化查询+表名白名单 | resolve_table验证 |
| XSS防护 | escape_html+X-Content-Type-Options | nosniff |
| 非root运行 | appuser(UID=1001) | entrypoint.sh切换 |
| 密码安全 | werkzeug hash | PBKDF2 |
| 密钥管理 | .env环境变量 | 64字符SECRET_KEY |

### 2.3 异步任务 (Celery)

| 任务名 | 调度 | 功能 |
|--------|------|------|
| analyze_files_task | 按需触发 | PDF解析+故障分析 |
| cleanup_expired_zip_files | 每10分钟 | 清理过期ZIP |
| cleanup_old_uploads | 每天3:00 | 清理>7天上传文件 |
| memory_cleanup | 每30分钟 | GC回收+缓存清理+Redis PURGE |

### 2.4 定时任务 (crond)

| 任务 | 调度 | 功能 |
|------|------|------|
| db-backup.sh | 每天12:30 | pg_dump备份(保留7天) |
| docker-cleanup.sh | 每周日14:00 | 清理未使用镜像/构建缓存 |
| container-memory-guard.sh | 每10分钟 | 容器内存>85%自动重启 |

### 2.5 前端模板 (9个)

| 模板 | 大小 | 特点 |
|------|------|------|
| analysis.html | 13KB | CSS/JS已外部化(原156KB) |
| main.html | - | 首页+分析上传 |
| admin.html | - | 管理后台 |
| hardware_failures.html | - | 硬件故障管理 |
| bugs.html | - | 软件Bug管理 |
| lis_issues.html | - | LIS协议管理 |
| board_compatibility.html | - | 板卡兼容查询 |
| login.html | - | 登录页 |
| register.html | - | 注册页 |

---

## 三、性能优化

### 3.1 数据库优化

| 参数 | 值 | 说明 |
|------|-----|------|
| shared_buffers | 256MB | PG共享缓冲区 |
| effective_cache_size | 512MB | 查询规划器缓存估算 |
| work_mem | 4MB | 排序/哈希内存 |
| max_connections | 50 | 最大连接数 |
| autovacuum_naptime | 60s | 自动清理间隔 |
| 活跃连接 | 9 | 通过PgBouncer池化 |
| 索引数 | 65 | 已清理101个未使用索引 |
| 数据库大小 | 22MB | 含30张表 |

### 3.2 连接池

| 组件 | 配置 | 说明 |
|------|------|------|
| PgBouncer | transaction模式 | pool_size=20, max_client=100 |
| Python DB池 | min=2, max=5 | 每进程连接数 |
| Gunicorn | 4 workers × gevent | --max-requests=500 |
| Celery | gevent × 4并发 | --max-tasks-per-child=500 |

### 3.3 缓存策略

| 层级 | 机制 | TTL | 命中率 |
|------|------|-----|--------|
| Nginx图片缓存 | proxy_cache | 7天 | 800-1300倍加速 |
| Nginx静态文件 | Cache-Control | 30天immutable | 浏览器缓存 |
| Redis API缓存 | api_cache装饰器 | 60-300秒 | MISS→HIT |
| PG表缓存 | 内存字典 | 300秒 | resolve_table |
| Gunicorn worker回收 | max-requests | 500请求 | 防内存泄漏 |
| Celery worker回收 | max-tasks-per-child | 500任务 | 防内存泄漏 |

### 3.4 API响应性能

| 端点 | 响应时间 | 缓存 |
|------|---------|------|
| /api/health | 8.5ms | 无 |
| /api/models | 6.7ms | Redis |
| /api/bugs | 8.2ms | Redis 60s |
| /api/hardware-failures | 7.4ms | Redis 60s |
| /api/motor_status | 8.5ms | Redis 300s |
| /api/board-compat/pcba | 9.6ms | Redis 300s |
| /api/board-compat/bootloader | ~9ms | Redis 300s |

### 3.5 内存管理

| 机制 | 频率 | 说明 |
|------|------|------|
| Python GC | 每30分钟 | gc.collect()+清缓存 |
| Redis PURGE | 每30分钟 | MEMORY PURGE |
| Gunicorn worker回收 | 每500请求 | --max-requests=500 |
| Celery worker回收 | 每500任务 | --max-tasks-per-child=500 |
| 容器内存保护 | 每10分钟 | >85%自动重启 |
| Prometheus告警 | 持续 | >80%警告, >90%严重 |

---

## 四、监控运维

### 4.1 Prometheus指标采集

| 目标 | 间隔 | 指标 |
|------|------|------|
| web | 15s | 请求计数/延迟/连接池 |
| node-exporter | 15s | CPU/内存/磁盘 |
| postgres-exporter | 30s | 连接/查询/表统计 |
| redis-exporter | 15s | 内存/命中/键数 |
| prometheus | 15s | 自身指标 |

### 4.2 告警规则 (7条)

| 告警 | 条件 | 级别 |
|------|------|------|
| ServiceDown | up==0 持续1分钟 | 严重 |
| HighDiskUsage | 磁盘>80% | 严重 |
| HighMemoryUsage | 内存>85% | 严重 |
| HighRequestLatency | P95>2秒 | 警告 |
| HighErrorRate | 5xx>10% | 警告 |
| ContainerMemoryHigh | 容器>80% | 警告 |
| ContainerMemoryCritical | 容器>90% | 严重 |

### 4.3 日志体系

| 组件 | 用途 | 存储 |
|------|------|------|
| Loki+Promtail | 容器日志聚合 | Loki 38MB |
| PG慢查询日志 | >1秒查询 | PG日志 |
| JSON结构化日志 | 应用日志 | ivd_app.log (100MB×10) |
| Docker日志 | 容器stdout | json-file 50MB×3 |

### 4.4 备份策略

| 数据 | 频率 | 保留 | 大小 |
|------|------|------|------|
| PostgreSQL | 每天12:30 | 7天 | 45.9MB |
| Prometheus | 7天+512MB上限 | 自动 | 102MB |
| Grafana | 持久卷 | - | 49.9MB |
| 上传文件 | 每天3:00清理>7天 | 7天 | 1.2GB |

---

## 五、资源使用

### 5.1 当前状态

| 资源 | 使用 | 总量 | 占比 |
|------|------|------|------|
| 内存 | 2.8GB | 9.7GB | 29% |
| 磁盘 | 17GB | 1007GB | 2% |
| CPU | <5% | 6核 | <1% |
| Swap | 0B | 4GB | 0% |

### 5.2 容器内存明细

| 容器 | 内存 | 限制 | 占比 |
|------|------|------|------|
| web | 123MB | 2G | 6.0% |
| grafana | 110MB | 512M | 21.6% |
| beat | 117MB | 256M | 45.6% |
| worker | 82MB | 1G | 8.1% |
| postgres | 74MB | 1G | 7.2% |
| prometheus | 39MB | 512M | 7.7% |
| loki | 38MB | 512M | 7.5% |
| db-backup | 25MB | 256M | 9.7% |
| nginx | 20MB | 256M | 7.9% |
| promtail | 17MB | 256M | 6.8% |
| redis | 10MB | 256M | 4.0% |
| node-exporter | 10MB | 128M | 8.1% |
| postgres-exporter | 9MB | 128M | 7.0% |
| redis-exporter | 9MB | 64M | 13.8% |
| go-parser | 7MB | 512M | 1.3% |
| pgbouncer | 5MB | 128M | 3.6% |

### 5.3 Docker卷

| 卷名 | 大小 | 说明 |
|------|------|------|
| uploads | 1.2GB | 用户上传文件 |
| prometheus_data | 102MB | 监控数据(7天) |
| postgres_data | 77MB | 数据库 |
| grafana_data | 50MB | 仪表盘 |
| db-backups | 46MB | 数据库备份 |
| upload_tmp | 38MB | 临时上传 |
| nginx_cache | 1MB | 图片缓存 |

---

## 六、优化历程

| 序号 | 提交 | 优化内容 | 效果 |
|------|------|---------|------|
| 1 | 322a09c | 内存管理+安全加固+UI统一 | 基础优化 |
| 2 | c1bcc02 | PgBouncer+PG配置修复+Gunicorn调优 | 内存624→504MB |
| 3 | 277c98a | Prometheus保留期+连接池+Celery并发 | 内存504→487MB |
| 4 | 184338a | API缓存+CSRF+静态外部化+错误处理 | DB查询-80%, HTML-92% |
| 5 | f0d9619 | COUNT(*) OVER()→独立COUNT查询 | 消除窗口函数开销 |
| 6 | 48d36ea | 修复Prometheus误告警 | 移除无效scrape |
| 7 | 952e4d0 | 清理101个未使用索引 | 索引166→65, 写入提速 |