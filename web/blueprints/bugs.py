#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - BUGS模块"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, make_response, render_template_string
from psycopg2.extras import RealDictCursor
import shared
from shared import api_login_required, api_super_admin_required, login_required
from services.cache import api_cache

def _bug_table(model):
    return shared.resolve_table(model, 'software_bugs')


bugs_bp = Blueprint('bugs', __name__)

@bugs_bp.route('/bugs')
@login_required
def bugs_page():
    series = request.args.get('series', 'SMART').upper()
    model = request.args.get('model', '')
    if series not in ('SMART', 'VENUS'):
        series = 'SMART'
    is_admin = session.get('admin_logged_in', False)
    return render_template_string(shared.get_template('BUGS_HTML'), series=series, model=model, is_admin=is_admin)



@bugs_bp.route('/api/bugs', methods=['GET'])
@api_cache(ttl=60, key_prefix='bugs')
def get_bugs():
    model = request.args.get('model', '').strip()
    version = request.args.get('version', '')
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(100, max(1, request.args.get('per_page', 20, type=int)))
    if not model:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    tbl = _bug_table(model)
    if not tbl:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    img_tbl = shared.safe_img_table(model, 'bug_images')
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        where = 'WHERE b.software_version = %s' if version else ''
        count_params = [version] if version else []
        cur.execute(f'SELECT COUNT(*) AS total FROM {tbl} b {where}', count_params)
        total = cur.fetchone()['total']
        data_params = ([version] if version else []) + [model, per_page, (page - 1) * per_page]
        cur.execute(f'SELECT b.id, %s AS model, b.software_version, b.title, b.cause, b.workaround, b.solution, b.created_at, b.updated_at, COALESCE(img_cnt.cnt, 0) AS image_count FROM {tbl} b LEFT JOIN (SELECT bug_id, COUNT(*) AS cnt FROM {img_tbl} GROUP BY bug_id) img_cnt ON img_cnt.bug_id = b.id {where} ORDER BY b.created_at DESC LIMIT %s OFFSET %s', data_params)
        rows = cur.fetchall()
        for row in rows:
            shared.format_row_timestamps(row)
        return jsonify({'results': [dict(r) for r in rows], 'total': total, 'page': page, 'per_page': per_page})



@bugs_bp.route('/api/bugs', methods=['POST'])
@api_login_required
def add_bug():
    model = request.form.get('model', '').strip()
    software_version = request.form.get('software_version', '').strip()
    title = request.form.get('title', '').strip()
    cause = request.form.get('cause', '').strip()
    workaround = request.form.get('workaround', '').strip()
    solution = request.form.get('solution', '').strip()
    if not model or not software_version or not title:
        return jsonify({'error': '型号、版本号和标题为必填项'}), 400
    tbl = _bug_table(model)
    if not tbl:
        return jsonify({'error': f'型号 {model} 的Bug表不存在'}), 404
    img_tbl = shared.safe_img_table(model, 'bug_images')
    images = []
    for key in request.files:
        if key.startswith('image'):
            img = request.files[key]
            if img.filename and img.content_type in shared.ALLOWED_IMAGE_TYPES:
                img_bytes = img.read()
                if len(img_bytes) <= 5 * 1024 * 1024:
                    images.append((img_bytes, img.content_type))
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f'INSERT INTO {tbl} (software_version, title, cause, workaround, solution) VALUES (%s, %s, %s, %s, %s) RETURNING id', (software_version, title, cause, workaround, solution))
        bug_id = cur.fetchone()[0]
        for i, (img_data, img_mime) in enumerate(images):
            cur.execute(f'INSERT INTO {img_tbl} (bug_id, image_data, image_mime, sort_order) VALUES (%s, %s, %s, %s)', (bug_id, img_data, img_mime, i))
        conn.commit()
    shared.audit_log('add_bug', target_type='bug', target_id=bug_id, detail=f'添加 {model} Bug#{bug_id} {title}')
    return jsonify({'id': bug_id, 'message': '添加成功', 'image_count': len(images)})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>', methods=['PUT'])
