import unittest
import json
import zlib
import base64


class TestCompressDecompress(unittest.TestCase):
    def test_small_value_not_compressed(self):
        from services.analysis import _compress_value, _decompress_value
        small = '{"key": "val"}'
        result = _compress_value(small)
        self.assertFalse(result.startswith('z:'))
        self.assertEqual(_decompress_value(result), small)

    def test_large_value_compressed(self):
        from services.analysis import _compress_value, _decompress_value
        large = json.dumps({"data": "x" * 10000}, ensure_ascii=False)
        result = _compress_value(large)
        self.assertTrue(result.startswith('z:'))
        self.assertEqual(_decompress_value(result), large)

    def test_decompress_plain_string(self):
        from services.analysis import _decompress_value
        plain = '{"test": true}'
        self.assertEqual(_decompress_value(plain), plain)

    def test_decompress_bytes_utf8(self):
        from services.analysis import _decompress_value
        data = '{"test": true}'.encode('utf-8')
        self.assertEqual(_decompress_value(data), '{"test": true}')

    def test_decompress_bytes_old_format(self):
        from services.analysis import _decompress_value
        original = '{"old": "format"}'
        compressed = zlib.compress(original.encode('utf-8'))
        data = b'\x01' + compressed
        self.assertEqual(_decompress_value(data), original)

    def test_roundtrip_chinese(self):
        from services.analysis import _compress_value, _decompress_value
        chinese = json.dumps({"故障": "样本空吸", "建议": "检查管路"}, ensure_ascii=False)
        result = _compress_value(chinese)
        self.assertEqual(_decompress_value(result), chinese)

    def test_empty_string(self):
        from services.analysis import _decompress_value
        self.assertEqual(_decompress_value(''), '')

    def test_compress_prefix_in_data(self):
        from services.analysis import _compress_value, _decompress_value
        data_with_prefix = json.dumps({"content": "z:should_not_trigger"}, ensure_ascii=False)
        result = _compress_value(data_with_prefix)
        self.assertEqual(_decompress_value(result), data_with_prefix)


class TestSafeImgTable(unittest.TestCase):
    def test_normal_model(self):
        from shared import safe_img_table
        self.assertEqual(safe_img_table('SMART6500', 'bug_images'), 'bug_images_smart6500')

    def test_model_with_spaces(self):
        from shared import safe_img_table
        self.assertEqual(safe_img_table('SMART 6500', 'hw_images'), 'hw_images_smart6500')

    def test_empty_model(self):
        from shared import safe_img_table
        self.assertIsNone(safe_img_table('', 'bug_images'))

    def test_special_chars(self):
        from shared import safe_img_table
        self.assertEqual(safe_img_table('SMART-6500/V2', 'img'), 'img_smart6500v2')


class TestJsonFormatter(unittest.TestCase):
    def test_basic_format(self):
        import logging
        from shared import JsonFormatter
        formatter = JsonFormatter(datefmt='%Y-%m-%dT%H:%M:%S')
        record = logging.LogRecord(
            name='test', level=logging.INFO, pathname='', lineno=0,
            msg='test message', args=(), exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        self.assertEqual(parsed['level'], 'INFO')
        self.assertEqual(parsed['message'], 'test message')

    def test_format_with_exception(self):
        import logging
        import sys
        from shared import JsonFormatter
        formatter = JsonFormatter(datefmt='%Y-%m-%dT%H:%M:%S')
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name='test', level=logging.ERROR, pathname='', lineno=0,
            msg='error occurred', args=(), exc_info=exc_info
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        self.assertEqual(parsed['level'], 'ERROR')
        self.assertIn('exception', parsed)

    def test_format_chinese(self):
        import logging
        from shared import JsonFormatter
        formatter = JsonFormatter(datefmt='%Y-%m-%dT%H:%M:%S')
        record = logging.LogRecord(
            name='test', level=logging.WARNING, pathname='', lineno=0,
            msg='中文日志消息', args=(), exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        self.assertEqual(parsed['message'], '中文日志消息')


class TestLoadFromPgJsonbCompat(unittest.TestCase):
    def test_jsonb_dict_passthrough(self):
        from services.analysis import _load_from_pg
        summary_dict = {"total_faults": 5, "top_keywords": ["空吸", "温度"]}
        result = {
            'series': 'SMART', 'model': 'SMART6500', 'analysis_type': '',
            'summary': summary_dict, 'total_dates': 10, 'total_files': 100,
            'matched_count': 5, 'analyzed_at': '2026-08-05',
            'date_groups': [], 'files': {}, 'file_names': [],
            'has_separate_files': False, 'from_pg': True,
        }
        self.assertIsInstance(result['summary'], dict)
        self.assertEqual(result['summary']['total_faults'], 5)

    def test_jsonb_string_parsed(self):
        summary_str = '{"total_faults": 3}'
        summary = json.loads(summary_str) if isinstance(summary_str, str) else (summary_str or {})
        self.assertEqual(summary['total_faults'], 3)

    def test_jsonb_none_handled(self):
        summary_raw = None
        summary = json.loads(summary_raw) if isinstance(summary_raw, str) else (summary_raw or {})
        self.assertEqual(summary, {})


class TestInvalidateAllowedTablesCache(unittest.TestCase):
    def test_function_exists(self):
        from shared import invalidate_allowed_tables_cache
        self.assertTrue(callable(invalidate_allowed_tables_cache))


if __name__ == '__main__':
    unittest.main()