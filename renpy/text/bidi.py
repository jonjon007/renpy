# Xbox stub for renpy.text.bidi
# Replaces the native Cython/fribidi module with pure Python LTR-only support.
# The native .pyd crashes on Xbox due to DLL thread-attach issues.

# FriBidi type constants (from fribidi-bidi-types.h)
LTR = 0x110    # FRIBIDI_MASK_STRONG | FRIBIDI_MASK_LETTER
RTL = 0x111    # FRIBIDI_MASK_STRONG | FRIBIDI_MASK_LETTER | FRIBIDI_MASK_RTL
ON = 0x040     # FRIBIDI_MASK_NEUTRAL
WLTR = 0x020   # FRIBIDI_MASK_WEAK
WRTL = 0x021   # FRIBIDI_MASK_WEAK | FRIBIDI_MASK_RTL


def log2vis(s, direction=ON):
    """Convert logical string to visual order. LTR text is unchanged."""
    if direction == ON or direction == WLTR:
        direction = LTR
    elif direction == WRTL:
        direction = RTL
    return s, direction


def get_embedding_levels(s, direction=ON):
    """Get bidi embedding levels. Returns all-zero (LTR) for LTR text."""
    if direction == ON or direction == WLTR:
        direction = LTR
    elif direction == WRTL:
        direction = RTL
    return [0] * len(s), direction
