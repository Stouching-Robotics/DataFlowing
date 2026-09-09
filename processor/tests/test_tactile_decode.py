import unittest

import numpy as np

from app.routes.video import _decode_tactile_matrix


class TactileDecodeTests(unittest.TestCase):
    def test_interleaved_row_diff_is_decoded_before_hwc_reshape(self):
        matrix = np.zeros((250, 250, 3), dtype=np.int16)
        matrix[12, 34] = [7, -2, 19]
        matrix[12, 35] = [8, -1, 23]
        matrix[200, 249] = [-11, 3, 31]

        rows = matrix.reshape(250, 750)
        diff = np.empty_like(rows)
        diff[:, 0] = rows[:, 0]
        diff[:, 1:] = rows[:, 1:] - rows[:, :-1]

        decoded = _decode_tactile_matrix(diff.reshape(-1).tolist())

        self.assertEqual(decoded.shape, (3, 250, 250))
        np.testing.assert_array_equal(decoded.transpose(1, 2, 0), matrix)


if __name__ == "__main__":
    unittest.main()
