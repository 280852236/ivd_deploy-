import os
import tempfile
import uuid
import shutil
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from celery_app import celery
from services.file_service import (
    process_text_file_from_bytes,
    extract_archive_metadata,
    build_date_groups_and_summary,
    _detect_file_type,
    _decode_bytes,
)
from services.text_utils import check_aspiration_anomaly, strip_control_chars
from services.rules import get_rules, REAGENT_COOLING_RULES
from services.analysis import store_analysis_result
from services.go_parser import analyze_zip
import shared
import logging

logger = logging.getLogger(__name__)

_MAX_QUICK_SCAN_LINES = 200

def _quick_scan_aspiration_files(archive_path, aspiration_metas, all_files, archive_type='ZIP'):
    import zipfile
    try:
        import rarfile
    except ImportError:
        rarfile = None
    try:
        if archive_type == 'ZIP':
            zf = zipfile.ZipFile(archive_path)
            ctx = zf
        else:
            if not rarfile:
                return
            ctx = rarfile.RarFile(archive_path)
        with ctx:
            for meta in aspiration_metas:
                name = meta['name']
                raw_name = meta.get('raw_name', name)
                file_size = meta.get('size', 0)
                try:
                    if file_size > 5 * 1024 * 1024:
                        continue
                    raw = ctx.read(raw_name)
                    content = _decode_bytes(raw, name)
                    content = strip_control_chars(content)
                    has_match = False
                    for line in content.splitlines()[:_MAX_QUICK_SCAN_LINES]:
                        result = check_aspiration_anomaly(line)
                        if result['matched']:
                            has_match = True
                            break
                    if name in all_files:
                        all_files[name]['has_aspiration_match'] = has_match
                except Exception as e:
                    logger.debug(f"空吸轻量扫描失败: {name} - {e}")
    except Exception as e:
        logger.warning(f"打开压缩包进行空吸轻量扫描失败: {e}")

def _cleanup_upload_files(file_paths):
    for fp in file_paths:
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except Exception:
            pass
    parent_dir = os.path.dirname(file_paths[0]) if file_paths else None
    if parent_dir and parent_dir.startswith(tempfile.gettempdir()) and 'ivd_upload_' in parent_dir:
        try:
            shutil.rmtree(parent_dir, ignore_errors=True)
        except Exception:
            pass

def _process_single_text_file(args):
    idx, file_path, rules, series, model = args
    filename = os.path.basename(file_path)
    with open(file_path, 'rb') as f:
        file_bytes = f.read()
    result = process_text_file_from_bytes(file_bytes, filename, rules, series, model)
    return idx, result

