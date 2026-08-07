import requests
import logging

logger = logging.getLogger(__name__)

GO_PARSER_URL = "http://go-parser:8082"
TIMEOUT = 120

def analyze_zip(zip_path, series, model, analysis_type=''):
    try:
        resp = requests.post(
            f"{GO_PARSER_URL}/analyze",
            json={
                'zip_path': zip_path,
                'series': series,
                'model': model,
                'analysis_type': analysis_type,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Go Parser analyze failed: {e}")
        return None

def search_text(content, pattern, is_regex=False, case_sensitive=False, whole_word=False):
    try:
        resp = requests.post(
            f"{GO_PARSER_URL}/search",
            json={
                'content': content,
                'pattern': pattern,
                'is_regex': is_regex,
                'case_sensitive': case_sensitive,
                'whole_word': whole_word,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Go Parser search failed: {e}")
        return None
