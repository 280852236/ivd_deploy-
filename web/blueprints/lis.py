#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - LIS模块"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, make_response, render_template_string
import re
from collections import Counter
import shared
from shared import api_login_required, api_super_admin_required, login_required
from PyPDF2 import PdfReader
import io
import time
import logging

lis_bp = Blueprint('lis', __name__)

_VALID_MSG_TYPES = frozenset({'QRY^Q02', 'DSR^Q03', 'ORU^R01', 'ACK^R01', 'ACK^Q03'})
_INVALID_MSG_TYPES = frozenset({'QCK^Q02', 'DSP^Q031'})
_VALID_SPECIMEN = frozenset({'Ser', 'Plasma', 'Urine', 'BALF', 'CSF', 'Automated', 'Serum', 'Whole Blood'})
_VALID_GENDER = frozenset({'M', 'F', 'O', '0', ''})

_RE_ESCAPE_COMBINED = re.compile(r'\[11\]|\[13\]|\[28\]|\\x0[bB]|\\x0[dD]|\\x1[cC]')
_ESCAPE_MAP = {
    '[11]': '\x0b', '[13]': '\r', '[28]': '\x1c',
    '\\x0b': '\x0b', '\\x0B': '\x0b',
    '\\x0d': '\r', '\\x0D': '\r',
    '\\x1c': '\x1c', '\\x1C': '\x1c',
}
_RE_GARBLED = re.compile(r'[\x00-\x08\x0e-\x0f\x10-\x1a\x1b\x1d-\x1f]')
_MSH2_CHARS = frozenset('^~\\&')
_TIMEOUT_TS = 5000

@lis_bp.route('/lis-issues')
@login_required
def lis_issues_page():
    series = request.args.get('series', 'SMART').upper()
    model = request.args.get('model', '')
    if series not in ('SMART', 'VENUS'):
        series = 'SMART'
    return render_template_string(shared.get_template('LIS_ISSUES_HTML'), series=series, model=model)



