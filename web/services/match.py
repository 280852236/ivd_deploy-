import re
import json
from typing import Dict, List, Optional
from services.db import db_connection
from services.text_utils import extract_line_context, extract_nearest_timestamp, normalize_event_date
from psycopg2.extras import RealDictCursor
from shared import resolve_table

_RE_LONG_HEX = re.compile(r'([A-F0-9]{12,32})', re.IGNORECASE)
_RE_SPACE_TRIPLE_DEC = re.compile(r'(\d{2})\s+(\d{2})\s+(\d{2})')
_RE_SPACE_TRIPLE_HEX = re.compile(r'([0-9A-Fa-f]{2})\s+([0-9A-Fa-f]{2})\s+([0-9A-Fa-f]{2})')


def _get_motor_status_tables():
    from shared import get_redis
    try:
        r = get_redis()
        cached = r.get("motor_status_tables")
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'motor_status_%'")
        tables = [row[0] for row in cur.fetchall()]
    try:
        r = get_redis()
        r.setex("motor_status_tables", 3600, json.dumps(tables))
    except Exception:
        pass
    return tables


def _resolve_motor_status_table(model: str) -> Optional[str]:
    if not model:
        return None
    table = resolve_table(model, 'motor_status')
    return table


def _batch_lookup_motor_status(triples: list, model: str = '') -> dict:
    if not triples:
        return {}
    from shared import get_redis
    r = get_redis()
    results_map = {}
    uncached = []
    pipe = r.pipeline()
    for board, motor, status in triples:
        pipe.get(f"motor:{board.upper()}:{motor.upper()}:{status.upper()}")
    cached = pipe.execute()
    for (board, motor, status), raw in zip(triples, cached):
        key = (board.upper(), motor.upper(), status.upper())
        if raw:
            try:
                results_map[key] = json.loads(raw)
            except Exception:
                uncached.append((board, motor, status))
        else:
            uncached.append((board, motor, status))
    if not uncached:
        return results_map
    table_name = _resolve_motor_status_table(model)
    if table_name:
        # VALUES JOIN 替代 OR 链，更高效
        values_clause = ", ".join(["(%s, %s, %s)"] * len(uncached))
        params = []
        for board, motor, status in uncached:
            params.extend([board, motor, status])
        query = f"""SELECT t.board_card, t.motor_code, t.status_code, t.motor_name,
                           t.action_type, t.target_value, t.sensor, t.description, t.full_description
                    FROM {table_name} t
                    JOIN (VALUES {values_clause}) AS v(board_card, motor_code, status_code)
                    ON t.board_card = v.board_card AND t.motor_code = v.motor_code AND t.status_code = v.status_code"""
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(query, params)
            for row in cur.fetchall():
                key = (row['board_card'].upper(), row['motor_code'].upper(), row['status_code'].upper())
                if key not in results_map:
                    row_dict = dict(row)
                    results_map[key] = row_dict
                    try:
                        r.setex(f"motor:{key[0]}:{key[1]}:{key[2]}", 86400, json.dumps(row_dict, ensure_ascii=False))
                    except Exception:
                        pass
        return results_map
    tables = _get_motor_status_tables()
    if not tables:
        return results_map
    # VALUES JOIN 替代 OR 链
    values_clause = ", ".join(["(%s, %s, %s)"] * len(uncached))
    params = []
    for board, motor, status in uncached:
        params.extend([board, motor, status])
    union_parts = []
    all_params = []
    for table_name in tables:
        union_parts.append(f"""
            SELECT t.board_card, t.motor_code, t.status_code, t.motor_name,
                   t.action_type, t.target_value, t.sensor, t.description, t.full_description
            FROM {table_name} t
            JOIN (VALUES {values_clause}) AS v(board_card, motor_code, status_code)
            ON t.board_card = v.board_card AND t.motor_code = v.motor_code AND t.status_code = v.status_code
        """)
        all_params.extend(params)
    query = " UNION ALL ".join(union_parts)
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, all_params)
        for row in cur.fetchall():
            key = (row['board_card'].upper(), row['motor_code'].upper(), row['status_code'].upper())
            if key not in results_map:
                row_dict = dict(row)
                results_map[key] = row_dict
                try:
                    r.setex(f"motor:{key[0]}:{key[1]}:{key[2]}", 86400, json.dumps(row_dict, ensure_ascii=False))
                except Exception:
                    pass
    return results_map


