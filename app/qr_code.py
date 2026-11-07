"""Pure-Python ISO/IEC 18004 QR Code matrix and SVG generator for session links."""

from __future__ import annotations

import html
from typing import List, Sequence, Tuple

# QR Version 1..9 specification for Error Correction Level L (7% recovery):
# (version, size, total_data_codewords, ec_codewords_per_block, num_blocks, alignment_centers)
_QR_VERSION_SPECS_L: Tuple[Tuple[int, int, int, int, int, Tuple[int, ...]], ...] = (
    (1, 21, 19, 7, 1, ()),
    (2, 25, 34, 10, 1, (6, 18)),
    (3, 29, 55, 15, 1, (6, 22)),
    (4, 33, 80, 20, 1, (6, 26)),
    (5, 37, 108, 26, 1, (6, 30)),
    (6, 41, 136, 18, 2, (6, 34)),
    (7, 45, 156, 20, 2, (6, 22, 38)),
    (8, 49, 194, 24, 2, (6, 24, 42)),
    (9, 53, 232, 30, 2, (6, 26, 46)),
)

# Precompute GF(256) log and exp tables with primitive polynomial 0x11D
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_val = 1
for _i in range(255):
    _GF_EXP[_i] = _val
    _GF_LOG[_val] = _i
    _val <<= 1
    if _val & 0x100:
        _val ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> List[int]:
    """Compute Reed-Solomon generator polynomial for `degree` error-correction codewords."""
    poly = [1]
    for i in range(degree):
        next_poly = [0] * (len(poly) + 1)
        root = _GF_EXP[i]
        for j, coeff in enumerate(poly):
            next_poly[j] ^= coeff
            next_poly[j + 1] ^= _gf_mul(coeff, root)
        poly = next_poly
    return poly