@api_login_required
def update_bug(model, bug_id):
    software_version = request.form.get('software_version', '').strip()
    title = request.form.get('title', '').strip()
    cause = request.form.get('cause', '').strip()
    workaround = request.form.get('workaround', '').strip()
    solution = request.form.get('solution', '').strip()
    if not software_version or not title:
        return jsonify({'error': '版本号和标题为必填项'}), 400
    tbl = _bug_table(model)
    if not tbl:
        return jsonify({'error': f'型号 {model} 的Bug表不存在'}), 404
    img_tbl = shared.safe_img_table(model, 'bug_images')
    images = []
    i = 0
    while True:
        field_name = f'image{i}'
        if field_name in request.files and request.files[field_name].filename:
            img = request.files[field_name]
            if img.content_type not in shared.ALLOWED_IMAGE_TYPES:
                return jsonify({'error': f'图片{i+1}: 仅支持 JPG/PNG/GIF/WebP 格式'}), 400
            img_bytes = img.read()
            if len(img_bytes) > 5 * 1024 * 1024:
                return jsonify({'error': f'图片{i+1}: 大小不能超过5MB'}), 400
            images.append((img_bytes, img.content_type))
            i += 1
        else:
            break
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f'UPDATE {tbl} SET software_version=%s, title=%s, cause=%s, workaround=%s, solution=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s', (software_version, title, cause, workaround, solution, bug_id))
        if images:
            cur.execute(f'SELECT COALESCE(MAX(sort_order), -1) FROM {img_tbl} WHERE bug_id=%s', (bug_id,))
            max_order = cur.fetchone()[0]
            for j, (img_data, img_mime) in enumerate(images):
                cur.execute(f'INSERT INTO {img_tbl} (bug_id, image_data, image_mime, sort_order) VALUES (%s, %s, %s, %s)', (bug_id, img_data, img_mime, max_order + 1 + j))
        conn.commit()
    shared.audit_log('update_bug', target_type='bug', target_id=bug_id, detail=f'更新 {model} Bug#{bug_id}')
    return jsonify({'message': '更新成功', 'added_images': len(images)})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>', methods=['DELETE'])
@api_login_required
def delete_bug(model, bug_id):
    tbl = _bug_table(model)
    if not tbl:
        return jsonify({'error': f'型号 {model} 的Bug表不存在'}), 404
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {tbl} WHERE id=%s', (bug_id,))
        conn.commit()
    shared.audit_log('delete_bug', target_type='bug', target_id=bug_id, detail=f'删除 {model} Bug#{bug_id}')
    return jsonify({'message': '删除成功'})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/images')
def get_bug_images(model, bug_id):
    img_tbl = shared.safe_img_table(model, 'bug_images')
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(f'SELECT id, image_mime, sort_order FROM {img_tbl} WHERE bug_id=%s ORDER BY sort_order', (bug_id,))
        rows = cur.fetchall()
        return jsonify([{'id': r['id'], 'mime': r['image_mime'], 'order': r['sort_order']} for r in rows])



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/images/<int:image_id>')
def get_bug_image(model, bug_id, image_id):
    img_tbl = shared.safe_img_table(model, 'bug_images')
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f'SELECT image_data, image_mime FROM {img_tbl} WHERE bug_id=%s AND id=%s', (bug_id, image_id))
        row = cur.fetchone()
        if not row or not row[0]:
            return '', 404
        from flask import Response
        response = Response(row[0], mimetype=row[1])
        response.headers['Cache-Control'] = 'public, max-age=604800'
        response.headers['Content-Length'] = len(row[0])
        return response



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/images/<int:image_id>', methods=['DELETE'])
@api_login_required
def delete_bug_image(model, bug_id, image_id):
    img_tbl = shared.safe_img_table(model, 'bug_images')
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {img_tbl} WHERE bug_id=%s AND id=%s', (bug_id, image_id))
        if cur.rowcount == 0:
            return jsonify({'error': '图片不存在'}), 404
    shared.audit_log('delete_bug_image', target_type='bug_image', target_id=image_id, detail=f'删除 {model} Bug#{bug_id} 图片#{image_id}')
    return jsonify({'success': True, 'message': '图片已删除'})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/image', methods=['DELETE'])
