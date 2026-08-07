import os
import re
import json
import zipfile
import rarfile
import logging
import tempfile
from typing import Dict, List
from services.analyzer import analyze_text
from services.file_utils import is_error_document, filter_relevant_analysis
from services.text_utils import (
    escape_html,
    highlight_line_text,
    extract_line_context,
    extract_nearest_timestamp,
    normalize_event_date,
    strip_control_chars,
    CONDITION_HIGHLIGHT_MAP
)

logger = logging.getLogger(__name__)

_ENCODINGS = ('utf-8', 'gbk', 'gb2312', 'latin-1')


def _decode_bytes(raw: bytes, filename: str) -> str:
    for encoding in _ENCODINGS:
        try:
            content = raw.decode(encoding)
            logger.debug(f"文件 {filename} 使用 {encoding} 编码解码成功")
            return content
        except UnicodeDecodeError:
            continue
    content = raw.decode('utf-8', errors='replace')
    logger.warning(f"文件 {filename} 编码解码失败，使用UTF-8替换模式")
    return content


_RE_DATE_FN = re.compile(r'(\d{4}-\d{1,2}-\d{1,2})')
_RE_DATE_CONTENT = re.compile(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})')


def _extract_file_date(fname: str, fdata: dict) -> str:
    date_match = _RE_DATE_FN.search(fname)
    if date_match:
        return date_match.group(1)
    analysis_list = fdata.get('analysis', [])
    for item in analysis_list:
        ed = item.get('event_date')
        if ed and ed != '未识别日期':
            return ed
    content = fdata.get('content', '')
    if content:
        date_match = _RE_DATE_CONTENT.search(content)
        if date_match:
            return date_match.group(1)
    return '未识别日期'


_SAMPLE_KW = frozenset({'样本空吸', '样本不足'})
_REAGENT_KW = frozenset({'试剂空吸', '试剂不足'})
_FAULT_KW = frozenset({'error', 'fault', '异常', '故障', '报警', '失败'})
_RECEIVE_KW = frozenset({'接收数据记录', '接收数据'})
_SAMPLE_KW_LOWER = frozenset(kw.lower() for kw in _SAMPLE_KW)
_REAGENT_KW_LOWER = frozenset(kw.lower() for kw in _REAGENT_KW)
_FAULT_KW_LOWER = frozenset(kw.lower() for kw in _FAULT_KW)


def _classify_file(fname, fdata):
    types = set()
    ft = fdata.get('file_type', 'unknown')
    if ft != 'unknown':
        types.add(ft)
        return types
    for item in fdata.get('analysis', []):
        if item['type'] == 'motor_status_match':
            types.add('fault')
        elif item['type'] == 'keyword_match':
            for kw in item.get('keywords', []):
                if isinstance(kw, list):
                    for k in kw:
                        if k in _SAMPLE_KW:
                            types.add('sample')
                        elif k in _REAGENT_KW:
                            types.add('reagent')
                        elif k in _FAULT_KW:
                            types.add('fault')
                else:
                    if kw in _SAMPLE_KW:
                        types.add('sample')
                    elif kw in _REAGENT_KW:
                        types.add('reagent')
                    elif kw in _FAULT_KW:
                        types.add('fault')
    if types:
        return types
    if any(kw in fname for kw in _RECEIVE_KW):
        types.add('receive')
    if any(kw in fname for kw in _SAMPLE_KW):
        types.add('sample')
    if any(kw in fname for kw in _REAGENT_KW):
        types.add('reagent')
    if any(kw in fname for kw in _FAULT_KW):
        types.add('fault')
    if types:
        return types
    content = fdata.get('content', '')
    # 短路：仅扫描前200行，分类关键词通常在文件头部
    head_lines = content.splitlines()[:200]
    head_content = '\n'.join(head_lines).lower()
    for line in head_lines:
        if '空吸' in line:
            if '样本' in line:
                types.add('sample')
            if '试剂' in line:
                types.add('reagent')
    if any(kw in head_content for kw in _SAMPLE_KW_LOWER):
        types.add('sample')
    if any(kw in head_content for kw in _REAGENT_KW_LOWER):
        types.add('reagent')
    if any(kw in head_content for kw in _FAULT_KW_LOWER):
        types.add('fault')
    return types


