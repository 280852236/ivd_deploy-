import re
import json
from typing import Dict, List

from psycopg2.extras import RealDictCursor
from services.db import db_connection
from services.cache import get_redis

REAGENT_COOLING_RULES = [
    {'keywords': ['温度异常', '温度超限', '温度失控'], 'advice': '🔧 排查：检查温度传感器、TEC制冷片供电', 'source': '制冷排查'},
    {'keywords': ['TEC', 'tec', '珀尔帖', '制冷片'], 'advice': '🔧 排查：检查TEC制冷片工作状态、驱动电流是否正常', 'source': '制冷排查'},
    {'keywords': ['制冷', '冷端', '散热'], 'advice': '🔧 排查：检查制冷模块散热器、风扇是否正常运转', 'source': '制冷排查'},
    {'keywords': ['试剂温度', '试剂制冷'], 'advice': '🔧 排查：检查试剂仓制冷模块、温度传感器校准', 'source': '制冷排查'},
    {'keywords': ['过热', '过温', '高温报警'], 'advice': '🔧 排查：检查制冷系统是否失效、散热通道是否堵塞', 'source': '制冷排查'},
    {'keywords': ['ADC', 'adc', 'NTC', 'ntc'], 'advice': '🔧 排查：检查温度采集ADC/NTC传感器读数是否异常', 'source': '制冷排查'},
    {'keywords': ['PID', 'pid'], 'advice': '🔧 排查：检查温度控制PID参数是否异常', 'source': '制冷排查'},
]


def get_rules(series: str, model: str) -> List[Dict]:
    cache_key = f"rules:{series}:{model}"
    try:
        r = get_redis()
        cached = r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT r.id, r.keywords, r.advice, r.source
            FROM rules r
            JOIN models m ON r.model_id = m.id
            JOIN series s ON m.series_id = s.id
            WHERE UPPER(s.name) = UPPER(%s) AND m.name = %s
        ''', (series, model))
        rows = cur.fetchall()
        rules = [
            {
                'id': row['id'],
                'keywords': [kw.strip() for kw in row['keywords'].split(',') if kw.strip()],
                'advice': row['advice'],
                'source': row['source']
            }
            for row in rows
        ]
    try:
        r = get_redis()
        r.setex(cache_key, 300, json.dumps(rules, ensure_ascii=False))
    except Exception:
        pass
    return rules


def clear_rules_cache(series: str = None, model: str = None):
    try:
        r = get_redis()
        if series and model:
            r.delete(f"rules:{series}:{model}")
        else:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match="rules:*", count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break
    except Exception:
        pass