@lis_bp.route('/api/lis/parse-log', methods=['POST'])
@api_login_required
def lis_parse_log():
    try:
        # ---------- 1. 参数与文件读取 ----------
        if 'log' not in request.files:
            return jsonify({'error': '请上传日志文件'}), 400
        log_f = request.files['log']
        if not log_f.filename:
            return jsonify({'error': '未选择日志文件'}), 400

        series = request.form.get('series', '').upper()
        model = request.form.get('model', '').strip()
        if series not in ('SMART', 'VENUS'):
            return jsonify({'error': '无效的系列'}), 400
        if not model:
            return jsonify({'error': '请选择型号'}), 400

        # ---------- 2. 获取协议模板 ----------
        proto_content = None
        if 'protocol' in request.files and request.files['protocol'].filename:
            proto_content = request.files['protocol'].read().decode('utf-8', errors='ignore')
        else:
            proto_content = None
            cache_key = f'lis_proto:{series}:{model}'
            try:
                r = shared.get_redis()
                cached = r.get(cache_key)
                if cached:
                    proto_content = cached
            except Exception:
                pass
            if not proto_content:
                with shared.db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute('SELECT content FROM lis_protocol_templates WHERE series=%s AND model=%s', (series, model))
                    row = cur.fetchone()
                    if row:
                        proto_content = row[0]
                        try:
                            r = shared.get_redis()
                            r.setex(cache_key, 3600, proto_content)
                        except Exception:
                            pass
        if not proto_content:
            return jsonify({'error': '该型号暂无协议模板，请先上传协议文件'}), 400

        # ---------- 3. 读取并预处理日志内容 ----------
        raw_bytes = log_f.read()
        log_content = raw_bytes.decode('utf-8', errors='ignore')

        # 转义字符替换（支持 [11], [13], [28] 和 \x0b 等形式）
        log_content = _RE_ESCAPE_COMBINED.sub(lambda m: _ESCAPE_MAP[m.group(0)], log_content)

        log_lines_raw = log_content.splitlines()

        # ---------- 4. 帧提取（跨行合并） ----------
        frames = []
        frame_buffer = ''
        frame_start_line = 0
        in_frame = False

        for idx, raw_line in enumerate(log_lines_raw):
            line = raw_line.rstrip('\r\n')
            if '\x0b' in line:                      # 新的帧开始
                if in_frame and frame_buffer:       # 保存上一个不完整帧
                    frames.append({
                        'start_line': frame_start_line,
                        'end_line': idx,
                        'content': frame_buffer,
                        'complete': False
                    })
                in_frame = True
                frame_buffer = line
                frame_start_line = idx + 1
            elif in_frame:
                frame_buffer += '\r' + line
                if '\x1c' in line:                  # 帧结束符
                    frames.append({
                        'start_line': frame_start_line,
                        'end_line': idx + 1,
                        'content': frame_buffer,
                        'complete': True
                    })
                    in_frame = False
                    frame_buffer = ''

        if in_frame and frame_buffer:
            frames.append({
                'start_line': frame_start_line,
                'end_line': len(log_lines_raw),
                'content': frame_buffer,
                'complete': False
            })

        # ---------- 5. 常量定义 ----------
        VALID_MSG_TYPES = _VALID_MSG_TYPES
        INVALID_MSG_TYPES = _INVALID_MSG_TYPES
        VALID_SPECIMEN = _VALID_SPECIMEN
        VALID_GENDER = _VALID_GENDER

        # ---------- 6. 解析协议模板（提取消息类型） ----------
        proto_msg_types = set()
        for line in proto_content.splitlines():
            line = line.strip()
            if not line or '|' not in line:
                continue
            fields = line.split('|')
            seg_id = fields[0].strip()
            if seg_id == 'MSH' and len(fields) > 8:
                proto_msg_types.add(fields[8].strip())

        # ---------- 7. 逐行解析与标注 ----------
        line_annotations = []          # 每行的标注列表
        msh10_cache = {}               # 用于交互流程配对: {control_id: [info1, info2, ...]}
        request_records = {}           # 记录请求行：{control_id: [info1, info2, ...]}
        current_frame_msh10 = None     # 当前帧的MSH-10控制ID

        for line_num, raw_line in enumerate(log_lines_raw, 1):
            line = raw_line.rstrip('\r\n')
            issues = []

            if not line.strip():
                line_annotations.append({'line': line_num, 'text': line, 'issues': []})
                continue

            # ----- 7.1 物理帧规则 -----
            has_vt = '\x0b' in line
            has_fs = '\x1c' in line
            if has_vt and not has_fs:
                issues.append({'type': 'fail', 'msg': '帧起始符<VT>存在但缺少结束符<FS>+<CR>，帧不完整'})
            if has_fs and not has_vt:
                issues.append({'type': 'info', 'msg': '帧结束符<FS>存在（非帧首行）'})

            # 非法控制字符检查
            garbled_chars = [f'0x{ord(c):02X}' for c in _RE_GARBLED.findall(line[:500])]
            if garbled_chars:
                garbled_str = ', '.join(garbled_chars[:5])
                issues.append({'type': 'fail', 'msg': f'非法控制字符: {garbled_str}'})

            # ----- 7.2 段解析：去除帧首尾，按 \r 分割为多个段 -----
            clean_line = line
            if has_vt:
                clean_line = clean_line.split('\x0b', 1)[-1]
            if has_fs:
                clean_line = clean_line.rsplit('\x1c', 1)[0]
            clean_line = clean_line.rstrip('\r')

            if '|' not in clean_line:
                line_annotations.append({'line': line_num, 'text': line, 'issues': issues})
                continue

            segments = clean_line.split('\r')
            for seg in segments:
                if not seg.strip():
                    continue
                fields = seg.split('|')
                if not fields:
                    continue
                seg_id = fields[0].strip()

                # ----- 7.3 MSH 消息头规则 -----
                if seg_id == 'MSH':
                    msh2 = fields[1] if len(fields) > 1 else ''
                    if msh2 and not _MSH2_CHARS.issubset(msh2):
                        issues.append({'type': 'warn', 'msg': f'MSH-2应包含^~\\&，实际为"{msh2}"'})

                    if len(fields) > 8:
                        msg_type = fields[8].strip()
                        if msg_type in INVALID_MSG_TYPES:
                            issues.append({'type': 'fail', 'msg': f'消息类型非法: {msg_type}'})
                        elif msg_type and msg_type not in VALID_MSG_TYPES:
                            issues.append({'type': 'warn', 'msg': f'消息类型 {msg_type} 不在标准定义中(QRY^Q02/DSR^Q03/ORU^R01/ACK^R01)'})
                            # 仅当非标准类型才检查模板定义
                            if proto_msg_types and msg_type not in proto_msg_types:
                                issues.append({'type': 'warn', 'msg': f'消息类型 {msg_type} 不在协议模板定义中'})
                        # 标准类型不检查模板（不再添加警告）

                    if len(fields) > 9:
                        control_id = fields[9].strip()
                        if not control_id:
                            issues.append({'type': 'warn', 'msg': 'MSH-10控制ID为空'})
                        else:
                            current_frame_msh10 = control_id
                            m_t = fields[8].strip() if len(fields) > 8 else ''
                            msg_timestamp = None
                            if len(fields) > 6:
                                ts_str = fields[6].strip()
                                if len(ts_str) == 14 and ts_str.isdigit():
                                    try:
                                        msg_timestamp = int(ts_str)
                                    except ValueError:
                                        pass
                            msh10_cache.setdefault(control_id, []).append({'type': m_t, 'line': line_num, 'timestamp': msg_timestamp})
                            if m_t in ('QRY^Q02', 'ORU^R01'):
                                request_records.setdefault(control_id, []).append({'type': m_t, 'line': line_num, 'timestamp': msg_timestamp})

                    # MSH-7 消息时间格式校验
                    if len(fields) > 6:
                        msg_time = fields[6].strip()
                        if msg_time and not (len(msg_time) == 14 and msg_time.isdigit()):
                            issues.append({'type': 'warn', 'msg': f'MSH-7消息时间格式应为YYYYMMDDHHMMSS，实际为"{msg_time}"'})

                    # MSH-11 处理ID校验
                    if len(fields) > 10:
                        process_id = fields[10].strip()
                        if process_id and process_id != 'P':
                            issues.append({'type': 'warn', 'msg': f'MSH-11处理ID应为P(生产)，实际为"{process_id}"'})

                    # MSH-12 版本号校验
                    if len(fields) > 11:
                        version = fields[11].strip()
                        if version:
                            if series == 'SMART':
                                if ',' not in version:
                                    issues.append({'type': 'warn', 'msg': f'MSH-12版本号应使用逗号分隔(如2, 3, 1)，实际为"{version}"'})
                            else:
                                if ',' in version:
                                    issues.append({'type': 'warn', 'msg': f'MSH-12版本号使用了逗号分隔，应使用点号，实际为"{version}"'})

                    if len(fields) > 17:
                        encoding = fields[17].strip()
                        if 'ASCII' in encoding.upper():
                            for fi, fld in enumerate(fields[1:], start=2):
                                if any(ord(c) > 127 for c in fld):
                                    issues.append({'type': 'fail', 'msg': f'编码声明为ASCII但MSH-{fi}含非ASCII字符，编码声明与实际不符'})
                                    break

                # ----- 7.4 Venus 业务段结构规则 (DSP) -----
                elif seg_id == 'DSP':
                    if len(fields) > 1:
                        dsp_idx_str = fields[1].strip()
                        try:
                            dsp_idx = int(dsp_idx_str)
                        except ValueError:
                            dsp_idx = -1
                            issues.append({'type': 'warn', 'msg': f'DSP序号非数字: "{dsp_idx_str}"', 'category': '解析错误'})

                        if dsp_idx == 1:
                            if len(fields) < 4 or not fields[3].strip():
                                issues.append({'type': 'fail', 'msg': '【项目识别失败】DSP-1(病员号): DSP-3必须非空', 'category': '项目识别'})
                            elif not any(c.isdigit() for c in fields[3]):
                                issues.append({'type': 'warn', 'msg': f'DSP-1(病员号): DSP-3="{fields[3].strip()}"，预期为数字或字母数字组合', 'category': '数据质量'})
                        elif dsp_idx == 2:
                            if len(fields) < 4 or not fields[3].strip():
                                issues.append({'type': 'warn', 'msg': 'DSP-2(姓名): DSP-3为空', 'category': '数据质量'})
                        elif dsp_idx == 3:
                            pass
                        elif dsp_idx == 9:
                            if len(fields) < 4 or not fields[3].strip():
                                issues.append({'type': 'fail', 'msg': '【项目识别失败】DSP-9(样本编号): DSP-3必须非空', 'category': '项目识别'})
                        elif dsp_idx == 18:
                            pass
                        elif dsp_idx >= 19:
                            if len(fields) < 3 or not fields[2].strip():
                                issues.append({'type': 'fail', 'msg': f'【项目识别失败】DSP-{dsp_idx}缺少测试序号(DSP-2为空)', 'category': '项目识别'})
                            if len(fields) < 4 or not fields[3].strip():
                                issues.append({'type': 'fail', 'msg': f'【项目识别失败】DSP-{dsp_idx}项目编号(DSP-3)为空', 'category': '项目识别'})
                            if len(fields) > 3:
                                project_code = fields[3].strip()
                                if project_code:
                                    if series in ('VENUS', 'SMART'):
                                        if not project_code.isdigit():
                                            issues.append({'type': 'fail', 'msg': f'【项目识别失败】DSP-{dsp_idx}项目编号(DSP-3)必须为数字，实际为"{project_code}"', 'category': '项目识别'})
                                    else:
                                        if not project_code.replace('-', '').replace('_', '').isalnum():
                                            issues.append({'type': 'warn', 'msg': f'DSP-{dsp_idx}项目编号"{project_code}"含特殊字符', 'category': '数据质量'})
                            if len(fields) > 4:
                                test_dilution = fields[4].strip()
                                if not test_dilution:
                                    issues.append({'type': 'info', 'msg': f'DSP-{dsp_idx}测试稀释倍数为空，上位机默认为1倍或不稀释处理', 'category': '稀释功能'})
                                else:
                                    try:
                                        test_dil_val = float(test_dilution)
                                        if test_dil_val == 0:
                                            issues.append({'type': 'info', 'msg': f'DSP-{dsp_idx}测试稀释倍数为0，上位机默认按1倍处理', 'category': '稀释功能'})
                                        elif test_dil_val < 1:
                                            issues.append({'type': 'warn', 'msg': f'DSP-{dsp_idx}测试稀释倍数为{test_dilution}，小于1可能异常', 'category': '稀释功能'})
                                        elif test_dil_val > 1000:
                                            issues.append({'type': 'warn', 'msg': f'DSP-{dsp_idx}测试稀释倍数为{test_dilution}，超过1000倍请确认', 'category': '稀释功能'})
                                    except ValueError:
                                        issues.append({'type': 'warn', 'msg': f'DSP-{dsp_idx}测试稀释倍数非数字值"{test_dilution}"', 'category': '稀释功能'})

                # ----- 7.5 MSA 段校验（ACK 确认） -----
                elif seg_id == 'MSA':
                    if len(fields) > 1:
                        ack_code = fields[1].strip()
                        if ack_code and ack_code not in ('AA', 'AE', 'AR'):
                            issues.append({'type': 'warn', 'msg': f'MSA-1确认码异常: "{ack_code}"'})
                        elif ack_code == 'AE':
                            issues.append({'type': 'fail', 'msg': 'MSA-1=AE，表示消息被拒绝'})
                        elif ack_code == 'AR':
                            issues.append({'type': 'fail', 'msg': 'MSA-1=AR，表示消息被拒绝'})
                    if len(fields) > 2:
                        ack_msg_id = fields[2].strip()
                        if ack_msg_id:
                            if current_frame_msh10 and ack_msg_id != current_frame_msh10:
                                issues.append({'type': 'fail', 'msg': f'MSA-2控制ID "{ack_msg_id}" 与MSH-10控制ID "{current_frame_msh10}" 不匹配'})
                            elif ack_msg_id not in request_records:
                                issues.append({'type': 'warn', 'msg': f'MSA-2控制ID "{ack_msg_id}" 未找到对应的请求'})

                # ----- 7.6 QRD 段校验（查询请求） -----
                elif seg_id == 'QRD':
                    if len(fields) > 7:
                        sample_code = fields[7].strip()   # QRD-8
                        if not sample_code:
                            issues.append({'type': 'fail', 'msg': '【项目识别失败】QRD-8样本条码为空', 'category': '项目识别'})
                    if len(fields) > 1:
                        qrd_time = fields[1].strip()
                        if qrd_time and not (len(qrd_time) == 14 and qrd_time.isdigit()):
                            issues.append({'type': 'warn', 'msg': f'QRD-1请求时间格式应为YYYYMMDDHHMMSS，实际为"{qrd_time}"', 'category': '数据质量'})

                # ----- 7.7 OBR 段校验（请求信息） -----
                elif seg_id == 'OBR':
                    if len(fields) > 2:
                        sample_code = fields[2].strip()
                        if not sample_code:
                            issues.append({'type': 'warn', 'msg': 'OBR-2样本条码为空', 'category': '数据质量'})
                    if len(fields) > 7:
                        obr_date = fields[7].strip()
                        if obr_date and not (len(obr_date) == 14 and obr_date.isdigit()):
                            issues.append({'type': 'warn', 'msg': f'OBR-8检测日期格式应为YYYYMMDDHHMMSS，实际为"{obr_date}"', 'category': '数据质量'})

                # ----- 7.8 OBX 段（结果） -----
                elif seg_id == 'OBX':
                    if len(fields) > 11:
                        obx12 = fields[11].strip()
                        if obx12 and obx12 not in ('N', 'A'):
                            issues.append({'type': 'warn', 'msg': f'OBX-12测试状态应为N(正常)或A(异常)，实际为: "{obx12}"', 'category': '数据质量'})
                    if len(fields) > 9 and len(fields) > 11:
                        obx10 = fields[9].strip()
                        obx12 = fields[11].strip()
                        if obx10 and obx12 == 'N':
                            issues.append({'type': 'warn', 'msg': f'OBX-10异常标识存在但OBX-12状态为N(正常)，可能冲突', 'category': '数据质量'})
                    if len(fields) > 3 and not fields[3].strip():
                        issues.append({'type': 'warn', 'msg': '【项目识别失败】OBX-3项目编号为空', 'category': '项目识别'})
                    if len(fields) > 5 and not fields[5].strip():
                        issues.append({'type': 'warn', 'msg': 'OBX-5发光值为空', 'category': '数据质量'})

                # ----- 7.9 字段值域规则 (DSP/PID) -----
                if seg_id == 'DSP' and len(fields) > 10:
                    specimen = fields[10].strip()
                    if specimen and specimen not in VALID_SPECIMEN:
                        issues.append({'type': 'warn', 'msg': f'标本种类DSP-10="{specimen}"不在枚举值中(Ser/Plasma/Urine/BALF/CSF/Automated)'})

                if seg_id == 'PID' and len(fields) > 8:
                    pass

            line_annotations.append({'line': line_num, 'text': line, 'issues': issues})

        # ---------- 8. 交互流程规则（队列+字典索引，30秒超时配对） ----------
        # 消息链配对规则：
        #   QRY^Q02 → DSR^Q03  (设备请求LIS，LIS回复测试信息)
        #   DSR^Q03 → ACK^Q03  (设备收到LIS信息后确认)
        #   ORU^R01 → ACK^R01  (设备发送结果给LIS，LIS确认)
        # 实现：队列保证顺序，字典索引加速查找，30秒超时

        def _ts_ok(ts1, ts2):
            if ts1 is None or ts2 is None:
                return True
            return abs(ts1 - ts2) <= _TIMEOUT_TS

        # 构建队列 + 字典索引（cid → [队列索引]）
        qry_queue = [];  qry_idx = {}   # {cid: [0, 3, 7]}
        dsr_queue = [];  dsr_idx = {}
        ack_q03_queue = []; ack_q03_idx = {}
        oru_queue = [];  oru_idx = {}
        ack_r01_queue = []; ack_r01_idx = {}

        for cid, info_list in msh10_cache.items():
            for info in info_list:
                entry = {'cid': cid, 'line': info['line'], 'ts': info.get('timestamp')}
                if info['type'] == 'QRY^Q02':
                    qry_idx.setdefault(cid, []).append(len(qry_queue))
                    qry_queue.append(entry)
                elif info['type'] == 'DSR^Q03':
                    dsr_idx.setdefault(cid, []).append(len(dsr_queue))
                    dsr_queue.append(entry)
                elif info['type'] == 'ACK^Q03':
                    ack_q03_idx.setdefault(cid, []).append(len(ack_q03_queue))
                    ack_q03_queue.append(entry)
                elif info['type'] == 'ORU^R01':
                    oru_idx.setdefault(cid, []).append(len(oru_queue))
                    oru_queue.append(entry)
                elif info['type'] == 'ACK^R01':
                    ack_r01_idx.setdefault(cid, []).append(len(ack_r01_queue))
                    ack_r01_queue.append(entry)

        flow_issues = []
        qry_matched = [False] * len(qry_queue)
        dsr_matched = [False] * len(dsr_queue)
        dsr_ack_matched = [False] * len(dsr_queue)  # DSR是否也匹配了ACK^Q03
        oru_matched = [False] * len(oru_queue)
        ack_q03_matched = [False] * len(ack_q03_queue)
        ack_r01_matched = [False] * len(ack_r01_queue)

        # 链1: QRY^Q02 → DSR^Q03（字典索引O(1)查找同cid，按队列顺序取第一个未匹配的）
        for i, qry in enumerate(qry_queue):
            for j in dsr_idx.get(qry['cid'], []):
                if not dsr_matched[j] and _ts_ok(qry['ts'], dsr_queue[j]['ts']):
                    qry_matched[i] = True
                    dsr_matched[j] = True
                    break

        # 链2: DSR^Q03 → ACK^Q03（只对已匹配QRY的DSR检查）
        for j, dsr in enumerate(dsr_queue):
            if not dsr_matched[j]:
                continue
            for k in ack_q03_idx.get(dsr['cid'], []):
                if not ack_q03_matched[k] and _ts_ok(dsr['ts'], ack_q03_queue[k]['ts']):
                    dsr_ack_matched[j] = True
                    ack_q03_matched[k] = True
                    break

        # 链3: ORU^R01 → ACK^R01
        for i, oru in enumerate(oru_queue):
            for j in ack_r01_idx.get(oru['cid'], []):
                if not ack_r01_matched[j] and _ts_ok(oru['ts'], ack_r01_queue[j]['ts']):
                    oru_matched[i] = True
                    ack_r01_matched[j] = True
                    break

        # 未匹配的QRY^Q02
        for i, qry in enumerate(qry_queue):
            if not qry_matched[i]:
                flow_issues.append({
                    'type': 'warn',
                    'msg': f'QRY^Q02(控制ID={qry["cid"]}, 行{qry["line"]})30秒内未找到对应的DSR^Q03响应',
                    'line': qry['line'],
                    'category': '通信状态'
                })

        # 未匹配的DSR^Q03
        for j, dsr in enumerate(dsr_queue):
            if dsr_matched[j] and not dsr_ack_matched[j]:
                flow_issues.append({
                    'type': 'warn',
                    'msg': f'DSR^Q03(控制ID={dsr["cid"]}, 行{dsr["line"]})30秒内未找到对应的ACK^Q03确认',
                    'line': dsr['line'],
                    'category': '通信状态'
                })
            elif not dsr_matched[j]:
                flow_issues.append({
                    'type': 'warn',
                    'msg': f'DSR^Q03(控制ID={dsr["cid"]}, 行{dsr["line"]})未找到对应的QRY^Q02请求',
                    'line': dsr['line'],
                    'category': '通信状态'
                })

        # 未匹配的ORU^R01
        for i, oru in enumerate(oru_queue):
            if not oru_matched[i]:
                flow_issues.append({
                    'type': 'fail',
                    'msg': f'【通信异常】ORU^R01(控制ID={oru["cid"]}, 行{oru["line"]})30秒内未找到对应的ACK^R01确认',
                    'line': oru['line'],
                    'category': '通信状态'
                })

        # 孤立ACK^Q03
        for k, ack in enumerate(ack_q03_queue):
            if not ack_q03_matched[k]:
                flow_issues.append({
                    'type': 'warn',
                    'msg': f'ACK^Q03(控制ID={ack["cid"]}, 行{ack["line"]})未找到对应的DSR^Q03',
                    'line': ack['line'],
                    'category': '通信状态'
                })

        # 孤立ACK^R01
        for j, ack in enumerate(ack_r01_queue):
            if not ack_r01_matched[j]:
                flow_issues.append({
                    'type': 'warn',
                    'msg': f'ACK^R01(控制ID={ack["cid"]}, 行{ack["line"]})未找到对应的ORU^R01',
                    'line': ack['line'],
                    'category': '通信状态'
                })

        # 重传检测：同一控制ID的QRY多次发送且均未匹配DSR
        qry_cid_counts = Counter(q['cid'] for q in qry_queue)
        for cid, count in qry_cid_counts.items():
            any_matched = any(qry_matched[i] for i in qry_idx.get(cid, []))
            if count > 1 and not any_matched:
                lines = [qry_queue[i]['line'] for i in qry_idx.get(cid, [])]
                flow_issues.append({
                    'type': 'warn',
                    'msg': f'【通信异常】控制ID={cid} 的 QRY^Q02 重传 {count} 次且30秒内未收到DSR^Q03响应',
                    'line': lines[-1],
                    'category': '通信状态'
                })

        # 挂载 flow_issues 到对应行
        line_ann_map = {ann['line']: ann for ann in line_annotations}
        for issue in flow_issues:
            target_line = issue.get('line')
            if target_line and target_line in line_ann_map:
                line_ann_map[target_line]['issues'].append({'type': issue['type'], 'msg': issue['msg']})
            elif line_annotations:
                line_annotations[-1]['issues'].append({'type': issue['type'], 'msg': issue['msg']})

        # ---------- 9. 帧完整性全局校验 ----------
        incomplete_frames = [f for f in frames if not f['complete']]
        if incomplete_frames and line_annotations:
            for f in incomplete_frames:
                start_line = f['start_line']
                ann = line_ann_map.get(start_line)
                if ann:
                    ann['issues'].append({
                        'type': 'fail',
                        'msg': f'行{f["start_line"]}-{f["end_line"]}: 帧不完整(缺少<FS>+<CR>结束符)'
                    })
                elif line_annotations:
                    line_annotations[-1]['issues'].append({
                        'type': 'fail',
                        'msg': f'行{f["start_line"]}-{f["end_line"]}: 帧不完整(缺少<FS>+<CR>结束符)'
                    })

        # ---------- 10. 统计（基于全部行） ----------
        total_issues = fail_count = warn_count = info_count = 0
        category_stats = {}
        for a in line_annotations:
            for i in a['issues']:
                total_issues += 1
                t = i['type']
                if t == 'fail':
                    fail_count += 1
                elif t == 'warn':
                    warn_count += 1
                elif t == 'info':
                    info_count += 1
                cat = i.get('category', '其他')
                category_stats[cat] = category_stats.get(cat, 0) + 1

        # 通信状态统计
        total_requests = len(qry_queue) + len(oru_queue)
        total_responses = len(dsr_matched) + len(ack_r01_matched)
        unresponded_requests = (len(qry_queue) - len(qry_matched)) + (len(oru_queue) - len(oru_matched))

        total_retransmits = sum(c - 1 for c in qry_cid_counts.values() if c > 1)
        success_rate = ((total_requests - unresponded_requests) / total_requests * 100) if total_requests > 0 else 100

        # ---------- 11. 返回全部行（含标记），前端自行高亮 ----------
        return jsonify({
            'log_lines': line_annotations,    # 全部行，每行带有 issues（空数组表示无问题）
            'summary': {
                'filename': log_f.filename,
                'total_lines': len(log_lines_raw),
                'total_frames': len(frames),
                'complete_frames': sum(1 for f in frames if f['complete']),
                'total_issues': total_issues,
                'fail_count': fail_count,
                'warn_count': warn_count,
                'info_count': info_count,
                'series': series,
                'model': model,
                'category_stats': category_stats,
                'communication': {
                    'total_requests': total_requests,
                    'total_responses': total_responses,
                    'unresponded_requests': unresponded_requests,
                    'total_retransmits': total_retransmits,
                    'success_rate': round(success_rate, 2)
                }
            }
        })

    except Exception as e:
        logging.getLogger(__name__).exception(f"LIS解析失败: {e}")
        return jsonify({'error': '日志解析失败'}), 500



