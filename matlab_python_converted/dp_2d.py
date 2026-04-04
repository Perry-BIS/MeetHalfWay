from __future__ import annotations

import numpy as np

from data_term import data_term
from reg_term import reg_term


def dp_2d(
    i1: np.ndarray,
    i2: np.ndarray,
    damax: int,
    damin: int,
    dlmax: int,
    dlmin: int,
    w: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Python translation of the MATLAB DP_2d function."""
    m, n = i1.shape

    axial_disp = np.zeros((m, n), dtype=np.float64)
    lateral_disp = np.zeros((m, n), dtype=np.float64)

    num_axial = damax - damin + 1
    num_lateral = dlmax - dlmin + 1

    c = np.zeros((m, num_axial, num_lateral), dtype=np.float64)
    posa_disp = np.arange(damin, damax + 1)
    posl_disp = np.arange(dlmin, dlmax + 1)
    sta = abs(damin)
    stl = abs(dlmin)

    min_cost_inda = np.zeros((m, num_axial, num_lateral), dtype=np.int64)
    min_cost_indl = np.zeros((m, num_axial, num_lateral), dtype=np.int64)
    temp = np.zeros((num_axial, num_lateral), dtype=np.float64)

    for line1 in range(stl + 1, n - dlmax):
        print(f"Processing column {line1 + 1} of {n}...")
        c_prev = c.copy()
        c = np.zeros((m, num_axial, num_lateral), dtype=np.float64)

        for samp in range(sta + 1, m - damax):
            for da1 in range(num_axial):
                for dl1 in range(num_lateral):
                    for da2 in range(num_axial):
                        for dl2 in range(num_lateral):
                            temp[da2, dl2] = (
                                (c[samp - 1, da2, dl2] + c_prev[samp, da2, dl2]) / 2.0
                                + w
                                * reg_term(
                                    int(posa_disp[da1]),
                                    int(posa_disp[da2]),
                                    int(posl_disp[dl1]),
                                    int(posl_disp[dl2]),
                                )
                            )

                    min_index = int(np.argmin(temp))
                    row1, col1 = np.unravel_index(min_index, temp.shape)
                    min_cost_inda[samp, da1, dl1] = row1
                    min_cost_indl[samp, da1, dl1] = col1
                    c[samp, da1, dl1] = temp[row1, col1] + data_term(
                        i1[samp, line1],
                        i2[samp + posa_disp[da1], line1 + posl_disp[dl1]],
                    )

        cm = c[m - damax - 1, :, :]
        last_min_index = np.flatnonzero(cm == cm.min())[-1]
        c1, t1 = np.unravel_index(int(last_min_index), cm.shape)

        axial_disp[m - damax - 1, line1] = posa_disp[c1]
        lateral_disp[m - damax - 1, line1] = posl_disp[t1]

        for samp in range(m - damax - 2, sta - 1, -1):
            c1 = int(min_cost_inda[samp + 1, c1, t1])
            t1 = int(min_cost_indl[samp + 1, c1, t1])
            axial_disp[samp, line1] = posa_disp[c1]
            lateral_disp[samp, line1] = posl_disp[t1]

    return axial_disp, lateral_disp
