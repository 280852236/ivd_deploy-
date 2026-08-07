#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - ANALYSIS模块"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, make_response, render_template_string
import tempfile
import os
import json
import logging
import re
import fnmatch
import time
from urllib.parse import unquote
import shared
from shared import api_login_required, api_super_admin_required, login_required
from services.file_utils import sanitize_filename
from services.analysis import get_analysis_result, get_file_content
from services.rules import get_rules

from services.pdf_import import extract_fault_entries, store_pdf_entries

logger = logging.getLogger(__name__)

analysis_bp = Blueprint('analysis', __name__)


def _update_date_groups_field(r, analysis_id, filename, field_updates):
    main_key = f"analysis:{analysis_id}"
    raw = r.get(main_key)
    if not raw:
        return
    from services.analysis import _decompress_value
    try:
        data = json.loads(_decompress_value(raw))
    except Exception:
        return
    date_groups = data.get('date_groups', [])
    updated = False
    for group in date_groups:
        for f in group.get('files', []):
            if f.get('name') == filename:
                for k, v in field_updates.items():
                    f[k] = v
                updated = True
                break
        if updated:
            break
    if updated:
        data['date_groups'] = date_groups
        from services.analysis import _compress_value
        ttl = r.ttl(main_key)
        if ttl is not None and ttl > 0:
            r.set(main_key, _compress_value(json.dumps(data, ensure_ascii=False)), ex=ttl)
        elif ttl == -1:
            r.set(main_key, _compress_value(json.dumps(data, ensure_ascii=False)))

@analysis_bp.route('/api/analyze', methods=['POST'])
@api_login_required
def analyze_file():
    series = request.form.get('series', '')
    model = request.form.get('model', '')
    analysis_type = request.form.get('analysis_type', '')  # 保留特殊类型
    if not series or not model:
        return jsonify({'error': '请选择设备系列和型号'}), 400

    files_to_process = []
    if 'file' in request.files and request.files['file'].filename:
        files_to_process = [request.files['file']]
    elif 'files' in request.files:
        files_list = request.files.getlist('files')
        files_to_process = [f for f in files_list if f.filename]
    if not files_to_process:
        return jsonify({'error': '未上传文件'}), 400
    
    # 只在试剂制冷排查时检查文件名
    if analysis_type == 'reagent_cooling':
        for f in files_to_process:
            filename = f.filename
            if '接收数据记录' not in filename:
                import json
                from flask import Response
                error_data = {'error': f'当前文件：{filename}\n\n请上传文件名包含"接收数据记录"的文件，例如：2026-06-26接收数据记录.txt'}
                return Response(json.dumps(error_data, ensure_ascii=False), mimetype='application/json'), 400

    # 保存文件到临时目录（传递给 Celery Worker）
    import os
    upload_dir = shared.Config.UPLOAD_DIR
    os.makedirs(upload_dir, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix='ivd_upload_', dir=upload_dir)
    file_paths = []
    for f in files_to_process:
        filename = sanitize_filename(f.filename)
        path = os.path.join(temp_dir, filename)
        f.save(path)
        file_paths.append(path)

    from tasks import analyze_files_task
    task = analyze_files_task.delay(file_paths, series, model, analysis_type)
    analysis_id = task.id   # 使用 Celery 任务 ID 作为唯一标识

    # 立即返回，让前端轮询
    return jsonify({
        'status': 'accepted',
        'analysis_id': analysis_id,
        'task_id': task.id,
        'poll_url': f'/api/task_status/{analysis_id}',
        'redirect_url': f'/analysis/{analysis_id}',
        'message': '分析任务已提交，请轮询状态'
    })



