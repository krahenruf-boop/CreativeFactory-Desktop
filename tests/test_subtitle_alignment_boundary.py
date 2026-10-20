import copy
import unittest

from autocut import context
from autocut.subtitle_alignment import scaled_alignment
from app.s3.subtitles import scaled_alignment as engine_scaled_alignment


class AlignmentBoundaryTests(unittest.TestCase):
    def alignment(self, end):
        return dict(characters=['a', '.'],
                    character_start_times_seconds=[0, 18.16],
                    character_end_times_seconds=[18.16, end])

    def test_provider_80ms_boundary_keeps_text_and_original_receipt(self):
        alignment = self.alignment(18.48)
        original = copy.deepcopy(alignment)
        with self.assertRaises(ValueError):
            engine_scaled_alignment(alignment, 1, 18.4)
        result = scaled_alignment(alignment, 1, 18.4)
        self.assertEqual(result['characters'], original['characters'])
        self.assertEqual(result['character_end_times_seconds'], [18.16, 18.4])
        self.assertEqual(alignment, original)

    def test_ordinary_valid_alignment_is_identical_to_renderer(self):
        for end in (18.2, 18.4, 18.47):
            alignment = self.alignment(end)
            self.assertEqual(scaled_alignment(alignment, 1, 18.4),
                             engine_scaled_alignment(alignment, 1, 18.4))

    def test_real_overshoot_stays_rejected(self):
        for end in (18.480001, 18.5, 19):
            with self.assertRaises(ValueError):
                scaled_alignment(self.alignment(end), 1, 18.4)

    def test_nonmonotonic_or_negative_times_stay_rejected_at_boundary(self):
        for start in (-1, 18.49):
            alignment = self.alignment(18.48)
            alignment['character_start_times_seconds'][1] = start
            with self.assertRaises(ValueError):
                scaled_alignment(alignment, 1, 18.4)
        alignment = self.alignment(18.48)
        alignment['character_end_times_seconds'][0] = 18.49
        with self.assertRaises(ValueError):
            scaled_alignment(alignment, 1, 18.4)

    def test_missing_arrays_stay_rejected_at_boundary(self):
        alignment = self.alignment(18.48)
        alignment['characters'] = []
        with self.assertRaises(ValueError):
            scaled_alignment(alignment, 1, 18.4)

    def test_existing_speed_scaling_and_clamp_are_preserved(self):
        alignment = dict(characters=['a', '.'],
                         character_start_times_seconds=[0, 19.976],
                         character_end_times_seconds=[19.976, 20.328])
        original = copy.deepcopy(alignment)
        result = scaled_alignment(alignment, 1.1, 18.4)
        self.assertEqual(result['characters'], original['characters'])
        self.assertLessEqual(max(result['character_end_times_seconds']), 18.4)
        self.assertEqual(alignment, original)


if __name__ == '__main__':
    unittest.main()
