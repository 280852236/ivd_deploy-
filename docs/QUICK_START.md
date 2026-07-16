# IVD智能故障分析平台 - 快速开始指南

## 🚀 5分钟快速部署

### 1. 启动服务

```bash
cd /home/ivduser/ivd_deploy
docker compose up -d
```

### 2. 验证服务

```bash
# 检查容器状态（应该全部Running）
docker compose ps

# 检查健康状态
curl -k https://127.0.0.1:8443/api/health
```

### 3. 访问应用

**主应用：** https://172.22.68.201:8443

**管理后台：** https://172.22.68.201:8443/admin/login
- 密码：`admin123`

**监控面板：** http://172.22.68.201:3000
- 用户名：`admin`
- 密码：`admin`

---

## ⚠️ 浏览器证书警告

首次访问HTTPS会看到证书警告，这是**正常现象**。

**处理方法：**
1. 点击"高级"
2. 点击"继续访问 172.22.68.201（不安全）"

---

## 📊 核心功能

### 文件分析
1. 选择设备型号（SMART6500、SMART500等）
2. 上传故障文件（TXT/PDF/LOG）
3. 查看分析结果和匹配规则

### 规则管理
1. 访问管理后台
2. 添加/编辑故障规则
3. 设置关键词和建议

### 监控运维
1. 访问Grafana监控面板
2. 查看系统资源使用
3. 查看应用性能指标

---

## 🔧 常用命令

```bash
# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f

# 重启服务
docker compose restart

# 停止服务
docker compose down

# 备份数据库
docker exec ivd_deploy-postgres-1 pg_dump -U ivd_user ivd_fault_db > backup.sql
```

---

## 📞 技术支持

详细文档请查看：`DEPLOYMENT_GUIDE.md`

---

**快速开始完成！**