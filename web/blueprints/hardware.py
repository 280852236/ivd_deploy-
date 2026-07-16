#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - HARDWARE模块"""

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
def _hardware_failure_table(model):
    m = re.sub(r'[^a-zA-Z0-9_]', '', model.lower())
    if not m:
        return None
    tbl = f'hardware_failures_{m}'
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        if not cur.fetchone()[0]:
            return None
    return tbl

hardware_bp = Blueprint('hardware', __name__)

@hardware_bp.route('/api/hardware-failures', methods=['GET'])
def get_hardware_failures():
    model = request.args.get('model', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(100, max(1, request.args.get('per_page', 20, type=int)))
    if not model:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    tbl = _hardware_failure_table(model)
    if not tbl:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    img_tbl = f"hardware_failure_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(f'SELECT COUNT(*) AS cnt FROM {tbl}')
        total = cur.fetchone()['cnt']
        cur.execute(f'SELECT id, %s AS model, phenomenon, cause, workaround, process, suggestion, solution, created_at, updated_at FROM {tbl} ORDER BY created_at DESC LIMIT %s OFFSET %s', [model, per_page, (page - 1) * per_page])
        rows = cur.fetchall()
        for row in rows:
            if row.get('created_at'):
                row['created_at'] = row['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if row.get('updated_at'):
                row['updated_at'] = row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            cur.execute(f'SELECT COUNT(*) AS cnt FROM {img_tbl} WHERE failure_id = %s', (row['id'],))
            row['image_count'] = cur.fetchone()['cnt']
        return jsonify({'results': [dict(r) for r in rows], 'total': total, 'page': page, 'per_page': per_page})



@hardware_bp.route('/api/hardware-failures', methods=['POST'])
def add_hardware_failure():
    model = request.form.get('model', '').strip()
    phenomenon = request.form.get('phenomenon', '').strip()
    cause = request.form.get('cause', '').strip()
    workaround = request.form.get('workaround', '').strip()
    process = request.form.get('process', '').strip()
    suggestion = request.form.get('suggestion', '').strip()
    solution = request.form.get('solution', '').strip()
    if not model or not phenomenon:
        return jsonify({'error': '型号和故障现象为必填项'}), 400
    tbl = _hardware_failure_table(model)
    if not tbl:
        return jsonify({'error': f'型号 {model} 的硬件故障表不存在'}), 404
    img_tbl = f"hardware_failure_images_{model.lower()}"
    allowed = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    try:
        with _db()() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(f'INSERT INTO {tbl} (phenomenon, cause, workaround, process, suggestion, solution) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id', (phenomenon, cause, workaround, process, suggestion, solution))
            failure_id = cur.fetchone()['id']
            for file in request.files.getlist('images'):
                if file and file.content_type in allowed:
                    img_data = file.read()
                    cur.execute(f'INSERT INTO {img_tbl} (failure_id, image_data) VALUES (%s, %s)', (failure_id, img_data))
            conn.commit()
        return jsonify({'success': True, 'id': failure_id})
    except Exception as e:
        return jsonify({'error': f'保存失败: {str(e)}'}), 500



@hardware_bp.route('/api/hardware-failures/<model>/<int:failure_id>', methods=['PUT'])
def update_hardware_failure(model, failure_id):
    phenomenon = request.form.get('phenomenon', '').strip()
    cause = request.form.get('cause', '').strip()
    workaround = request.form.get('workaround', '').strip()
    process = request.form.get('process', '').strip()
    suggestion = request.form.get('suggestion', '').strip()
    solution = request.form.get('solution', '').strip()
    if not phenomenon:
        return jsonify({'error': '故障现象为必填项'}), 400
    tbl = _hardware_failure_table(model)
    if not tbl:
        return jsonify({'error': f'型号 {model} 的硬件故障表不存在'}), 404
    img_tbl = f"hardware_failure_images_{model.lower()}"
    allowed = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    try:
        with _db()() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(f'UPDATE {tbl} SET phenomenon=%s, cause=%s, workaround=%s, process=%s, suggestion=%s, solution=%s, updated_at=NOW() WHERE id=%s', (phenomenon, cause, workaround, process, suggestion, solution, failure_id))
            for file in request.files.getlist('images'):
                if file and file.content_type in allowed:
                    img_data = file.read()
                    cur.execute(f'INSERT INTO {img_tbl} (failure_id, image_data) VALUES (%s, %s)', (failure_id, img_data))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f'更新失败: {str(e)}'}), 500



