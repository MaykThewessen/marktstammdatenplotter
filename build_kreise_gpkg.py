"""
Build germany_kreise.gpkg from Eurostat GISCO NUTS boundaries.

NUTS-3 = Kreise (admin_level=6 in OSM notation)
NUTS-1 city-states (Berlin, Hamburg, Bremen) = admin_level=4

This replicates the structure produced by the original osmfilter + ogr2ogr pipeline.
"""

import geopandas as gpd
import pandas as pd
import requests

NUTS3_URL = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2021_4326_LEVL_3.geojson"
NUTS1_URL = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2021_4326_LEVL_1.geojson"

# NUTS-3 codes for Berlin, Hamburg, Bremen (city-states) — these exist as both
# NUTS-1 and NUTS-3. We use the NUTS-1 polygon as admin_level=4 to match OSM behaviour.
CITY_STATE_NUTS1 = {"DE3": "Berlin", "DE6": "Hamburg", "DE5": "Bremen"}

print("Downloading NUTS-3 boundaries from Eurostat GISCO...")
nuts3 = gpd.read_file(NUTS3_URL)
nuts3_de = nuts3[nuts3["CNTR_CODE"] == "DE"].copy()
print(f"  {len(nuts3_de)} NUTS-3 regions for Germany")

# Remove city-state NUTS-3 entries to avoid duplication;
# we'll add them back as merged NUTS-1 polygons with admin_level=4.
# Berlin=DE3xx, Hamburg=DE6xx, Bremen=DE5xx
city_nuts3_mask = nuts3_de["NUTS_ID"].str.startswith(("DE3", "DE5", "DE6"))
kreise = nuts3_de[~city_nuts3_mask].copy()
kreise["admin_level"] = "6"
kreise["boundary"] = "administrative"
kreise = kreise.rename(columns={"NAME_LATN": "name"})[["name", "admin_level", "boundary", "geometry"]]

print("Downloading NUTS-1 boundaries for city-states...")
nuts1 = gpd.read_file(NUTS1_URL)
city_states = nuts1[nuts1["NUTS_ID"].isin(CITY_STATE_NUTS1.keys())].copy()
city_states["admin_level"] = "4"
city_states["boundary"] = "administrative"
city_states["name"] = city_states["NUTS_ID"].map(CITY_STATE_NUTS1)
city_states = city_states[["name", "admin_level", "boundary", "geometry"]]

print(f"  City-states added: {city_states['name'].tolist()}")

combined = pd.concat([kreise, city_states], ignore_index=True)
gdf = gpd.GeoDataFrame(combined, crs="EPSG:4326")

output_path = "germany_kreise.gpkg"
gdf.to_file(output_path, layer="multipolygons", driver="GPKG")
print(f"\nSaved {len(gdf)} administrative units to '{output_path}'")
print(f"  admin_level=6 (Kreise): {(gdf['admin_level']=='6').sum()}")
print(f"  admin_level=4 (city-states): {(gdf['admin_level']=='4').sum()}")