def _rs_encode(data: Sequence[int], ec_len: int) -> List[int]:
    """Compute `ec_len` Reed-Solomon error correction bytes for `data`."""
    gen = _rs_generator_poly(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor != 0:
            for j in range(ec_len):
                rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


def _bch_format_bits(ec_level_bits: int, mask: int) -> int:
    """Compute 15-bit BCH format information with mask 0x5412."""
    data = ((ec_level_bits & 0x03) << 3) | (mask & 0x07)
    rem = data << 10
    for i in range(4, -1, -1):
        if rem & (1 << (i + 10)):
            rem ^= 0x537 << i
    return ((data << 10) | rem) ^ 0x5412


def _bch_version_bits(version: int) -> int:
    """Compute 18-bit BCH version information for versions >= 7."""
    rem = version << 12
    for i in range(5, -1, -1):
        if rem & (1 << (i + 12)):
            rem ^= 0x1F25 << i
    return (version << 12) | rem


def _place_finder_pattern(
    matrix: List[List[bool]], reserved: List[List[bool]], row: int, col: int
) -> None:
    size = len(matrix)
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            r, c = row + dr, col + dc
            if 0 <= r < size and 0 <= c < size:
                in_outer = 0 <= dr <= 6 and 0 <= dc <= 6
                in_ring = dr in (0, 6) or dc in (0, 6)
                in_center = 2 <= dr <= 4 and 2 <= dc <= 4
                matrix[r][c] = bool(in_outer and (in_ring or in_center))
                reserved[r][c] = True


def _place_alignment_pattern(
    matrix: List[List[bool]], reserved: List[List[bool]], center_r: int, center_c: int
) -> None:
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            r, c = center_r + dr, center_c + dc
            dist = max(abs(dr), abs(dc))
            matrix[r][c] = dist in (0, 2)
            reserved[r][c] = True


def _write_format_bits(
    matrix: List[List[bool]], reserved: List[List[bool]], mask: int
) -> None:
    """Place 15-bit format info for ECC Level L (binary 01) and given mask (MSB bit 14 to LSB bit 0)."""
    size = len(matrix)
    fmt = _bch_format_bits(0b01, mask)

    # Around top-left finder: ordered from Format Bit 14 (MSB) down to Format Bit 0 (LSB)
    coords_tl = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
    ]
    for i, (r, c) in enumerate(coords_tl):
        bit = bool((fmt >> (14 - i)) & 1)
        matrix[r][c] = bit
        reserved[r][c] = True

    # Bottom-left and top-right: ordered from Format Bit 14 (MSB) down to Format Bit 0 (LSB)
    coords_tr_bl = [
        (size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
        (size - 5, 8), (size - 6, 8), (size - 7, 8),
        (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
        (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1),
    ]
    for i, (r, c) in enumerate(coords_tr_bl):
        bit = bool((fmt >> (14 - i)) & 1)
        matrix[r][c] = bit
        reserved[r][c] = True


def _write_version_bits(
    matrix: List[List[bool]], reserved: List[List[bool]], version: int
) -> None:
    if version < 7:
        return
    size = len(matrix)
    vbits = _bch_version_bits(version)
    for i in range(18):
        bit = bool((vbits >> i) & 1)
        r = i // 3
        c = size - 11 + (i % 3)
        matrix[r][c] = bit
        reserved[r][c] = True
        matrix[c][r] = bit
        reserved[c][r] = True


def _mask_bit(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return ((r // 2) + (c // 3)) % 2 == 0
    if mask == 5:
        return ((r * c) % 2 + (r * c) % 3) == 0
    if mask == 6:
        return (((r * c) % 2 + (r * c) % 3) % 2) == 0
    return (((r + c) % 2 + (r * c) % 3) % 2) == 0


def _penalty_score(matrix: List[List[bool]]) -> int:
    """Compute approximate ISO/IEC 18004 mask penalty score to pick the cleanest mask."""
    size = len(matrix)
    score = 0

    # Rule 1: 5+ consecutive same-color modules in row/col
    for r in range(size):
        run_len = 1
        for c in range(1, size):
            if matrix[r][c] == matrix[r][c - 1]:
                run_len += 1
                if run_len == 5:
                    score += 3
                elif run_len > 5:
                    score += 1
            else:
                run_len = 1

    for c in range(size):
        run_len = 1
        for r in range(1, size):
            if matrix[r][c] == matrix[r - 1][c]:
                run_len += 1
                if run_len == 5:
                    score += 3
                elif run_len > 5:
                    score += 1
            else:
                run_len = 1

    # Rule 2: 2x2 blocks of same color
    for r in range(size - 1):
        for c in range(size - 1):
            val = matrix[r][c]
            if (
                matrix[r][c + 1] == val
                and matrix[r + 1][c] == val
                and matrix[r + 1][c + 1] == val
            ):
                score += 3

    return score


def encode_qr_matrix(text: str) -> List[List[bool]]:
    """Encode a UTF-8 string into a 2D boolean QR Code matrix (True = dark module)."""
    payload = text.encode("utf-8")
    n_bytes = len(payload)

    selected_spec = None
    for spec in _QR_VERSION_SPECS_L:
        _ver, _sz, total_data_cw, _ec_cw, _blocks, _align = spec
        # Byte mode header is 4 bits mode + 8 bits length = 12 bits (fits with 2 bytes overhead)
        if n_bytes + 2 <= total_data_cw:
            selected_spec = spec
            break

    if selected_spec is None:
        # Truncate safely if an unusually long string is passed (>230 bytes)
        payload = payload[:230]
        n_bytes = len(payload)
        selected_spec = _QR_VERSION_SPECS_L[-1]

    version, size, total_data_cw, ec_cw_per_block, num_blocks, align_centers = selected_spec

    # Build bit stream: Byte mode (0100) + 8-bit byte count + payload bytes + terminator
    bits: List[int] = [0, 1, 0, 0]
    for shift in range(7, -1, -1):
        bits.append((n_bytes >> shift) & 1)
    for b in payload:
        for shift in range(7, -1, -1):
            bits.append((b >> shift) & 1)

    max_bits = total_data_cw * 8
    term_len = min(4, max_bits - len(bits))
    bits.extend([0] * term_len)
    while len(bits) % 8 != 0:
        bits.append(0)

    data_codewords: List[int] = []
    for i in range(0, len(bits), 8):
        byte_val = 0
        for bit in bits[i : i + 8]:
            byte_val = (byte_val << 1) | bit
        data_codewords.append(byte_val)

    pad_bytes = (0xEC, 0x11)
    pad_idx = 0
    while len(data_codewords) < total_data_cw:
        data_codewords.append(pad_bytes[pad_idx % 2])
        pad_idx += 1

    # Split into blocks and compute Reed-Solomon EC codewords
    block_size = total_data_cw // num_blocks
    data_blocks: List[List[int]] = []
    ec_blocks: List[List[int]] = []
    for b_idx in range(num_blocks):
        blk = data_codewords[b_idx * block_size : (b_idx + 1) * block_size]
        data_blocks.append(blk)
        ec_blocks.append(_rs_encode(blk, ec_cw_per_block))

    # Interleave data codewords then EC codewords
    final_codewords: List[int] = []
    for col_i in range(block_size):
        for b_idx in range(num_blocks):
            final_codewords.append(data_blocks[b_idx][col_i])
    for col_i in range(ec_cw_per_block):
        for b_idx in range(num_blocks):
            final_codewords.append(ec_blocks[b_idx][col_i])

    final_bits: List[int] = []
    for cw in final_codewords:
        for shift in range(7, -1, -1):
            final_bits.append((cw >> shift) & 1)

    # Construct base matrix and reserved mask
    base_matrix = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    _place_finder_pattern(base_matrix, reserved, 0, 0)
    _place_finder_pattern(base_matrix, reserved, 0, size - 7)
    _place_finder_pattern(base_matrix, reserved, size - 7, 0)

    # Alignment patterns
    for ar in align_centers:
        for ac in align_centers:
            if (ar == 6 and ac == 6) or (ar == 6 and ac == size - 7) or (ar == size - 7 and ac == 6):
                continue
            _place_alignment_pattern(base_matrix, reserved, ar, ac)

    # Timing patterns (row 6 and col 6)
    for i in range(8, size - 8):
        bit = (i % 2) == 0
        if not reserved[6][i]:
            base_matrix[6][i] = bit
            reserved[6][i] = True
        if not reserved[i][6]:
            base_matrix[i][6] = bit
            reserved[i][6] = True

    # Dark module
    base_matrix[4 * version + 9][8] = True
    reserved[4 * version + 9][8] = True

    # Reserve format & version areas before data placement
    _write_format_bits(base_matrix, reserved, 0)
    _write_version_bits(base_matrix, reserved, version)

    # Upward/downward 2-column zigzag data placement
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        row_iter = range(size - 1, -1, -1) if upward else range(size)
        for r in row_iter:
            for dc in (0, -1):
                c = col + dc
                if not reserved[r][c]:
                    bit_val = bool(final_bits[bit_idx]) if bit_idx < len(final_bits) else False
                    base_matrix[r][c] = bit_val
                    bit_idx += 1
        upward = not upward
        col -= 2

    # Evaluate masks 0..7 and select lowest penalty
    best_matrix: List[List[bool]] = base_matrix
    best_score = 10**9
    for mask in range(8):
        candidate = [row[:] for row in base_matrix]
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and _mask_bit(mask, r, c):
                    candidate[r][c] = not candidate[r][c]
        _write_format_bits(candidate, reserved, mask)
        score = _penalty_score(candidate)
        if score < best_score:
            best_score = score
            best_matrix = candidate

    return best_matrix


def qr_matrix_to_svg(
    matrix: Sequence[Sequence[bool]],
    border: int = 4,
    dark_color: str = "#0D1014",
    light_color: str = "#FFFFFF",
) -> str:
    """Render a QR Code boolean matrix as a crisp standalone SVG string."""
    size = len(matrix)
    total = size + border * 2
    rects: List[str] = []
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if val:
                rects.append(f"M{c + border},{r + border}h1v1h-1z")
    path_data = "".join(rects)
    dark_esc = html.escape(dark_color, quote=True)
    light_esc = html.escape(light_color, quote=True)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'shape-rendering="crispEdges" role="img" aria-label="Session QR Code">'
        f'<rect width="{total}" height="{total}" fill="{light_esc}" rx="1.5"/>'
        f'<path d="{path_data}" fill="{dark_esc}"/>'
        f"</svg>"
    )
