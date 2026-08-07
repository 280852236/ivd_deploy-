import re
from typing import List

_DANGEROUS_CHARS_TABLE = str.maketrans('<>:"|?*', '_______')
_ERROR_KW = {'error', 'fault', '异常', '故障', '报警'}


def sanitize_filename(filename: str) -> str:
    filename = filename.replace('\\', '/').split('/')[-1]
    filename = filename.translate(_DANGEROUS_CHARS_TABLE)
    return filename if filename else 'unnamed'


def validate_input(text: str, max_length: int = 5000) -> bool:
    return bool(text) and len(text) <= max_length


def is_error_document(name: str, content: str) -> bool:
    if '样本' in content and ('空吸' in content or '余量探测' in content):
        return False
    if '试剂' in content and ('空吸' in content or '余量探测' in content):
        return False
    lowered = content.lower()
    return name.lower().endswith('.log') or any(kw in lowered for kw in _ERROR_KW)


def filter_relevant_analysis(analysis: List[dict]) -> List[dict]:
    return [
        item for item in analysis
        if item.get('type') in ['motor_status_match', 'keyword_match']
    ]
