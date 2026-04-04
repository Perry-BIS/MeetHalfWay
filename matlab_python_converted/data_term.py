import numpy as np


def data_term(rf1: float, rf2: float) -> float:
    """Equivalent of the MATLAB data_term function."""
    return float(np.abs(rf1 - rf2))
