import os
import logging
from celery import Celery
from celery.signals import after_setup_logger
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')

try:
    import gevent.monkey
    gevent.monkey.patch_all()

except ImportError:
    pass
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')


celery = Celery(
    'ivd_analyzer',
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=['tasks']
)


@after_setup_logger.connect
def _setup_json_logger(logger, **kwargs):
    import shared
    _fmt = shared.JsonFormatter(datefmt='%Y-%m-%dT%H:%M:%S')
    for h in logger.handlers:
        h.setFormatter(_fmt)
    for h in logging.root.handlers:
        h.setFormatter(_fmt)


celery.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='Asia/Shanghai',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,
    task_soft_time_limit=25 * 60,
    task_send_sent_event=True,
    result_expires=int(os.getenv('ANALYSIS_TTL_HOURS', '2')) * 3600,
    worker_max_tasks_per_child=500,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={
        'max_connections': 25,
        'visibility_timeout': 3600,
    },
)

from celery.schedules import crontab
celery.conf.beat_schedule = {
    'cleanup-expired-zip-every-10-minutes': {
        'task': 'cleanup_expired_zip_files',
        'schedule': crontab(minute='*/10'),
    },
    'cleanup-old-uploads-daily': {
        'task': 'cleanup_old_uploads',
        'schedule': crontab(hour='3', minute='0'),
    },
    'memory-cleanup-every-30-minutes': {
        'task': 'memory_cleanup',
        'schedule': crontab(minute='*/30'),
    },
}
