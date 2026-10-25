# Copyright 2026 Jonathan Martin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
SDL_Renderer-based GPU-accelerated 2D renderer for Ren'Py.

Uses SDL2's SDL_Renderer API which maps to D3D12 on Xbox Series,
D3D11 on Windows, Metal on macOS, etc. This avoids the need for
OpenGL on platforms that don't support it (like Xbox).
"""

from __future__ import division, absolute_import, with_statement, print_function, unicode_literals
from renpy.compat import PY2, basestring, bchr, bord, chr, open, pystr, range, round, str, tobytes, unicode  # *

import math
import os
import sys
import threading
import time
import weakref
import collections

import renpy
import pygame_sdl2 as pygame
from pygame_sdl2.render import Renderer as SDLRenderer, GPUTexture, \
    BLENDMODE_NONE, BLENDMODE_BLEND, BLENDMODE_ADD, BLENDMODE_MOD, \
    SCALEMODE_NEAREST, SCALEMODE_LINEAR, \
    TEXTUREACCESS_TARGET

from renpy.display.render import BLIT, DISSOLVE, IMAGEDISSOLVE, PIXELLATE, FLATTEN


def _dbg(msg):
    """Write debug message to Xbox OutputDebugString via redirected stdout."""
    try:
        sys.stdout.write("SDLR: " + str(msg) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _on_main_thread():
    return threading.current_thread() is threading.main_thread()


# 2D affine matrices are tuples (a, b, c, d, e, f):
#   x' = a*x + b*y + c
#   y' = d*x + e*y + f
_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)

# (zdz, zdw) of an identity model matrix.
_Z_IDENTITY = (1.0, 0.0)
_QUAD_INDICES = [0, 1, 2, 0, 2, 3]

# Half-size of the clip box on an axis that isn't clipped (GL2's BIG_PIXELS).
_BIG_PIXELS = 1000000.0

# Below this, nothing is visible in an 8-bit target.
_MIN_ALPHA = 1.0 / 512.0

# Forces the next _apply_clip() to call set_clip_rect.
_NO_CLIP_APPLIED = ("unset",)


def _mat_mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * D, a * B + b * E, a * C + b * F + c,
            d * A + e * D, d * B + e * E, d * C + e * F + f)


def _is_projective(rev):
    """True if `rev` can't be split into a 2D affine part plus z (zdz, zdw):
    perspective cameras, and x/y rotation that mixes z into x and y."""
    return (rev.wdx != 0.0 or rev.wdy != 0.0 or rev.wdz != 0.0 or rev.wdw != 1.0 or
            rev.xdz != 0.0 or rev.ydz != 0.0 or rev.zdx != 0.0 or rev.zdy != 0.0)


def _reverse_matrix(render, full=False):
    """
    The matrix for render.reverse (child -> render space), or None.

    Normally the 2D affine part as a 6-tuple (z is tracked separately). When
    `full` is true or the matrix is projective, the 4x4 Matrix itself is
    returned and the subtree is drawn with full 3D math.
    """
    rev = render.reverse
    if rev is None:
        return None

    if full or _is_projective(rev):
        return rev

    m = (rev.xdx, rev.xdy, rev.xdw, rev.ydx, rev.ydy, rev.ydw)
    if m == _IDENTITY:
        return None

    return m


def _reverse_z(render):
    """(zdz, zdw) of render.reverse when it moves or scales z, else None."""
    rev = render.reverse
    if rev is None:
        return None

    zdz = rev.zdz
    zdw = rev.zdw
    if zdw == 0.0 and zdz == 1.0:
        return None

    return (zdz, zdw)


def _to_matrix(m, z):
    """A 4x4 Matrix from a 2D affine 6-tuple and its (zdz, zdw)."""
    a, b, c, d, e, f = m
    return renpy.display.matrix.Matrix((
        a, b, 0.0, c,
        d, e, 0.0, f,
        0.0, 0.0, z[0], z[1],
        0.0, 0.0, 0.0, 1.0))


def _child_matrix(m, cx, cy, rev):
    """GL2: child model = parent * offset(cx, cy) * render.reverse."""
    if type(m) is not tuple or (rev is not None and type(rev) is not tuple):
        Matrix = renpy.display.matrix.Matrix
        if type(m) is tuple:
            m = _to_matrix(m, _Z_IDENTITY)
        if cx or cy:
            m = m * Matrix.offset(cx, cy, 0.0)
        if rev is not None:
            m = m * rev
        return m

    if cx or cy:
        a, b, c, d, e, f = m
        m = (a, b, a * cx + b * cy + c, d, e, d * cx + e * cy + f)
    if rev is not None:
        m = _mat_mul(m, rev)
    return m


def _mul_scale(m, sx, sy):
    """m * scale(sx, sy)."""
    if type(m) is tuple:
        return _mat_mul(m, (sx, 0.0, 0.0, 0.0, sy, 0.0))
    return m * renpy.display.matrix.Matrix.scale(sx, sy, 1.0)


def _project(m, x, y):
    """(x, y, z, w) of local point (x, y) through a 4x4 Matrix, divided by w.
    None if the point is at or behind the eye."""
    px, py, pz, pw = m.transform(x, y, 0.0, 1.0, 4)
    if pw <= 1e-6:
        return None
    return (px / pw, py / pw, pz / pw, pw)


def _bbox(m, x0, y0, x1, y1):
    """Drawable-space bounding box (x0, y0, x1, y1) of a local rectangle."""
    if type(m) is not tuple:
        pts = [_project(m, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        if None in pts:
            return (-_BIG_PIXELS, -_BIG_PIXELS, _BIG_PIXELS, _BIG_PIXELS)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    a, b, c, d, e, f = m
    xs = (a * x0 + b * y0, a * x1 + b * y0, a * x1 + b * y1, a * x0 + b * y1)
    ys = (d * x0 + e * y0, d * x1 + e * y0, d * x1 + e * y1, d * x0 + e * y1)
    return (min(xs) + c, min(ys) + f, max(xs) + c, max(ys) + f)


def _color_byte(v):
    if v <= 0.0:
        return 0
    if v >= 1.0:
        return 255
    return int(v * 255.0 + 0.5)


def _compose_blend(src_color, dst_color, src_alpha, dst_alpha):
    """SDL_ComposeCustomBlendMode with SDL_BLENDOPERATION_ADD."""
    add = 1
    return (add | (src_color << 4) | (dst_color << 8) |
            (add << 16) | (src_alpha << 20) | (dst_alpha << 24))


def _classify_matrixcolor(cm):
    """
    Returns ("identity", None), ("tint", (r, g, b)) when the matrix only
    scales rgb by factors in [0, 1] (drawable with vertex color), or
    ("general", rows) with rows a 4-tuple of 4-tuples.
    """
    rows = (
        (cm.xdx, cm.xdy, cm.xdz, cm.xdw),
        (cm.ydx, cm.ydy, cm.ydz, cm.ydw),
        (cm.zdx, cm.zdy, cm.zdz, cm.zdw),
        (cm.wdx, cm.wdy, cm.wdz, cm.wdw),
    )

    eps = 1e-6
    diagonal = all(abs(rows[i][j]) < eps for i in range(4) for j in range(4) if i != j)

    if diagonal and abs(rows[3][3] - 1.0) < eps:
        tint = (rows[0][0], rows[1][1], rows[2][2])
        if all(abs(t - 1.0) < eps for t in tint):
            return "identity", None
        if all(-eps <= t <= 1.0 + eps for t in tint):
            return "tint", tint

    return "general", tuple(tuple(round(v, 5) for v in row) for row in rows)


def _straight_colormatrix(rows, premultiplied):
    """
    The 20-element list for renpy.display.module.colormatrix that gives the
    result of GL2's matrixcolor (M * premultiplied color).

    On premultiplied pixels that is M itself. On straight-alpha pixels,
    M * (a*rgb, a) = a * (M[:, :3] * rgb + M[:, 3]), so the alpha column acts
    as a constant offset on straight color. When the alpha row is (0, 0, 0, k)
    the result alpha is k*a, so the color is divided by k to stay straight.
    """
    if premultiplied:
        rv = []
        for row in rows:
            rv.extend(row)
            rv.append(0.0)
        return rv

    arow = rows[3]
    k = 1.0
    if abs(arow[0]) + abs(arow[1]) + abs(arow[2]) < 1e-6 and arow[3] > 1e-6:
        k = arow[3]

    rv = []
    for row in rows[:3]:
        rv.extend([row[0] / k, row[1] / k, row[2] / k, 0.0, row[3] / k])
    rv.extend([arow[0], arow[1], arow[2], arow[3], 0.0])
    return rv


class TextureCache:
    """
    GPU textures for pygame Surfaces, reused across frames.

    Each entry keeps its surface alive, so id(surface) can't be recycled by
    a different surface while cached. Textures are destroyed only in
    end_frame(): on D3D12 every SDL_DestroyTexture flushes and waits for the
    GPU, which is cheap right after present (already idle) but very slow
    mid-frame.
    """

    def __init__(self, renderer, max_age=120, max_bytes=256 * 1024 * 1024):
        self.renderer = renderer
        self.entries = collections.OrderedDict()  # id(surf) -> [surf, tex, nbytes, last_frame], LRU first
        self.stale = set()        # ids mutated since upload (may be added off the main thread)
        self.retired = []         # textures to destroy in end_frame()
        self.upload_queue = []    # surfaces waiting for lazy upload
        self.bytes = 0
        self.max_age = max_age
        self.max_bytes = max_bytes
        self.frame = 0

    def get(self, surf):
        """Get cached GPU texture for a pygame Surface, or None."""
        sid = id(surf)
        entry = self.entries.get(sid)
        if entry is None:
            return None
        if sid in self.stale or not entry[1].valid:
            self.stale.discard(sid)
            self._retire(sid)
            return None
        entry[3] = self.frame
        self.entries.move_to_end(sid)
        return entry[1]

    def get_or_upload(self, surf):
        tex = self.get(surf)
        if tex is None:
            tex = self.upload(surf)
        return tex

    _upload_log_count = 0

    def upload(self, surf):
        """Upload a surface to GPU texture immediately. Returns GPUTexture."""
        sid = id(surf)

        # D3D12 rejects zero-size textures
        try:
            sw, sh = surf.get_size()
        except Exception:
            return None
        if sw <= 0 or sh <= 0:
            return None

        self.stale.discard(sid)
        self._retire(sid)

        # Xbox D3D12 workaround: surfaces from SDL_ConvertSurface have
        # internal state that corrupts alpha during texture upload. Copy
        # pixels into a fresh pygame.Surface via BLENDMODE_NONE blit.
        copied = False
        try:
            clean = pygame.Surface((sw, sh), pygame.SRCALPHA, 32)
            renpy.display.accelerator.nogil_copy(surf, clean)
            copied = True
        except Exception as e:
            _dbg("upload: fresh copy FAILED: %s" % e)
            clean = surf

        TextureCache._upload_log_count += 1
        if TextureCache._upload_log_count <= 10:
            _dbg("upload #%d: %dx%d copied=%s" % (
                TextureCache._upload_log_count, sw, sh, copied))

        try:
            tex = self.renderer.create_texture_from_surface(clean)
        except Exception as e:
            _dbg("upload: create_texture_from_surface FAILED: %s" % e)
            return None

        tex.set_blend_mode(BLENDMODE_BLEND)
        # SDL's default sampling is nearest; GL2 samples scaled images linearly.
        tex.set_scale_mode(SCALEMODE_LINEAR)
        nbytes = sw * sh * 4
        self.entries[sid] = [surf, tex, nbytes, self.frame]
        self.bytes += nbytes

        return tex

    def enqueue(self, surf):
        """Queue a surface for lazy upload (one per frame)."""
        self.upload_queue.append(surf)

    def upload_one(self):
        """Upload one queued surface. Returns True if work was done."""
        if not self.upload_queue:
            return False
        surf = self.upload_queue.pop(0)
        self.get_or_upload(surf)
        return True

    def invalidate(self, surf):
        """Mark a surface as mutated — its cached texture is stale."""
        sid = id(surf)
        if sid in self.entries:
            self.stale.add(sid)

    def _retire(self, sid):
        entry = self.entries.pop(sid, None)
        if entry is not None:
            self.bytes -= entry[2]
            self.retired.append(entry[1])

    def end_frame(self):
        """Evict old/stale textures and destroy retired ones. Call after present."""
        for sid in list(self.stale):
            self.stale.discard(sid)
            self._retire(sid)

        oldest = self.frame - self.max_age
        while self.entries:
            sid, entry = next(iter(self.entries.items()))
            if entry[3] >= self.frame:
                break
            if entry[3] > oldest and self.bytes <= self.max_bytes:
                break
            self._retire(sid)

        self._destroy_retired()
        self.frame += 1

    def _destroy_retired(self):
        retired, self.retired = self.retired, []
        for tex in retired:
            try:
                tex.destroy()
            except Exception:
                pass

    def clear(self):
        """Destroy all cached textures."""
        for sid in list(self.entries):
            self._retire(sid)
        self._destroy_retired()
        self.upload_queue.clear()
        self.stale.clear()
        self.bytes = 0


class SDLRendererDraw:
    """
    A Ren'Py renderer that uses SDL2's SDL_Renderer API for GPU-accelerated
    2D compositing. On Xbox this maps to D3D12, on Windows to D3D11.
    """

    # SDL_BlendFactor: ZERO=1, ONE=2, SRC_ALPHA=5, ONE_MINUS_SRC_ALPHA=6.
    BLEND_PREMULTIPLIED = _compose_blend(2, 6, 2, 6)
    BLEND_MASK = _compose_blend(1, 5, 1, 5)
    BLEND_MASK_INVERTED = _compose_blend(1, 6, 1, 6)

    MATRIXCOLOR_CACHE_SIZE = 64
    TARGET_MAX_AGE = 120
    MAX_TARGET_SIZE = 4096

    # Grid used for quads whose w varies (3D rotation under perspective).
    PROJECTED_GRID = 8

    def __init__(self):
        self.renderer = None
        self.texture_cache = None
        self.window = None

        self.virtual_size = None
        self.physical_size = None

        # Virtual-to-draw coordinate mapping (1:1 since SDL_Renderer handles scaling)
        self.draw_per_virt = 1.0
        self.virt_to_draw = None
        self.draw_to_virt = None

        # Rendering info dict
        self.info = {
            "renderer": "sdlrenderer",
            "resizable": False,
            "additive": True,
            "models": False,
        }

        self.full_redraw = True
        self.next_frame = 0
        self.fast_redraw_frames = 0

        # Drawable-space clip box (x0, y0, x1, y1) of the node being drawn,
        # and the one last given to SDL.
        self._clip = None
        self._applied_clip = _NO_CLIP_APPLIED

        # (zdz, zdw) of the current model matrix. Without perspective, GL2
        # clips anything whose z leaves [-1, 1] (e.g. zpos -3000).
        self._z = _Z_IDENTITY

        # Render targets: (texture, previous target, saved clip, saved z) while
        # drawing into them, a per-size pool reused across frames, and this frame's.
        self._target_stack = []
        self._rt_free = {}
        self._rt_used = []

        # id(surface) -> weakref for surfaces holding premultiplied color.
        self._premultiplied = {}

        # (id(surface), matrix, premultiplied) -> (surface, recolored surface)
        self._matrixcolor_cache = collections.OrderedDict()
        self._grid_indices = {}

        # Debug frame counter — log verbose for first N frames
        self._frame_count = 0
        self._dbg_frames = 5

    def init(self, virtual_size):
        """Initialize the SDL_Renderer backend."""

        virtual_w, virtual_h = virtual_size
        self.virtual_size = virtual_size

        try:
            _dbg("init: starting, virtual_size=%s" % (virtual_size,))

            xbox = os.environ.get("RENPY_PLATFORM", "").startswith("xbox")
            _dbg("init: xbox=%s" % xbox)

            flags = pygame.FULLSCREEN if xbox else pygame.RESIZABLE

            # Create window WITHOUT SDL_WINDOW_OPENGL — we use SDL_Renderer, not GL
            if os.environ.get("PYGAME_SDL2_AVOID_GL"):
                # Already avoided by env var
                pass

            # On Xbox GDK, SDL_CreateWindow clamps (0,0) to (1,1).
            # The D3D12 backbuffer size = window size, so we MUST pass
            # an explicit resolution. Use virtual_size for the backbuffer —
            # D3D12 will present/scale to the display's native resolution.
            window_size = virtual_size if xbox else virtual_size

            self.window = pygame.display.set_mode(
                window_size,
                flags,
            )

            # Surfaces in RGBA byte order (what ftfont and the decoders write),
            # and the sample surfaces ffmedia needs to decode video frames.
            # Needs the window, as GL2 and swdraw also do it after set_mode.
            renpy.display.pgrender.set_rgba_masks()

            # Query window size to verify
            win_obj = pygame.display.get_window()
            if win_obj is not None:
                win_size = win_obj.get_size()
                _dbg("init: requested=%s, window.get_size()=%s" % (window_size, win_size))
            else:
                win_size = window_size
                _dbg("init: no window object")

            # Create the accelerated SDL_Renderer with vsync
            self.renderer = SDLRenderer(vsync=True)

            rinfo = self.renderer.info()
            _dbg("init: renderer=%s accel=%s rtt=%s vsync=%s" % (
                rinfo.get("name", "?"), rinfo.get("accelerated", False),
                rinfo.get("rtt", False), rinfo.get("vsync", False)))

            if not rinfo.get("accelerated", False):
                _dbg("init: WARNING - not hardware-accelerated!")

            if not rinfo.get("rtt", False):
                _dbg("init: WARNING - render targets not supported!")
                return False

            # Present a black frame to prime the D3D12 swapchain
            self.renderer.clear((0, 0, 0, 255))
            self.renderer.render_present()

            # Query the actual output size from the D3D12 backbuffer
            renderer_size = self.renderer.get_renderer_output_size()
            _dbg("init: renderer_output_size=%s" % (renderer_size,))

            # Use the best available physical size
            # Prefer renderer output size if it looks valid, otherwise use window size
            if renderer_size[0] > 1 and renderer_size[1] > 1:
                self.physical_size = renderer_size
            elif win_size[0] > 1 and win_size[1] > 1:
                self.physical_size = win_size
                _dbg("init: using window size as physical (renderer reported %s)" % (renderer_size,))
            else:
                # On Xbox GDK, the SDL window is a dummy 1x1 object.
                # The D3D12 swapchain handles the real display resolution.
                # Use virtual_size as physical — the renderer will draw in
                # these coordinates and D3D12 presents at native resolution.
                self.physical_size = virtual_size
                _dbg("init: Xbox GDK — all size queries returned (1,1), "
                     "using virtual_size=%s as physical" % (virtual_size,))

            _dbg("init: final physical=%s virtual=%s" % (self.physical_size, self.virtual_size))

            # On Xbox GDK, do NOT call SDL_RenderSetLogicalSize — the
            # window reports (1,1) which makes logical size calculate
            # an absurd scale factor. Instead, render in virtual coords
            # directly and let D3D12 present at native resolution.
            if self.physical_size[0] > 1 and self.physical_size[1] > 1 and \
               self.physical_size != virtual_size:
                self.renderer.set_logical_size(virtual_w, virtual_h)
                _dbg("init: set logical size %dx%d" % (virtual_w, virtual_h))
            else:
                _dbg("init: skipping set_logical_size (physical matches virtual or is 1x1)")

            # Initialize texture cache
            self.texture_cache = TextureCache(self.renderer)

            # Set up coordinate mapping matrices (1:1 — SDL_Renderer handles scaling)
            self.draw_per_virt = 1.0
            self.virt_to_draw = renpy.display.render.Matrix2D(1.0, 0, 0, 1.0)
            self.draw_to_virt = renpy.display.render.Matrix2D(1.0, 0, 0, 1.0)

            # Fill in info dict
            self.info["renderer"] = "sdlrenderer"
            self.info["resizable"] = not xbox
            self.info["additive"] = True
            self.info["models"] = False

            _dbg("init: complete, returning True")

            return True

        except Exception as e:
            _dbg("init: FAILED: %s" % e)
            import traceback
            _dbg(traceback.format_exc())
            return False

    def quit(self):
        """Shut down the renderer."""
        self.kill_textures()
        self.texture_cache = None
        self.renderer = None
        self.window = None

    def resize(self):
        """Handle window resize."""
        if self.renderer is None:
            return

        self.physical_size = self.renderer.get_renderer_output_size()

        if self.virtual_size:
            vw, vh = self.virtual_size
            self.renderer.set_logical_size(vw, vh)

    def update(self, force=False):
        """Check for state changes. Returns True if redraw needed."""
        if force:
            return True
        return False

    def can_block(self):
        """Can we block waiting for input?"""
        return True

    def should_redraw(self, needs_redraw, first_pass, can_block):
        """Determine if we need to redraw the screen."""
        if not needs_redraw:
            return False

        framerate = renpy.config.framerate
        if framerate is None:
            return True

        now = pygame.time.get_ticks()
        frametime = 1000.0 / framerate

        if self.next_frame > now + frametime:
            self.next_frame = now

        if now < self.next_frame and not first_pass:
            return False

        self.next_frame = now + frametime
        return True

    # -------------------------------------------------------------------------
    # Texture management
    # -------------------------------------------------------------------------

    def mutated_surface(self, surf):
        """Mark a surface as changed."""
        if self.texture_cache:
            self.texture_cache.invalidate(surf)

        sid = id(surf)
        cache = self._matrixcolor_cache
        for key in [k for k in list(cache) if k[0] == sid]:
            cache.pop(key, None)

    def load_texture(self, surf, transient=False, properties=None):
        """Upload a surface to a GPU texture. Returns the surface for caching."""
        if properties and properties.get("premultiplied", False):
            self._mark_premultiplied(surf)

        if self.texture_cache is None:
            return surf

        # The image preload thread and decode pool call this too. SDL_Renderer
        # is single-threaded (and on Xbox the D3D12 queue may be suspended for
        # PLM), so only the main thread touches the GPU. Drawing uploads what
        # it needs itself (_get_texture), so skipping here loses nothing.
        if not _on_main_thread():
            return surf

        self.texture_cache.get_or_upload(surf)
        return surf

    def ready_one_texture(self):
        """Upload one queued texture."""
        if self.texture_cache:
            return self.texture_cache.upload_one()
        return False

    def kill_textures(self):
        """Destroy all cached textures."""
        self._matrixcolor_cache.clear()

        targets = [tex for key, tex in self._rt_used]
        for pool in self._rt_free.values():
            targets.extend(tex for tex, last in pool)
        self._rt_used = []
        self._rt_free = {}

        for tex in targets:
            try:
                tex.destroy()
            except Exception:
                pass

        if self.texture_cache:
            self.texture_cache.clear()

    def solid_texture(self, w, h, color):
        """Create a solid color surface/texture."""
        surf = pygame.Surface((w, h), pygame.SRCALPHA, 32)
        surf.fill(color)
        self.load_texture(surf)
        return surf

    def _dump_tree(self, node, depth, max_depth):
        """Log render tree structure for diagnostics."""
        if depth > max_depth:
            return
        indent = "  " * depth
        if isinstance(node, renpy.display.render.Render):
            ops = {0: "BLIT", 1: "DISSOLVE", 2: "IMAGEDISSOLVE", 3: "PIXELLATE", 4: "FLATTEN"}
            op = ops.get(node.operation, str(node.operation))
            fwd = "fwd" if node.forward is not None else "no-fwd"
            _dbg("%sRender op=%s %s nc=%d %.0fx%.0f a=%.2f" % (
                indent, op, fwd, len(node.children),
                node.width, node.height, node.alpha))
            for child, cx, cy, focus, main in node.children:
                self._dump_tree(child, depth + 1, max_depth)
        else:
            try:
                sw, sh = node.get_size()
                _dbg("%sSurface %dx%d" % (indent, sw, sh))
            except Exception:
                _dbg("%s%s" % (indent, type(node).__name__))

    # -------------------------------------------------------------------------
    # Main rendering
    #
    # The render tree is walked the way gl2draw.draw_one walks it: every node
    # carries a 2D affine matrix from its local space to the drawable, a
    # drawable-space clip rectangle, and the merged alpha/over/matrixcolor
    # uniforms. Leaves are emitted as textured quads with SDL_RenderGeometry,
    # which handles rotation, shear and flips the same way GL2 does.
    # -------------------------------------------------------------------------

    def draw_screen(self, surftree, flip=True):
        """Render the scene graph and present."""
        if self.renderer is None:
            _dbg("draw_screen: renderer is None!")
            return

        self._frame_count += 1
        verbose = self._frame_count <= self._dbg_frames

        if verbose:
            _dbg("draw_screen: frame %d" % self._frame_count)
            self._dump_tree(surftree, 0, 3)

        try:
            self._reset_draw_state()
            self.renderer.clear((0, 0, 0, 255))
            self._draw(surftree, _IDENTITY, 1.0, 1.0, None)
            self._reset_draw_state()

            if flip:
                self.renderer.render_present()

            if verbose:
                _dbg("draw_screen: frame %d complete" % self._frame_count)
        except Exception as e:
            _dbg("draw_screen: EXCEPTION frame %d: %s" % (self._frame_count, e))
            import traceback
            _dbg(traceback.format_exc())

            # A failure inside a render target or clip must not leak into the
            # following frames (it would hide everything drawn later).
            self._reset_draw_state()
        finally:
            self._end_frame()

    def _end_frame(self):
        self._release_targets()
        if self.texture_cache is not None:
            self.texture_cache.end_frame()

    def _reset_draw_state(self):
        if self._target_stack:
            self._target_stack = []
            self.renderer.set_render_target(None)
        self._clip = None
        self._applied_clip = _NO_CLIP_APPLIED
        self._z = _Z_IDENTITY
        self.renderer.set_clip_rect(None)

    def _get_texture(self, surf):
        """Get the GPUTexture for a pygame Surface, uploading it on first use.

        Textures persist across frames, so animations only re-issue draw
        calls instead of re-copying and re-uploading every surface.
        """
        if self.texture_cache is None:
            return None

        try:
            return self.texture_cache.get_or_upload(surf)
        except Exception:
            return None

    # Premultiplied surfaces ---------------------------------------------------

    def _mark_premultiplied(self, surf):
        sid = id(surf)
        registry = self._premultiplied

        def forget(ref, sid=sid, registry=registry):
            if registry.get(sid) is ref:
                registry.pop(sid, None)

        try:
            registry[sid] = weakref.ref(surf, forget)
        except TypeError:
            pass

    def _is_premultiplied(self, surf):
        ref = self._premultiplied.get(id(surf))
        return ref is not None and ref() is surf

    # Clipping -------------------------------------------------------------------

    def _clip_render(self, render, m):
        """Intersect the clip with render's (0, 0, w, h) box. False if empty."""
        big = _BIG_PIXELS
        x0 = 0.0 if render.xclipping else -big
        x1 = render.width if render.xclipping else big
        y0 = 0.0 if render.yclipping else -big
        y1 = render.height if render.yclipping else big

        return self._clip_bbox(_bbox(m, x0, y0, x1, y1))

    def _clip_bbox(self, box):
        clip = self._clip
        if clip is not None:
            box = (max(box[0], clip[0]), max(box[1], clip[1]),
                   min(box[2], clip[2]), min(box[3], clip[3]))

        if box[2] - box[0] < 0.01 or box[3] - box[1] < 0.01:
            return False

        self._clip = box
        return True

    def _apply_clip(self):
        clip = self._clip
        if clip is self._applied_clip:
            return

        self._applied_clip = clip

        if clip is None:
            self.renderer.set_clip_rect(None)
            return

        lo, hi = -_BIG_PIXELS, _BIG_PIXELS
        x0 = int(math.floor(max(lo, min(hi, clip[0]))))
        y0 = int(math.floor(max(lo, min(hi, clip[1]))))
        x1 = int(math.ceil(max(lo, min(hi, clip[2]))))
        y1 = int(math.ceil(max(lo, min(hi, clip[3]))))
        self.renderer.set_clip_rect((x0, y0, max(0, x1 - x0), max(0, y1 - y0)))

    # Tree walk ------------------------------------------------------------------

    def _draw(self, what, m, alpha, over, cm):
        """Draw `what` with local-to-drawable matrix `m`."""

        if what is None:
            return

        if not isinstance(what, renpy.display.render.Render):
            self._draw_surface(what, m, alpha, over, cm)
            return

        r = what

        alpha *= r.alpha
        over *= r.over

        if alpha <= _MIN_ALPHA:
            return

        saved_clip = self._clip
        saved_z = self._z

        if (r.xclipping or r.yclipping) and not self._clip_render(r, m):
            self._clip = saved_clip
            return

        try:
            rev = _reverse_matrix(r, type(m) is not tuple)

            if rev is not None and type(rev) is not tuple:
                if type(m) is tuple:
                    m = _to_matrix(m, saved_z)
            else:
                rz = _reverse_z(r)
                if rz is not None:
                    zdz, zdw = saved_z
                    self._z = (zdz * rz[0], zdz * rz[1] + zdw)

            uniforms = r.uniforms
            if uniforms:
                matrixcolor = uniforms.get("u_renpy_matrixcolor", None)
                if matrixcolor is not None:
                    cm = matrixcolor if cm is None else cm * matrixcolor

            op = r.operation

            if op == DISSOLVE:
                self._draw_dissolve(r, m, rev, alpha, over, cm)
            elif op == IMAGEDISSOLVE:
                self._draw_imagedissolve(r, m, rev, alpha, over, cm)
            elif op == PIXELLATE:
                self._draw_pixellate(r, m, rev, alpha, over, cm)
            elif r.shaders and "renpy.mask" in r.shaders and len(r.children) >= 2:
                self._draw_alphamask(r, m, rev, alpha, over, cm)
            else:
                # BLIT, and FLATTEN drawn without an intermediate texture (the
                # only difference is overlap within a translucent subtree).
                self._draw_children(r, m, rev, alpha, over, cm)
        finally:
            self._clip = saved_clip
            self._z = saved_z

    def _draw_children(self, r, m, rev, alpha, over, cm):
        for child, cx, cy, focus, main in r.children:
            self._draw(child, _child_matrix(m, cx, cy, rev), alpha, over, cm)

    def _draw_surface(self, surf, m, alpha, over, cm):
        """Draw a leaf pygame Surface as a quad through `m`."""

        try:
            w, h = surf.get_size()
        except Exception:
            return

        if w <= 0 or h <= 0:
            return

        # Outside GL2's depth range. (In 3D mode z is checked per vertex.)
        if type(m) is tuple and not -1.0 <= self._z[1] <= 1.0:
            return

        box = _bbox(m, 0.0, 0.0, w, h)
        clip = self._clip
        if clip is not None and (box[2] <= clip[0] or box[0] >= clip[2] or
                                 box[3] <= clip[1] or box[1] >= clip[3]):
            return

        premultiplied = self._is_premultiplied(surf)
        tint = None

        if cm is not None:
            kind, value = _classify_matrixcolor(cm)
            if kind == "tint":
                tint = value
            elif kind == "general":
                surf = self._matrixcolor_surface(surf, value, premultiplied)

        tex = self._get_texture(surf)
        if tex is None:
            return

        # Text is laid out on the pixel grid; keep it crisp the way GL2's
        # pixel_perfect does.
        if premultiplied and type(m) is tuple and m[0] == 1.0 and m[4] == 1.0 and m[1] == 0.0 and m[3] == 0.0:
            m = (1.0, 0.0, float(round(m[2])), 0.0, 1.0, float(round(m[5])))

        self._draw_quad(tex, m, w, h, alpha, over, premultiplied, tint)

    def _draw_quad(self, tex, m, w, h, alpha, over, premultiplied, tint=None):
        """Draw a texture covering local (0, 0, w, h) with Ren'Py alpha/over."""

        tr, tg, tb = tint if tint is not None else (1.0, 1.0, 1.0)

        if premultiplied:
            # out = tex * (alpha, alpha, alpha, alpha*over), blended (ONE, 1-SA)
            tex.set_blend_mode(self.BLEND_PREMULTIPLIED)
            self._emit_quad(tex, m, w, h, (alpha * tr, alpha * tg, alpha * tb, alpha * over))
            return

        if over >= 0.999:
            tex.set_blend_mode(BLENDMODE_BLEND)
            self._emit_quad(tex, m, w, h, (tr, tg, tb, alpha))
        elif over <= 0.001:
            tex.set_blend_mode(BLENDMODE_ADD)
            self._emit_quad(tex, m, w, h, (tr, tg, tb, alpha))
        else:
            # Straight alpha: rgb*a*alpha + dst*(1 - a*alpha*over), split into
            # a normal pass and an additive pass.
            tex.set_blend_mode(BLENDMODE_BLEND)
            self._emit_quad(tex, m, w, h, (tr, tg, tb, alpha * over))
            tex.set_blend_mode(BLENDMODE_ADD)
            self._emit_quad(tex, m, w, h, (tr, tg, tb, alpha * (1.0 - over)))

    def _emit_quad(self, tex, m, w, h, color):
        self._apply_clip()

        r = _color_byte(color[0])
        g = _color_byte(color[1])
        b = _color_byte(color[2])
        a = _color_byte(color[3])

        if a == 0 and r == 0 and g == 0 and b == 0:
            return

        if type(m) is not tuple:
            self._emit_projected_quad(tex, m, w, h, (r, g, b, a))
            return

        x0 = m[2]
        y0 = m[5]
        ax = m[0] * w
        ay = m[3] * w
        bx = m[1] * h
        by = m[4] * h

        self.renderer.render_geometry(tex, [
            (x0, y0, r, g, b, a, 0.0, 0.0),
            (x0 + ax, y0 + ay, r, g, b, a, 1.0, 0.0),
            (x0 + ax + bx, y0 + ay + by, r, g, b, a, 1.0, 1.0),
            (x0 + bx, y0 + by, r, g, b, a, 0.0, 1.0),
        ], _QUAD_INDICES)

    def _emit_projected_quad(self, tex, m, w, h, rgba):
        """
        Draw local (0, 0, w, h) through a 4x4 (possibly perspective) Matrix.

        SDL interpolates texture coordinates linearly in screen space, which is
        exact while w is constant over the quad (anything facing the camera).
        Otherwise the quad is split into a grid so the error stays small.
        """
        r, g, b, a = rgba

        corners = [_project(m, x, y) for x, y in ((0.0, 0.0), (w, 0.0), (w, h), (0.0, h))]

        # Behind the eye, or entirely outside GL2's depth range.
        if None in corners:
            return
        if all(p[2] > 1.0 for p in corners) or all(p[2] < -1.0 for p in corners):
            return

        ws = [p[3] for p in corners]
        if max(ws) - min(ws) <= 0.005 * max(ws):
            self.renderer.render_geometry(tex, [
                (corners[0][0], corners[0][1], r, g, b, a, 0.0, 0.0),
                (corners[1][0], corners[1][1], r, g, b, a, 1.0, 0.0),
                (corners[2][0], corners[2][1], r, g, b, a, 1.0, 1.0),
                (corners[3][0], corners[3][1], r, g, b, a, 0.0, 1.0),
            ], _QUAD_INDICES)
            return

        n = self.PROJECTED_GRID
        vertices = [ ]
        for j in range(n + 1):
            v = float(j) / n
            for i in range(n + 1):
                u = float(i) / n
                p = _project(m, u * w, v * h)
                if p is None:
                    return
                vertices.append((p[0], p[1], r, g, b, a, u, v))

        indices = self._grid_indices.get(n)
        if indices is None:
            indices = [ ]
            for j in range(n):
                for i in range(n):
                    k = j * (n + 1) + i
                    indices.extend((k, k + 1, k + n + 2, k, k + n + 2, k + n + 1))
            self._grid_indices[n] = indices

        self.renderer.render_geometry(tex, vertices, indices)

    # matrixcolor ------------------------------------------------------------------

    def _matrixcolor_surface(self, surf, rows, premultiplied):
        """
        Returns a copy of `surf` with the 4x4 color matrix `rows` applied,
        matching GL2's renpy.matrixcolor (which works on premultiplied color).
        Results are cached per (surface, matrix).
        """

        key = (id(surf), rows, premultiplied)
        cache = self._matrixcolor_cache

        entry = cache.get(key)
        if entry is not None and entry[0] is surf:
            cache.move_to_end(key)
            return entry[1]

        w, h = surf.get_size()

        try:
            src = pygame.Surface((w, h), pygame.SRCALPHA, 32)
            renpy.display.accelerator.nogil_copy(surf, src)
            dst = pygame.Surface((w, h), pygame.SRCALPHA, 32)
            renpy.display.module.colormatrix(src, dst, _straight_colormatrix(rows, premultiplied))
        except Exception as e:
            _dbg("matrixcolor: failed: %s" % e)
            return surf

        if premultiplied:
            self._mark_premultiplied(dst)

        cache[key] = (surf, dst)
        while len(cache) > self.MATRIXCOLOR_CACHE_SIZE:
            cache.popitem(last=False)

        return dst

    # Render targets -------------------------------------------------------------------

    def _acquire_target(self, w, h):
        key = (w, h)
        free = self._rt_free.get(key)

        tex = None
        while free and tex is None:
            tex = free.pop()[0]
            if not tex.valid:
                tex = None

        if tex is None:
            tex = self.renderer.create_target_texture(w, h)
            tex.set_blend_mode(self.BLEND_PREMULTIPLIED)

        # Pixellate leaves its target on nearest sampling.
        tex.set_scale_mode(SCALEMODE_LINEAR)

        self._rt_used.append((key, tex))
        return tex

    def _release_targets(self):
        frame = self._frame_count

        for key, tex in self._rt_used:
            self._rt_free.setdefault(key, []).append((tex, frame))
        self._rt_used = []

        retired = self.texture_cache.retired if self.texture_cache is not None else None

        for key in list(self._rt_free):
            keep = []
            for tex, last in self._rt_free[key]:
                if frame - last > self.TARGET_MAX_AGE:
                    if retired is not None:
                        retired.append(tex)
                else:
                    keep.append((tex, last))
            if keep:
                self._rt_free[key] = keep
            else:
                del self._rt_free[key]

    def _push_target(self, w, h):
        """Make a transparent w x h render target current. Returns it."""
        tex = self._acquire_target(w, h)

        previous = self._target_stack[-1][0] if self._target_stack else None
        self._target_stack.append((tex, previous, self._clip, self._z))

        self.renderer.set_render_target(tex)
        self._clip = None
        self._applied_clip = _NO_CLIP_APPLIED
        self._z = _Z_IDENTITY
        self.renderer.set_clip_rect(None)

        self.renderer.set_draw_blend_mode(BLENDMODE_NONE)
        self.renderer.clear((0, 0, 0, 0))
        self.renderer.fill_rect((0, 0, 0, 0), (0, 0, w, h))
        self.renderer.set_draw_blend_mode(BLENDMODE_BLEND)

        return tex

    def _pop_target(self):
        tex, previous, clip, z = self._target_stack.pop()
        self.renderer.set_render_target(previous)
        self._clip = clip
        self._z = z
        self._applied_clip = _NO_CLIP_APPLIED
        return tex

    def _target_scale(self, m, w, h):
        """Texture size and scale for rendering a w x h subtree drawn by m."""
        if type(m) is tuple:
            sx = math.hypot(m[0], m[3])
            sy = math.hypot(m[1], m[4])
        else:
            box = _bbox(m, 0.0, 0.0, w, h)
            sx = (box[2] - box[0]) / max(w, 1e-6)
            sy = (box[3] - box[1]) / max(h, 1e-6)

        sx = max(0.05, min(4.0, sx))
        sy = max(0.05, min(4.0, sy))

        tw = int(math.ceil(w * sx))
        th = int(math.ceil(h * sy))

        limit = self.MAX_TARGET_SIZE
        if tw > limit:
            sx *= float(limit) / tw
            tw = limit
        if th > limit:
            sy *= float(limit) / th
            th = limit

        return max(1, tw), max(1, th), sx, sy

    def _render_children_to_target(self, r, children, rev, sx, sy, tw, th, cm=None):
        """Draw (child, cx, cy) children of r into a fresh tw x th target."""
        tex = self._push_target(tw, th)
        try:
            base = (sx, 0.0, 0.0, 0.0, sy, 0.0)
            for child, cx, cy in children:
                self._draw(child, _child_matrix(base, cx, cy, rev), 1.0, 1.0, cm)
        finally:
            self._pop_target()
        return tex

    # Special operations -------------------------------------------------------------

    def _draw_alphamask(self, r, m, rev, alpha, over, cm):
        """
        AlphaMask (shader renpy.mask): child color times mask alpha.

        The child is drawn into a target, the mask into a second one, and the
        mask target is multiplied into the child target with a (ZERO, SRC_ALPHA)
        blend. The result is premultiplied and is drawn through `m`.
        """

        w, h = r.width, r.height
        if w <= 0 or h <= 0:
            return

        tw, th, sx, sy = self._target_scale(m, w, h)

        child, ccx, ccy = r.children[0][:3]
        mask, mcx, mcy = r.children[1][:3]

        uniforms = r.uniforms or {}
        inverted = uniforms.get("u_renpy_mask_multiplier", 1) < 0

        try:
            mask_tex = self._render_children_to_target(r, [(mask, mcx, mcy)], rev, sx, sy, tw, th)
            child_tex = self._push_target(tw, th)
            try:
                base = (sx, 0.0, 0.0, 0.0, sy, 0.0)
                self._draw(child, _child_matrix(base, ccx, ccy, rev), 1.0, 1.0, cm)

                mask_tex.set_blend_mode(self.BLEND_MASK_INVERTED if inverted else self.BLEND_MASK)
                self._emit_quad(mask_tex, _IDENTITY, tw, th, (1.0, 1.0, 1.0, 1.0))
            finally:
                self._pop_target()
        except Exception as e:
            _dbg("alphamask: render target failed (%s); drawing unmasked" % e)
            self._draw_children(r, m, rev, alpha, over, cm)
            return

        self._draw_quad(child_tex, _mul_scale(m, 1.0 / sx, 1.0 / sy),
                        tw, th, alpha, over, True)

    def _draw_dissolve(self, r, m, rev, alpha, over, cm):
        """Cross-fade: bottom at full alpha, then top at complete * alpha.

        Drawn straight to the current target (no intermediate textures); for
        an opaque bottom this is screen = top*t + bottom*(1-t).
        """
        children = r.children
        if len(children) < 2:
            return

        # Without models, Dissolve doesn't crop its children to its size.
        if not self._clip_bbox(_bbox(m, 0.0, 0.0, r.width, r.height)):
            return

        complete = max(0.0, min(1.0, r.operation_complete))

        bottom, bx, by = children[0][:3]
        top, tx, ty = children[1][:3]

        self._draw(bottom, _child_matrix(m, bx, by, rev), alpha, over, cm)

        top_alpha = alpha * complete
        if top_alpha > _MIN_ALPHA:
            self._draw(top, _child_matrix(m, tx, ty, rev), top_alpha, over, cm)

    def _draw_imagedissolve(self, r, m, rev, alpha, over, cm):
        """Image-dissolve using a mask. CPU fallback via _renpy.imageblend."""
        children = r.children
        if len(children) < 3:
            return

        complete = r.operation_complete
        param = r.operation_parameter

        w = max(1, int(math.ceil(r.width)))
        h = max(1, int(math.ceil(r.height)))

        surfs = [ ]
        for child, cx, cy, focus, main in children[:3]:
            surfs.append(self._render_to_surface_at(child, _child_matrix(_IDENTITY, cx, cy, rev), w, h, cm))

        bottom_surf, top_surf, mask_surf = surfs
        if bottom_surf is None or top_surf is None or mask_surf is None:
            return

        # Build the alpha ramp
        step = max(1, int(256 * param))
        position = int(complete * (256 + step))

        ramp = bytearray(256)
        for i in range(256):
            if i < position - step:
                ramp[i] = 255
            elif i >= position:
                ramp[i] = 0
            else:
                v = 255 - int(255 * (i - (position - step)) / step)
                ramp[i] = max(0, min(255, v))
        ramp = bytes(ramp)

        dst_surf = pygame.Surface((w, h), pygame.SRCALPHA, 32)
        try:
            renpy.display.module.imageblend(bottom_surf, top_surf, dst_surf, mask_surf, ramp)
        except Exception:
            dst_surf = bottom_surf if complete < 0.5 else top_surf

        # The inputs were read back from transparent render targets, so the
        # blend is premultiplied too.
        self._mark_premultiplied(dst_surf)
        tex = self._get_texture(dst_surf)
        if tex is not None:
            self._draw_quad(tex, m, w, h, alpha, over, True)

    def _draw_pixellate(self, r, m, rev, alpha, over, cm):
        """Pixellate: render small, then upscale with nearest-neighbor."""
        children = r.children
        if not children:
            return

        child, cx, cy = children[0][:3]
        param = max(1, int(r.operation_parameter))

        w = max(1, int(math.ceil(r.width)))
        h = max(1, int(math.ceil(r.height)))

        small_w = max(1, w // param)
        small_h = max(1, h // param)

        try:
            small = self._render_children_to_target(
                r, [(child, cx, cy)], rev,
                float(small_w) / w, float(small_h) / h, small_w, small_h, cm)
        except Exception as e:
            _dbg("pixellate: render target failed (%s); drawing unpixellated" % e)
            self._draw_children(r, m, rev, alpha, over, cm)
            return

        small.set_scale_mode(SCALEMODE_NEAREST)
        self._draw_quad(small, _mul_scale(m, float(w) / small_w, float(h) / small_h),
                        small_w, small_h, alpha, over, True)

    # Read-back helpers -------------------------------------------------------------

    def _render_to_surface_at(self, what, m, w, h, cm=None):
        """
        Draw `what` with matrix `m` into a transparent w x h target and read
        it back. The returned Surface holds premultiplied color.
        """
        if w <= 0 or h <= 0:
            return None

        try:
            self._push_target(w, h)
        except Exception:
            return None

        try:
            self._draw(what, m, 1.0, 1.0, cm)
            surf = self.renderer.render_read_pixels()
        except Exception as e:
            _dbg("_render_to_surface: failed: %s" % e)
            surf = None
        finally:
            tex = self._pop_target()

        # Nothing samples the target after the read-back, so it can be reused.
        for i, (key, used) in enumerate(self._rt_used):
            if used is tex:
                del self._rt_used[i]
                self._rt_free.setdefault(key, []).append((tex, self._frame_count))
                break

        if surf is not None:
            self._mark_premultiplied(surf)

        return surf

    def _render_to_surface(self, render, w, h, alpha=True):
        """Render a render tree to a pygame Surface (for CPU operations)."""
        return self._render_to_surface_at(render, _IDENTITY, w, h)

    # -------------------------------------------------------------------------
    # Compositing to texture (public interface)
    # -------------------------------------------------------------------------

    def render_to_texture(self, what, alpha):
        """Render a render tree to a surface (for Ren'Py's cache)."""
        if not isinstance(what, renpy.display.render.Render):
            return what

        w = max(1, int(math.ceil(what.width)))
        h = max(1, int(math.ceil(what.height)))
        if w <= 0 or h <= 0:
            return pygame.Surface((1, 1), pygame.SRCALPHA, 32)

        surf = self._render_to_surface(what, w, h, alpha)
        if surf is None:
            surf = pygame.Surface((w, h), pygame.SRCALPHA, 32)

        return surf

    def is_pixel_opaque(self, what, x, y):
        """Check if a pixel is opaque by rendering to surface and checking."""
        if not isinstance(what, renpy.display.render.Render):
            return True

        # Use the existing cached surface if available, but do NOT cache
        # new surfaces from render_to_texture on the Render object: the
        # renderer always draws from the render tree, and a stale snapshot
        # stashed on render.surface would never be refreshed.
        surf = what.surface
        if surf is None:
            surf = self.render_to_texture(what, True)
            # Intentionally NOT setting what.surface = surf

        if surf is None:
            return True

        x = int(x)
        y = int(y)

        if x < 0 or y < 0:
            return False

        try:
            sw, sh = surf.get_size()
        except Exception:
            return True

        if x >= sw or y >= sh:
            return False

        try:
            color = surf.get_at((x, y))
            return color[3] > 0
        except Exception:
            return True

    # -------------------------------------------------------------------------
    # Coordinate mapping
    # -------------------------------------------------------------------------

    def translate_point(self, x, y):
        """Physical → virtual coordinates."""
        if self.virtual_size is None or self.physical_size is None:
            return (x, y)

        vw, vh = self.virtual_size
        pw, ph = self.physical_size

        if pw == 0 or ph == 0:
            return (x, y)

        # Account for letterboxing
        physical_aspect = float(pw) / float(ph)
        virtual_aspect = float(vw) / float(vh)

        if physical_aspect > virtual_aspect:
            # Pillarboxed
            scale = float(ph) / float(vh)
            offset_x = (pw - vw * scale) / 2.0
            rx = (x - offset_x) / scale
            ry = y / scale
        else:
            # Letterboxed
            scale = float(pw) / float(vw)
            offset_y = (ph - vh * scale) / 2.0
            rx = x / scale
            ry = (y - offset_y) / scale

        return (rx, ry)

    def untranslate_point(self, x, y):
        """Virtual → physical coordinates."""
        if self.virtual_size is None or self.physical_size is None:
            return (x, y)

        vw, vh = self.virtual_size
        pw, ph = self.physical_size

        if pw == 0 or ph == 0:
            return (x, y)

        physical_aspect = float(pw) / float(ph)
        virtual_aspect = float(vw) / float(vh)

        if physical_aspect > virtual_aspect:
            scale = float(ph) / float(vh)
            offset_x = (pw - vw * scale) / 2.0
            rx = x * scale + offset_x
            ry = y * scale
        else:
            scale = float(pw) / float(vw)
            offset_y = (ph - vh * scale) / 2.0
            rx = x * scale
            ry = y * scale + offset_y

        return (rx, ry)

    def mouse_event(self, ev):
        """Translate a mouse event from physical to virtual coords. Returns (x, y)."""
        x, y = getattr(ev, 'pos', (0, 0))
        return self.translate_point(x, y)

    def get_mouse_pos(self):
        """Get mouse position in virtual coordinates."""
        x, y = pygame.mouse.get_pos()
        return self.translate_point(x, y)

    def set_mouse_pos(self, x, y):
        """Set mouse position from virtual coordinates."""
        x, y = self.untranslate_point(x, y)
        pygame.mouse.set_pos([x, y])

    # -------------------------------------------------------------------------
    # Screenshot
    # -------------------------------------------------------------------------

    def screenshot(self, surftree):
        """Take a screenshot."""
        if self.renderer is None:
            return pygame.Surface((1, 1), pygame.SRCALPHA, 32)

        # Render the tree without presenting
        self.draw_screen(surftree, flip=False)

        try:
            surf = self.renderer.render_read_pixels()
            return surf
        except Exception:
            return pygame.Surface((1, 1), pygame.SRCALPHA, 32)

    # -------------------------------------------------------------------------
    # Misc
    # -------------------------------------------------------------------------

    def event_peek_sleep(self):
        """Sleep briefly to avoid busy-waiting."""
        pass

    def get_physical_size(self):
        """Return physical window size."""
        if self.physical_size:
            return self.physical_size
        if self.renderer:
            return self.renderer.get_renderer_output_size()
        return self.virtual_size or (1, 1)

    def get_texture_size(self):
        """Return total texture memory and count."""
        if self.texture_cache:
            return (self.texture_cache.bytes, len(self.texture_cache.entries))
        return (0, 0)