@celery.task(bind=True, name='analyze_files', autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=60, max_retries=3)
def analyze_files_task(self, file_paths, series, model, analysis_type=''):
    analysis_id = self.request.id

    try:
        rules = get_rules(series, model)
        if analysis_type == 'reagent_cooling':
            rules = rules + REAGENT_COOLING_RULES
            logger.info(f"试剂制冷排查模式，已追加 {len(REAGENT_COOLING_RULES)} 条专用规则")

        all_files = {}
        all_analysis = []
        all_file_metadata = []
        preview_text = ''
        temp_zip_path = None
        archive_type = None
        raw_name_map = {}

        archive_paths = []
        text_paths = []
        for idx, fp in enumerate(file_paths):
            fn = os.path.basename(fp).lower()
            if fn.endswith('.zip') or fn.endswith('.rar'):
                archive_paths.append((idx, fp))
            else:
                text_paths.append((idx, fp))

        _go_used = False
        if archive_paths and len(archive_paths) == 1 and not text_paths:
            idx, file_path = archive_paths[0]
            filename = os.path.basename(file_path)
            if filename.lower().endswith('.zip'):
                go_result = analyze_zip(file_path, series, model, analysis_type)
                if go_result and go_result.get('files'):
                    logger.info(f"Go Parser分析完成: {go_result['total_files']} 文件, 故障={go_result['summary']['fault']}, 样本={go_result['summary']['sample']}, 试剂={go_result['summary']['reagent']}")
                    all_files = {}
                    for name, fr in go_result['files'].items():
                        all_files[name] = {
                            'size': fr.get('size', 0),
                            'is_critical': fr.get('is_critical', False),
                            'file_type': fr.get('file_type', 'unknown'),
                            'is_aspiration_file': fr.get('is_aspiration_file', False),
                            'has_fault': fr.get('has_fault', False),
                            'has_aspiration_match': fr.get('has_aspiration_match', False),
                            'analysis': [],
                            'html_content': '',
                            'content': '',
                            'not_loaded': True,
                        }
                    all_file_metadata = go_result.get('file_metadata', [])
                    temp_zip_path = file_path
                    archive_type = 'ZIP'
                    date_groups = go_result.get('date_groups', [])
                    summary = go_result.get('summary', {})
                    result_data = {
                        'analysis': [],
                        'file_metadata': all_file_metadata[:50],
                        'files': all_files,
                        'preview': '',
                        'matched_count': 0,
                        'has_more_files': False,
                        'next_index': 0,
                        'total_candidates': len(all_files),
                        'file_name': filename,
                        'series': series,
                        'model': model,
                        'analysis_type': analysis_type,
                        'analyzed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'temp_zip_path': temp_zip_path,
                        'archive_type': archive_type,
                        'raw_name_map': {},
                        'zip_processed': len(all_files),
                        'date_groups': date_groups,
                        'summary': summary,
                        'total_dates': len(date_groups),
                        'total_files': len(all_files),
                    }
                    store_analysis_result(analysis_id, result_data)
                    try:
                        r = shared.get_redis()
                        r.publish(f'task_events:{analysis_id}', json.dumps({'analysis_id': analysis_id, 'status': 'completed'}))
                    except Exception:
                        pass
                    _go_used = True
                    logger.info("Go Parser分析路径完成，跳过Python处理")

        if not _go_used:
          for idx, file_path in archive_paths:
            filename = os.path.basename(file_path)
            if filename.lower().endswith('.zip'):
                atype = 'ZIP'
            else:
                atype = 'RAR'
            metadata_list = extract_archive_metadata(file_path, archive_type=atype)
            aspiration_metas = []
            for meta in metadata_list:
                name = meta['name']
                file_type = meta.get('file_type', 'unknown')
                is_aspiration_file = file_type in ('sample', 'reagent')
                all_files[name] = {
                    'size': meta['size'],
                    'is_critical': meta['is_critical'],
                    'file_type': file_type,
                    'is_aspiration_file': is_aspiration_file,
                    'has_fault': file_type == 'fault',
                    'has_aspiration_match': False,
                    'analysis': [],
                    'html_content': '',
                    'content': '',
                    'not_loaded': True,
                }
                all_file_metadata.append({
                    'name': name,
                    'size': meta['size'],
                    'is_critical': meta['is_critical'],
                })
                raw_name_map[name] = meta.get('raw_name', name)
                if is_aspiration_file:
                    aspiration_metas.append(meta)
            if aspiration_metas:
                _quick_scan_aspiration_files(file_path, aspiration_metas, all_files, archive_type=atype)
            temp_zip_path = file_path
            archive_type = atype
            logger.info(f"{atype} 元数据提取完成: {len(metadata_list)} 个文本文件, 空吸轻量扫描: {len(aspiration_metas)} 个")

        if text_paths:
            worker_count = min(2, len(text_paths))  # 限制2线程，避免DB连接池耗尽
            task_args = [(idx, fp, rules, series, model) for idx, fp in text_paths]
            if worker_count > 1 and len(text_paths) > 1:
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = {executor.submit(_process_single_text_file, args): args[0] for args in task_args}
                    for future in as_completed(futures):
                        idx, result = future.result()
                        for fname, fdata in result.get('files', {}).items():
                            if fname in all_files:
                                unique_name = f"{fname}_{idx}"
                                all_files[unique_name] = fdata
                            else:
                                all_files[fname] = fdata
                        all_analysis.extend(result.get('analysis', []))
                        all_file_metadata.extend(result.get('file_metadata', []))
                        if not preview_text:
                            preview_text = result.get('preview', '')
            else:
                for idx, result in (_process_single_text_file(a) for a in task_args):
                    for fname, fdata in result.get('files', {}).items():
                        if fname in all_files:
                            unique_name = f"{fname}_{idx}"
                            all_files[unique_name] = fdata
                        else:
                            all_files[fname] = fdata
                    all_analysis.extend(result.get('analysis', []))
                    all_file_metadata.extend(result.get('file_metadata', []))
                    if not preview_text:
                        preview_text = result.get('preview', '')

        result_data = {
            'analysis': all_analysis,
            'file_metadata': all_file_metadata[:50],
            'files': all_files,
            'preview': preview_text[:1000] if preview_text else '',
            'matched_count': len(all_analysis),
            'has_more_files': False,
            'next_index': 0,
            'total_candidates': len(all_files),
            'file_name': os.path.basename(file_paths[0]) if len(file_paths) == 1 else f"{len(file_paths)}个文件",
            'series': series,
            'model': model,
            'analysis_type': analysis_type,
            'analyzed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'temp_zip_path': temp_zip_path,
            'archive_type': archive_type,
            'raw_name_map': raw_name_map,
            'zip_processed': len(all_files),
        }
        files = result_data['files']
        date_groups, summary = build_date_groups_and_summary(files)
        result_data['date_groups'] = date_groups
        result_data['summary'] = summary
        result_data['total_dates'] = len(date_groups)
        result_data['total_files'] = len(files)

        store_analysis_result(analysis_id, result_data)

        try:
            r = shared.get_redis()
            r.publish(f'task_events:{analysis_id}', json.dumps({'analysis_id': analysis_id, 'status': 'completed'}))
        except Exception:
            pass

        cleanup_paths = [fp for fp in file_paths if not fp.lower().endswith(('.zip', '.rar'))]
        _cleanup_upload_files(cleanup_paths)
        return {'status': 'completed', 'analysis_id': analysis_id}
    except Exception as e:
        _cleanup_upload_files(file_paths)
        try:
            r = shared.get_redis()
            r.publish(f'task_events:{analysis_id}', json.dumps({'analysis_id': analysis_id, 'status': 'failed'}))
        except Exception:
            pass
        logger.exception(f"异步分析任务失败: {e}")
        raise


