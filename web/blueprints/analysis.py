#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - ANALYSIS模块"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, make_response, render_template_string
import sys
import tempfile
import os
import logging

logger = logging.getLogger(__name__)

def _get_app():
    return sys.modules['app']
def _db():
    return _get_app().db_connection
def _config():
    return _get_app().Config
def _rts():
    return _get_app().render_template_string
def _escape_html():
    return _get_app().escape_html

analysis_bp = Blueprint('analysis', __name__)

@analysis_bp.route('/api/analyze', methods=['POST'])
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
    upload_dir = _config().UPLOAD_DIR
    os.makedirs(upload_dir, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix='ivd_upload_', dir=upload_dir)
    file_paths = []
    for f in files_to_process:
        filename = _get_app().sanitize_filename(f.filename)
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
def analysis_view(analysis_id):
    get_analysis_result = _get_app().get_analysis_result
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
        embedded_data = _json.dumps(lightweight, ensure_ascii=False).replace('</script', '<\\/script').replace('<!--', '<\\!--')
        return _rts()(_get_app().ANALYSIS_HTML, analysis_id=analysis_id, embedded_data=embedded_data)
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
                <p>''' + _escape_html()(str(res.info)) + '''</p>
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
        'series': data['series'],
        'model': data['model'],
        'analysis_type': data.get('analysis_type', ''),
        'file_name': data['file_name'],
        'analyzed_at': data['analyzed_at'],
        'date_groups': data['date_groups'],
        'total_dates': data['total_dates'],
        'total_files': data['total_files'],
        'summary': data['summary'],
        'has_more_files': data.get('zip_has_more', False),
        'zip_total_candidates': data.get('zip_total_candidates', 0),
        'zip_processed': len(data.get('files', {})),
    })



@analysis_bp.route('/api/analysis/<analysis_id>/load-more', methods=['POST'])
def load_more_files(analysis_id):
    get_analysis_result = _get_app().get_analysis_result
    store_analysis_result = _get_app().store_analysis_result
    get_rules = _get_app().get_rules
    process_zip_file = _get_app().process_zip_file
    _build_date_groups = _get_app()._build_date_groups
    _compute_summary = _get_app()._compute_summary
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    if not data.get('zip_has_more'):
        return jsonify({'error': '没有更多文件需要加载', 'has_more_files': False}), 400

    temp_path = data.get('temp_zip_path')
    if not temp_path or not os.path.exists(temp_path):
        data['zip_has_more'] = False
        store_analysis_result(analysis_id, data)
        return jsonify({'error': '临时ZIP文件已清理，无法继续加载'}), 410

    try:
        rules = get_rules(data['series'], data['model'])
        next_index = data.get('zip_next_index', 100)
        with open(temp_path, 'rb') as f:
            batch_result = process_zip_file(f, rules, data['series'], data['model'], batch_size=500, start_index=next_index)

        new_files = batch_result.get('files', {})
        existing_files = data['files']
        existing_files.update(new_files)

        new_date_groups = _build_date_groups(existing_files)
        new_summary = _compute_summary(existing_files)

        data['files'] = existing_files
        data['date_groups'] = new_date_groups
        data['total_dates'] = len(new_date_groups)
        data['total_files'] = len(existing_files)
        data['summary'] = new_summary
        data['zip_next_index'] = batch_result.get('next_index', next_index + 100)
        data['zip_has_more'] = batch_result.get('has_more_files', False)

        store_analysis_result(analysis_id, data)

        return jsonify({
            'success': True,
            'new_files': len(new_files),
            'total_files_now': len(existing_files),
            'total_dates_now': len(new_date_groups),
            'date_groups': new_date_groups,
            'summary': new_summary,
            'has_more_files': batch_result.get('has_more_files', False),
            'zip_processed': len(existing_files),
            'zip_total_candidates': data.get('zip_total_candidates', 0),
        })
    except Exception as e:
        logger.error(f"分批加载失败: {analysis_id} - {e}", exc_info=True)
        return jsonify({'error': f'加载失败: {str(e)}'}), 500