def build_date_groups_and_summary(files: Dict):
    date_map = {}
    fault_count = sample_count = reagent_count = receive_count = 0
    for fname, fdata in files.items():
        file_date = _extract_file_date(fname, fdata)
        if file_date not in date_map:
            date_map[file_date] = {}
        types = _classify_file(fname, fdata)
        date_map[file_date][fname] = types
        if 'fault' in types and 'sample' not in types and 'reagent' not in types:
            fault_count += 1
        if 'sample' in types:
            sample_count += 1
        if 'reagent' in types:
            reagent_count += 1
        if 'receive' in types:
            receive_count += 1
    date_groups = []
    for date in sorted(date_map.keys(), reverse=True):
        file_list = []
        for fname in sorted(date_map[date].keys()):
            fdata = files.get(fname, {})
            file_list.append({
                'name': fname,
                'size': fdata.get('size', 0),
                'is_critical': fdata.get('is_critical', False),
                'types': sorted(list(date_map[date][fname])),
                'has_fault': fdata.get('has_fault', False),
                'is_aspiration_file': fdata.get('is_aspiration_file', False),
                'has_aspiration_match': fdata.get('has_aspiration_match', False)
            })
        date_groups.append({'date': date, 'files': file_list})
    summary = {'fault': fault_count, 'sample': sample_count, 'reagent': reagent_count, 'receive': receive_count}
    return date_groups, summary


def build_date_groups(files: Dict) -> List[Dict]:
    date_groups, _ = build_date_groups_and_summary(files)
    return date_groups


def compute_summary(files: Dict) -> Dict:
    _, summary = build_date_groups_and_summary(files)
    return summary


def is_relevant_filename(filename: str) -> bool:
    keywords = ['样本空吸', '试剂空吸', '接收数据记录', '故障代码']
    return any(kw in filename for kw in keywords)


def process_text_file_from_bytes(file_bytes: bytes, filename: str, rules: List[Dict], series: str, model: str) -> Dict:
    MAX_FILE_CONTENT = 20 * 1024 * 1024
    MAX_LINES = 500000
    MAX_HTML_LINES = 5000
    content = _decode_bytes(file_bytes, filename)
    return _process_text_content(content, filename, rules, series, model, MAX_FILE_CONTENT, MAX_LINES, MAX_HTML_LINES)


def _detect_file_type(filename: str) -> str:
    if '样本空吸' in filename:
        return 'sample'
    if '试剂空吸' in filename:
        return 'reagent'
    if '接收数据' in filename:
        return 'receive'
    if '故障代码' in filename:
        return 'fault'
    if any(kw in filename for kw in ('事件通知', '运行', '启动', '暂停', '温度', '振摇', '余量', '旗标', '稀释', '取样', '进样', '命令', '模块', '信息')):
        return 'general'
    return 'unknown'


def _build_advice_maps(filtered_analysis, has_aspiration):
    advice_map = {}
    unmatched_map = {}
    if not has_aspiration:
        for item in filtered_analysis:
            if item['type'] == 'motor_status_match':
                orig = item.get('original_text', '').strip()
                if orig and item.get('advice'):
                    if item.get('unmatched'):
                        unmatched_map[orig] = item['advice']
                    else:
                        advice_map[orig] = item['advice']
    for item in filtered_analysis:
        if item['type'] == 'keyword_match':
            orig = item.get('original_text', '').strip()
            if orig and item.get('advice'):
                advice_map[orig] = item['advice']
    return advice_map, unmatched_map


def _collect_highlight_keywords(filtered_analysis):
    seen = set()
    highlight_keywords = []
    for item in filtered_analysis:
        if item['type'] == 'keyword_match':
            for kw in item.get('keywords', []):
                if isinstance(kw, list):
                    for k in kw:
                        if k and not isinstance(k, list) and k not in seen:
                            seen.add(k)
                            highlight_keywords.append(k)
                elif kw and kw not in seen:
                    seen.add(kw)
                    highlight_keywords.append(kw)
            orig = item.get('original_text', '').strip()
            if orig and len(orig) <= 500 and orig not in seen:
                seen.add(orig)
                highlight_keywords.append(orig)
            for cond in item.get('matched_conditions', []):
                for cond_text in CONDITION_HIGHLIGHT_MAP.get(cond, []):
                    if cond_text not in seen:
                        seen.add(cond_text)
                        highlight_keywords.append(cond_text)
        elif item['type'] == 'motor_status_match':
            orig = item.get('original_text', '').strip()
            if orig and len(orig) <= 500 and orig not in seen:
                seen.add(orig)
                highlight_keywords.append(orig)
    return highlight_keywords