@lis_bp.route('/api/lis/templates', methods=['GET'])
def lis_list_templates():
    try:
        series = request.args.get('series', '').upper()
        with shared.db_connection() as conn:
            cur = conn.cursor()
            if series:
                cur.execute('SELECT id, series, model, filename, pdf_filename, created_at, updated_at FROM lis_protocol_templates WHERE series=%s ORDER BY series, model', (series,))
            else:
                cur.execute('SELECT id, series, model, filename, pdf_filename, created_at, updated_at FROM lis_protocol_templates ORDER BY series, model LIMIT 1000')
            rows = cur.fetchall()
            templates = [{'id': r[0], 'series': r[1], 'model': r[2], 'filename': r[3], 'pdf_filename': r[4], 'created_at': str(r[5]), 'updated_at': str(r[6])} for r in rows]
            return jsonify(templates)
    except Exception as e:
        return jsonify({'error': '服务器内部错误'}), 500



@lis_bp.route('/api/lis/templates', methods=['POST'])
@api_login_required
def lis_upload_template():
    try:
        if 'file' not in request.files:
            return jsonify({'error': '未上传文件'}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({'error': '未选择文件'}), 400
        series = request.form.get('series', '').upper()
        model = request.form.get('model', '').strip()
        if series not in ('SMART', 'VENUS'):
            return jsonify({'error': '无效的系列'}), 400
        if not model:
            return jsonify({'error': '请选择型号'}), 400

        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        file_bytes = f.read()

        if ext == 'pdf':
            pdf_data = file_bytes
            pdf_filename = f.filename
            content = _extract_hl7_from_pdf(pdf_data)
            filename = f.filename
        elif ext in ('txt', 'log', 'hl7'):
            pdf_data = None
            pdf_filename = None
            content = file_bytes.decode('utf-8', errors='ignore')
            filename = f.filename
        else:
            return jsonify({'error': '仅支持 PDF / .txt / .log / .hl7 文件'}), 400

        if not content.strip():
            content = ''

        with shared.db_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT id FROM lis_protocol_templates WHERE series=%s AND model=%s', (series, model))
            existing = cur.fetchone()
            if existing:
                if pdf_data is not None:
                    cur.execute('UPDATE lis_protocol_templates SET filename=%s, content=%s, pdf_data=%s, pdf_filename=%s, updated_at=NOW() WHERE series=%s AND model=%s', (filename, content, pdf_data, pdf_filename, series, model))
                else:
                    cur.execute('UPDATE lis_protocol_templates SET filename=%s, content=%s, updated_at=NOW() WHERE series=%s AND model=%s', (filename, content, series, model))
            else:
                cur.execute('INSERT INTO lis_protocol_templates (series, model, filename, content, pdf_data, pdf_filename) VALUES (%s, %s, %s, %s, %s, %s)', (series, model, filename, content, pdf_data, pdf_filename))
            conn.commit()
        shared.audit_log('lis_upload_template', target_type='lis_template', detail=f'上传协议模板 {series}/{model} {filename}')
        return jsonify({'success': True, 'message': f'协议模板已保存: {series} {model}', 'extracted_content': content[:500]})
    except Exception as e:
        return jsonify({'error': '服务器内部错误'}), 500



@lis_bp.route('/api/lis/templates/<series>/<model>', methods=['GET'])
def lis_get_template(series, model):
    try:
        series = series.upper()
        with shared.db_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT id, filename, content, pdf_filename, created_at, updated_at FROM lis_protocol_templates WHERE series=%s AND model=%s', (series, model))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': '未找到该型号的协议模板'}), 404
            return jsonify({'id': row[0], 'filename': row[1], 'content': row[2], 'pdf_filename': row[3], 'created_at': str(row[4]), 'updated_at': str(row[5])})
    except Exception as e:
        return jsonify({'error': '服务器内部错误'}), 500



