# IVD智能故障分析平台 - 部署文档

## 文档信息

- **版本**: 3.0.0
- **日期**: 2026-07-06
- **作者**: IVD开发团队
- **状态**: 生产就绪

---

## 目录

1. [系统概述](#1-系统概述)
2. [系统架构](#2-系统架构)
3. [部署要求](#3-部署要求)
4. [快速部署](#4-快速部署)
5. [配置说明](#5-配置说明)
6. [访问地址](#6-访问地址)
7. [功能验证](#7-功能验证)
8. [监控运维](#8-监控运维)
9. [常见问题](#9-常见问题)
10. [维护指南](#10-维护指南)

---

## 1. 系统概述

### 1.1 功能介绍

IVD智能故障分析平台是一个基于机器学习和规则匹配的故障诊断系统，主要功能包括：

- **故障文件分析**: 支持多种格式文件上传和解析（TXT、PDF、LOG等）
- **智能规则匹配**: 基于关键词匹配故障原因和解决方案
- **多型号支持**: 支持SMART6500、SMART500、VENUS系列等多种设备型号
- **数据分析**: 提供故障统计、趋势分析、热力图等可视化功能
- **管理后台**: 规则管理、型号管理、数据导入导出
- **监控告警**: 集成Prometheus + Grafana监控平台

### 1.2 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 后端框架 | Flask | 3.0.0 |
| 异步任务 | Celery | 5.3.4 |
| 数据库 | PostgreSQL | 15 |
| 缓存 | Redis | 7 |
| 解析器 | Go | 1.23 |
| 反向代理 | Nginx | latest |
| 监控 | Prometheus + Grafana | latest |
| 日志聚合 | Loki + Promtail | latest |
| 容器化 | Docker Compose | v2 |

---

## 2. 系统架构

### 2.1 服务架构

```
┌─────────────────────────────────────────────────────────┐
│                    用户访问层                            │
│  HTTPS (8443) → Nginx → Web应用 (Flask)                │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                    应用服务层                            │
│  ┌──────┐  ┌──────┐  ┌──────────┐  ┌────────┐         │
│  │ Web  │  │Worker│  │Go-Parser │  │ Celery │         │
│  │Flask │  │Celery│  │  解析器   │  │Exporter│         │
│  └──────┘  └──────┘  └──────────┘  └────────┘         │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                    数据存储层                            │
│  ┌──────────┐  ┌──────┐  ┌─────────┐                  │
│  │PostgreSQL│  │ Redis│  │ Uploads │                  │
│  │  数据库   │  │ 缓存 │  │ 文件存储 │                  │
│  └──────────┘  └──────┘  └─────────┘                  │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                    监控运维层                            │
│  ┌─────────┐  ┌──────┐  ┌──────┐  ┌────────┐         │
│  │Prometheus│  │Grafana│  │ Loki │  │Promtail│         │
│  │  指标存储 │  │ 可视化│  │日志存储│  │日志收集│         │
│  └─────────┘  └──────┘  └──────┘  └────────┘         │
└─────────────────────────────────────────────────────────┘
```

### 2.2 网络架构

```
Docker网络: ivd_net (172.28.0.0/16)
网关: 172.28.0.1

服务IP分配:
- postgres:      172.28.0.10
- redis:         172.28.0.11
- go-parser:     172.28.0.12
- web:           172.28.0.13
- worker:        172.28.0.14
- loki:          172.28.0.15
- promtail:      172.28.0.16
- node-exporter: 172.28.0.17
- prometheus:    172.28.0.18
- grafana:       172.28.0.19
- nginx:         172.28.0.20
```

### 2.3 数据流

```
用户上传文件
    ↓
Nginx (HTTPS) → Web应用
    ↓
Go解析器 (文件解析)
    ↓
Celery Worker (异步处理)
    ↓
规则匹配引擎 (关键词匹配)
    ↓
数据库存储 (结果保存)
    ↓
前端展示 (结果呈现)
```

---

## 3. 部署要求

### 3.1 硬件要求

| 项目 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | 4核 | 8核 |
| 内存 | 8GB | 16GB |
| 磁盘 | 50GB | 100GB SSD |
| 网络 | 100Mbps | 1Gbps |

### 3.2 软件要求

| 软件 | 版本 | 说明 |
|------|------|------|
| Docker | 20.10+ | 容器运行时 |
| Docker Compose | v2+ | 容器编排工具 |
| OpenSSL | 1.1+ | SSL证书生成 |

### 3.3 端口要求

| 端口 | 服务 | 说明 |
|------|------|------|
| 8081 | Nginx | HTTP端口（重定向到HTTPS） |
| 8443 | Nginx | HTTPS端口 |
| 3000 | Grafana | 监控面板 |
| 9090 | Prometheus | 指标查询 |
| 3100 | Loki | 日志查询 |
| 5432 | PostgreSQL | 数据库（可选暴露） |

---

## 4. 快速部署

### 4.1 部署前准备

```bash
# 1. 检查Docker版本
docker --version
docker compose version

# 2. 检查磁盘空间
df -h

# 3. 检查端口占用
netstat -tlnp | grep -E '8081|8443|3000|9090|5432'
```

### 4.2 一键部署

```bash
# 进入部署目录
cd /home/ivduser/ivd_deploy

# 启动所有服务
docker compose up -d

# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f
```

### 4.3 验证部署

```bash
# 1. 检查容器状态（应该全部Running）
docker compose ps

# 2. 检查健康状态
curl -k https://127.0.0.1:8443/api/health

# 3. 检查数据库连接
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "SELECT COUNT(*) FROM rules;"

# 4. 检查监控
curl http://127.0.0.1:9090/api/v1/targets
```

---

## 5. 配置说明

### 5.1 HTTPS配置

**证书文件位置：**
```
/home/ivduser/ivd_deploy/ssl/
├── ivd.crt  # SSL证书
└── ivd.key  # 私钥文件
```

**证书信息：**
- 类型：自签名证书
- 有效期：365天
- 加密：TLS 1.2/1.3
- 算法：RSA 2048位

**更新证书：**
```bash
# 重新生成证书
cd /home/ivduser/ivd_deploy/ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ivd.key -out ivd.crt \
  -subj "/C=CN/ST=Beijing/L=Beijing/O=IVD/OU=IT/CN=172.22.68.201"

# 重启Nginx
docker compose restart nginx
```

### 5.2 数据库配置

**连接信息：**
```yaml
主机: postgres (服务名) 或 172.28.0.10 (固定IP)
端口: 5432
数据库: ivd_fault_db
用户名: ivd_user
密码: ivd_pass
```

**数据库表：**
- `rules` - 故障规则表
- `models` - 设备型号表
- `series` - 设备系列表
- `motor_status_*` - 电机状态表（按型号分表）
- `rule_keywords` - 规则关键词表
- `version_history` - 版本历史表

### 5.3 环境变量

**Web服务环境变量：**
```bash
DB_HOST=postgres                    # 数据库主机
DB_HOST_IP=172.28.0.10              # 数据库IP（fallback）
REDIS_HOST=redis                    # Redis主机
REDIS_HOST_IP=172.28.0.11           # Redis IP（fallback）
SECRET_KEY=your-secret-key          # Flask密钥
ADMIN_PASSWORD=admin123             # 管理员密码
MAX_CONTENT_LENGTH=209715200        # 最大上传200MB
ANALYSIS_TTL_HOURS=2                # 分析结果保留时间
```

### 5.4 Nginx配置

**配置文件：** `/home/ivduser/ivd_deploy/nginx.conf`

**关键配置：**
```nginx
# HTTP重定向到HTTPS
server {
    listen 80;
    return 301 https://$host:8443$request_uri;
}

# HTTPS服务
server {
    listen 443 ssl;
    ssl_certificate /etc/nginx/ssl/ivd.crt;
    ssl_certificate_key /etc/nginx/ssl/ivd.key;
    client_max_body_size 200M;  # 最大上传200MB
}
```

---

## 6. 访问地址

### 6.1 应用访问

| 服务 | 地址 | 说明 |
|------|------|------|
| IVD应用 | https://172.22.68.201:8443 | 主应用入口 |
| 管理后台 | https://172.22.68.201:8443/admin/login | 规则管理 |
| API文档 | https://172.22.68.201:8443/api/health | 健康检查 |

**登录信息：**
- 管理员密码：`admin123`

### 6.2 监控访问

| 服务 | 地址 | 说明 |
|------|------|------|
| Grafana | http://172.22.68.201:3000 | 监控面板 |
| Prometheus | http://172.22.68.201:9090 | 指标查询 |
| Loki | http://172.22.68.201:3100 | 日志查询 |

**登录信息：**
- Grafana：`admin` / `admin`

### 6.3 浏览器证书警告

首次访问HTTPS会看到证书警告，这是**正常现象**。

**处理方法：**

Chrome/Edge：
1. 点击"高级"
2. 点击"继续访问 172.22.68.201（不安全）"

Firefox：
1. 点击"高级"
2. 点击"接受风险并继续"

---

## 7. 功能验证

### 7.1 API验证

```bash
# 1. 健康检查
curl -k https://172.22.68.201:8443/api/health

# 2. 获取型号列表
curl -k "https://172.22.68.201:8443/api/models?series=SMART"

# 3. 获取规则列表
curl -k "https://172.22.68.201:8443/api/rules?series=SMART&model=SMART6500"

# 4. 获取电机状态
curl -k "https://172.22.68.201:8443/api/motor_status?model=SMART6500&limit=10"
```

### 7.2 数据验证

```bash
# 检查规则数量
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "SELECT COUNT(*) FROM rules;"

# 检查型号数量
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "SELECT COUNT(*) FROM models;"

# 检查状态数据
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "SELECT COUNT(*) FROM motor_status_smart6500;"
```

### 7.3 功能测试

**测试步骤：**
1. 访问 https://172.22.68.201:8443
2. 选择设备型号（如SMART6500）
3. 上传故障文件（TXT/PDF/LOG）
4. 查看分析结果
5. 查看匹配的规则和建议

---

## 8. 监控运维

### 8.1 Grafana面板

**预置面板：**
- IVD监控总览（中文）
- IVD业务分析（中文）
- Node Exporter Full（系统监控）

**访问Grafana：**
1. 打开 http://172.22.68.201:3000
2. 登录：admin / admin
3. 选择Dashboard查看

### 8.2 Prometheus指标

**可用指标：**
- `node_cpu_seconds_total` - CPU使用率
- `node_memory_MemAvailable_bytes` - 可用内存
- `node_disk_io_time_seconds_total` - 磁盘IO
- `node_network_receive_bytes_total` - 网络接收

**查询示例：**
```promql
# CPU使用率
100 - (avg by(instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# 内存使用率
(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100

# 磁盘使用率
(1 - (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})) * 100
```

### 8.3 日志查看

**Docker日志：**
```bash
# 查看所有服务日志
docker compose logs -f

# 查看特定服务日志
docker compose logs -f web
docker compose logs -f worker
docker compose logs -f nginx
```

**Loki日志查询：**
在Grafana中使用Explore功能，选择Loki数据源：
```logql
# 查看web服务日志
{container="ivd_deploy-web-1"}

# 查看错误日志
{container="ivd_deploy-web-1"} |= "error"
```

---

## 9. 常见问题

### 9.1 服务无法启动

**问题：** 容器启动失败

**解决：**
```bash
# 1. 查看日志
docker compose logs 服务名

# 2. 检查端口占用
netstat -tlnp | grep 端口号

# 3. 检查磁盘空间
df -h

# 4. 重启服务
docker compose restart 服务名
```

### 9.2 数据库连接失败

**问题：** 无法连接PostgreSQL

**解决：**
```bash
# 1. 检查PostgreSQL状态
docker compose ps postgres

# 2. 检查健康状态
docker exec ivd_deploy-postgres-1 pg_isready -U ivd_user

# 3. 检查网络连接
docker exec ivd_deploy-web-1 ping postgres

# 4. 查看日志
docker compose logs postgres
```

### 9.3 HTTPS证书问题

**问题：** 浏览器显示证书警告

**解决：**
这是正常现象，自签名证书会触发警告。点击"高级" → "继续访问"即可。

**如需消除警告：**
1. 导入证书到浏览器信任列表
2. 或使用Let's Encrypt免费证书
3. 或购买商业SSL证书

### 9.4 文件上传失败

**问题：** 上传文件时出错

**解决：**
```bash
# 1. 检查文件大小（最大200MB）
ls -lh 文件路径

# 2. 检查Nginx配置
docker exec ivd_deploy-nginx-1 nginx -t

# 3. 检查磁盘空间
df -h

# 4. 查看错误日志
docker compose logs web | grep error
```

### 9.5 规则匹配失败

**问题：** 无法匹配故障规则

**解决：**
```bash
# 1. 检查规则数据
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "SELECT * FROM rules LIMIT 5;"

# 2. 检查关键词
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "SELECT * FROM rule_keywords LIMIT 10;"

# 3. 查看匹配日志
docker compose logs web | grep -i match
```

---

## 10. 维护指南

### 10.1 日常维护

**每日检查：**
```bash
# 1. 检查服务状态
docker compose ps

# 2. 检查磁盘使用
df -h

# 3. 检查内存使用
free -h

# 4. 检查错误日志
docker compose logs --since 24h | grep -i error
```

**每周维护：**
```bash
# 1. 清理旧日志
docker system prune -f

# 2. 备份数据库
docker exec ivd_deploy-postgres-1 pg_dump -U ivd_user ivd_fault_db > backup_$(date +%Y%m%d).sql

# 3. 检查证书有效期
openssl x509 -in /home/ivduser/ivd_deploy/ssl/ivd.crt -noout -dates
```

### 10.2 数据备份

**备份数据库：**
```bash
# 完整备份
docker exec ivd_deploy-postgres-1 pg_dump -U ivd_user ivd_fault_db > backup_$(date +%Y%m%d).sql

# 仅备份规则
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "COPY rules TO STDOUT WITH CSV HEADER" > rules_backup.csv
```

**恢复数据库：**
```bash
# 恢复完整备份
cat backup_20260706.sql | docker exec -i ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db
```

### 10.3 性能优化

**数据库优化：**
```bash
# 清理和分析
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "VACUUM ANALYZE;"

# 重建索引
docker exec ivd_deploy-postgres-1 psql -U ivd_user -d ivd_fault_db -c "REINDEX DATABASE ivd_fault_db;"
```

**Docker优化：**
```bash
# 清理未使用资源
docker system prune -a -f

# 查看资源使用
docker stats
```

### 10.4 升级更新

**升级步骤：**
```bash
# 1. 备份数据
docker exec ivd_deploy-postgres-1 pg_dump -U ivd_user ivd_fault_db > backup_before_upgrade.sql

# 2. 停止服务
docker compose down

# 3. 拉取最新代码
git pull

# 4. 重新构建
docker compose build

# 5. 启动服务
docker compose up -d

# 6. 验证升级
curl -k https://127.0.0.1:8443/api/health
```

### 10.5 故障排查

**排查流程：**
```
1. 检查容器状态
   ↓
2. 查看服务日志
   ↓
3. 检查网络连接
   ↓
4. 检查资源使用
   ↓
5. 检查配置文件
   ↓
6. 重启服务
```

**常用命令：**
```bash
# 查看所有容器状态
docker compose ps

# 查看资源使用
docker stats

# 查看网络
docker network ls
docker network inspect ivd_deploy_ivd_net

# 进入容器调试
docker exec -it ivd_deploy-web-1 /bin/bash

# 查看进程
docker top ivd_deploy-web-1
```

---

## 附录

### A. 配置文件清单

```
/home/ivduser/ivd_deploy/
├── docker-compose.yml          # Docker编排配置
├── nginx.conf                  # Nginx配置
├── prometheus.yml              # Prometheus配置
├── loki-config.yaml           # Loki配置
├── promtail-config.yaml       # Promtail配置
├── ssl/                       # SSL证书目录
│   ├── ivd.crt
│   └── ivd.key
├── grafana/                   # Grafana配置
│   ├── grafana.ini
│   ├── provisioning/
│   └── dashboards/
└── web/                       # Web应用代码
    ├── app.py
    ├── tasks.py
    ├── Dockerfile
    └── requirements.txt
```

### B. 环境变量清单

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| DB_HOST | postgres | 数据库主机 |
| DB_PORT | 5432 | 数据库端口 |
| DB_USER | ivd_user | 数据库用户 |
| DB_PASSWORD | ivd_pass | 数据库密码 |
| DB_NAME | ivd_fault_db | 数据库名 |
| REDIS_HOST | redis | Redis主机 |
| SECRET_KEY | your-secret-key | Flask密钥 |
| ADMIN_PASSWORD | admin123 | 管理员密码 |
| MAX_CONTENT_LENGTH | 209715200 | 最大上传200MB |
| ANALYSIS_TTL_HOURS | 2 | 结果保留时间 |

### C. API接口清单

| 接口 | 方法 | 说明 |
|------|------|------|
| /api/health | GET | 健康检查 |
| /api/series | GET | 获取系列列表 |
| /api/models | GET | 获取型号列表 |
| /api/rules | GET | 获取规则列表 |
| /api/rules | POST | 创建规则 |
| /api/rules/:id | PUT | 更新规则 |
| /api/rules/:id | DELETE | 删除规则 |
| /api/motor_status | GET | 获取电机状态 |
| /api/analyze | POST | 分析文件 |
| /api/task_status/:id | GET | 查询任务状态 |
| /api/analysis/:id | GET | 获取分析结果 |

### D. 联系方式

- **技术支持**: IVD开发团队
- **文档更新**: 2026-07-06
- **版本**: 3.0.0

---

**文档结束**