@api_login_required
def delete_all_bug_images(model, bug_id):
    img_tbl = shared.safe_img_table(model, 'bug_images')
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {img_tbl} WHERE bug_id=%s', (bug_id,))
    shared.audit_log('delete_all_bug_images', target_type='bug_image', target_id=bug_id, detail=f'删除 {model} Bug#{bug_id} 所有图片')
    return jsonify({'success': True, 'message': '所有图片已删除'})



@bugs_bp.route('/api/bugs/versions', methods=['GET'])
def get_bug_versions():
    model = request.args.get('model', '').strip()
    if not model:
        return jsonify([])
    tbl = _bug_table(model)
    if not tbl:
        return jsonify([])
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f'SELECT DISTINCT software_version FROM {tbl} ORDER BY software_version DESC LIMIT 500')
        return jsonify([row[0] for row in cur.fetchall()])



@bugs_bp.route('/api/bugs/search', methods=['GET'])
def search_bugs():
    q = request.args.get('q', '').strip()
    series = request.args.get('series', '').strip()
    model = request.args.get('model', '').strip()
    page = max(1, int(request.args.get('page', '1')))
    per_page = 20
    if not q:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    like_q = f'%{q}%'
    results = []
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if model:
            clean_m = shared._CLEAN_RE.sub('', model.lower())
            tables = [(model, f"software_bugs_{clean_m}", shared.safe_img_table(model, 'bug_images'))]
        elif series:
            cur.execute("SELECT m.name FROM models m JOIN series s ON m.series_id = s.id WHERE UPPER(s.name) = UPPER(%s)", (series,))
            models = [row['name'] for row in cur.fetchall()]
            tables = [(m, f"software_bugs_{shared._CLEAN_RE.sub('', m.lower())}", shared.safe_img_table(m, 'bug_images')) for m in models]
        else:
            cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'software_bugs_%'")
            bug_tables = [row['tablename'] for row in cur.fetchall()]
            tables = [(t.replace('software_bugs_', '').upper(), t, t.replace('software_bugs_', 'bug_images_')) for t in bug_tables]

        if tables:
            queries = []
            params = []
            for model_name, tbl, img_tbl in tables:
                queries.append(f"""
                    SELECT b.id, %s AS model, b.software_version, b.title, b.cause, b.workaround, b.solution,
                           b.created_at, b.updated_at, COALESCE(img_cnt.cnt, 0) AS image_count
                    FROM {tbl} b
                    LEFT JOIN (SELECT bug_id, COUNT(*) AS cnt FROM {img_tbl} GROUP BY bug_id) img_cnt ON img_cnt.bug_id = b.id
                    WHERE b.title ILIKE %s OR b.cause ILIKE %s OR b.workaround ILIKE %s OR b.solution ILIKE %s OR b.software_version ILIKE %s
                """)
                params.extend([model_name, like_q, like_q, like_q, like_q, like_q])
            union_query = " UNION ALL ".join(queries)
            count_query = f"SELECT COUNT(*) AS cnt FROM ({union_query}) AS all_bugs"
            cur.execute(count_query, params)
            total = cur.fetchone()['cnt']
            offset = (page - 1) * per_page
            paginated_query = f"SELECT * FROM ({union_query}) AS all_bugs ORDER BY created_at DESC LIMIT %s OFFSET %s"
            cur.execute(paginated_query, params + [per_page, offset])
            for row in cur.fetchall():
                shared.format_row_timestamps(row)
                results.append(dict(row))
    return jsonify({'results': results, 'total': total, 'page': page, 'per_page': per_page})