@lis_bp.route('/api/lis/templates/<int:tid>', methods=['DELETE'])
@api_login_required
def lis_delete_template(tid):
    try:
        with shared.db_connection() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM lis_protocol_templates WHERE id=%s RETURNING id', (tid,))
            if not cur.fetchone():
                return jsonify({'error': '模板不存在'}), 404
            conn.commit()
        shared.audit_log('lis_delete_template', target_type='lis_template', target_id=tid, detail=f'删除协议模板#{tid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': '服务器内部错误'}), 500



@lis_bp.route('/api/lis/templates/<int:tid>/content', methods=['PUT'])
@api_login_required
def lis_update_template_content(tid):
    try:
        data = request.get_json()
        content = data.get('content', '') if data else ''
        with shared.db_connection() as conn:
            cur = conn.cursor()
            cur.execute('UPDATE lis_protocol_templates SET content=%s, updated_at=NOW() WHERE id=%s RETURNING id', (content, tid))
            if not cur.fetchone():
                return jsonify({'error': '模板不存在'}), 404
            conn.commit()
        shared.audit_log('lis_update_template_content', target_type='lis_template', target_id=tid, detail=f'更新协议模板#{tid}内容')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': '服务器内部错误'}), 500



@lis_bp.route('/api/lis/templates/<int:tid>/pdf', methods=['GET'])
def lis_download_template_pdf(tid):
    try:
        with shared.db_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT pdf_data, pdf_filename FROM lis_protocol_templates WHERE id=%s', (tid,))
            row = cur.fetchone()
            if not row or not row[0]:
                return jsonify({'error': '无PDF文件'}), 404
            filename = row[1] or 'protocol.pdf'
            pdf_bytes = bytes(row[0]) if isinstance(row[0], memoryview) else row[0]
            from flask import Response
            from urllib.parse import quote
            response = Response(pdf_bytes, mimetype='application/pdf')
            encoded_name = quote(filename)
            response.headers['Content-Disposition'] = f"inline; filename*=UTF-8''{encoded_name}"
            return response
    except Exception as e:
        return jsonify({'error': '服务器内部错误'}), 500


