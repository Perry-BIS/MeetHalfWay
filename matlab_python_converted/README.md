# MATLAB to Python conversion

This folder contains a Python translation of the MATLAB code from:

`C:\Users\m1536\Desktop\Matlab`

## Files

- `main.py`: equivalent of `main.m`
- `dp_2d.py`: equivalent of `DP_2d.m`
- `data_term.py`: equivalent of `data_term.m`
- `reg_term.py`: equivalent of `reg_term.m`

## Run

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the script:

```powershell
python main.py --data-dir "C:\Users\m1536\Desktop\Matlab"
```

Optional figure export:

```powershell
python main.py --data-dir "C:\Users\m1536\Desktop\Matlab" --save-figure result.png
```

## Notes

- The code keeps the original MATLAB module split.
- `.mat` loading first uses `scipy.io.loadmat`, then falls back to `h5py` for MATLAB v7.3 files.
- The dynamic programming section is a direct nested-loop translation, so it may run slowly on large arrays.
