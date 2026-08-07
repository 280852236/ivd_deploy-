import os
import re
import socket
import logging

logger = logging.getLogger(__name__)

_RE_REDIS_URL = re.compile(r'redis://([^:]+):(\d+)')
_RE_HTTP_URL = re.compile(r'http://([^:]+):(\d+)')

def test_connection(host, port, timeout=2):
    """测试主机连接是否可用"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

def get_db_host():
    service_name = os.environ.get('DB_HOST', 'postgres')
    fallback_ip = os.environ.get('DB_HOST_IP', '172.28.0.10')
    port = int(os.environ.get('DB_PORT', 5432))
    if os.environ.get('DOCKER_ENV') == 'true' or os.path.exists('/.dockerenv'):
        logger.info(f"✅ Docker环境，使用服务名连接数据库: {service_name}")
        return service_name
    if test_connection(service_name, port):
        logger.info(f"✅ 使用服务名连接数据库: {service_name}")
        return service_name
    else:
        logger.warning(f"⚠️  服务名连接失败，使用固定IP: {fallback_ip}")
        return fallback_ip

def get_redis_url():
    primary_url = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
    fallback_url = os.environ.get('REDIS_URL_IP', 'redis://172.28.0.11:6379/0')
    if os.environ.get('DOCKER_ENV') == 'true' or os.path.exists('/.dockerenv'):
        logger.info(f"✅ Docker环境，使用服务名连接Redis")
        return primary_url
    match = _RE_REDIS_URL.search(primary_url)
    if match:
        host, port = match.groups()
        if test_connection(host, int(port)):
            logger.info(f"✅ 使用服务名连接Redis: {host}")
            return primary_url
    logger.warning(f"⚠️  服务名连接失败，使用固定IP")
    return fallback_url

def get_go_parser_url():
    primary_url = os.environ.get('GO_PARSER_URL', 'http://go-parser:8082/parse')
    fallback_url = os.environ.get('GO_PARSER_URL_IP', 'http://172.28.0.12:8082/parse')
    if os.environ.get('DOCKER_ENV') == 'true' or os.path.exists('/.dockerenv'):
        logger.info(f"✅ Docker环境，使用服务名连接Go Parser")
        return primary_url
    match = _RE_HTTP_URL.search(primary_url)
    if match:
        host, port = match.groups()
        if test_connection(host, int(port)):
            logger.info(f"✅ 使用服务名连接Go Parser: {host}")
            return primary_url
    logger.warning(f"⚠️  服务名连接失败，使用固定IP")
    return fallback_url

# 导出便捷函数
__all__ = ['get_db_host', 'get_redis_url', 'get_go_parser_url', 'test_connection']