@hardware_bp.route('/api/hardware-failures/<model>/<int:failure_id>', methods=['DELETE'])
def delete_hardware_failure(model, failure_id):
    tbl = _hardware_failure_table(model)
    if not tbl:
        return jsonify({'error': f'型号 {model} 的硬件故障表不存在'}), 404
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {tbl} WHERE id = %s', (failure_id,))
        conn.commit()
    return jsonify({'success': True})



@hardware_bp.route('/api/hardware-failures/<model>/<int:failure_id>/images', methods=['GET'])
def get_hardware_failure_images(model, failure_id):
    img_tbl = f"hardware_failure_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(f'SELECT id FROM {img_tbl} WHERE failure_id = %s ORDER BY id', (failure_id,))
        rows = cur.fetchall()
    return jsonify([{'id': r['id']} for r in rows])



@hardware_bp.route('/api/hardware-failures/<model>/<int:failure_id>/images/<int:image_id>', methods=['GET'])
def get_hardware_failure_image(model, failure_id, image_id):
    from flask import Response
    img_tbl = f"hardware_failure_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'SELECT image_data, image_mime FROM {img_tbl} WHERE id = %s AND failure_id = %s', (image_id, failure_id))
        row = cur.fetchone()
        if not row or not row[0]:
            return 'Image not found', 404
        img_data, img_mime = row[0], row[1] or 'image/jpeg'
    response = Response(img_data, mimetype=img_mime)
    response.headers['Cache-Control'] = 'public, max-age=31536000'
    response.headers['Content-Length'] = len(img_data)
    return response



@hardware_bp.route('/api/hardware-failures/<model>/<int:failure_id>/images/<int:image_id>', methods=['DELETE'])
def delete_hardware_failure_image(model, failure_id, image_id):
    img_tbl = f"hardware_failure_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {img_tbl} WHERE id = %s AND failure_id = %s', (image_id, failure_id))
        conn.commit()
    return jsonify({'success': True})



@hardware_bp.route('/api/hardware-failures/search', methods=['GET'])
def search_hardware_failures():
    q = request.args.get('q', '').strip()
    model = request.args.get('model', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(100, max(1, request.args.get('per_page', 20, type=int)))
    if not q:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    if not model:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    tbl = _hardware_failure_table(model)
    if not tbl:
        return jsonify({'results': [], 'total': 0, 'page': page, 'per_page': per_page})
    img_tbl = f"hardware_failure_images_{model.lower()}"
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        like_q = f'%{q}%'
        cur.execute(f"SELECT COUNT(*) AS cnt FROM {tbl} WHERE phenomenon ILIKE %s OR cause ILIKE %s OR workaround ILIKE %s OR process ILIKE %s OR suggestion ILIKE %s OR solution ILIKE %s", (like_q, like_q, like_q, like_q, like_q, like_q))
        total = cur.fetchone()['cnt']
        cur.execute(f"SELECT id, %s AS model, phenomenon, cause, workaround, process, suggestion, solution, created_at, updated_at FROM {tbl} WHERE phenomenon ILIKE %s OR cause ILIKE %s OR workaround ILIKE %s OR process ILIKE %s OR suggestion ILIKE %s OR solution ILIKE %s ORDER BY created_at DESC LIMIT %s OFFSET %s", [model, like_q, like_q, like_q, like_q, like_q, like_q, per_page, (page - 1) * per_page])
        rows = cur.fetchall()
        for row in rows:
            if row.get('created_at'):
                row['created_at'] = row['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if row.get('updated_at'):
                row['updated_at'] = row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            cur.execute(f'SELECT COUNT(*) AS cnt FROM {img_tbl} WHERE failure_id = %s', (row['id'],))
            row['image_count'] = cur.fetchone()['cnt']
        return jsonify({'results': [dict(r) for r in rows], 'total': total, 'page': page, 'per_page': per_page})


@hardware_bp.route('/hardware-failures')
def hardware_failures_page():
    series = request.args.get('series', 'SMART').upper()
    model = request.args.get('model', '')
    if series not in ('SMART', 'VENUS'):
        series = 'SMART'
    is_admin = session.get('admin_logged_in', False)
    return _rts()(_get_app().HARDWARE_FAILURES_HTML, series=series, model=model, is_admin=is_admin)



@hardware_bp.route('/board-compatibility')
def board_compatibility_page():
    series = request.args.get('series', 'SMART').upper()
    model = request.args.get('model', '')
    if series not in ('SMART', 'VENUS'):
        series = 'SMART'
    return _rts()(_get_app().BOARD_COMPATIBILITY_HTML, series=series, model=model)




