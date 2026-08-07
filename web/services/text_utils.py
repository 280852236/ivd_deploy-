import re


_CONTROL_CHAR_TABLE = str.maketrans('', '', ''.join(chr(i) for i in range(32) if chr(i) not in '\n\r\t'))
_HTML_ESCAPE_TABLE = str.maketrans({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
})


def strip_control_chars(text: str) -> str:
    return text.translate(_CONTROL_CHAR_TABLE)


_TIMESTAMP_PATTERNS = [
    re.compile(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}'),
    re.compile(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}'),
    re.compile(r'\d{1,2}:\d{2}:\d{2}'),
    re.compile(r'\d{1,2}:\d{2}')
]


def escape_html(text: str) -> str:
    if not text:
        return ''
    return text.translate(_HTML_ESCAPE_TABLE)


_HIGHLIGHT_SPAN_OPEN = '<span style="background:#fef08a;border-radius:2px;padding:0 2px;">'
_HIGHLIGHT_SPAN_CLOSE = '</span>'

from functools import lru_cache

@lru_cache(maxsize=256)
def _build_highlight_pattern(keywords):
    flat = []
    for kw in keywords:
        if isinstance(kw, (list, tuple)):
            for k in kw:
                if isinstance(k, str) and k:
                    flat.append(k)
        elif isinstance(kw, str) and kw:
            flat.append(kw)
    key = tuple(sorted(set(flat)))
    if not key:
        return re.compile(r'(?!)')
    parts = [f'({re.escape(escape_html(kw))})' for kw in key]
    combined = '|'.join(parts)
    return re.compile(combined, re.IGNORECASE)


def highlight_line_text(escaped_line: str, keywords):
    if not keywords:
        return escaped_line
    kw_tuple = tuple(keywords) if not isinstance(keywords, tuple) else keywords
    pattern = _build_highlight_pattern(kw_tuple)
    return pattern.sub(
        lambda m: f'{_HIGHLIGHT_SPAN_OPEN}{m.group(0)}{_HIGHLIGHT_SPAN_CLOSE}',
        escaped_line
    )


def extract_line_context(text: str, index: int) -> str:
    start = text.rfind('\n', 0, index) + 1
    end = text.find('\n', index)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def extract_nearest_timestamp(text: str, index: int) -> str:
    window = 250
    start = max(0, index - window)
    end = min(len(text), index + window)
    context = text[start:end]
    for pattern in _TIMESTAMP_PATTERNS:
        last_match = None
        for m in pattern.finditer(context):
            last_match = m
        if last_match:
            return last_match.group(0)
    return ''


def normalize_event_date(event_time: str) -> str:
    if not event_time:
        return '未识别日期'
    date_only = event_time.split(' ')[0]
    if '-' in date_only or '/' in date_only:
        return date_only.replace('/', '-').strip()
    return date_only


CONDITION_HIGHLIGHT_MAP = {
    '执行过重测': ['执行过重测：True', '执行过重测: True'],
    '余量不足': ['余量不足：True', '余量不足: True'],
    '电路异常': ['电路异常：True', '电路异常: True'],
    '脱离液面失败': ['脱离液面失败：True', '脱离液面失败: True'],
    '空吸': ['空吸：True', '空吸: True'],
    '重测3次失败': ['重测3次失败：True', '重测3次失败: True'],
    '液位探测无效': ['液位探测有效：False', '液位探测有效: False'],
}


def check_aspiration_anomaly(line: str) -> dict:
    conditions = []
    matched = False
    file_type = 'none'
    if '样本' in line and ('空吸' in line or '取样本' in line or '取稀释样本' in line):
        file_type = 'sample'
    elif '试剂' in line and ('空吸' in line or '试剂针' in line):
        file_type = 'reagent'
    if file_type == 'none':
        return {'matched': False, 'conditions': [], 'type': 'none'}
    if '执行过重测：True' in line or '执行过重测: True' in line:
        conditions.append('执行过重测'); matched = True
    if '余量不足：True' in line or '余量不足: True' in line:
        conditions.append('余量不足'); matched = True
    if '电路异常：True' in line or '电路异常: True' in line:
        conditions.append('电路异常'); matched = True
    if '脱离液面失败：True' in line or '脱离液面失败: True' in line:
        conditions.append('脱离液面失败'); matched = True
    if '空吸：True' in line or '空吸: True' in line:
        conditions.append('空吸'); matched = True
    if '重测3次失败：True' in line or '重测3次失败: True' in line:
        conditions.append('重测3次失败'); matched = True
    if '液位探测有效：False' in line or '液位探测有效: False' in line:
        conditions.append('液位探测无效'); matched = True
    return {'matched': matched, 'conditions': conditions, 'type': file_type}
