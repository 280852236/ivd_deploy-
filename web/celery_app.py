import os
from celery import Celery
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

celery = Celery(
    'ivd_analyzer',
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=['tasks']   # 显式加载 tasks 模块
)

celery.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='Asia/Shanghai',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,
    task_soft_time_limit=25 * 60,
)

# 删除原有的 celery.autodiscover_tasks(['.']) 这行