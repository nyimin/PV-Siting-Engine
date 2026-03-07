import geopandas as gpd
import pandas as pd
import numpy as np
import os
from shapely.geometry import Point

def analyze_layout(gpkg_path):
    print("--- PV Layout Engine Output Analysis ---")
    
    layers = ["solar_blocks", "pv_rows", "inverters", "transformers", "internal_roads", "mv_cables"]
    data = {}
    
    for layer in layers:
        try:
            data[layer] = gpd.read_file(gpkg_path, layer=layer)
            print(f"Loaded {layer}: {len(data[layer])} features")
        except Exception as e:
            print(f"Failed to load {layer}: {e}")
            
    # 1. Block Analysis
    if "solar_blocks" in data and "pv_rows" in data:
        blocks = data["solar_blocks"]
        rows = data["pv_rows"]
        
        print("\n=== PV Block Structure ===")
        print(f"Total Blocks: {len(blocks)}")
        if not blocks.empty:
            print(f"Average Fill %: {blocks['fill_pct'].mean():.1f}%")
            print(f"Capacity per block (AC): {blocks['capacity_ac_mw'].mean():.2f} MW")
            
            # Check contiguous / fragmented by checking row counts per block
            rows_per_block = rows.groupby("block_id").size()
            print(f"Average rows per block: {rows_per_block.mean():.1f}")
            print(f"Min rows per block: {rows_per_block.min()}")
            
    # 2. Inverter & Transformer Placement
    if "inverters" in data and "transformers" in data and "solar_blocks" in data:
        invs = data["inverters"]
        txs = data["transformers"]
        blocks = data["solar_blocks"]
        
        print("\n=== Inverter & Transformer Placement ===")
        print(f"Total Inverters: {len(invs)}")
        print(f"Total Transformers: {len(txs)}")
        
        # Check if tx is at block centroid
        tx_centroid_distances = []
        for _, block in blocks.iterrows():
            bid = block.get("block_id")
            block_tx = txs[txs["block_id"] == bid]
            if not block_tx.empty:
                # Calculate distance from transformer to the geometric centroid of the block
                tx_point = block_tx.geometry.iloc[0]
                block_centroid = block.geometry.centroid
                dist = tx_point.distance(block_centroid)
                tx_centroid_distances.append(dist)
        
        if tx_centroid_distances:
            print(f"Avg Transformer distance to block centroid: {np.mean(tx_centroid_distances):.2f}m")
            
    # 3. MV Feeder Topology
    if "mv_cables" in data:
        mv = data["mv_cables"]
        print("\n=== MV Feeder Topology ===")
        print(f"Total MV Cables: {len(mv)}")
        feeders = mv["feeder_id"].unique()
        print(f"Number of Feeders: {len(feeders)}")
        for f in sorted(feeders):
            f_len = mv[mv["feeder_id"] == f].geometry.length.sum() / 1000
            print(f"  {f}: {f_len:.2f} km")
            
    # 4. Roads
    if "internal_roads" in data:
        roads = data["internal_roads"]
        print("\n=== Internal Road Layout ===")
        print(f"Total Roads: {len(roads)}")
        if not roads.empty:
            for rtype in roads["road_type"].unique():
                rt_len = roads[roads["road_type"] == rtype].geometry.length.sum() / 1000
                print(f"  {rtype}: {rt_len:.2f} km")

if __name__ == '__main__':
    analyze_layout('outputs/layout.gpkg')
