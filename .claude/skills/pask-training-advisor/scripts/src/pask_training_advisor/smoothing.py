"""Moving median shared by the diagnosis features and the figures.

``diagnose`` and ``plots`` use this one implementation so that ``min_loss`` and spike counts
in ``diagnosis.json`` match the smoothed curves drawn in the figures.
"""

import numpy as np


def smooth(values: np.ndarray, window: int) -> np.ndarray:
    """Smooth a series with a centered moving median.

    The window shrinks to the number of points when the series is shorter, and is made odd so
    that it stays centered. Near the ends the median is taken over the points that exist.

    Args:
        values: Finite values in logging order.
        window: Requested window size in points. Must be at least 1.

    Returns:
        The smoothed values, the same length as ``values``.

    Raises:
        ValueError: If ``window`` is smaller than 1.
    """
    if window < 1:
        raise ValueError(f"window must be at least 1, got {window}")
    point_count = len(values)
    if point_count == 0:
        return np.asarray(values, dtype=float)
    effective_window = min(window, point_count)
    if effective_window % 2 == 0:
        effective_window -= 1
    half_window = effective_window // 2
    return np.array(
        [
            np.median(values[max(0, index - half_window) : index + half_window + 1])
            for index in range(point_count)
        ],
        dtype=float,
    )