_CSS_MATCH_DIV = 'style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;"'
_CSS_MATCH_SPAN = 'style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;"'
_CSS_UNMATCH_DIV = 'style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;"'
_CSS_UNMATCH_SPAN = 'style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;"'
_CSS_HL_LINE = 'style="line-height:1.6; padding:4px 8px; background:linear-gradient(135deg,#fef9c3 0%,#fef08a 100%); border-left:3px solid #eab308; border-radius:4px; margin:2px 0;"'
_CSS_NORMAL_LINE = 'style="line-height:1.6; padding:1px 0;"'


def _build_key_automaton(key_map):
    try:
        import ahocorasick
    except ImportError:
        return None
    if not key_map:
        return None
    A = ahocorasick.Automaton()
    for orig_key, orig_advice in key_map.items():
        if orig_key and len(orig_key) <= 500:
            A.add_word(orig_key, (orig_key, orig_advice))
    A.make_automaton()
    return A


def _generate_html_content(lines, advice_map, unmatched_map, highlight_keywords, is_aspiration_file):
    html_lines = []
    advice_auto = _build_key_automaton(advice_map)
    unmatched_auto = _build_key_automaton(unmatched_map)
    for line in lines:
        trimmed = line.strip()
        advice_html = ''
        line_has_match = False
        if trimmed in advice_map:
            advice = escape_html(advice_map[trimmed])
            if is_aspiration_file:
                advice_html = f'<div {_CSS_MATCH_DIV}>💡 故障对比诊断：{advice}</div>'
            else:
                advice_html = f'<span {_CSS_MATCH_SPAN}>💡 {advice}</span>'
            line_has_match = True
        elif trimmed in unmatched_map:
            advice = escape_html(unmatched_map[trimmed])
            if is_aspiration_file:
                advice_html = f'<div {_CSS_UNMATCH_DIV}>⚠️ {advice}</div>'
            else:
                advice_html = f'<span {_CSS_UNMATCH_SPAN}>⚠️ {advice}</span>'
            line_has_match = True
        elif advice_auto and trimmed:
            found = False
            for end_idx, (orig_key, orig_advice) in advice_auto.iter(trimmed):
                advice = escape_html(orig_advice)
                if is_aspiration_file:
                    advice_html = f'<div {_CSS_MATCH_DIV}>💡 故障对比诊断：{advice}</div>'
                else:
                    advice_html = f'<span {_CSS_MATCH_SPAN}>💡 {advice}</span>'
                line_has_match = True
                found = True
                break
            if not found and unmatched_auto:
                for end_idx, (orig_key, orig_advice) in unmatched_auto.iter(trimmed):
                    advice = escape_html(orig_advice)
                    if is_aspiration_file:
                        advice_html = f'<div {_CSS_UNMATCH_DIV}>⚠️ {advice}</div>'
                    else:
                        advice_html = f'<span {_CSS_UNMATCH_SPAN}>⚠️ {advice}</span>'
                    line_has_match = True
                    break
        if line_has_match and is_aspiration_file:
            html_lines.append(f'<div {_CSS_HL_LINE}>{escape_html(line)}</div>')
        else:
            if is_aspiration_file:
                highlighted = highlight_line_text(escape_html(line), highlight_keywords)
                html_lines.append(f'<div {_CSS_NORMAL_LINE}>{highlighted}</div>')
            else:
                html_lines.append(f'<div {_CSS_NORMAL_LINE}>{escape_html(line)}{advice_html}</div>')
        if is_aspiration_file and advice_html:
            html_lines.append(advice_html)
    return '\n'.join(html_lines)


def _run_analysis(content, rules, series, model, file_type):
    is_aspiration_file = file_type in ('sample', 'reagent')
    is_receive_file = file_type == 'receive'
    is_fault_file = file_type == 'fault'
    if is_receive_file or file_type == 'unknown':
        return []
    elif is_fault_file:
        return analyze_text(content, rules, series, model, skip_motor_status=False)
    elif is_aspiration_file:
        return analyze_text(content, rules, series, model, skip_motor_status=True)
    else:
        return analyze_text(content, rules, series, model, skip_motor_status=False)


