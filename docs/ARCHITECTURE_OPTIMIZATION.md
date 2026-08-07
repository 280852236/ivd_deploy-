# IVD平台架构优化方案

## 一、当前架构

```
用户 → Nginx(8443) → Gunicorn(4 workers, gevent) → Flask App
                                                      ↓
                                              Celery Worker(4并发) → Go-parser
                                                      ↓
                                              Redis(缓存+队列) + PostgreSQL(持久化)
```

## 二、已实施优化

| # | 优化项 | 文件 | 预期收益 |
|---|--------|------|----------|
| 1 | 数据库GIN全文索引+时间索引 | scripts/add_performance_indexes.sql | 搜索5-10x |
| 2 | 全局变量缓存→Redis(1h TTL) | services/match.py | 多进程共享缓存 |
| 3 | scan_iter→精确key删除 | services/rules.py | 减少Redis阻塞 |
| 4 | Gunicorn 2→4 worker + gevent | docker-compose.yml | 并发2-3x |
| 5 | Celery 2→4并发, max_tasks 100→500 | docker-compose.yml + celery_app.py | 吞吐2-3x |
| 6 | Redis Hash存储文件内容 | services/analysis.py | key数-80% |
| 7 | Aho-Corasick多关键词搜索 | services/analyzer.py | 搜索5-10x |
| 8 | Go-parser日志+超时分类 | services/analyzer.py | 可观测性 |
| 9 | 编码检测公共函数 | services/file_service.py | 代码复用 |
| 10 | str.translate替代生成器 | services/text_utils.py | 字符过滤3-5x |
| 11 | 正则预编译 | services/text_utils.py | 热路径10-20% |

## 三、长期架构优化建议

### 3.1 服务拆分（微服务化）

当前Web服务职责过重，建议拆分：

```
API Gateway (认证/限流/路由)
    ├── Analysis Service (文件解析/故障分析)
    ├── Data Service (规则/Bug/硬件故障CRUD)
    └── Search Service (全文搜索/结果聚合)
```

**实施路径**：
1. 先将Analysis Service独立部署（最耗资源的部分）
2. Data Service保持内嵌（CRUD操作简单）
3. Search Service可后期引入Elasticsearch

### 3.2 数据流优化

**当前**：用户上传 → Web保存临时文件 → Celery读取 → 分析 → Redis存储 → 前端轮询

**建议**：
1. 上传文件直接存MinIO/S3对象存储，减轻Web服务I/O压力
2. 使用WebSocket/SSE替代轮询，实时推送分析进度
3. Celery任务增加进度反馈（`self.update_state(state='PROGRESS')`）

### 3.3 PostgreSQL高级特性

1. **全文搜索替代ILIKE**：已在数据库层添加GIN索引，后续可在应用层使用`to_tsquery`替代ILIKE
2. **JSONB存储分析结果**：减少Redis内存压力，支持历史数据查询
3. **物化视图**：对Bug列表+图片计数等复杂查询使用物化视图

### 3.4 监控增强

1. 添加Celery任务失败告警（`task_failure`信号）
2. 添加Redis内存使用监控
3. 添加数据库慢查询日志（`log_min_duration_statement = 500`）

## 四、性能基准

优化后预期性能：

| 指标 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| Bug搜索响应 | ~3s | ~0.3s | 10x |
| 文件分析并发 | 2 | 8 | 4x |
| Redis key数(500文件) | 500+ | 2 | 250x |
| 关键词搜索(50个) | O(50n) | O(n) | 50x |
| Web并发处理 | 2 worker | 4 gevent | 2-4x |