"""
view_set_comparator.py
======================
EA Settings Comparator page — migrated from main dashboard.
"""

import streamlit as st
import pandas as pd
from datetime import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from set_comparator import parse_set_file, build_comparison_df, export_set_file, create_zip


def render():
    st.markdown("""
        <style>
        [data-testid="stDataFrame"] {
            margin-left: auto;
            margin-right: auto;
        }
        </style>
    """, unsafe_allow_html=True)

    st.title("⚙ EA Settings Comparator")

    # ── Session state init ────────────────────────────────────────────────────
    if 'ea_files'  not in st.session_state: st.session_state['ea_files']  = {}
    if 'ea_raw'    not in st.session_state: st.session_state['ea_raw']    = {}
    if 'ea_order'  not in st.session_state: st.session_state['ea_order']  = {}
    if 'ea_bytes'  not in st.session_state: st.session_state['ea_bytes']  = {}
    if 'ea_edited' not in st.session_state: st.session_state['ea_edited'] = {}

    # ── Controls ──────────────────────────────────────────────────────────────
    col1, col2, col3 = st.columns([1, 1, 4])
    with col1:
        n_files = st.selectbox("Number of files", list(range(2, 11)), index=0)
    with col2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑 Clear All", type="secondary"):
            for key in ['ea_files', 'ea_raw', 'ea_order', 'ea_bytes', 'ea_edited']:
                st.session_state[key] = {}
            st.rerun()

    # ── File upload slots ─────────────────────────────────────────────────────
    st.divider()
    upload_cols = st.columns(min(n_files, 5))
    for i in range(n_files):
        with upload_cols[i % 5]:
            uploaded = st.file_uploader(
                f"File {i+1}", type=['set'], key=f"ea_upload_{i}"
            )
            if uploaded is not None:
                file_bytes = uploaded.read()
                fname      = uploaded.name
                params, raw_lines, order, is_ubs = parse_set_file(file_bytes, fname)
                st.session_state['ea_files'][fname]  = params
                st.session_state['ea_raw'][fname]    = raw_lines
                st.session_state['ea_order'][fname]  = order
                st.session_state['ea_bytes'][fname]  = file_bytes
                if fname not in st.session_state['ea_edited']:
                    st.session_state['ea_edited'][fname] = params.copy()
                if is_ubs:
                    st.success(f"✓ {fname} — {len(params)} params")
                else:
                    st.warning(
                        f"⚠️ {fname} — this file does not appear to be an "
                        f"Ultimate Breakout System .set file. The comparator is "
                        f"optimised for UBS format only. Results may be incomplete."
                    )

    # ── Comparison table ──────────────────────────────────────────────────────
    files_data = st.session_state['ea_files']

    if len(files_data) >= 2:
        st.divider()

        filenames = list(files_data.keys())

        col1, col2, col3 = st.columns(3)
        with col1:
            source_file = st.selectbox("Source file for comparison", filenames)
        with col2:
            pct_threshold = st.slider("Highlight % variation from source", 0, 100, 10)
        with col3:
            show_diff_only = st.toggle("Show different rows only", value=False)

        files_list = [
            (fn, st.session_state['ea_files'][fn],
             st.session_state['ea_raw'][fn],
             st.session_state['ea_order'][fn])
            for fn in filenames
        ]
        df = build_comparison_df(files_list)

        value_cols  = [c for c in df.columns if c != 'Parameter']
        df['_diff'] = df[value_cols].nunique(axis=1) > 1

        df_display = df[df['_diff']].copy() if show_diff_only else df.copy()
        df_display = df_display.drop(columns=['_diff'])

        source_vals = files_data.get(source_file, {})

        def style_cells(row):
            styles = [''] * len(row)
            param  = row['Parameter']
            src_v  = source_vals.get(param, '')
            for j, col in enumerate(row.index):
                if col == 'Parameter':
                    continue
                cell_v = row[col]
                if col == source_file:
                    styles[j] = 'background-color: rgba(100,100,255,0.15)'
                    continue
                if cell_v == '' or src_v == '':
                    if cell_v != src_v:
                        styles[j] = 'background-color: rgba(255,180,0,0.2)'
                    continue
                try:
                    sv = float(src_v)
                    cv = float(cell_v)
                    if sv == 0:
                        if cv != 0:
                            styles[j] = 'background-color: rgba(255,100,100,0.2)'
                    else:
                        pct_diff = abs((cv - sv) / sv) * 100
                        if pct_diff > pct_threshold:
                            styles[j] = 'background-color: rgba(255,100,100,0.2)'
                        elif pct_diff > 0:
                            styles[j] = 'background-color: rgba(255,180,0,0.15)'
                except:
                    if cell_v != src_v:
                        styles[j] = 'background-color: rgba(255,180,0,0.2)'
            return styles

        st.markdown(
            f"**{len(df_display)} parameters** — "
            f"{int(df[df['_diff']].shape[0])} rows differ across files"
        )

        styled     = df_display.style.apply(style_cells, axis=1)
        row_height = 35
        table_h    = min(len(df_display) * row_height + 40, 2000)

        col_config = {'Parameter': st.column_config.TextColumn('Parameter', width='medium')}
        for fn in filenames:
            col_config[fn] = st.column_config.TextColumn(fn, width='small')

        st.dataframe(
            styled, width='content', hide_index=True,
            height=table_h, column_config=col_config
        )

        # ── Edit & Export ─────────────────────────────────────────────────────
        st.divider()
        st.subheader("Edit & Export")

        edit_file = st.selectbox("Select file to edit", filenames, key='ea_edit_sel')

        if edit_file:
            edited_params = st.session_state['ea_edited'].get(edit_file, {})
            order         = st.session_state['ea_order'].get(edit_file, [])
            raw_lines     = st.session_state['ea_raw'].get(edit_file, {})

            edit_df = pd.DataFrame([
                {'Parameter': k, 'Value': edited_params.get(k, '')}
                for k in order
            ])

            edited = st.data_editor(
                edit_df, width='stretch', hide_index=True, height=400,
                column_config={
                    'Parameter': st.column_config.TextColumn('Parameter', disabled=True),
                    'Value'    : st.column_config.TextColumn('Value'),
                },
                key=f"ea_editor_{edit_file}"
            )

            # Guard against Streamlit returning edited df without expected columns
            if 'Parameter' in edited.columns and 'Value' in edited.columns:
                st.session_state['ea_edited'][edit_file] = dict(
                    zip(edited['Parameter'], edited['Value'].astype(str))
                )

            col1, col2 = st.columns(2)
            with col1:
                export_bytes = export_set_file(
                    edit_file,
                    st.session_state['ea_edited'][edit_file],
                    raw_lines, order,
                    st.session_state['ea_bytes'][edit_file]
                )
                st.download_button(
                    label     = f"⬇ Export {edit_file}",
                    data      = export_bytes,
                    file_name = edit_file,
                    mime      = 'application/octet-stream',
                    key       = 'ea_export_single'
                )

            with col2:
                all_exports = []
                for fn in filenames:
                    fb = export_set_file(
                        fn,
                        st.session_state['ea_edited'].get(fn, files_data[fn]),
                        st.session_state['ea_raw'][fn],
                        st.session_state['ea_order'][fn],
                        st.session_state['ea_bytes'][fn]
                    )
                    all_exports.append((fn, fb))
                zip_bytes = create_zip(all_exports)
                st.download_button(
                    label     = "⬇ Export All as ZIP",
                    data      = zip_bytes,
                    file_name = f"ea_settings_{datetime.today().strftime('%Y%m%d')}.zip",
                    mime      = 'application/zip',
                    key       = 'ea_export_all'
                )

    elif len(files_data) == 1:
        st.info("Upload at least 2 files to compare")
    else:
        st.info("Upload .set files above to begin comparison")