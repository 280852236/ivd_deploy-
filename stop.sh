#!/bin/bash
cd "$(dirname "$0")"
echo "停止IVD智能故障分析平台..."
docker compose down --remove-orphans
echo "已停止"