@analysis_bp.route('/analysis/<analysis_id>')
@login_required
def analysis_view(analysis_id):
    data = get_analysis_result(analysis_id)
    if data:
        import json as _json
        lightweight = {
            'series': data.get('series', ''),
            'model': data.get('model', ''),
            'analysis_type': data.get('analysis_type', ''),
            'file_name': data.get('file_name', ''),
            'analyzed_at': data.get('analyzed_at', ''),
            'date_groups': data.get('date_groups', []),
            'total_dates': data.get('total_dates', 0),
            'total_files': data.get('total_files', 0),
            'summary': data.get('summary', {}),
            'has_more_files': data.get('has_more_files', False),
            'zip_total_candidates': data.get('zip_total_candidates', 0),
            'zip_processed': data.get('zip_processed', 0),
        }
        embedded_data = json.dumps(lightweight, ensure_ascii=False).replace('</script', '<\\/script').replace('<!--', '<\\!--')
        return render_template_string(shared.get_template("ANALYSIS_HTML"), analysis_id=analysis_id, embedded_data=embedded_data)
    try:
        from celery.result import AsyncResult
        from celery_app import celery
        res = AsyncResult(analysis_id, app=celery)
        if res.state == 'FAILURE':
            return '''
            <!DOCTYPE html>
            <html>
            <head><meta charset="UTF-8"><title>分析失败</title></head>
            <body style="font-family:system-ui;text-align:center;padding:50px;">
                <h2>❌ 分析任务失败</h2>
                <p>''' + shared.escape_html(str(res.info)) + '''</p>
                <a href="/" style="color:#1e6f9f;">← 返回上传页面</a>
            </body>
            </html>
            ''', 500
    except Exception:
        pass
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>分析进行中</title>
    <style>
        .loader { width:48px;height:48px;border:5px solid #e5e7eb;border-top-color:#2563eb;border-radius:50%;animation:spin 1s linear infinite;margin:30px auto; }
        @keyframes spin { to { transform:rotate(360deg); } }
    </style>
    <script>
        let pollCount = 0;
        function pollStatus() {
            pollCount++;
            fetch('/api/task_status/''' + analysis_id + '''')
                .then(r => r.json())
                .then(d => {
                    if (d.status === 'completed' && d.redirect_url) {
                        window.location.href = d.redirect_url;
                    } else if (d.status === 'failed') {
                        document.getElementById('statusText').textContent = '分析失败: ' + (d.error || '未知错误');
                        document.getElementById('statusText').style.color = '#ef4444';
                        document.querySelector('.loader').style.display = 'none';
                    } else {
                        document.getElementById('statusText').textContent = '正在智能分析中... (已等待 ' + pollCount + ' 秒)';
                        setTimeout(pollStatus, 1000);
                    }
                })
                .catch(() => { setTimeout(pollStatus, 2000); });
        }
        setTimeout(pollStatus, 1000);
    </script>
    </head>
    <body style="font-family:system-ui;text-align:center;padding:50px;">
        <div class="loader"></div>
        <h2 id="statusText">正在智能分析中...</h2>
        <p style="color:#6b7280;margin-top:10px;">大文件分析可能需要较长时间，请耐心等待</p>
        <a href="/" style="color:#1e6f9f;display:inline-block;margin-top:20px;">← 返回上传页面</a>
    </body>
    </html>
    '''



@analysis_bp.route('/api/analysis/<analysis_id>')
def get_analysis_data(analysis_id):
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    return jsonify({
        'series': data.get('series', ''),
        'model': data.get('model', ''),
        'analysis_type': data.get('analysis_type', ''),
        'file_name': data.get('file_name', ''),
        'analyzed_at': data.get('analyzed_at', ''),
        'date_groups': data.get('date_groups', []),
        'total_dates': data.get('total_dates', 0),
        'total_files': data.get('total_files', 0),
        'summary': data.get('summary', {}),
        'has_more_files': data.get('has_more_files', False),
        'zip_total_candidates': data.get('zip_total_candidates', 0),
        'zip_processed': len(data.get('files', {})),
    })



@analysis_bp.route('/api/analysis/<analysis_id>/load-more', methods=['POST'])
@api_login_required
def load_more_files(analysis_id):
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    return jsonify({'error': '已启用按需加载，无需分批加载', 'has_more_files': False}), 400



@analysis_bp.route('/api/analysis/<analysis_id>/file')
def get_analysis_file(analysis_id):
    filename = request.args.get('name', '')
    if not filename:
        return jsonify({'error': '请指定文件名'}), 400
    
    file_data = get_file_content(analysis_id, filename)
    
    if file_data and not file_data.get('not_loaded'):
        return jsonify(file_data)
    
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期，请重新上传文件'}), 410
    
    if data.get('from_pg') and not data.get('temp_zip_path'):
        return jsonify({'error': '分析结果缓存已过期，文件内容无法加载，请重新上传文件进行分析'}), 410
    
    file_names = data.get('file_names', [])
    decoded = unquote(filename) if filename != unquote(filename) else None
    
    if filename not in file_names and (not decoded or decoded not in file_names):
        return jsonify({'error': f'文件 {filename} 不在分析结果中'}), 404
    
    actual_filename = filename if filename in file_names else decoded
    
    if file_data and file_data.get('not_loaded'):
        pass
    elif not file_data:
        file_data = get_file_content(analysis_id, actual_filename)
    
    if file_data and not file_data.get('not_loaded'):
        return jsonify(file_data)
    
    temp_path = data.get('temp_zip_path')
    archive_type = data.get('archive_type', 'ZIP')
    raw_name_map = data.get('raw_name_map', {})
    raw_name = raw_name_map.get(actual_filename, actual_filename)
    
    if not temp_path or not os.path.exists(temp_path):
        return jsonify({'error': '临时压缩文件已清理，无法加载文件内容'}), 410
    
    try:
        from services.file_service import analyze_single_file_from_archive
        rules = get_rules(data['series'], data['model'])
        result = analyze_single_file_from_archive(temp_path, raw_name, actual_filename, rules, data['series'], data['model'], archive_type=archive_type)
        if result is None:
            return jsonify({'error': f'无法读取文件 {actual_filename}'}), 500
        
        files = result.get('files', {})
        fdata = files.get(actual_filename, files.get(list(files.keys())[0] if files else '', {}))
        
        if fdata:
            from services.cache import get_redis
            r = get_redis()
            files_key = f"analysis:{analysis_id}:files"
            ttl = shared.Config.ANALYSIS_TTL_HOURS * 3600
            pipe = r.pipeline()
            pipe.hset(files_key, actual_filename, json.dumps(fdata, ensure_ascii=False))
            pipe.expire(files_key, ttl)
            pipe.execute()
            _update_date_groups_field(r, analysis_id, actual_filename, {
                'has_aspiration_match': fdata.get('has_aspiration_match', False),
                'has_fault': fdata.get('has_fault', False),
            })
        
        # 按需生成html_content：如果为空但有_html_meta，实时生成并缓存
        html_content = fdata.get('html_content', '') if fdata else ''
        if not html_content and fdata and fdata.get('_html_meta'):
            try:
                from services.file_service import _generate_html_content, escape_html
                meta = fdata['_html_meta']
                content_text = fdata.get('content', '')
                lines = content_text.splitlines()
                max_hl = meta.get('max_html_lines', 5000)
                advice_map = meta.get('advice_map', {})
                unmatched_map = meta.get('unmatched_map', {})
                highlight_keywords = meta.get('highlight_keywords', [])
                is_aspiration = meta.get('is_aspiration_file', False)
                html_content = _generate_html_content(lines[:max_hl], advice_map, unmatched_map, highlight_keywords, is_aspiration)
                total_lines = meta.get('total_lines', len(lines))
                if total_lines > max_hl:
                    html_content += '<div id="htmlTruncationMarker" data-total="{}" data-rendered="{}" style="text-align:center;padding:12px;color:#6366f1;font-size:0.85rem;border-top:1px dashed #c7d2fe;margin-top:8px;cursor:pointer;"><i class="fas fa-angle-double-down" style="margin-right:6px;"></i>加载更多行 (已渲染 {} / 共{} 行)</div>'.format(total_lines, max_hl, max_hl, total_lines)
                # 缓存生成的html_content到Redis
                fdata['html_content'] = html_content
                fdata.pop('_html_meta', None)
                from services.cache import get_redis
                r = get_redis()
                files_key = f"analysis:{analysis_id}:files"
                ttl = shared.Config.ANALYSIS_TTL_HOURS * 3600
                pipe = r.pipeline()
                pipe.hset(files_key, actual_filename, json.dumps(fdata, ensure_ascii=False))
                pipe.expire(files_key, ttl)
                pipe.execute()
            except Exception as e:
                logger.warning(f"按需生成html_content失败: {e}")
        
        if fdata and not fdata.get('html_content') and fdata.get('_html_meta'):
            pass  # already handled above
        
        return jsonify({
            'name': actual_filename,
            'content': fdata.get('content', '') if fdata else '',
            'html_content': html_content,
            'has_fault': fdata.get('has_fault', False) if fdata else False,
            'size': fdata.get('size', 0) if fdata else 0,
            'is_critical': fdata.get('is_critical', False) if fdata else False,
            'analysis': fdata.get('analysis', []) if fdata else []
        })
    except Exception as e:
        logger.error(f"按需加载文件失败: {actual_filename} - {e}", exc_info=True)
        return jsonify({'error': '加载文件失败'}), 500



@analysis_bp.route('/api/analysis/<analysis_id>/tree')
def get_analysis_tree(analysis_id):
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', 50, type=int)
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    all_groups = data['date_groups']
    page = all_groups[offset:offset + limit]
    return jsonify({
        'date_groups': page,
        'offset': offset,
        'limit': limit,
        'total': len(all_groups),
        'has_more': (offset + limit) < len(all_groups)
    })

# ========== PDF导入API ==========


@analysis_bp.route('/api/import_pdf', methods=['POST'])
@api_super_admin_required
def import_pdf():
    if not session.get('admin_logged_in'):
        return jsonify({'error': '请先登录管理员'}), 401

    series = request.form.get('series', '').strip()
    model = request.form.get('model', '').strip()
    if not series or not model:
        return jsonify({'error': '请选择设备系列和型号'}), 400
    if 'file' not in request.files:
        return jsonify({'error': '未上传PDF文件'}), 400
    file = request.files['file']
    filename = file.filename
    if not filename.lower().endswith('.pdf'):
        return jsonify({'error': '请上传PDF格式文件'}), 400

    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return jsonify({'error': '请安装PyPDF2库: pip install PyPDF2'}), 500

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        reader = PdfReader(tmp_path)
        full_text = ''
        for page in reader.pages:
            text = page.extract_text()
            if text:
                full_text += text + '\n'
        if not full_text.strip():
            return jsonify({'error': 'PDF内容为空或无法提取文本（可能是扫描件）'}), 400

        entries = extract_fault_entries(full_text)
        if not entries:
            return jsonify({'error': '未能从PDF中提取到电机状态数据'}), 400

        added_count = store_pdf_entries(entries, series, model)
        try:
            _r = shared.get_redis()
            for _key in _r.scan_iter("motor:*", count=200):
                _r.delete(_key)
            _r.delete("motor_status_tables")
        except Exception:
            pass
        if added_count == 0:
            return jsonify({'error': '所有条目已存在，无需导入'}), 400

        logger.info(f"PDF导入完成: {added_count} 条电机状态 - {series}/{model}")
        return jsonify({
            'success': True,
            'message': f'成功导入 {added_count} 条电机状态数据',
            'count': added_count,
            'total_extracted': len(entries),
            'text_length': len(full_text)
        })
    except Exception as e:
        logger.error(f"PDF导入失败: {e}", exc_info=True)
        return jsonify({'error': 'PDF处理失败'}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

@analysis_bp.route('/api/task_status/<analysis_id>', methods=['GET'])
def task_status(analysis_id):
    from celery.result import AsyncResult
    from celery_app import celery
    data = get_analysis_result(analysis_id)
    if data:
        return jsonify({
            'status': 'completed',
            'redirect_url': f'/analysis/{analysis_id}',
            'total_dates': data.get('total_dates', 0),
            'total_files': data.get('total_files', 0),
            'summary': data.get('summary', {}),
        })
    res = AsyncResult(analysis_id, app=celery)
    if res.ready():
        if res.successful():
            return jsonify({'status': 'completed', 'redirect_url': f'/analysis/{analysis_id}'})
        else:
            return jsonify({'status': 'failed', 'error': str(res.info)}), 500
    state = res.state
    meta = res.info if isinstance(res.info, dict) else {}
    return jsonify({'status': 'pending', 'state': state, 'progress': meta}), 202


@analysis_bp.route('/api/task_events/<analysis_id>')
def task_events(analysis_id):
    from flask import Response

    def generate():
        r = shared.get_redis()
        pubsub = r.pubsub()
        pubsub.subscribe(f'task_events:{analysis_id}')
        try:
            data = get_analysis_result(analysis_id)
            if data:
                yield f"data: {json.dumps({'status': 'completed', 'redirect_url': f'/analysis/{analysis_id}'})}\n\n"
                return
            timeout = 600
            start = time.time()
            while time.time() - start < timeout:
                msg = pubsub.get_message(timeout=5)
                if msg and msg['type'] == 'message':
                    try:
                        payload = json.loads(msg['data'])
                        if payload.get('analysis_id') == analysis_id:
                            if payload.get('status') == 'completed':
                                payload['redirect_url'] = f'/analysis/{analysis_id}'
                            yield f"data: {json.dumps(payload)}\n\n"
                            if payload.get('status') in ('completed', 'failed'):
                                return
                    except Exception:
                        pass
                data = get_analysis_result(analysis_id)
                if data:
                    yield f"data: {json.dumps({'status': 'completed', 'redirect_url': f'/analysis/{analysis_id}'})}\n\n"
                    return
                yield f": heartbeat\n\n"
            yield f"data: {json.dumps({'status': 'timeout'})}\n\n"
        finally:
            try:
                pubsub.unsubscribe(f'task_events:{analysis_id}')
            except Exception:
                pass
            try:
                pubsub.close()
            except Exception:
                pass

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    })

@analysis_bp.route('/api/search-in-files', methods=['POST'])
@api_login_required
def search_in_files():
    try:
        data = request.get_json()
        pattern = data.get('pattern', '')
        is_regex = data.get('is_regex', False)
        case_sensitive = data.get('case_sensitive', False)
        file_pattern = data.get('file_pattern', '*')
        analysis_id = data.get('analysis_id', '')
        if not pattern:
            return jsonify({'error': '请输入搜索内容'}), 400
        analysis_data = get_analysis_result(analysis_id)
        if not analysis_data:
            return jsonify({'error': '分析结果不存在或已过期'}), 404
        if is_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                return jsonify({'error': f'正则表达式错误: {str(e)}'}), 400
        else:
            escaped = re.escape(pattern)
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(escaped, flags)
        results = []
        total_matches = 0
        file_count = 0
        r = shared.get_redis()
        files_key = f"analysis:{analysis_id}:files"
        file_names = analysis_data.get('file_names', [])
        if file_pattern and file_pattern != '*':
            pats = [p.strip() for p in file_pattern.split(',')]
            file_names = [fn for fn in file_names if any(fnmatch.fnmatch(fn, p) for p in pats)]
        # 分批加载，每批50个文件，避免大分析结果OOM
        BATCH_SIZE = 50
        file_contents = []
        for i in range(0, len(file_names), BATCH_SIZE):
            batch = file_names[i:i + BATCH_SIZE]
            pipe = r.pipeline()
            for filename in batch:
                pipe.hget(files_key, filename)
            file_contents.extend(pipe.execute())
        temp_path = analysis_data.get('temp_zip_path')
        archive_type = analysis_data.get('archive_type', 'ZIP')
        raw_name_map = analysis_data.get('raw_name_map', {})
        _MAX_SEARCH_LINES = 200000
        if temp_path and os.path.exists(temp_path):
            import zipfile as _zf
            import rarfile as _rf
            from services.file_service import _decode_bytes, strip_control_chars
            _zip_cache = {}
        for filename, content_json in zip(file_names, file_contents):
            content = ''
            if content_json:
                try:
                    file_data = json.loads(content_json)
                    content = file_data.get('content', '')
                except (json.JSONDecodeError, TypeError):
                    pass
            if not content and temp_path and os.path.exists(temp_path):
                try:
                    raw_name = raw_name_map.get(filename, filename)
                    if archive_type == 'ZIP':
                        if temp_path not in _zip_cache:
                            _zip_cache[temp_path] = _zf.ZipFile(temp_path)
                        raw = _zip_cache[temp_path].read(raw_name)
                    else:
                        if temp_path not in _zip_cache:
                            _zip_cache[temp_path] = _rf.RarFile(temp_path)
                        raw = _zip_cache[temp_path].read(raw_name)
                    content = strip_control_chars(_decode_bytes(raw, filename))
                except Exception:
                    continue
            if not content:
                continue
            file_count += 1
            lines = content.split('\n')
            if len(lines) > _MAX_SEARCH_LINES:
                lines = lines[:_MAX_SEARCH_LINES]
            for idx, line in enumerate(lines, 1):
                matches = regex.findall(line)
                if matches:
                    for match in matches:
                        results.append({'file': filename, 'line': idx, 'text': line[:200], 'match': match})
                        total_matches += 1
                    if total_matches >= 5000:
                        break
            if total_matches >= 5000:
                break
        for zf in _zip_cache.values():
            try: zf.close()
            except Exception: pass
        return jsonify({'results': results[:5000], 'total_matches': total_matches, 'file_count': file_count})
    except Exception as e:
        logger.error(f"search_in_files失败: {e}", exc_info=True)
        return jsonify({'error': f'搜索失败'}), 500

# ========== Web界面路由 ==========

@analysis_bp.route('/api/analysis-history', methods=['GET'])
@login_required
def analysis_history():
    try:
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 20
        series = request.args.get('series', '').strip().upper()
        model = request.args.get('model', '').strip()
        with shared.db_connection() as conn:
            cur = conn.cursor()
            where_clauses = []
            params = []
            if series:
                where_clauses.append("series = %s")
                params.append(series)
            if model:
                where_clauses.append("model = %s")
                params.append(model)
            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            cur.execute(f"SELECT COUNT(*) FROM analysis_results{where_sql}", params)
            total = cur.fetchone()[0]
            offset = (page - 1) * per_page
            cur.execute(f"""
                SELECT analysis_id, series, model, analysis_type, summary, total_dates, total_files, matched_count, analyzed_at
                FROM analysis_results{where_sql}
                ORDER BY analyzed_at DESC
                LIMIT %s OFFSET %s
            """, params + [per_page, offset])
            rows = cur.fetchall()
            items = []
            for row in rows:
                aid, s, m, atype, summary_raw, td, tf, mc, aa = row
                summary = summary_raw if isinstance(summary_raw, dict) else (json.loads(summary_raw) if summary_raw else {})
                items.append({
                    'analysis_id': aid,
                    'series': s or '',
                    'model': m or '',
                    'analysis_type': atype or '',
                    'summary': summary,
                    'total_dates': td or 0,
                    'total_files': tf or 0,
                    'matched_count': mc or 0,
                    'analyzed_at': str(aa) if aa else '',
                })
            return jsonify({'items': items, 'total': total, 'page': page, 'per_page': per_page})
    except Exception as e:
        logger.error(f"analysis_history失败: {e}", exc_info=True)
        return jsonify({'error': '查询历史记录失败'}), 500

@analysis_bp.route('/analysis-history')
@login_required
def analysis_history_page():
    return render_template_string('''
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>分析历史记录</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
<style>
body { background: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.page-header { background: linear-gradient(135deg, #1e40af, #3b82f6); color: white; padding: 24px 0; margin-bottom: 24px; }
.history-card { background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 16px; margin-bottom: 12px; transition: all 0.2s; cursor: pointer; }
.history-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.12); transform: translateY(-1px); }
.model-badge { font-size: 0.75rem; padding: 3px 10px; border-radius: 20px; }
.badge-smart { background: #dbeafe; color: #1e40af; }
.badge-venus { background: #fce7f3; color: #be185d; }
.stat-item { text-align: center; padding: 8px; }
.stat-value { font-size: 1.25rem; font-weight: 700; color: #1e40af; }
.stat-label { font-size: 0.75rem; color: #64748b; }
.empty-state { text-align: center; padding: 60px 20px; color: #94a3b8; }
.empty-state i { font-size: 3rem; margin-bottom: 12px; }
.pagination-area { display: flex; justify-content: center; padding: 20px 0; }
.filter-bar { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.filter-bar select { border-radius: 8px; border: 1px solid #e2e8f0; padding: 6px 12px; font-size: 0.875rem; }
.summary-tag { display: inline-block; background: #f1f5f9; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; margin: 2px; color: #475569; }
</style>
</head>
<body>
<div class="page-header">
    <div class="container">
        <div class="d-flex align-items-center">
            <a href="/" class="text-white text-decoration-none me-3"><i class="bi bi-arrow-left"></i></a>
            <h4 class="mb-0"><i class="bi bi-clock-history me-2"></i>分析历史记录</h4>
        </div>
    </div>
</div>
<div class="container">
    <div class="filter-bar">
        <select id="filterSeries" onchange="loadHistory(1)">
            <option value="">全部系列</option>
            <option value="SMART">SMART</option>
            <option value="VENUS">VENUS</option>
        </select>
        <select id="filterModel" onchange="loadHistory(1)">
            <option value="">全部型号</option>
        </select>
    </div>
    <div id="historyList"></div>
    <div id="pagination" class="pagination-area"></div>
</div>
<script>
const CSRF = document.cookie.match(/csrf_token=([^;]+)/)?.[1] || '';
async function loadHistory(page) {
    const series = document.getElementById('filterSeries').value;
    const model = document.getElementById('filterModel').value;
    const params = new URLSearchParams({page});
    if (series) params.set('series', series);
    if (model) params.set('model', model);
    const resp = await fetch('/api/analysis-history?' + params);
    const data = await resp.json();
    const list = document.getElementById('historyList');
    if (!data.items || data.items.length === 0) {
        list.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i><div>暂无分析记录</div></div>';
        document.getElementById('pagination').innerHTML = '';
        return;
    }
    list.innerHTML = data.items.map(item => {
        const badgeCls = item.series === 'VENUS' ? 'badge-venus' : 'badge-smart';
        const summaryTags = Object.entries(item.summary || {}).slice(0, 5).map(([k,v]) => {
            if (typeof v === 'number') return '<span class="summary-tag">' + k + ': ' + v + '</span>';
            return '';
        }).join('');
        return '<div class="history-card" onclick="viewDetail(\\''+item.analysis_id+'\\')">' +
            '<div class="d-flex justify-content-between align-items-start">' +
                '<div>' +
                    '<span class="model-badge ' + badgeCls + '">' + item.series + ' ' + item.model + '</span>' +
                    '<span class="ms-2 text-muted" style="font-size:0.8rem;">' + (item.analyzed_at || '') + '</span>' +
                '</div>' +
                '<div class="d-flex gap-3">' +
                    '<div class="stat-item"><div class="stat-value">' + item.total_dates + '</div><div class="stat-label">日期</div></div>' +
                    '<div class="stat-item"><div class="stat-value">' + item.total_files + '</div><div class="stat-label">文件</div></div>' +
                    '<div class="stat-item"><div class="stat-value">' + item.matched_count + '</div><div class="stat-label">匹配</div></div>' +
                '</div>' +
            '</div>' +
            (summaryTags ? '<div class="mt-2">' + summaryTags + '</div>' : '') +
        '</div>';
    }).join('');
    const totalPages = Math.ceil(data.total / data.per_page);
    let pagHtml = '';
    if (page > 1) pagHtml += '<button class="btn btn-outline-primary btn-sm me-2" onclick="loadHistory('+(page-1)+')">上一页</button>';
    pagHtml += '<span class="align-self-center" style="font-size:0.875rem;color:#64748b;">第 ' + page + ' / ' + totalPages + ' 页 (共 ' + data.total + ' 条)</span>';
    if (page < totalPages) pagHtml += '<button class="btn btn-outline-primary btn-sm ms-2" onclick="loadHistory('+(page+1)+')">下一页</button>';
    document.getElementById('pagination').innerHTML = pagHtml;
}
function viewDetail(aid) { window.location.href = '/analysis/' + aid; }
loadHistory(1);
</script>
</body>
</html>
''')

