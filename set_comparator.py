import io
import zipfile
from pathlib import Path

def parse_set_file(file_bytes, filename):
    """Parse a .set file and return dict of {param: value} and ordered param list"""
    text      = file_bytes.decode('utf-16')
    params    = {}
    raw_lines = {}  # preserve full line for export
    order     = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(';'):
            continue
        if '=' not in line:
            continue
        key, _, rest = line.partition('=')
        key = key.strip()
        # Value is first field before ||
        parts = rest.split('||')
        value = parts[0].strip()
        params[key]    = value
        raw_lines[key] = rest  # preserve everything after =
        order.append(key)

    return params, raw_lines, order

def build_comparison_df(files_data):
    """
    files_data: list of (filename, params, raw_lines, order)
    Returns DataFrame with param names as index, filenames as columns
    """
    import pandas as pd

    # Build union of all param keys preserving order from first file
    all_keys = []
    seen     = set()
    for _, _, _, order in files_data:
        for k in order:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    # Build dataframe
    rows = []
    for key in all_keys:
        row = {'Parameter': key}
        for filename, params, _, _ in files_data:
            row[filename] = params.get(key, '')
        rows.append(row)

    return pd.DataFrame(rows)

def export_set_file(filename, params_edited, raw_lines, order, original_bytes):
    """
    Rebuild .set file with edited values, preserving || fields
    Returns bytes (utf-16 encoded)
    """
    # Get original header comments
    text  = original_bytes.decode('utf-16')
    lines = text.splitlines()
    header_lines = []
    for line in lines:
        if line.startswith(';'):
            header_lines.append(line)
        else:
            break

    output_lines = header_lines.copy()

    for key in order:
        if key not in params_edited:
            continue
        new_value = params_edited[key]
        rest      = raw_lines.get(key, new_value)
        parts     = rest.split('||')
        parts[0]  = str(new_value)
        output_lines.append(f"{key}={'||'.join(parts)}")

    content = '\r\n'.join(output_lines) + '\r\n'
    return content.encode('utf-16')

def create_zip(files_export):
    """
    files_export: list of (filename, bytes)
    Returns zip bytes
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname, fbytes in files_export:
            zf.writestr(fname, fbytes)
    buf.seek(0)
    return buf.read()