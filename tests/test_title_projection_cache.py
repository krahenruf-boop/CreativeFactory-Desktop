"""Polling a real-size history must reuse checked layouts without weakening checks."""
import unittest
from unittest.mock import patch
import autocut.context
from autocut.production_preflight import _title


class TitleProjectionCacheTests(unittest.TestCase):
    def tearDown(self):
        _title.cache_clear()

    def test_repeated_history_larger_than_512_does_not_recheck_every_title(self):
        _title.cache_clear()
        fit = dict(lines=['A visible', 'product benefit'], reference_font_size=48)
        with patch('app.s3.visual_preset.build_visual_plan', return_value={}), patch('autocut.title_layout.apply', return_value=fit) as check:
            first = [_title(f'Fixture title {n}', '{}') for n in range(530)]
            second = [_title(f'Fixture title {n}', '{}') for n in range(530)]
        self.assertEqual(first, second)
        self.assertEqual(check.call_count, 530)
        self.assertEqual(_title.cache_info().hits, 530)
        self.assertLessEqual(_title.cache_info().currsize, 4096)

    def test_changed_text_and_job_options_still_require_fresh_checks(self):
        _title.cache_clear()
        with patch('app.s3.visual_preset.build_visual_plan', return_value={}), patch('autocut.title_layout.apply', side_effect=ValueError('TITLE_SHORTEN_REQUIRED')) as check:
            one = _title('Oversized fixture', '{}')
            _title('Oversized fixture', '{}')
            two = _title('Different fixture', '{}')
            _title('Different fixture', '{"title_position_adjustment":"up"}')
        self.assertEqual(check.call_count, 3)
        self.assertEqual(one['status'], 'BLOCKED')
        self.assertEqual(two['error'], 'TITLE_SHORTEN_REQUIRED')


if __name__ == '__main__':
    unittest.main()