def _format_match_result(board_card: str, motor_code: str, status_code: str, db_row: Optional[Dict]) -> Optional[Dict]:
    if not db_row:
        return None
    diagnosis = db_row['full_description'] if db_row['full_description'] else (db_row['description'] + '失败/异常')
    command_parts = []
    if db_row['action_type']:
        command_parts.append(db_row['action_type'])
    if db_row['target_value']:
        command_parts.append(db_row['target_value'])
    if db_row['sensor']:
        command_parts.append(db_row['sensor'])
    command_text = ' | '.join(command_parts) if command_parts else (db_row['description'] or diagnosis)
    db_desc = db_row['full_description'] or db_row['description'] or diagnosis
    return {
        'type': 'motor_status_match',
        'board_card': board_card,
        'motor_code': motor_code,
        'status_code': status_code,
        'motor_name': db_row['motor_name'],
        'action_type': db_row['action_type'],
        'target_value': db_row['target_value'],
        'sensor': db_row['sensor'],
        'description': db_row['description'],
        'full_description': db_row['full_description'],
        'db_match_text': db_desc,
        'db_command': command_text,
        'diagnosis': diagnosis,
        'command': command_text,
        'keywords': [f"{board_card} {motor_code} {status_code}"],
        'advice': diagnosis,
        'source': '电机状态表'
    }


def lookup_motor_status_by_code(board_card: str, motor_code: str, status_code: str) -> Optional[Dict]:
    results_map = _batch_lookup_motor_status([(board_card, motor_code, status_code)])
    key = (board_card.upper(), motor_code.upper(), status_code.upper())
    db_row = results_map.get(key)
    return _format_match_result(board_card, motor_code, status_code, db_row)


def find_motor_status_matches(text: str, model: str = '') -> List[Dict]:
    seen = set()
    hex_entries = []

    for match in _RE_LONG_HEX.finditer(text):
        hex_str = match.group(1).upper()
        groups = [hex_str[i:i+2] for i in range(0, len(hex_str), 2)]
        if len(groups) >= 4:
            board = groups[0]
            motor = groups[2]
            status = groups[3]
            key = (board, motor, status, match.start())
            if key not in seen:
                seen.add(key)
                hex_entries.append({
                    'board': board, 'motor': motor, 'status': status,
                    'hex_str': hex_str, 'pos': match.start()
                })

    for pattern in [_RE_SPACE_TRIPLE_DEC, _RE_SPACE_TRIPLE_HEX]:
        for match in pattern.finditer(text):
            board_card, motor_code, status_code = match.group(1).upper(), match.group(2).upper(), match.group(3).upper()
            key = (board_card, motor_code, status_code, match.start())
            if key not in seen:
                seen.add(key)
                hex_entries.append({
                    'board': board_card, 'motor': motor_code, 'status': status_code,
                    'hex_str': f'{board_card} {motor_code} {status_code}', 'pos': match.start()
                })

    triples = [(e['board'], e['motor'], e['status']) for e in hex_entries]
    results_map = _batch_lookup_motor_status(triples, model)

    matches = []
    unmatched_hex = []
    for entry in hex_entries:
        board, motor, status = entry['board'], entry['motor'], entry['status']
        key = (board.upper(), motor.upper(), status.upper())
        db_row = results_map.get(key)
        result = _format_match_result(board, motor, status, db_row)
        ts = extract_nearest_timestamp(text, entry['pos']) or ''
        if result:
            result['original_text'] = extract_line_context(text, entry['pos'])
            result['event_time'] = ts
            result['event_date'] = normalize_event_date(ts)
            result['raw_hex'] = entry['hex_str']
            matches.append(result)
        else:
            unmatched_hex.append({
                'hex_str': entry['hex_str'],
                'board': board,
                'motor': motor,
                'status': status,
                'original_text': extract_line_context(text, entry['pos']),
                'event_time': ts,
                'event_date': normalize_event_date(ts)
            })

    for uh in unmatched_hex:
        matches.append({
            'type': 'motor_status_match',
            'board_card': uh['board'],
            'motor_code': uh['motor'],
            'status_code': uh['status'],
            'motor_name': '',
            'action_type': '',
            'target_value': '',
            'sensor': '',
            'description': '',
            'full_description': '',
            'db_match_text': '',
            'db_command': '',
            'diagnosis': f"未识别故障码 [{uh['hex_str']}]，板卡:{uh['board']} 电机:{uh['motor']} 状态:{uh['status']}，请补充电机状态表数据",
            'command': '',
            'keywords': [f"{uh['board']} {uh['motor']} {uh['status']}"],
            'advice': f"未识别故障码 [{uh['hex_str']}]，板卡:{uh['board']} 电机:{uh['motor']} 状态:{uh['status']}，请补充电机状态表数据",
            'source': '电机状态表(未匹配)',
            'original_text': uh['original_text'],
            'event_time': uh['event_time'],
            'event_date': uh['event_date'],
            'raw_hex': uh.get('hex_str', ''),
            'unmatched': True
        })
    return matches