def _process_text_content(content: str, filename: str, rules: List[Dict], series: str, model: str, MAX_FILE_CONTENT: int, MAX_LINES: int, MAX_HTML_LINES: int = 5000) -> Dict:
    content = strip_control_chars(content)
    original_content = content
    lines = original_content.splitlines()
    if len(lines) > MAX_LINES:
        display_lines = lines[:MAX_LINES]
        content = '\n'.join(display_lines) + f'\n... (内容已截断，仅显示前{MAX_LINES}行)'
    else:
        display_lines = lines
        content = original_content
    file_type = _detect_file_type(filename)
    is_aspiration_file = file_type in ('sample', 'reagent')
    is_receive_file = file_type == 'receive'
    is_fault_file = file_type == 'fault'
    analysis = _run_analysis(original_content, rules, series, model, file_type)
    for item in analysis:
        item['source_file'] = filename
        if file_type != 'unknown':
            item['file_type'] = file_type
    filtered_analysis = [item for item in analysis if item['type'] in ['motor_status_match', 'keyword_match']]
    has_aspiration = any(item['type'] == 'keyword_match' and '空吸' in str(item.get('keywords', [])) for item in filtered_analysis)
    advice_map, unmatched_map = _build_advice_maps(filtered_analysis, has_aspiration)
    highlight_keywords = _collect_highlight_keywords(filtered_analysis)
    # 按需生成：首次分析不生成html_content，API请求时按需生成
    _html_meta = {
        'advice_map': dict(advice_map),
        'unmatched_map': dict(unmatched_map),
        'highlight_keywords': list(highlight_keywords),
        'is_aspiration_file': is_aspiration_file,
        'total_lines': len(display_lines),
        'max_html_lines': MAX_HTML_LINES,
    }
    has_fault = False
    if is_fault_file:
        has_fault = True
    elif not is_aspiration_file and not is_receive_file:
        has_fault = any(item['type'] == 'motor_status_match' for item in filtered_analysis)
    is_critical = is_error_document(filename, content)
    file_metadata = [{'name': filename, 'size': len(content), 'is_critical': is_critical, 'preview': content[:200]}]

    files = {filename: {
        'content': content[:MAX_FILE_CONTENT],
        'html_content': '',
        '_html_meta': _html_meta,
        'has_fault': has_fault,
        'size': len(content),
        'is_critical': is_critical,
        'analysis': filtered_analysis,
        'file_type': file_type,
        'is_aspiration_file': is_aspiration_file,
        'has_aspiration_match': has_aspiration
    }}
    return {
        'analysis': filtered_analysis,
        'file_size': len(content),
        'matched_count': len(filtered_analysis),
        'preview': content[:1000] + ('...' if len(content) > 1000 else ''),
        'file_metadata': file_metadata,

        'files': files
    }


_MAX_ARCHIVE_FILES = 2000
_MAX_ARCHIVE_FILE_SIZE = 5 * 1024 * 1024
_MAX_ARCHIVE_CONTENTS_MAP = 2000
_MAX_ARCHIVE_FILE_CONTENT = 20 * 1024 * 1024
_MAX_ARCHIVE_LINES = 500000
_ASPIRATION_KW = {'样本空吸', '样本不足', '试剂空吸', '试剂不足'}


