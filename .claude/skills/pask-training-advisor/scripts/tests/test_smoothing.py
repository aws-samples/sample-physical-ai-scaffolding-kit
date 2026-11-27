"""Tests of the moving median shared by the diagnosis and the figures."""

import numpy as np
import pytest

from pask_training_advisor.smoothing import smooth


def test_smooth_is_a_centered_moving_median() -> None:
    values = np.array([1.0, 100.0, 1.0, 1.0, 1.0])

    smoothed = smooth(values, 3)

    np.testing.assert_allclose(smoothed, [50.5, 1.0, 1.0, 1.0, 1.0])


def test_smooth_shrinks_window_for_short_series() -> None:
    values = np.array([3.0, 1.0, 2.0, 5.0])

    smoothed = smooth(values, 11)

    np.testing.assert_allclose(smoothed, [2.0, 2.0, 2.0, 3.5])


def test_smooth_keeps_empty_series_empty() -> None:
    assert smooth(np.array([], dtype=float), 11).shape == (0,)


def test_smooth_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError, match="window"):
        smooth(np.array([1.0]), 0)