@analysis_bp.route('/api/analysis/<analysis_id>/file')
def get_analysis_file(analysis_id):
    get_file_content = _get_app().get_file_content
    get_analysis_result = _get_app().get_analysis_result
    filename = request.args.get('name', '')
    if not filename:
        return jsonify({'error': '请指定文件名'}), 400
    
    file_data = get_file_content(analysis_id, filename)
    
    if file_data:
        return jsonify(file_data)
    
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    
    file_data = data.get('files', {}).get(filename)
    if not file_data:
        decoded = unquote(filename)
        file_data = data.get('files', {}).get(decoded)
    if not file_data:
        return jsonify({'error': f'文件 {filename} 不在分析结果中'}), 404
    
    return jsonify({
        'name': filename,
        'content': file_data.get('content', ''),
        'html_content': file_data.get('html_content', ''),
        'has_fault': file_data.get('has_fault', False),
        'size': file_data.get('size', 0),
        'is_critical': file_data.get('is_critical', False),
        'analysis': file_data.get('analysis', [])
    })



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
        os.unlink(tmp_path)
        if not full_text.strip():
            return jsonify({'error': 'PDF内容为空或无法提取文本（可能是扫描件）'}), 400

        entries = extract_fault_entries(full_text)
        if not entries:
            return jsonify({'error': '未能从PDF中提取到电机状态数据'}), 400

        added_count = store_pdf_entries(entries, series, model)
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
        return jsonify({'error': f'PDF处理失败: {str(e)}'}), 500

@analysis_bp.route('/api/task_status/<analysis_id>', methods=['GET'])
def task_status(analysis_id):
    from celery.result import AsyncResult
    from celery_app import celery
    get_analysis_result = _get_app().get_analysis_result
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
            data = get_analysis_result(analysis_id)
            if data:
                return jsonify({
                    'status': 'completed',
                    'redirect_url': f'/analysis/{analysis_id}',
                    'total_dates': data.get('total_dates', 0),
                    'total_files': data.get('total_files', 0),
                    'summary': data.get('summary', {}),
                })
            return jsonify({'status': 'completed', 'redirect_url': f'/analysis/{analysis_id}'})
        else:
            return jsonify({'status': 'failed', 'error': str(res.info)}), 500
    state = res.state
    meta = res.info if isinstance(res.info, dict) else {}
    return jsonify({'status': 'pending', 'state': state, 'progress': meta}), 202

@analysis_bp.route('/api/search-in-files', methods=['POST'])
def search_in_files():
    import re, fnmatch
    try:
        data = request.get_json()
        pattern = data.get('pattern', '')
        is_regex = data.get('is_regex', False)
        case_sensitive = data.get('case_sensitive', False)
        file_pattern = data.get('file_pattern', '*')
        analysis_id = data.get('analysis_id', '')
        if not pattern:
            return jsonify({'error': '请输入搜索内容'}), 400
        get_analysis_result = _get_app().get_analysis_result
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
        for group in analysis_data.get('date_groups', []):
            for file_info in group.get('files', []):
                filename = file_info.get('name', '')
                if file_pattern and file_pattern != '*':
                    patterns = [p.strip() for p in file_pattern.split(',')]
                    if not any(fnmatch.fnmatch(filename, p) for p in patterns):
                        continue
                content = file_info.get('content', '')
                if not content:
                    continue
                file_count += 1
                for idx, line in enumerate(content.split('\n'), 1):
                    matches = regex.findall(line)
                    if matches:
                        for match in matches:
                            results.append({'file': filename, 'line': idx, 'text': line[:200], 'match': match})
                            total_matches += 1
        return jsonify({'results': results[:1000], 'total_matches': total_matches, 'file_count': file_count})
    except Exception as e:
        return jsonify({'error': f'搜索失败'}), 500

# ========== Web界面路由 ==========


