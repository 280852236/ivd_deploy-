import hashlib
import json
import requests
import logging
from typing import Dict, List
from services.text_utils import (
    escape_html,
    extract_line_context,
    extract_nearest_timestamp,
    normalize_event_date,
    check_aspiration_anomaly,
    CONDITION_HIGHLIGHT_MAP
)
from services.match import find_motor_status_matches

logger = logging.getLogger(__name__)

try:
    import ahocorasick
    _HAS_AHOCORASICK = True
except ImportError:
    _HAS_AHOCORASICK = False

from collections import OrderedDict

_automaton_cache = OrderedDict()
_AUTOMATON_CACHE_MAX = 32


def _evict_automaton_cache():
    while len(_automaton_cache) > _AUTOMATON_CACHE_MAX:
        _automaton_cache.popitem(last=False)


def _rules_signature(rules: List[Dict]) -> str:
    sig_parts = []
    for rule in rules:
        keywords = rule.get('keywords', [])
        if isinstance(keywords, str):
            keywords = [kw.strip() for kw in keywords.split(',') if kw.strip()]
        sig_parts.append(f"{rule.get('id','')}:" + ",".join(keywords) + f":{rule.get('source','')}" )
    return hashlib.sha256("|".join(sig_parts).encode('utf-8')).hexdigest()


def _get_automaton(rules: List[Dict]):
    signature = _rules_signature(rules)
    if signature in _automaton_cache:
        return _automaton_cache[signature]
    keywords_map = {}
    for rule_idx, rule in enumerate(rules):
        keywords = rule.get('keywords', [])
        if isinstance(keywords, str):
            keywords = [kw.strip() for kw in keywords.split(',') if kw.strip()]
        for keyword in keywords:
            kw_lower = keyword.lower()
            keywords_map[kw_lower] = (keyword, rule_idx)
    automaton = _build_automaton(keywords_map)
    _automaton_cache[signature] = automaton
    _evict_automaton_cache()
    return automaton


def _build_automaton(keywords_map):
    if not _HAS_AHOCORASICK:
        return None
    A = ahocorasick.Automaton()
    for kw_lower, (keyword, rule_idx) in keywords_map.items():
        A.add_word(kw_lower, (keyword, rule_idx))
    A.make_automaton()
    return A


def convert_go_results(go_results: list) -> list:
    converted = []
    for item in go_results:
        if item['type'] == 'motor_status_match':
            mm = item.get('motor_match', {})
            converted.append({
                'type': 'motor_status_match',
                'board_card': mm.get('board_card', ''),
                'motor_code': mm.get('motor_code', ''),
                'status_code': mm.get('status_code', ''),
                'motor_name': mm.get('motor_name', ''),
                'action_type': mm.get('action_type', ''),
                'target_value': mm.get('target_value', ''),
                'sensor': mm.get('sensor', ''),
                'description': mm.get('description', ''),
                'full_description': mm.get('full_description', ''),
                'db_match_text': mm.get('diagnosis', ''),
                'db_command': mm.get('command', ''),
                'diagnosis': mm.get('diagnosis', ''),
                'command': mm.get('command', ''),
                'keywords': mm.get('keywords', []),
                'advice': mm.get('advice', ''),
                'source': mm.get('source', ''),
                'original_text': item.get('original_text', ''),
                'event_time': item.get('event_time', ''),
                'event_date': item.get('event_date', ''),
                'unmatched': mm.get('unmatched', False),
            })
        elif item['type'] == 'keyword_match':
            advice = item.get('advice', '')
            matched_conditions = item.get('matched_conditions', [])
            if matched_conditions and any(ord(ch) < 32 for ch in advice):
                advice = f"检测到 {len(matched_conditions)} 个异常条件：" + '、'.join(matched_conditions)
            converted.append({
                'type': 'keyword_match',
                'keywords': item.get('keywords', []),
                'advice': advice,
                'source': item.get('source', ''),
                'original_text': item.get('original_text', ''),
                'event_time': item.get('event_time', ''),
                'event_date': item.get('event_date', ''),
                'matched_conditions': matched_conditions,
                'matched_count': item.get('matched_count', 0),
            })
    return converted