def _process_archive_entries(name_map, read_fn, info_fn, rules, series, model,
                              batch_size=0, start_index=0, archive_type='ZIP'):
    file_metadata = []

    files = {}
    combined_analysis = []
    preview_text = ''
    candidate_raw = [rn for rn, fn in name_map.items() if fn.lower().endswith(('.txt', '.log', '.md', '.csv'))]
    relevant_raw = candidate_raw
    total_candidates = len(relevant_raw)
    logger.info(f"{archive_type} 共 {len(name_map)} 条目, 筛选出 {len(candidate_raw)} 个文本文件")
    start = max(0, start_index)
    if batch_size and batch_size > 0:
        relevant_raw = relevant_raw[start:start + batch_size]
    else:
        relevant_raw = relevant_raw[start:]
    for raw_name in relevant_raw:
        name = name_map[raw_name]
        if len(file_metadata) >= _MAX_ARCHIVE_FILES:
            logger.warning(f"已达累计处理上限 {_MAX_ARCHIVE_FILES} 个文件，跳过剩余")
            break
        try:
            info = info_fn(raw_name)
            if hasattr(info, 'file_size') and info.file_size > _MAX_ARCHIVE_FILE_SIZE:
                logger.info(f"跳过过大文件 ({info.file_size} 字节): {name}")
                continue
        except Exception:
            pass
        try:
            raw = read_fn(raw_name)
            if len(raw) > _MAX_ARCHIVE_FILE_SIZE:
                logger.info(f"跳过过大文件 ({len(raw)} 字节): {name}")
                continue
            content = _decode_bytes(raw, name)
            content = strip_control_chars(content)
            file_type = _detect_file_type(name)
            is_aspiration_file = file_type in ('sample', 'reagent')
            is_receive_file = file_type == 'receive'
            is_fault_file = file_type == 'fault'
            lines = content.splitlines()
            if len(lines) > 500000:
                content_for_analysis = '\n'.join(lines[:500000])
            else:
                content_for_analysis = content
            file_analysis = []
            try:
                if is_receive_file or file_type == 'unknown':
                    file_analysis = []
                else:
                    file_analysis = _run_analysis(content_for_analysis, rules, series, model, file_type)
            except Exception as ae:
                logger.warning(f"分析文件失败(仍保留文件): {name} - {ae}")
            for item in file_analysis:
                item['source_file'] = name
                if file_type != 'unknown':
                    item['file_type'] = file_type
            filtered_file_analysis = [item for item in file_analysis if item['type'] in ['motor_status_match', 'keyword_match']]
            is_relevant = file_type != 'unknown' or is_relevant_filename(name)
            has_relevant_content = len(filtered_file_analysis) > 0
            if is_relevant or has_relevant_content or True:
                is_critical = is_error_document(name, content)
                has_fault = False
                if is_fault_file:
                    has_fault = True
                elif not is_aspiration_file and not is_receive_file:
                    has_fault = any(item['type'] == 'motor_status_match' for item in filtered_file_analysis)
                has_aspiration = any(item['type'] == 'keyword_match' and any((kw in _ASPIRATION_KW if not isinstance(kw, list) else any(k in _ASPIRATION_KW for k in kw)) for kw in item.get('keywords', [])) for item in filtered_file_analysis)
                advice_map, unmatched_map = _build_advice_maps(filtered_file_analysis, has_aspiration)
                hl_kw = _collect_highlight_keywords(filtered_file_analysis)
                # 按需生成：不生成html_content，存储元数据
                _html_meta = {
                    'advice_map': dict(advice_map),
                    'unmatched_map': dict(unmatched_map),
                    'highlight_keywords': list(hl_kw),
                    'is_aspiration_file': is_aspiration_file,
                    'total_lines': len(lines),
                    'max_html_lines': 5000,
                }
                file_metadata.append({'name': name, 'size': len(content), 'is_critical': is_critical, 'preview': content[:200]})

                if len(files) < _MAX_ARCHIVE_CONTENTS_MAP:
                    files[name] = {'content': content[:_MAX_ARCHIVE_FILE_CONTENT], 'html_content': '', '_html_meta': _html_meta, 'has_fault': has_fault, 'size': len(content), 'is_critical': is_critical, 'analysis': filtered_file_analysis, 'file_type': file_type, 'is_aspiration_file': is_aspiration_file, 'has_aspiration_match': has_aspiration}
            combined_analysis.extend(filtered_file_analysis)
            if len(preview_text) < 1000:
                preview_text += content[:1000 - len(preview_text)]
        except Exception as e:
            logger.warning(f"读取文件失败: {name} - {e}", exc_info=True)
            continue
    next_index = start + len(relevant_raw)
    return {
        'analysis': combined_analysis,
        'file_metadata': file_metadata[:200],

        'files': files,
        'total_files': len(file_metadata),
        'matched_count': len(combined_analysis),
        'preview': preview_text + ('...' if len(preview_text) >= 1000 else ''),
        'has_more_files': next_index < total_candidates,
        'next_index': next_index,
        'total_candidates': total_candidates,
    }


