"""Export wind turbine data from MaStR JSON files to CSV."""

import sys
sys.path.insert(0, ".")

from parser import load_data
from dataclasses import asdict
import pandas as pd

DATA_DIR = "non-pv-data"

print("Loading data from JSON files...")
all_plants = load_data(DATA_DIR)

df = pd.DataFrame([asdict(p) for p in all_plants])
print(f"Total entries: {len(df)}")
print(f"Energy types: {df['energy_type'].value_counts().to_dict()}")

# Filter for wind
wind = df[df["energy_type"] == "Wind"].copy()
print(f"Wind entries: {len(wind)}")

# Export to CSV
output_path = "wind_data.csv"
wind.to_csv(output_path, index=False)
print(f"Exported {len(wind)} wind entries to {output_path}")


# https://zenodo.org/records/18697247