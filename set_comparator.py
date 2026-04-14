import io
import zipfile
from pathlib import Path

def parse_set_file(file_bytes, filename):
    """Parse a .set file and return dict of {param: value} and ordered param list"""
    # MT5 .set files can be UTF-16 LE (with BOM) or plain UTF-8 depending on MT5 version
    if file_bytes[:2] == b'\xff\xfe':
        text = file_bytes[2:].decode('utf-16-le')
    elif file_bytes[:2] == b'\xfe\xff':
        text = file_bytes[2:].decode('utf-16-be')
    else:
        # No BOM — try UTF-8 first, fall back to latin-1
        try:
            text = file_bytes.decode('utf-8')
        except Exception:
            text = file_bytes.decode('latin-1')
    params    = {}
    raw_lines = {}  # preserve full line for export
    order     = []

    # Detect format: Ultimate Breakout .set files use || delimiter
    # Other EA formats (plain key=value with comma-suffixed metadata) are not fully supported
    is_ubs_format = any('||' in line for line in text.splitlines() if '=' in line)

    all_raw_lines = []  # all lines in order for round-trip export
    for line in text.splitlines():
        stripped = line.strip()
        all_raw_lines.append(line)
        if not stripped or stripped.startswith(';'):
            continue
        if '=' not in stripped:
            continue
        key, _, rest = stripped.partition('=')
        key = key.strip()
        # Skip optimisation metadata lines (ParamName,F / ParamName,1 etc.)
        if ',' in key:
            continue
        # Value is first field before ||
        parts = rest.split('||')
        value = parts[0].strip()
        params[key]    = value
        raw_lines[key] = rest  # preserve everything after =
        order.append(key)

    # Store full original lines for round-trip (preserves metadata lines)
    raw_lines['__all_lines__'] = all_raw_lines

    return params, raw_lines, order, is_ubs_format

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
    if original_bytes[:2] == b'\xff\xfe':
        text = original_bytes[2:].decode('utf-16-le')
    elif original_bytes[:2] == b'\xfe\xff':
        text = original_bytes[2:].decode('utf-16-be')
    else:
        try:
            text = original_bytes.decode('utf-8')
        except Exception:
            text = original_bytes.decode('latin-1')
    lines = text.splitlines()
    header_lines = []
    for line in lines:
        if line.startswith(';'):
            header_lines.append(line)
        else:
            break

    # Use the full original line list for round-trip, only updating real param values
    all_lines = raw_lines.get('__all_lines__', [])
    if all_lines:
        output_lines = []
        for line in all_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(';') or '=' not in stripped:
                output_lines.append(line)
                continue
            key, _, rest = stripped.partition('=')
            key = key.strip()
            if ',' in key or key not in params_edited:
                # Metadata line or unknown — preserve as-is
                output_lines.append(line)
            else:
                new_value = params_edited[key]
                parts     = rest.split('||')
                parts[0]  = str(new_value)
                output_lines.append(f"{key}={'||'.join(parts)}")
        content = '\r\n'.join(output_lines) + '\r\n'
    else:
        # Fallback: rebuild from order (original behaviour)
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
    # Preserve original encoding on export
    if original_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return b'\xff\xfe' + content.encode('utf-16-le')
    else:
        return content.encode('utf-8')

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