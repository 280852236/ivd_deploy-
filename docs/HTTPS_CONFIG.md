# HTTPS安全配置说明

## 配置概述

已为IVD智能故障分析平台配置HTTPS安全访问，提升数据传输安全性。

## 证书信息

- **证书类型**: 自签名证书（适合内网环境）
- **有效期**: 365天
- **证书文件**: `/home/ivduser/ivd_deploy/ssl/ivd.crt`
- **私钥文件**: `/home/ivduser/ivd_deploy/ssl/ivd.key`
- **加密算法**: RSA 2048位
- **TLS版本**: TLSv1.2, TLSv1.3

## 访问地址

### HTTP访问（自动重定向到HTTPS）
- http://172.22.68.201:8081

### HTTPS访问（推荐）
- **IVD应用**: https://172.22.68.201:8443
- **管理后台**: https://172.22.68.201:8443/admin/login
- **用户名**: admin
- **密码**: admin123

### 其他服务（保持HTTP）
- **Grafana监控**: http://172.22.68.201:3000 (admin/admin)
- **Prometheus**: http://172.22.68.201:9090

## 浏览器证书警告

由于使用自签名证书，浏览器会显示安全警告。这是正常现象，解决方法：

### Chrome/Edge
1. 点击"高级"
2. 点击"继续访问 172.22.68.201（不安全）"

### Firefox
1. 点击"高级"
2. 点击"接受风险并继续"

### Safari
1. 点击"显示详细信息"
2. 点击"访问此网站"

## 安全优势

1. **数据加密**: 所有传输数据（文件上传、密码、分析结果）均加密
2. **防止窃听**: 防止中间人攻击和数据泄露
3. **符合规范**: 满足等保2.0安全要求
4. **零成本**: 使用自签名证书，无需购买商业证书

## 生产环境建议

如果需要公网访问或消除浏览器警告，建议：

1. **使用Let's Encrypt免费证书**（需要域名）
   ```bash
   # 安装certbot
   apt-get install certbot
   
   # 申请证书
   certbot certonly --standalone -d your-domain.com
   ```

2. **购买商业SSL证书**（适合企业环境）
   - 阿里云、腾讯云等云服务商提供SSL证书服务
   - 价格：约100-1000元/年

3. **使用企业内部CA证书**
   - 适合有内部PKI体系的企业

## 证书更新

证书有效期1年，到期前需要更新：

```bash
# 重新生成证书
cd /home/ivduser/ivd_deploy/ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ivd.key -out ivd.crt \
  -subj "/C=CN/ST=Beijing/L=Beijing/O=IVD/OU=IT/CN=172.22.68.201"

# 重启Nginx
docker compose restart nginx
```

## 技术实现

### Nginx配置
- HTTP端口80自动重定向到HTTPS
- HTTPS端口443提供安全访问
- 支持大文件上传（200MB）
- 支持WebSocket连接

### Docker配置
- 挂载SSL证书目录到容器
- 暴露HTTP和HTTPS端口
- 自动重启策略

## 验证HTTPS

启动服务后验证：

```bash
# 检查Nginx配置
docker compose exec nginx nginx -t

# 测试HTTPS连接
curl -k https://172.22.68.201:8443

# 查看证书信息
openssl s_client -connect 172.22.68.201:8443 -showcerts
```

## 注意事项

1. **首次访问**: 需要接受浏览器证书警告
2. **端口变更**: HTTPS使用8443端口（原HTTP端口8081会重定向）
3. **内网环境**: 自签名证书适合内网，公网建议使用正规证书
4. **证书备份**: 建议备份ssl目录，避免重新生成后需要重新信任

## 故障排查

### 无法访问HTTPS
```bash
# 检查Nginx容器状态
docker compose ps nginx

# 查看Nginx日志
docker compose logs nginx

# 检查证书文件
ls -lh /home/ivduser/ivd_deploy/ssl/
```

### 证书错误
```bash
# 验证证书格式
openssl x509 -in /home/ivduser/ivd_deploy/ssl/ivd.crt -text -noout

# 验证私钥
openssl rsa -in /home/ivduser/ivd_deploy/ssl/ivd.key -check
```