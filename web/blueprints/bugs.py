#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - BUGS模块"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, make_response, render_template_string
import re
import sys
from psycopg2.extras import RealDictCursor
def _get_app():
    return sys.modules['app']
def _db():
    return _get_app().db_connection
def _rts():
    return _get_app().render_template_string
def _bug_table(model):
    m = re.sub(r'[^a-zA-Z0-9_]', '', model.lower())
    if not m:
        return None
    tbl = f'software_bugs_{m}'
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        if not cur.fetchone()[0]:
            return None
    return tbl

bugs_bp = Blueprint('bugs', __name__)

@bugs_bp.route('/bugs')
def bugs_page():
    series = request.args.get('series', 'SMART').upper()
    model = request.args.get('model', '')
    if series not in ('SMART', 'VENUS'):
        series = 'SMART'
    is_admin = session.get('admin_logged_in', False)
    return _rts()(_get_app().BUGS_HTML, series=series, model=model, is_admin=is_admin)



@bugs_bp.route('/api/bugs', methods=['GET'])
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
    img_tbl = f"bug_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        where = 'WHERE software_version = %s' if version else ''
        count_params = [version] if version else []
        cur.execute(f'SELECT COUNT(*) AS cnt FROM {tbl} {where}', count_params)
        total = cur.fetchone()['cnt']
        select_params = ([version] if version else []) + [per_page, (page - 1) * per_page]
        cur.execute(f'SELECT id, %s AS model, software_version, title, cause, workaround, solution, created_at, updated_at FROM {tbl} {where} ORDER BY created_at DESC LIMIT %s OFFSET %s', [model] + select_params)
        rows = cur.fetchall()
        for row in rows:
            if row.get('created_at'):
                row['created_at'] = row['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if row.get('updated_at'):
                row['updated_at'] = row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            cur.execute(f'SELECT COUNT(*) AS cnt FROM {img_tbl} WHERE bug_id = %s', (row['id'],))
            row['image_count'] = cur.fetchone()['cnt']
        return jsonify({'results': [dict(r) for r in rows], 'total': total, 'page': page, 'per_page': per_page})



@bugs_bp.route('/api/bugs', methods=['POST'])
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
    img_tbl = f"bug_images_{model.lower()}"
    allowed = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    images = []
    for key in request.files:
        if key.startswith('image'):
            img = request.files[key]
            if img.filename and img.content_type in allowed:
                img_bytes = img.read()
                if len(img_bytes) <= 5 * 1024 * 1024:
                    images.append((img_bytes, img.content_type))
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'INSERT INTO {tbl} (software_version, title, cause, workaround, solution) VALUES (%s, %s, %s, %s, %s) RETURNING id', (software_version, title, cause, workaround, solution))
        bug_id = cur.fetchone()[0]
        for i, (img_data, img_mime) in enumerate(images):
            cur.execute(f'INSERT INTO {img_tbl} (bug_id, image_data, image_mime, sort_order) VALUES (%s, %s, %s, %s)', (bug_id, img_data, img_mime, i))
        conn.commit()
    return jsonify({'id': bug_id, 'message': '添加成功', 'image_count': len(images)})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>', methods=['PUT'])
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
    img_tbl = f"bug_images_{model.lower()}"
    images = []
    i = 0
    while True:
        field_name = f'image{i}'
        if field_name in request.files and request.files[field_name].filename:
            img = request.files[field_name]
            allowed = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
            if img.content_type not in allowed:
                return jsonify({'error': f'图片{i+1}: 仅支持 JPG/PNG/GIF/WebP 格式'}), 400
            img_bytes = img.read()
            if len(img_bytes) > 5 * 1024 * 1024:
                return jsonify({'error': f'图片{i+1}: 大小不能超过5MB'}), 400
            images.append((img_bytes, img.content_type))
            i += 1
        else:
            break
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'UPDATE {tbl} SET software_version=%s, title=%s, cause=%s, workaround=%s, solution=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s', (software_version, title, cause, workaround, solution, bug_id))
        if images:
            cur.execute(f'SELECT COALESCE(MAX(sort_order), -1) FROM {img_tbl} WHERE bug_id=%s', (bug_id,))
            max_order = cur.fetchone()[0]
            for j, (img_data, img_mime) in enumerate(images):
                cur.execute(f'INSERT INTO {img_tbl} (bug_id, image_data, image_mime, sort_order) VALUES (%s, %s, %s, %s)', (bug_id, img_data, img_mime, max_order + 1 + j))
        conn.commit()
    return jsonify({'message': '更新成功', 'added_images': len(images)})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>', methods=['DELETE'])
def delete_bug(model, bug_id):
    tbl = _bug_table(model)
    if not tbl:
        return jsonify({'error': f'型号 {model} 的Bug表不存在'}), 404
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {tbl} WHERE id=%s', (bug_id,))
        conn.commit()
    return jsonify({'message': '删除成功'})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/images')