def _extract_hl7_from_pdf(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
        return '\n'.join(text_parts)
    except Exception:
        return ''


def _build_hl7_message(msg_type, fields):
    now = time.strftime('%Y%m%d%H%M%S')
    sending_app = fields.get('sending_app', 'IVD')
    sending_fac = fields.get('sending_fac', 'IVD_LAB')
    receiving_app = fields.get('receiving_app', 'LIS')
    receiving_fac = fields.get('receiving_fac', 'HIS')
    msg_ctrl_id = fields.get('msg_ctrl_id', f'IVD{now}')
    seg = '|'
    msh = f'MSH|^~\\&|{sending_app}|{sending_fac}|{receiving_app}|{receiving_fac}|{now}||{msg_type}|{msg_ctrl_id}|P|2.4'
    segments = [msh]
    if msg_type.startswith('QRY'):
        qrd = f'QRD|{now}|R|{msg_ctrl_id}|||||{fields.get("query_type", "LAB")}|||||'
        qrf = f'QRF|{fields.get("query_name", "ALL")}|||||'
        segments.extend([qrd, qrf])
    elif msg_type.startswith('ORU'):
        pid = f'PID|||{fields.get("patient_id", "P001")}||{fields.get("patient_name", "TEST")}||{fields.get("dob", "19900101")}|{fields.get("gender", "M")}'
        obr = f'OBR|||{fields.get("specimen_id", "S001")}||{fields.get("test_name", "GLU")}||{now}'
        obx_lines = []
        results = fields.get('results', [])
        if not results:
            results = [{'value': '5.2', 'unit': 'mmol/L', 'test': 'GLU', 'ref_range': '3.9-6.1'}]
        for i, r in enumerate(results, 1):
            obx_lines.append(f'OBX|NM|{r.get("test", "GLU")}^{r.get("test", "GLU")}|||{r.get("value", "5.2")}|{r.get("unit", "mmol/L")}|{r.get("ref_range", "3.9-6.1")}|||F|||{now}')
        segments.extend([pid, obr] + obx_lines)
    return '\x0b' + '\r'.join(segments) + '\x1c\r'


@lis_bp.route('/api/lis/simulate', methods=['POST'])
@api_login_required
def lis_simulate():
    try:
        data = request.get_json() or {}
        msg_type = data.get('msg_type', 'QRY^Q02')
        host = data.get('host', '').strip()
        port = data.get('port', 0)
        timeout_sec = min(data.get('timeout', 5), 30)
        fields = data.get('fields', {})
        if msg_type not in _VALID_MSG_TYPES:
            return jsonify({'error': f'不支持的消息类型: {msg_type}'}), 400
        hl7_msg = _build_hl7_message(msg_type, fields)
        if not host or not port:
            return jsonify({'hl7_message': hl7_msg, 'note': '仅生成消息，未指定目标地址'})
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout_sec)
            sock.connect((host, int(port)))
            sock.sendall(hl7_msg.encode('utf-8'))
            response = b''
            start = time.time()
            while time.time() - start < timeout_sec:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    if b'\x1c' in response:
                        break
                except socket.timeout:
                    break
            sock.close()
            resp_text = response.decode('utf-8', errors='replace')
            return jsonify({
                'hl7_message': hl7_msg,
                'response': resp_text,
                'response_length': len(response),
                'status': 'success',
                'target': f'{host}:{port}',
            })
        except ConnectionRefusedError:
            return jsonify({'hl7_message': hl7_msg, 'error': f'连接被拒绝: {host}:{port}', 'status': 'connection_refused'}), 502
        except socket.timeout:
            return jsonify({'hl7_message': hl7_msg, 'error': f'连接超时: {host}:{port}', 'status': 'timeout'}), 504
        except Exception as e:
            return jsonify({'hl7_message': hl7_msg, 'error': '连接失败', 'status': 'error'}), 502
    except Exception as e:
        logging.getLogger(__name__).exception(f"LIS模拟失败: {e}")
        return jsonify({'error': '模拟测试失败'}), 500


