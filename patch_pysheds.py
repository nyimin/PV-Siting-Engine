import os
import glob

pysheds_dir = "D:/Triune/Stack Space - Documents/Code/PVLayoutEngine/.venv/Lib/site-packages/pysheds"

for py_file in glob.glob(os.path.join(pysheds_dir, "**/*.py"), recursive=True):
    with open(py_file, "r", encoding="utf-8") as f:
        content = f.read()
    
    if "np.in1d" in content:
        content = content.replace("np.in1d", "np.isin")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Patched {py_file}")
