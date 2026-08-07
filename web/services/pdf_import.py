import re
from typing import Dict, List
from services.data_init import ensure_table_exists, get_table_name
from services.db import db_connection
from psycopg2.extras import execute_values

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_LONG_HEX_RE = re.compile(r'([A-F0-9]{12,32})', re.IGNORECASE)
_LINE_START_RE = re.compile(r'^(?:[A-Z0-9]{6}|[0-9A-Fa-f]{2}[ \t\u00A0\u3000]+[0-9A-Fa-f]{2}[ \t\u00A0\u3000]+[0-9A-Fa-f]{2})')
_LEAD_SEP_RE = re.compile(r'^[\s:：\-–—]+')
_MULTI_SPACE_RE = re.compile(r'\s+')


def extract_fault_entries(text: str) -> List[Dict]:
    text = _HTML_TAG_RE.sub('', text)
    lines = text.splitlines()
    entries = []
    seen = set()
    i = 0
    total = len(lines)
    skip_phrases = ['版本号', '第', '页', '审核', '审批', '文件编号', '编制', '批准', '日期', '修改', '受控状态', 'SMART', '电机状态表']

    while i < total:
        line = lines[i].rstrip('\r\n').strip()
        if not line:
            i += 1
            continue
        if any(phrase in line for phrase in skip_phrases) and len(line) < 50:
            i += 1
            continue
        match = _LONG_HEX_RE.search(line)
        if not match:
            i += 1
            continue
        hex_str = match.group(1).upper()
        board = hex_str[0:2].upper()
        motor = hex_str[2:4].upper()
        status = hex_str[4:6].upper()
        key = (board, motor, status, i, match.start())
        if key in seen:
            i += 1
            continue
        seen.add(key)
        description = line[match.end():].strip()
        description = _LEAD_SEP_RE.sub('', description)
        j = i + 1
        while j < total:
            next_line = lines[j].strip()
            if not next_line:
                j += 1
                continue
            if _LONG_HEX_RE.search(next_line) and _LINE_START_RE.match(next_line):
                break
            if _LINE_START_RE.match(next_line):
                break
            description += (' ' + next_line) if description else next_line
            j += 1
        i = j
        description = _MULTI_SPACE_RE.sub(' ', description).strip()
        full_description = (description + '失败/异常') if description else '失败/异常'
        entry = {
            'board_card': board,
            'motor_code': motor,
            'status_code': status,
            'motor_name': '',
            'action_type': '',
            'target_value': '',
            'sensor': '',
            'description': description,
            'full_description': full_description,
            'keywords': f"{board} {motor} {status}"
        }
        entries.append(entry)
    return entries


def store_pdf_entries(entries: List[Dict], series: str, model: str) -> int:
    if not entries:
        return 0
    ensure_table_exists(model)
    table_name = get_table_name(model)
    values = []
    for entry in entries:
        board_card = entry.get('board_card', '').strip().upper()
        motor_code = entry.get('motor_code', '').strip().upper()
        status_code = entry.get('status_code', '').strip().upper()
        if not (board_card and motor_code and status_code):
            continue
        values.append((
            board_card, motor_code, status_code,
            entry.get('motor_name', ''),
            entry.get('action_type', ''),
            entry.get('target_value', ''),
            entry.get('sensor', ''),
            entry.get('description', ''),
            entry.get('full_description', ''),
            'PDF导入'
        ))
    if not values:
        return 0
    with db_connection() as conn:
        cur = conn.cursor()
        execute_values(
            cur,
            f'''INSERT INTO {table_name} (
                board_card, motor_code, status_code, motor_name,
                action_type, target_value, sensor, description,
                full_description, source_file
            ) VALUES %s
            ON CONFLICT (board_card, motor_code, status_code) DO NOTHING''',
            values
        )
        return cur.rowcount