@lis_bp.route('/lis-simulator')
@login_required
def lis_simulator_page():
    return render_template_string('''
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LIS 协议模拟测试</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
<style>
body { background: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.page-header { background: linear-gradient(135deg, #7c3aed, #a78bfa); color: white; padding: 24px 0; margin-bottom: 24px; }
.sim-card { background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 20px; margin-bottom: 16px; }
.msg-preview { background: #1e293b; color: #a5f3fc; border-radius: 8px; padding: 16px; font-family: 'Courier New', monospace; font-size: 0.8rem; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; }
.resp-preview { background: #1e293b; color: #86efac; border-radius: 8px; padding: 16px; font-family: 'Courier New', monospace; font-size: 0.8rem; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; }
.form-label { font-weight: 600; font-size: 0.875rem; color: #475569; }
.btn-send { background: linear-gradient(135deg, #7c3aed, #6d28d9); color: white; border: none; border-radius: 8px; padding: 10px 24px; font-weight: 600; }
.btn-send:hover { background: linear-gradient(135deg, #6d28d9, #5b21b6); color: white; }
.btn-generate { background: #f1f5f9; color: #475569; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 24px; }
.status-badge { font-size: 0.75rem; padding: 4px 12px; border-radius: 20px; }
.status-success { background: #dcfce7; color: #166534; }
.status-error { background: #fee2e2; color: #991b1b; }
.status-timeout { background: #fef3c7; color: #92400e; }
</style>
</head>
<body>
<div class="page-header">
    <div class="container">
        <div class="d-flex align-items-center">
            <a href="/lis-issues" class="text-white text-decoration-none me-3"><i class="bi bi-arrow-left"></i></a>
            <h4 class="mb-0"><i class="bi bi-broadcast me-2"></i>LIS 协议模拟测试</h4>
        </div>
    </div>
</div>
<div class="container">
    <div class="row">
        <div class="col-lg-6">
            <div class="sim-card">
                <h6 class="mb-3"><i class="bi bi-gear me-1"></i> 消息配置</h6>
                <div class="mb-3">
                    <label class="form-label">消息类型</label>
                    <select class="form-select" id="msgType" onchange="syncTemplate()">
                        <option value="QRY^Q02">QRY^Q02 (查询)</option>
                        <option value="ORU^R01">ORU^R01 (结果上报)</option>
                        <option value="ACK^R01">ACK^R01 (确认)</option>
                    </select>
                </div>
                <div class="row mb-3">
                    <div class="col-6">
                        <label class="form-label">发送应用</label>
                        <input type="text" class="form-control" id="sendingApp" value="IVD">
                    </div>
                    <div class="col-6">
                        <label class="form-label">接收应用</label>
                        <input type="text" class="form-control" id="receivingApp" value="LIS">
                    </div>
                </div>
                <div class="row mb-3">
                    <div class="col-6">
                        <label class="form-label">发送机构</label>
                        <input type="text" class="form-control" id="sendingFac" value="IVD_LAB">
                    </div>
                    <div class="col-6">
                        <label class="form-label">接收机构</label>
                        <input type="text" class="form-control" id="receivingFac" value="HIS">
                    </div>
                </div>
                <div id="oruFields" style="display:none;">
                    <h6 class="mt-3 mb-2"><i class="bi bi-person me-1"></i> 患者信息</h6>
                    <div class="row mb-2">
                        <div class="col-4"><input type="text" class="form-control form-control-sm" id="patientId" value="P001" placeholder="患者ID"></div>
                        <div class="col-4"><input type="text" class="form-control form-control-sm" id="patientName" value="TEST" placeholder="姓名"></div>
                        <div class="col-2"><input type="text" class="form-control form-control-sm" id="gender" value="M" placeholder="性别"></div>
                        <div class="col-2"><input type="text" class="form-control form-control-sm" id="dob" value="19900101" placeholder="生日"></div>
                    </div>
                    <h6 class="mt-3 mb-2"><i class="bi bi-test-tube me-1"></i> 检验结果</h6>
                    <div class="row mb-2">
                        <div class="col-3"><input type="text" class="form-control form-control-sm" id="testName" value="GLU" placeholder="项目"></div>
                        <div class="col-3"><input type="text" class="form-control form-control-sm" id="testValue" value="5.2" placeholder="结果"></div>
                        <div class="col-3"><input type="text" class="form-control form-control-sm" id="testUnit" value="mmol/L" placeholder="单位"></div>
                        <div class="col-3"><input type="text" class="form-control form-control-sm" id="testRef" value="3.9-6.1" placeholder="参考范围"></div>
                    </div>
                </div>
            </div>
        </div>
        <div class="col-lg-6">
            <div class="sim-card">
                <h5 class="mb-3"><i class="bi bi-book me-1"></i> HL7 标准模板参考</h5>
                <div class="mb-2">
                    <button class="btn btn-sm btn-outline-primary active" id="tplBtnQry" onclick="showTemplate('qry')">QRY^Q02 查询</button>
                    <button class="btn btn-sm btn-outline-primary" id="tplBtnOru" onclick="showTemplate('oru')">ORU^R01 结果上报</button>
                    <button class="btn btn-sm btn-outline-primary" id="tplBtnAck" onclick="showTemplate('ack')">ACK 确认</button>
                </div>
                <div id="tplQry">
                    <div class="msg-preview" style="color:#e2e8f0;">MSH|^~\\&|IVD|IVD_LAB|LIS|HIS|20260811143000||QRY^Q02|IVD20260811143000|P|2.4
QRD|20260811143000|R|IVD20260811143000|||||LAB|||||
QRF|ALL|||||</div>
                    <table class="table table-sm table-bordered mt-2" style="font-size:0.8rem;">
                        <thead><tr style="background:#f1f5f9;"><th width="80">段</th><th width="60">字段</th><th width="120">名称</th><th>说明</th></tr></thead>
                        <tbody>
                            <tr><td rowspan="6"><b>MSH</b><br>消息头</td><td>MSH-1</td><td>字段分隔符</td><td>| 竖线，所有段必须以此开头</td></tr>
                            <tr><td>MSH-2</td><td>编码字符</td><td>^~\\& 分别为：组件/重复/转义/子组件分隔符</td></tr>
                            <tr><td>MSH-3</td><td>发送应用</td><td>IVD — 发送方应用名（本设备）</td></tr>
                            <tr><td>MSH-5</td><td>接收应用</td><td>LIS — 接收方应用名（目标系统）</td></tr>
                            <tr><td>MSH-8</td><td>消息类型</td><td><b>QRY^Q02</b> — 查询消息^触发事件</td></tr>
                            <tr><td>MSH-10</td><td>消息控制ID</td><td>唯一标识，用于配对请求和响应</td></tr>
                            <tr><td rowspan="3"><b>QRD</b><br>查询定义</td><td>QRD-1</td><td>查询时间</td><td>格式: YYYYMMDDHHMMSS</td></tr>
                            <tr><td>QRD-3</td><td>查询ID</td><td>与MSH-10一致，用于响应配对</td></tr>
                            <tr><td>QRD-8</td><td>查询内容</td><td><b>LAB</b> — 查询检验项目</td></tr>
                            <tr><td><b>QRF</b><br>查询过滤</td><td>QRF-1</td><td>过滤值</td><td><b>ALL</b> — 查询全部结果</td></tr>
                        </tbody>
                    </table>
                </div>
                <div id="tplOru" style="display:none;">
                    <div class="msg-preview" style="color:#e2e8f0;">MSH|^~\\&|IVD|IVD_LAB|LIS|HIS|20260811143000||ORU^R01|IVD20260811143000|P|2.4
PID|||P001||张三||19900101|M
OBR|||S001||GLU||20260811143000
OBX|NM|GLU^GLU|||5.2|mmol/L|3.9-6.1|||F|||20260811143000</div>
                    <table class="table table-sm table-bordered mt-2" style="font-size:0.8rem;">
                        <thead><tr style="background:#f1f5f9;"><th width="80">段</th><th width="60">字段</th><th width="120">名称</th><th>说明</th></tr></thead>
                        <tbody>
                            <tr><td><b>MSH</b></td><td>MSH-8</td><td>消息类型</td><td><b>ORU^R01</b> — 结果上报消息</td></tr>
                            <tr><td rowspan="4"><b>PID</b><br>患者信息</td><td>PID-3</td><td>患者ID</td><td>P001 — 病员号/住院号</td></tr>
                            <tr><td>PID-5</td><td>患者姓名</td><td>张三 — 患者名称</td></tr>
                            <tr><td>PID-7</td><td>出生日期</td><td>格式: YYYYMMDD</td></tr>
                            <tr><td>PID-8</td><td>性别</td><td>M=男 F=女 O=其他</td></tr>
                            <tr><td rowspan="2"><b>OBR</b><br>检验请求</td><td>OBR-3</td><td>样本号</td><td>S001 — 样本条码/编号</td></tr>
                            <tr><td>OBR-5</td><td>检验项目</td><td>GLU — 葡萄糖检测</td></tr>
                            <tr><td rowspan="5"><b>OBX</b><br>结果数据</td><td>OBX-2</td><td>值类型</td><td>NM=数值 ST=文本</td></tr>
                            <tr><td>OBX-3</td><td>项目编号</td><td>GLU^GLU — 项目码^项目名</td></tr>
                            <tr><td>OBX-5</td><td>检测值</td><td><b>5.2</b> — 实际检测结果</td></tr>
                            <tr><td>OBX-6</td><td>单位</td><td>mmol/L — 结果单位</td></tr>
                            <tr><td>OBX-7</td><td>参考范围</td><td>3.9-6.1 — 正常范围</td></tr>
                        </tbody>
                    </table>
                </div>
                <div id="tplAck" style="display:none;">
                    <div class="msg-preview" style="color:#e2e8f0;">MSH|^~\\&|LIS|HIS|IVD|IVD_LAB|20260811143001||ACK^R01|LIS20260811143001|P|2.4
MSA|AA|IVD20260811143000</div>
                    <table class="table table-sm table-bordered mt-2" style="font-size:0.8rem;">
                        <thead><tr style="background:#f1f5f9;"><th width="80">段</th><th width="60">字段</th><th width="120">名称</th><th>说明</th></tr></thead>
                        <tbody>
                            <tr><td><b>MSH</b></td><td>MSH-8</td><td>消息类型</td><td><b>ACK^R01</b> — 确认响应</td></tr>
                            <tr><td rowspan="2"><b>MSA</b><br>确认段</td><td>MSA-1</td><td>确认码</td><td><b>AA</b>=接受 AE=错误 AR=拒绝</td></tr>
                            <tr><td>MSA-2</td><td>消息控制ID</td><td>必须与请求的MSH-10一致，表示对哪条消息的确认</td></tr>
                        </tbody>
                    </table>
                </div>
                <div class="mt-3 p-3" style="background:#fef3c7;border-radius:8px;font-size:0.85rem;">
                    <b><i class="bi bi-lightbulb me-1"></i>关键要点：</b>
                    <ul class="mb-0 mt-1">
                        <li><b>帧格式</b>：完整消息以 <code>[VT]</code>(\x0b) 开头，<code>[FS][CR]</code>(\x1c\r) 结尾</li>
                        <li><b>段分隔</b>：段与段之间用 <code>\r</code>(回车) 分隔，不是换行</li>
                        <li><b>字段分隔</b>：字段内用 <code>|</code> 分隔，组件用 <code>^</code> 分隔</li>
                        <li><b>配对规则</b>：QRY^Q02 → DSR^Q03（查询→响应），ORU^R01 → ACK^R01（上报→确认）</li>
                        <li><b>控制ID</b>：MSH-10是消息唯一标识，响应的MSA-2必须与请求的MSH-10匹配</li>
                        <li><b>超时机制</b>：30秒内未收到响应视为通信异常</li>
                    </ul>
                </div>
            </div>
        </div>
    </div>
</div>
<script>
function showTemplate(type) {
    document.getElementById('tplQry').style.display = type === 'qry' ? 'block' : 'none';
    document.getElementById('tplOru').style.display = type === 'oru' ? 'block' : 'none';
    document.getElementById('tplAck').style.display = type === 'ack' ? 'block' : 'none';
    document.getElementById('tplBtnQry').classList.toggle('active', type === 'qry');
    document.getElementById('tplBtnOru').classList.toggle('active', type === 'oru');
    document.getElementById('tplBtnAck').classList.toggle('active', type === 'ack');
function syncTemplate() {
    const msgType = document.getElementById('msgType').value;
    document.getElementById('oruFields').style.display = msgType.startsWith('ORU') ? 'block' : 'none';
    if (msgType.startsWith('QRY')) showTemplate('qry');
    else if (msgType.startsWith('ORU')) showTemplate('oru');
    else if (msgType.startsWith('ACK')) showTemplate('ack');
}
</script>
</body>
</html>
''')


