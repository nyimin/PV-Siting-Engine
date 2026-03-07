import os

file_path = "layout/block_generator.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Find the start of the function definition
# It starts at `def generate_solar_blocks`
start_idx = content.find("def generate_solar_blocks(buildable_area_gdf, config, terrain_paths=None):")
if start_idx == -1:
    print("Function not found")
    exit(1)

new_func = """def generate_solar_blocks(buildable_area_gdf, config, terrain_paths=None):
    \"""
    Generates PV blocks by tessellating the buildable area with a regular E-W/N-S grid,
    then fills the grid with 2P portrait fixed-tilt rows. Employs 2D Spatial Clustering 
    (K-Means) to group generated rows into contiguous, commercial-scale Inverter Blocks 
    (e.g., 3.2 MWac).
    \"""
    logger.info("Generating tessellated PV rows and clustering into Power Blocks...")

    if buildable_area_gdf.empty:
        return buildable_area_gdf, buildable_area_gdf

    # Derive site latitude from buildable area centroid
    site_wgs84 = buildable_area_gdf.to_crs(epsg=4326)
    site_latitude = site_wgs84.geometry.union_all().centroid.y
    winter_solar_elevation = max(10.0, 90.0 - abs(site_latitude) - 23.5)
    logger.info(f"  Site latitude: {site_latitude:.2f}° — winter solar elevation: {winter_solar_elevation:.1f}°")

    solar_cfg = config["solar"]
    block_cfg = config["block"]
    tracker_type = solar_cfg.get("tracking", "fixed")

    # Compute physical parameters (GCR-driven)
    (flat_pitch, strings_per_row, string_ew_width, phys_row_length,
     table_height_m, row_height_m, tilt_deg, target_strings_per_block) = \\
        _compute_block_dimensions(config, site_latitude)

    # ── DC/AC capacity from first principles ────────────────────────────
    mod_power_w = solar_cfg["module_power_w"]
    mods_per_string = solar_cfg["modules_per_string"]
    inv_capacity_kw = solar_cfg["inverter_capacity_kw"]
    invs_per_block = block_cfg["inverters_per_block"]
    strings_per_inv = solar_cfg["strings_per_inverter"]

    dc_per_block = (invs_per_block * strings_per_inv * mods_per_string * mod_power_w) / 1e6  # MWdc
    ac_per_block = (invs_per_block * inv_capacity_kw) / 1000  # MWac
    logger.info(f"  Target Power Block: {dc_per_block:.3f} MWdc / {ac_per_block:.3f} MWac "
                f"({target_strings_per_block} strings)")

    min_fill = block_cfg.get("min_fill_fraction", 0.60)
    oblique = block_cfg.get("oblique_tessellation", False)
    
    if tracker_type == "fixed" and oblique:
        logger.info("  Oblique tessellation disabled: Fixed-tilt systems require strict True South alignment.")
        oblique = False

    working_area = buildable_area_gdf.copy()
    centroid = working_area.geometry.union_all().centroid
    angle_deg = 0.0

    if oblique:
        # Find principal axis from Minimum Bounding Rectangle
        hull = working_area.geometry.union_all().convex_hull
        rect = hull.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        edge_lengths = [math.hypot(coords[i+1][0] - coords[i][0], coords[i+1][1] - coords[i][1]) for i in range(4)]
        longest_edge_idx = np.argmax(edge_lengths)
        dx = coords[longest_edge_idx+1][0] - coords[longest_edge_idx][0]
        dy = coords[longest_edge_idx+1][1] - coords[longest_edge_idx][1]
        angle_rad = math.atan2(dy, dx)
        angle_deg = math.degrees(angle_rad)
        
        working_area["geometry"] = working_area.geometry.rotate(-angle_deg, origin=centroid)
        logger.info(f"  Oblique tessellation: rotated bounding box by {-angle_deg:.2f}° to align with principal axis.")

    minx, miny, maxx, maxy = working_area.total_bounds
    block_col_width = phys_row_length + 5.0      # E-W width of one block column

    all_candidate_rows = []

    # ── Phase 1: Generate all possible rows ──
    x = minx
    while x < maxx:
        col_poly = Polygon([
            (x, miny), (x + block_col_width, miny),
            (x + block_col_width, maxy), (x, maxy)
        ])

        col_intersection = gpd.overlay(
            gpd.GeoDataFrame(geometry=[col_poly], crs=working_area.crs),
            working_area,
            how='intersection'
        )

        if not col_intersection.empty:
            for _, cand in col_intersection.iterrows():
                geom = cand.geometry
                polys = list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]

                for poly in polys:
                    c_minx, c_miny, c_maxx, c_maxy = poly.bounds
                    y = c_miny + row_height_m / 2

                    while y + row_height_m / 2 <= c_maxy:
                        row_pitch = flat_pitch
                        if terrain_paths and "slope" in terrain_paths:
                            mid_x = (c_minx + c_maxx) / 2
                            pt = Point(mid_x, y)
                            if angle_deg != 0.0:
                                pt = rotate(pt, angle_deg, origin=centroid)
                            pt_slope = _sample_raster_mean(pt.buffer(10), terrain_paths["slope"])
                            if pt_slope is not None and pt_slope > 0:
                                row_pitch = _compute_slope_adjusted_pitch(
                                    flat_pitch, tilt_deg, pt_slope, winter_solar_elevation
                                )

                        half_len = phys_row_length / 2
                        half_h = row_height_m / 2
                        row_center_x = x + block_col_width / 2

                        row_rect = Polygon([
                            (row_center_x - half_len, y - half_h),
                            (row_center_x + half_len, y - half_h),
                            (row_center_x + half_len, y + half_h),
                            (row_center_x - half_len, y + half_h),
                        ])

                        if poly.contains(row_rect.centroid):
                            overlap = poly.intersection(row_rect)
                            if overlap.area / row_rect.area >= 0.50:
                                true_row_rect = row_rect
                                if angle_deg != 0.0:
                                    true_row_rect = rotate(row_rect, angle_deg, origin=centroid)

                                terrain_ok, slope_val = _check_row_terrain(true_row_rect, terrain_paths, config)
                                if terrain_ok:
                                    all_candidate_rows.append({
                                        "geometry": true_row_rect,
                                        "slope_deg": round(slope_val, 2),
                                        "strings": strings_per_row
                                    })
                        y += row_pitch
        x += block_col_width

    total_generated_strings = sum(r["strings"] for r in all_candidate_rows)
    logger.info(f"  Generated {len(all_candidate_rows)} total rows ({total_generated_strings} strings).")

    if not all_candidate_rows:
        logger.warning("No valid rows generated.")
        empty = gpd.GeoDataFrame(columns=["block_id", "geometry"], crs=buildable_area_gdf.crs)
        return empty, empty

    # ── Phase 2: Spatial Clustering of Rows into Power Blocks ──
    n_blocks = max(1, int(round(total_generated_strings / target_strings_per_block)))
    logger.info(f"  Clustering rows into ~{n_blocks} spatial Power Blocks (K-Means)...")
    
    coords = np.array([(r["geometry"].centroid.x, r["geometry"].centroid.y) for r in all_candidate_rows])
    
    try:
        from sklearn.cluster import KMeans
        km = KMeans(n_clusters=n_blocks, n_init=10, random_state=42)
        labels = km.fit_predict(coords)
    except ImportError:
        logger.warning("  sklearn not available! Falling back to 1D chunking (legacy behavior).")
        labels = np.array([i // max(1, len(all_candidate_rows) // n_blocks) for i in range(len(all_candidate_rows))])

    blocks = []
    final_rows = []
    block_id_counter = 1

    # Group by cluster label
    for cluster_id in range(n_blocks):
        cluster_rows = [all_candidate_rows[i] for i in range(len(all_candidate_rows)) if labels[i] == cluster_id]
        if not cluster_rows:
            continue
            
        current_strings = 0
        current_block_rows = []
        
        # Sort by Y then X to ensure contiguous internal numbering
        cluster_rows.sort(key=lambda r: (r["geometry"].centroid.y, r["geometry"].centroid.x))
        
        for r in cluster_rows:
            if current_strings >= target_strings_per_block:
                break # Limit block to target capacity exactly
                
            strings_to_add = min(r["strings"], target_strings_per_block - current_strings)
            current_block_rows.append((r, strings_to_add))
            current_strings += strings_to_add

        fill_pct = current_strings / target_strings_per_block
        if fill_pct >= min_fill:
            row_geoms = [b[0]["geometry"] for b in current_block_rows]
            block_geom = unary_union(row_geoms).convex_hull
            
            block_name = f"B{block_id_counter:03d}"
            block_dc = round(current_strings * mods_per_string * mod_power_w / 1e6, 3)
            block_ac = round((current_strings / target_strings_per_block) * ac_per_block, 3)

            blocks.append({
                "block_id": block_name,
                "geometry": block_geom,
                "area_ha": round(block_geom.area / 10000, 3),
                "capacity_ac_mw": block_ac,
                "capacity_dc_mw": block_dc,
                "strings": current_strings,
                "fill_pct": round(fill_pct * 100, 1),
            })
            for r_idx, (r_data, s_add) in enumerate(current_block_rows):
                final_rows.append({
                    "block_id": block_name,
                    "row_id": r_idx,
                    "geometry": r_data["geometry"],
                    "strings": s_add,
                    "slope_deg": r_data["slope_deg"],
                })
            block_id_counter += 1

    blocks_gdf = gpd.GeoDataFrame(blocks, crs=buildable_area_gdf.crs)
    rows_gdf = gpd.GeoDataFrame(final_rows, crs=buildable_area_gdf.crs)

    # ── Target AC capacity limiting ──
    target_ac_mw = config.get("project", {}).get("target_ac_mw")
    if target_ac_mw and target_ac_mw > 0:
        target_blocks = int(round(target_ac_mw / ac_per_block))
        logger.info(f"  Target limit applied: {target_ac_mw} MWac → {target_blocks} full blocks needed.")

        blocks_gdf = blocks_gdf.sort_values(by="strings", ascending=False)
        if len(blocks_gdf) > target_blocks:
            blocks_gdf = blocks_gdf.head(target_blocks).reset_index(drop=True)
            logger.info(f"  → Retained top {target_blocks} blocks (highest fill).")
        
        valid_block_ids = blocks_gdf["block_id"].tolist()
        rows_gdf = rows_gdf[rows_gdf["block_id"].isin(valid_block_ids)].copy()

    total_ac = blocks_gdf["capacity_ac_mw"].sum() if not blocks_gdf.empty else 0
    total_dc = blocks_gdf["capacity_dc_mw"].sum() if not blocks_gdf.empty else 0
    total_rows = len(rows_gdf)
    logger.info(
        f"Generated {len(blocks_gdf)} blocks / {total_rows} PV rows: "
        f"{total_ac:.2f} MWac / {total_dc:.2f} MWdc "
        f"(DC/AC = {total_dc/total_ac:.2f})" if total_ac > 0 else
        f"Generated {len(blocks_gdf)} blocks / {total_rows} PV rows."
    )

    return blocks_gdf, rows_gdf
"""

new_content = content[:start_idx] + new_func

with open(file_path, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Rewritten block generator successfully")
