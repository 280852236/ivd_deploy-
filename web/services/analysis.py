import os
import json
import time
import zlib
import base64
import zipfile
import rarfile
import tempfile
import logging
from typing import Dict, List
from shared import Config
from services.db import db_connection
from services.cache import get_redis

logger = logging.getLogger(__name__)

_COMPRESS_THRESHOLD = 4096
_COMPRESS_PREFIX = 'z:'  # base64编码的压缩数据前缀，兼容decode_responses=True


def _compress_value(data_str: str) -> str:
    """压缩JSON字符串：大于阈值时zlib压缩+base64编码，返回合法UTF-8字符串"""
    raw = data_str.encode('utf-8')
    if len(raw) > _COMPRESS_THRESHOLD:
        compressed = zlib.compress(raw, 6)
        if len(compressed) + len(_COMPRESS_PREFIX) < len(raw):
            return _COMPRESS_PREFIX + base64.b64encode(compressed).decode('ascii')
    return data_str


def _decompress_value(data) -> str:
    """解压：如果是压缩数据(前缀z:)则base64解码+zlib解压，否则原样返回"""
    if isinstance(data, bytes):
        # decode_responses=True时一般不会走到这里，但做兜底
        try:
            data = data.decode('utf-8')
        except UnicodeDecodeError:
            # 极端情况：旧格式二进制压缩数据，尝试旧格式解压
            if data and data[0:1] == b'\x01':
                return zlib.decompress(data[1:]).decode('utf-8')
            raise
    if data and data.startswith(_COMPRESS_PREFIX):
        compressed = base64.b64decode(data[len(_COMPRESS_PREFIX):])
        return zlib.decompress(compressed).decode('utf-8')
    return data


def store_analysis_result(analysis_id, data):
    for attempt in range(3):
        try:
            r = get_redis()
            key = f"analysis:{analysis_id}"
            files_key = f"analysis:{analysis_id}:files"
            files = data.get('files', {})
            ttl = Config.ANALYSIS_TTL_HOURS * 3600
            pipe = r.pipeline()
            if files:
                mapping = {}
                for filename, file_data in files.items():
                    raw_str = json.dumps(file_data, ensure_ascii=False)
                    mapping[filename] = _compress_value(raw_str)
                pipe.hset(files_key, mapping=mapping)
                pipe.expire(files_key, ttl)
            data_without_files = {k: v for k, v in data.items() if k != 'files'}
            data_without_files['has_separate_files'] = bool(files)
            data_without_files['file_names'] = list(files.keys())
            main_str = json.dumps(data_without_files, ensure_ascii=False)
            pipe.set(key, _compress_value(main_str), ex=ttl)
            pipe.execute()
            _persist_to_pg(analysis_id, data)
            return
        except Exception as e:
            logger.warning(f"store_analysis_result 第{attempt+1}次失败: {e}")
            if attempt == 2:
                raise
            time.sleep(0.1 * (2 ** attempt))


def _persist_to_pg(analysis_id, data):
    try:
        summary = data.get('summary', {})
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO analysis_results (analysis_id, series, model, analysis_type, summary, total_dates, total_files, matched_count, analyzed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (analysis_id) DO UPDATE SET
                    summary = EXCLUDED.summary,
                    total_dates = EXCLUDED.total_dates,
                    total_files = EXCLUDED.total_files,
                    matched_count = EXCLUDED.matched_count
            """, (
                analysis_id,
                data.get('series', ''),
                data.get('model', ''),
                data.get('analysis_type', ''),
                json.dumps(summary, ensure_ascii=False) if summary else None,
                data.get('total_dates', 0),
                data.get('total_files', 0),
                data.get('matched_count', 0),
                data.get('analyzed_at', ''),
            ))
    except Exception as e:
        logger.debug(f"PG持久化分析结果失败(非关键): {e}")


def get_analysis_result(analysis_id, include_files=False):
    for attempt in range(3):
        try:
            r = get_redis()
            key = f"analysis:{analysis_id}"
            data = r.get(key)
            if data:
                result = json.loads(_decompress_value(data))
                if include_files and result.get('has_separate_files'):
                    file_names = result.get('file_names', [])
                    if file_names:
                        files_key = f"analysis:{analysis_id}:files"
                        all_files = r.hgetall(files_key)
                        result['files'] = {}
                        for filename in file_names:
                            raw = all_files.get(filename)
                            if raw:
                                result['files'][filename] = json.loads(_decompress_value(raw))
                return result
            break
        except Exception as e:
            logger.warning(f"get_analysis_result Redis第{attempt+1}次失败: {e}")
            if attempt == 2:
                break
            time.sleep(0.1 * (2 ** attempt))
    return _load_from_pg(analysis_id)


def _load_from_pg(analysis_id):
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT series, model, analysis_type, summary, total_dates, total_files, matched_count, analyzed_at FROM analysis_results WHERE analysis_id = %s", (analysis_id,))
            row = cur.fetchone()
            if not row:
                return None
            series, model, analysis_type, summary_raw, total_dates, total_files, matched_count, analyzed_at = row
            summary = json.loads(summary_raw) if isinstance(summary_raw, str) else (summary_raw or {})
            result = {
                'series': series or '',
                'model': model or '',
                'analysis_type': analysis_type or '',
                'summary': summary,
                'total_dates': total_dates or 0,
                'total_files': total_files or 0,
                'matched_count': matched_count or 0,
                'analyzed_at': analyzed_at or '',
                'date_groups': [],
                'files': {},
                'file_names': [],
                'has_separate_files': False,
                'from_pg': True,
            }
            return result
    except Exception as e:
        logger.debug(f"PG降级读取分析结果失败: {e}")
        return None


def get_file_content(analysis_id, filename):
    for attempt in range(3):
        try:
            r = get_redis()
            files_key = f"analysis:{analysis_id}:files"
            content = r.hget(files_key, filename)
            if content:

                return json.loads(_decompress_value(content))
            return None
        except Exception as e:
            logger.warning(f"get_file_content 第{attempt+1}次失败: {e}")
            if attempt == 2:
                return None
            time.sleep(0.1 * (2 ** attempt))


def delete_analysis_result(analysis_id):
    for attempt in range(3):
        try:
            r = get_redis()
            key = f"analysis:{analysis_id}"
            files_key = f"analysis:{analysis_id}:files"
            data = r.get(key)
            if data:

                parsed = json.loads(_decompress_value(data))
                temp_path = parsed.get('temp_zip_path')
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logger.info(f"已清理临时ZIP文件: {temp_path}")
                    except Exception as e:
                        logger.warning(f"清理临时ZIP文件失败: {temp_path} - {e}")
                pipe = r.pipeline()
                pipe.delete(key)
                pipe.delete(files_key)
                pipe.execute()
            try:
                with db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM analysis_results WHERE analysis_id = %s", (analysis_id,))
            except Exception:
                pass
            return
        except Exception as e:
            logger.warning(f"delete_analysis_result 第{attempt+1}次失败: {e}")
            if attempt == 2:
                return
