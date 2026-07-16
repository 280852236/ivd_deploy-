# Docker服务固定IP配置

## 问题说明
Docker容器重启后IP地址会动态变化，导致服务间连接失败。

## 解决方案
为所有服务配置固定IP地址，使用Docker的自定义网络IPAM配置。

## 网络配置
- **网络名称**: ivd_net
- **子网**: 172.28.0.0/16
- **网关**: 172.28.0.1

## 服务IP分配表

| 服务名称 | IP地址 | 说明 |
|---------|--------|------|
| postgres | 172.28.0.10 | PostgreSQL数据库 |
| redis | 172.28.0.11 | Redis缓存/消息队列 |
| go-parser | 172.28.0.12 | Go解析服务 |
| web | 172.28.0.13 | Flask Web服务 |
| worker | 172.28.0.14 | Celery Worker |
| loki | 172.28.0.15 | 日志存储 |
| promtail | 172.28.0.16 | 日志收集 |
| node-exporter | 172.28.0.17 | 系统指标 |
| prometheus | 172.28.0.18 | 指标存储 |
| grafana | 172.28.0.19 | 可视化 |
| nginx | 172.28.0.20 | 反向代理 |

## 配置示例

### docker-compose.yml
```yaml
networks:
  ivd_net:
    driver: bridge
    ipam:
      driver: default
      config:
        - subnet: 172.28.0.0/16
          gateway: 172.28.0.1

services:
  postgres:
    networks:
      ivd_net:
        ipv4_address: 172.28.0.10
```

### 环境变量配置
```yaml
environment:
  DB_HOST: 172.28.0.10          # PostgreSQL
  REDIS_URL: redis://172.28.0.11:6379/0  # Redis
  GO_PARSER_URL: http://172.28.0.12:8082/parse  # Go Parser
```

## 验证方法

### 查看网络配置
```bash
docker network inspect ivd_deploy_ivd_net
```

### 查看服务IP
```bash
docker inspect ivd_deploy-postgres-1 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
```

## 注意事项

1. **重启后IP不变**: 使用固定IP后，容器重启IP地址保持不变
2. **网络重建**: 如果删除网络(`docker compose down`)，需要重新创建
3. **IP冲突**: 确保IP地址不冲突，建议使用连续的IP段
4. **服务依赖**: 服务启动顺序由depends_on控制，不受IP影响

## 修改日期
2026-07-06
