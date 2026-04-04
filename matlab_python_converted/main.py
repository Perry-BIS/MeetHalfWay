from __future__ import annotations

import argparse
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.io import loadmat

from dp_2d import dp_2d


DEFAULT_DATA_DIR = Path(r"C:\Users\m1536\Desktop\Matlab")


def load_im5(mat_path: Path) -> np.ndarray:
    """Load the Im5 variable from a MATLAB .mat file."""
    try:
        data = loadmat(mat_path)
        if "Im5" not in data:
            raise KeyError(f"'Im5' not found in {mat_path}")
        return np.asarray(data["Im5"], dtype=np.float64)
    except NotImplementedError:
        import h5py

        with h5py.File(mat_path, "r") as handle:
            if "Im5" not in handle:
                raise KeyError(f"'Im5' not found in {mat_path}")
            return np.array(handle["Im5"], dtype=np.float64)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Python translation of the MATLAB 2D displacement estimation script."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory containing 0_both_apodization.mat and 1_both_apodization.mat",
    )
    parser.add_argument("--damin", type=int, default=-40)
    parser.add_argument("--damax", type=int, default=40)
    parser.add_argument("--dlmin", type=int, default=-2)
    parser.add_argument("--dlmax", type=int, default=2)
    parser.add_argument("--weight", type=float, default=0.1)
    parser.add_argument(
        "--save-figure",
        type=Path,
        default=None,
        help="Optional output path for the displacement figure",
    )
    return parser.parse_args()


def crop_results(
    axial_disp: np.ndarray,
    lateral_disp: np.ndarray,
    damin: int,
    damax: int,
    dlmin: int,
    dlmax: int,
) -> tuple[np.ndarray, np.ndarray]:
    axial_disp_final = axial_disp.copy()
    lateral_disp_final = lateral_disp.copy()

    sta = abs(damin)
    stl = abs(dlmin)

    row_s = sta + 1
    row_e = axial_disp.shape[0] - damax
    col_s = stl + 1 + 5
    col_e = axial_disp.shape[1] - dlmax

    if row_e > row_s and col_e > col_s:
        axial_disp_final = axial_disp[row_s:row_e, col_s:col_e]
        lateral_disp_final = lateral_disp[row_s:row_e, col_s:col_e]
    else:
        print("Image is too small for edge cropping, using the original result.")

    return axial_disp_final, lateral_disp_final


def plot_results(
    axial_disp_final: np.ndarray,
    lateral_disp_final: np.ndarray,
    save_figure: Path | None = None,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 6), facecolor="white")

    im0 = axes[0].imshow(axial_disp_final, aspect="auto", origin="upper")
    axes[0].set_title("Axial Displacement")
    axes[0].set_xlabel("Lateral")
    axes[0].set_ylabel("Axial")
    fig.colorbar(im0, ax=axes[0])

    im1 = axes[1].imshow(lateral_disp_final, aspect="auto", origin="upper")
    axes[1].set_title("Lateral Displacement")
    axes[1].set_xlabel("Lateral")
    axes[1].set_ylabel("Axial")
    fig.colorbar(im1, ax=axes[1])

    plt.tight_layout()

    if save_figure is not None:
        fig.savefig(save_figure, dpi=200, bbox_inches="tight")

    plt.show()


def main() -> None:
    args = parse_args()

    print("Loading MATLAB data...")
    im2 = load_im5(args.data_dir / "0_both_apodization.mat")
    im1 = load_im5(args.data_dir / "1_both_apodization.mat")

    max_im = np.max(im2)
    im2 = im2 / max_im
    im1 = im1 / max_im

    print("Running 2D dynamic programming displacement estimation...")
    start = time.perf_counter()
    axial_disp, lateral_disp = dp_2d(
        im1,
        im2,
        args.damax,
        args.damin,
        args.dlmax,
        args.dlmin,
        args.weight,
    )
    elapsed = time.perf_counter() - start
    print(f"Finished in {elapsed:.2f} seconds.")

    axial_disp_final, lateral_disp_final = crop_results(
        axial_disp,
        lateral_disp,
        args.damin,
        args.damax,
        args.dlmin,
        args.dlmax,
    )

    plot_results(axial_disp_final, lateral_disp_final, args.save_figure)


if __name__ == "__main__":
    main()