@celery.task(name='cleanup_expired_zip_files')
def cleanup_expired_zip_files_task():
    """清理过期的临时ZIP文件"""
    try:
        import shared
        r = shared.get_redis()
        cleaned_count = 0
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match="analysis:*", count=100)
            for key in keys:
                data = r.get(key)
                if data:
                    try:
                        from services.analysis import _decompress_value
                        parsed = json.loads(_decompress_value(data))
                        temp_path = parsed.get('temp_zip_path')
                        if temp_path and os.path.exists(temp_path):
                            ttl = r.ttl(key)
                            if ttl is not None and ttl < 60:
                                try:
                                    os.remove(temp_path)
                                    logger.info(f"清理过期ZIP: {temp_path}")
                                    cleaned_count += 1
                                except Exception as e:
                                    logger.warning(f"清理ZIP失败: {temp_path} - {e}")
                    except Exception:
                        pass
            if cursor == 0:
                break
        return {'status': 'completed', 'cleaned_count': cleaned_count}
    except Exception as e:
        logger.exception(f"清理过期ZIP文件失败: {e}")
        raise

@celery.task(name='cleanup_old_uploads')
def cleanup_old_uploads_task():
    """清理超过7天的上传文件"""
    import time as _time
    upload_dir = os.getenv('UPLOAD_DIR', '/app/uploads')
    max_age_days = 7
    now = _time.time()
    cleaned = 0
    try:
        if os.path.exists(upload_dir):
            for name in os.listdir(upload_dir):
                path = os.path.join(upload_dir, name)
                try:
                    age_days = (now - os.path.getmtime(path)) / 86400
                    if age_days > max_age_days and name.startswith('ivd_upload_'):
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                        cleaned += 1
                except Exception:
                    pass
        logger.info(f"上传文件清理完成: 删除{cleaned}个超过{max_age_days}天的文件")
        return {'status': 'completed', 'cleaned_count': cleaned}
    except Exception as e:
        logger.exception(f"上传文件清理失败: {e}")
        raise

@celery.task(name='memory_cleanup')
def memory_cleanup_task():
    """定期内存清理：GC回收+缓存清理+内存报告"""
    import gc
    import time as _time
    try:
        before_count = len(gc.get_objects())
        collected = gc.collect()
        after_count = len(gc.get_objects())
        try:
            import shared
            with shared._table_cache_lock:
                shared._table_cache.clear()
                shared._table_cache_time.clear()
            shared._conn_last_validated.clear()
        except Exception:
            pass
        try:
            r = shared.get_redis()
            r.execute_command('MEMORY', 'PURGE')
        except Exception:
            pass
        logger.info(f"内存清理: GC回收{collected}对象, 对象数{before_count}->{after_count}, 缓存已清")
        return {'status': 'completed', 'gc_collected': collected, 'objects_before': before_count, 'objects_after': after_count}
    except Exception as e:
        logger.exception(f"内存清理失败: {e}")
        raise