# Go Parser 连接池复用
_go_parser_session = None

def _get_go_parser_session():
    global _go_parser_session
    if _go_parser_session is None:
        _go_parser_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=2)
        _go_parser_session.mount('http://', adapter)
        _go_parser_session.mount('https://', adapter)
    return _go_parser_session


def analyze_text(text: str, rules: List[Dict], series: str = '', model: str = '', skip_motor_status: bool = False, go_parser_url: str = '') -> List[Dict]:
    if not text:
        return []
    if series and model and go_parser_url:
        try:
            resp = _get_go_parser_session().post(
                go_parser_url,
                json={'text': text, 'series': series, 'model': model, 'skip_motor_status': skip_motor_status},
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                go_results = data.get('results', [])
                if go_results is not None:
                    logger.info(f"Go-parser解析成功, 结果数: {len(go_results)}")
                    return convert_go_results(go_results)
            else:
                logger.warning(f"Go-parser返回非200状态码: {resp.status_code}")
        except requests.Timeout:
            logger.warning("Go-parser超时(30s), 回退到Python解析")
        except Exception as e:
            logger.warning(f"Go-parser异常: {e}, 回退到Python解析")
    matched = []
    if not skip_motor_status:
        matched.extend(find_motor_status_matches(text, model))
    lines = text.splitlines()
    matched_lines = set()
    for ln in lines:
        result = check_aspiration_anomaly(ln)
        if result['matched']:
            keyword = '样本空吸' if result['type'] == 'sample' else '试剂空吸'
            conditions_str = '、'.join(result['conditions'])
            advice_text = f"检测到 {len(result['conditions'])} 个异常条件：{conditions_str}。建议检查样本/试剂供应情况和管路连接。"
            matched.append({
                'type': 'keyword_match',
                'keywords': [keyword],
                'advice': advice_text,
                'source': '异常条件检测',
                'original_text': ln.strip(),
                'event_time': (_et := extract_nearest_timestamp(ln, 0)) or '',
                'event_date': normalize_event_date(_et),
                'matched_conditions': result['conditions'],
                'matched_count': len(result['conditions'])
            })
            matched_lines.add(ln.strip())
    text_lower = text.lower()
    automaton = _get_automaton(rules)
    if automaton:
        seen = set()
        for end_idx, (keyword, rule_idx) in automaton.iter(text_lower):
            rule = rules[rule_idx]
            original_text = extract_line_context(text, end_idx)
            ot_stripped = original_text.strip()
            if ot_stripped in matched_lines or ot_stripped in seen:
                continue
            event_time = extract_nearest_timestamp(text, end_idx) or ''
            matched.append({
                'type': 'keyword_match',
                'keywords': [keyword],
                'advice': rule['advice'],
                'source': '手动规则' if rule.get('source') != 'pdf' else 'PDF知识库',
                'original_text': original_text,
                'event_time': event_time,
                'event_date': normalize_event_date(event_time)
            })
            matched_lines.add(ot_stripped)
            seen.add(ot_stripped)
    else:
        for rule in rules:
            keywords = rule.get('keywords', [])
            if isinstance(keywords, str):
                keywords = [kw.strip() for kw in keywords.split(',') if kw.strip()]
            for keyword in keywords:
                lower_keyword = keyword.lower()
                start_pos = 0
                while True:
                    idx = text_lower.find(lower_keyword, start_pos)
                    if idx == -1:
                        break
                    original_text = extract_line_context(text, idx)
                    if original_text.strip() in matched_lines:
                        start_pos = idx + 1
                        continue
                    event_time = extract_nearest_timestamp(text, idx) or ''
                    matched.append({
                        'type': 'keyword_match',
                        'keywords': [keyword],
                        'advice': rule['advice'],
                        'source': '手动规则' if rule.get('source') != 'pdf' else 'PDF知识库',
                        'original_text': original_text,
                        'event_time': event_time,
                        'event_date': normalize_event_date(event_time)
                    })
                    matched_lines.add(original_text.strip())
                    start_pos = idx + 1
    return matched