def process_zip_file(file, rules: List[Dict], series: str, model: str, batch_size: int = 0, start_index: int = 0) -> Dict:
    with zipfile.ZipFile(file) as zf:
        all_names_raw = zf.namelist()
        name_map = {}
        for raw_name in all_names_raw:
            fixed = raw_name
            try:
                fixed = raw_name.encode('cp437').decode('gbk')
            except (UnicodeDecodeError, UnicodeEncodeError):
                try:
                    fixed = raw_name.encode('cp437').decode('utf-8')
                except (UnicodeDecodeError, UnicodeEncodeError):
                    fixed = raw_name
            name_map[raw_name] = fixed
            if ".." in fixed or fixed.startswith("/") or os.path.isabs(fixed):
                continue
        return _process_archive_entries(
            name_map, zf.read, zf.getinfo, rules, series, model,
            batch_size=batch_size, start_index=start_index, archive_type='ZIP'
        )


def process_rar_file(file_path, rules: List[Dict], series: str, model: str) -> Dict:
    with rarfile.RarFile(file_path) as rf:
        all_names_raw = rf.namelist()
        name_map = {}
        for raw_name in all_names_raw:
            try:
                raw_name.encode('cp437').decode('gbk')
                name_map[raw_name] = raw_name
            except (UnicodeDecodeError, UnicodeEncodeError):
                name_map[raw_name] = raw_name
            if ".." in raw_name or raw_name.startswith("/") or os.path.isabs(raw_name):
                continue
        result = _process_archive_entries(
            name_map, rf.read, rf.getinfo, rules, series, model,
            archive_type='RAR'
        )
        result['has_more_files'] = False
        result['next_index'] = 0
        return result


def extract_archive_metadata(archive_path, archive_type='ZIP'):
    name_map = {}
    if archive_type == 'ZIP':
        with zipfile.ZipFile(archive_path) as zf:
            all_names_raw = zf.namelist()
            for raw_name in all_names_raw:
                fixed = raw_name
                try:
                    fixed = raw_name.encode('cp437').decode('gbk')
                except (UnicodeDecodeError, UnicodeEncodeError):
                    try:
                        fixed = raw_name.encode('cp437').decode('utf-8')
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        fixed = raw_name
                name_map[raw_name] = fixed
                if ".." in fixed or fixed.startswith("/") or os.path.isabs(fixed):
                    continue
            return _extract_metadata_from_entries(name_map, zf.getinfo)
    else:
        with rarfile.RarFile(archive_path) as rf:
            all_names_raw = rf.namelist()
            for raw_name in all_names_raw:
                name_map[raw_name] = raw_name
                if ".." in raw_name or raw_name.startswith("/") or os.path.isabs(raw_name):
                    continue
            return _extract_metadata_from_entries(name_map, rf.getinfo)


def _extract_metadata_from_entries(name_map, info_fn):
    file_metadata = []
    for raw_name, name in name_map.items():
        if name.endswith('/'):
            continue
        if not name.lower().endswith(('.txt', '.log', '.md', '.csv')):
            continue
        file_size = 0
        try:
            info = info_fn(raw_name)
            if hasattr(info, 'file_size'):
                file_size = info.file_size
        except Exception:
            pass
        if file_size > _MAX_ARCHIVE_FILE_SIZE:
            continue
        file_type = _detect_file_type(name)
        is_critical = any(kw in name for kw in ('样本空吸', '试剂空吸', '故障代码', 'error', 'fault'))
        file_metadata.append({
            'name': name,
            'size': file_size,
            'is_critical': is_critical,
            'file_type': file_type,
            'raw_name': raw_name,
        })
    return file_metadata


def analyze_single_file_from_archive(archive_path, raw_name, filename, rules, series, model, archive_type='ZIP'):
    try:
        if archive_type == 'ZIP':
            with zipfile.ZipFile(archive_path) as zf:
                raw = zf.read(raw_name)
        else:
            with rarfile.RarFile(archive_path) as rf:
                raw = rf.read(raw_name)
    except Exception as e:
        logger.error(f"从压缩包读取文件失败: {filename} - {e}")
        return None
    if len(raw) > _MAX_ARCHIVE_FILE_SIZE:
        return {'error': '文件过大', 'name': filename}
    content = _decode_bytes(raw, filename)
    content = strip_control_chars(content)
    return _process_text_content(content, filename, rules, series, model, _MAX_ARCHIVE_FILE_CONTENT, _MAX_ARCHIVE_LINES)