def get_bug_images(model, bug_id):
    img_tbl = f"bug_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(f'SELECT id, image_mime, sort_order FROM {img_tbl} WHERE bug_id=%s ORDER BY sort_order', (bug_id,))
        rows = cur.fetchall()
        return jsonify([{'id': r['id'], 'mime': r['image_mime'], 'order': r['sort_order']} for r in rows])



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/images/<int:image_id>')
def get_bug_image(model, bug_id, image_id):
    img_tbl = f"bug_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'SELECT image_data, image_mime FROM {img_tbl} WHERE bug_id=%s AND id=%s', (bug_id, image_id))
        row = cur.fetchone()
        if not row or not row[0]:
            return '', 404
        from flask import Response
        response = Response(row[0], mimetype=row[1])
        response.headers['Cache-Control'] = 'public, max-age=31536000'
        response.headers['Content-Length'] = len(row[0])
        return response



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/images/<int:image_id>', methods=['DELETE'])
def delete_bug_image(model, bug_id, image_id):
    img_tbl = f"bug_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {img_tbl} WHERE bug_id=%s AND id=%s', (bug_id, image_id))
        if cur.rowcount == 0:
            return jsonify({'error': '图片不存在'}), 404
    return jsonify({'success': True, 'message': '图片已删除'})



@bugs_bp.route('/api/bugs/<model>/<int:bug_id>/image', methods=['DELETE'])
def delete_all_bug_images(model, bug_id):
    img_tbl = f"bug_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {img_tbl} WHERE bug_id=%s', (bug_id,))
    return jsonify({'success': True, 'message': '所有图片已删除'})



@bugs_bp.route('/api/bugs/versions', methods=['GET'])
def get_bug_versions():
    model = request.args.get('model', '').strip()
    if not model:
        return jsonify([])
    tbl = _bug_table(model)
    if not tbl:
        return jsonify([])
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'SELECT DISTINCT software_version FROM {tbl} ORDER BY software_version DESC')
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
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if model:
            tbl = f"software_bugs_{model.lower()}"
            img_tbl = f"bug_images_{model.lower()}"
            cur.execute(f"SELECT id, %s AS model, software_version, title, cause, workaround, solution, created_at, updated_at FROM {tbl} WHERE title ILIKE %s OR cause ILIKE %s OR workaround ILIKE %s OR solution ILIKE %s OR software_version ILIKE %s", (model, like_q, like_q, like_q, like_q, like_q))
            for row in cur.fetchall():
                if row.get('created_at'):
                    row['created_at'] = row['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                if row.get('updated_at'):
                    row['updated_at'] = row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
                cur.execute(f'SELECT COUNT(*) AS cnt FROM {img_tbl} WHERE bug_id = %s', (row['id'],))
                row['image_count'] = cur.fetchone()['cnt']
                results.append(dict(row))
        elif series:
            cur.execute("SELECT m.name FROM models m JOIN series s ON m.series_id = s.id WHERE UPPER(s.name) = UPPER(%s)", (series,))
            models = [row['name'] for row in cur.fetchall()]
            for m in models:
                tbl = f"software_bugs_{m.lower()}"
                img_tbl = f"bug_images_{m.lower()}"
                cur.execute(f"SELECT id, %s AS model, software_version, title, cause, workaround, solution, created_at, updated_at FROM {tbl} WHERE title ILIKE %s OR cause ILIKE %s OR workaround ILIKE %s OR solution ILIKE %s OR software_version ILIKE %s", (m, like_q, like_q, like_q, like_q, like_q))
                for row in cur.fetchall():
                    if row.get('created_at'):
                        row['created_at'] = row['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                    if row.get('updated_at'):
                        row['updated_at'] = row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
                    cur.execute(f'SELECT COUNT(*) AS cnt FROM {img_tbl} WHERE bug_id = %s', (row['id'],))
                    row['image_count'] = cur.fetchone()['cnt']
                    results.append(dict(row))
        else:
            cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'software_bugs_%'")
            tables = [row['tablename'] for row in cur.fetchall()]
            for tbl in tables:
                model_name = tbl.replace('software_bugs_', '').upper()
                img_tbl = f"bug_images_{model_name.lower()}"
                cur.execute(f"SELECT id, %s AS model, software_version, title, cause, workaround, solution, created_at, updated_at FROM {tbl} WHERE title ILIKE %s OR cause ILIKE %s OR workaround ILIKE %s OR solution ILIKE %s OR software_version ILIKE %s", (model_name, like_q, like_q, like_q, like_q, like_q))
                for row in cur.fetchall():
                    if row.get('created_at'):
                        row['created_at'] = row['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                    if row.get('updated_at'):
                        row['updated_at'] = row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
                    cur.execute(f'SELECT COUNT(*) AS cnt FROM {img_tbl} WHERE bug_id = %s', (row['id'],))
                    row['image_count'] = cur.fetchone()['cnt']
                    results.append(dict(row))
    results.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    total = len(results)
    start = (page - 1) * per_page
    page_results = results[start:start + per_page]
    return jsonify({'results': page_results, 'total': total, 'page': page, 'per_page': per_page})



