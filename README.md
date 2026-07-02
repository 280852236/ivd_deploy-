# IVD故障分析平台 - Docker部署说明

## 🚀 快速开始

### 一键部署
```bash
cd /home/ivduser/ivd_deploy
./deploy.sh
```

### 停止服务
```bash
./stop.sh
```

## 📦 服务架构

```
┌─────────────────────────────────────────────────────┐
│                  Docker Network                      │
│                                                      │
│  ┌──────────┐      ┌──────────┐      ┌──────────┐  │
│  │  Web     │─────▶│  Go      │─────▶│PostgreSQL│  │
│  │  (8081)  │      │Parser    │      │  (5432)  │  │
│  │          │      │  (8082)  │      │          │  │
│  └──────────┘      └──────────┘      └──────────┘  │
│       │                                    │        │
│       │                                    │        │
│       ▼                                    ▼        │
│  ┌──────────┐                        ┌──────────┐  │
│  │  Redis   │                        │  Volume  │  │
│  │  (6379)  │                        │  (数据)  │  │
│  └──────────┘                        └──────────┘  │
│       │                                             │
│       │                                             │
│       ▼                                             │
│  ┌──────────┐                                      │
│  │  Worker  │  Celery异步任务                      │
│  │          │                                      │
│  └──────────┘                                      │
└─────────────────────────────────────────────────────┘
```

## 🔧 服务说明

### 1. PostgreSQL (端口5432)
- 数据库服务
- 自动初始化表结构和默认数据
- 数据持久化存储

### 2. Redis (端口6379)
- 缓存服务
- Celery消息队列
- 分析结果临时存储

### 3. Go Parser (端口8082)
- 文本解析服务
- 正则匹配引擎
- 电机状态码查询

### 4. Web (端口8081)
- Flask Web应用
- 用户界面
- API接口

### 5. Worker
- Celery异步任务
- 文件分析处理
- 后台任务执行

## 📝 配置文件

### docker-compose.yml
主要配置文件，定义所有服务

### web/Dockerfile
Python应用镜像

### go-parser/Dockerfile
Go解析器镜像

### init-db.sql
数据库初始化脚本

## 🔍 常用命令

### 查看日志
```bash
# 所有服务日志
docker-compose logs -f

# 单个服务日志
docker-compose logs -f web
docker-compose logs -f worker
docker-compose logs -f go-parser
docker-compose logs -f postgres
```

### 重启服务
```bash
# 重启所有服务
docker-compose restart

# 重启单个服务
docker-compose restart web
```

### 进入容器
```bash
# 进入Web容器
docker-compose exec web bash

# 进入Go容器
docker-compose exec go-parser sh

# 进入PostgreSQL
docker-compose exec postgres psql -U ivd_user -d ivd_fault_db
```

### 查看状态
```bash
docker-compose ps
docker-compose top
```

## 🛠️ 开发调试

### 重新构建镜像
```bash
# 重新构建所有镜像
docker-compose build --no-cache

# 重新构建单个服务
docker-compose build --no-cache web
docker-compose build --no-cache go-parser
```

### 查看资源使用
```bash
docker stats
```

### 清理数据
```bash
# 停止并删除所有容器、网络、卷
docker-compose down -v

# 清理未使用的镜像
docker image prune -a
```

## ⚠️ 注意事项

1. **首次启动**：数据库初始化需要10-20秒，请耐心等待
2. **端口冲突**：确保8081、8082、5432、6379端口未被占用
3. **数据持久化**：PostgreSQL数据存储在Docker卷中，不会丢失
4. **日志查看**：建议使用`-f`参数实时查看日志

## 🐛 故障排查

### 服务无法启动
```bash
# 查看详细日志
docker-compose logs

# 检查端口占用
netstat -tunlp | grep -E '8081|8082|5432|6379'
```

### 数据库连接失败
```bash
# 检查PostgreSQL状态
docker-compose exec postgres pg_isready -U ivd_user

# 手动连接测试
docker-compose exec postgres psql -U ivd_user -d ivd_fault_db
```

### Go解析器无响应
```bash
# 检查Go服务
curl http://localhost:8082/parse -X POST \
  -H "Content-Type: application/json" \
  -d '{"text":"test","series":"SMART","model":"SMART6500"}'

# 查看Go日志
docker-compose logs go-parser
```

## 📊 性能优化

### 调整Worker并发数
编辑`docker-compose.yml`:
```yaml
worker:
  command: celery -A celery_app worker --loglevel=info --concurrency=4
```

### 调整Web Worker数
编辑`docker-compose.yml`:
```yaml
web:
  command: gunicorn -w 4 -b 0.0.0.0:8081 app:app
```

### 调整数据库连接池
在Web容器环境变量中添加:
```yaml
environment:
  DB_POOL_MIN: 5
  DB_POOL_MAX: 20
```