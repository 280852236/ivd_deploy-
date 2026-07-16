# Docker网络最佳实践

## 三种方案对比

### ❌ 方案1：动态IP + IP地址（最差）
```yaml
# 问题：重启后IP变化，连接失败
environment:
  DB_HOST: 172.18.0.2  # 动态IP，重启会变
```
**不推荐**

### ✅ 方案2：服务名 + Docker DNS（推荐）
```yaml
# 优点：简洁、自动解析、不受IP影响
environment:
  DB_HOST: postgres
  REDIS_URL: redis://redis:6379/0
```
**前提**：Docker DNS正常工作

### ✅ 方案3：固定IP（稳定可靠）
```yaml
# 优点：永久固定、不依赖DNS
networks:
  ivd_net:
    ipam:
      config:
        - subnet: 172.28.0.0/16

services:
  postgres:
    networks:
      ivd_net:
        ipv4_address: 172.28.0.10

environment:
  DB_HOST: 172.28.0.10
```
**推荐用于生产环境**

### 🌟 方案4：混合方案（最佳）
```yaml
# 既配置固定IP，又使用服务名
networks:
  ivd_net:
    ipam:
      config:
        - subnet: 172.28.0.0/16

services:
  postgres:
    networks:
      ivd_net:
        ipv4_address: 172.28.0.10
    # 服务名自动解析到固定IP

environment:
  # 优先使用服务名（DNS解析）
  DB_HOST: postgres
  # 如果DNS失败，可以fallback到IP
  # DB_HOST: 172.28.0.10
```
**最推荐**

## 为什么选择固定IP？

1. **生产环境稳定性** - 不依赖DNS服务
2. **故障排查方便** - IP固定，日志清晰
3. **网络监控友好** - 固定IP便于防火墙、监控配置
4. **避免DNS问题** - 某些环境下Docker DNS不稳定

## 什么时候用服务名？

1. **开发环境** - 快速迭代，配置简单
2. **单机部署** - Docker DNS通常稳定
3. **服务发现** - 结合Consul、etcd等

## 结论

- **开发环境**：使用服务名（方案2）
- **生产环境**：使用固定IP（方案3）
- **高可用环境**：混合方案（方案4）

当前项目使用**方案3（固定IP）**，适合生产环境部署。
