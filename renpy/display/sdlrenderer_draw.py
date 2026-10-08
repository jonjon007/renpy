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


def _clear_target(renderer, w, h):
    """Clear the current render target to opaque black."""
    renderer.set_draw_blend_mode(BLENDMODE_NONE)
    renderer.clear((0, 0, 0, 255))
    renderer.fill_rect((0, 0, 0, 255), (0, 0, w, h))
    renderer.set_draw_blend_mode(BLENDMODE_BLEND)


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

        # Nesting depth for clip rects (we restore by popping)
        self._clip_stack = []

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
        if self.texture_cache:
            self.texture_cache.clear()
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

    def load_texture(self, surf, transient=False, properties=None):
        """Upload a surface to a GPU texture. Returns the surface for caching."""
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
            # Clear screen
            self.renderer.clear((0, 0, 0, 255))

            # Walk the render tree
            self._draw_render(surftree, 0.0, 0.0, 1.0, None)

            # Present
            if flip:
                self.renderer.render_present()

            if verbose:
                _dbg("draw_screen: frame %d complete" % self._frame_count)
        except Exception as e:
            _dbg("draw_screen: EXCEPTION frame %d: %s" % (self._frame_count, e))
            import traceback
            _dbg(traceback.format_exc())
        finally:
            if self.texture_cache is not None:
                self.texture_cache.end_frame()

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

    def _draw_render(self, render, x, y, alpha, clip):
        """Recursively draw a Render tree node."""

        if render is None:
            return

        # Handle special operations
        if render.operation != BLIT:
            self._draw_special(render, x, y, alpha)
            return

        # Always skip render.surface and traverse children instead.
        # On Xbox D3D12, render.surface gets set to render target read-back
        # surfaces (via Render.render_to_texture) which have corrupted alpha.
        # Our per-frame texture cache creates correct textures from the
        # original source surfaces found in the render tree children.

        # Set up clipping
        pushed_clip = False
        if render.xclipping or render.yclipping:
            cw = render.width if render.xclipping else 32767
            ch = render.height if render.yclipping else 32767
            clip_rect = (int(x), int(y), int(cw), int(ch))
            self._push_clip(clip_rect)
            pushed_clip = True

        # Draw children back to front
        effective_alpha = alpha * render.alpha

        for child, cx, cy, focus, main in render.children:
            child_x = x + cx
            child_y = y + cy

            if isinstance(child, renpy.display.render.Render):
                # Check for transforms
                if child.forward is not None:
                    self._draw_transformed(child, child_x, child_y, effective_alpha)
                else:
                    self._draw_render(child, child_x, child_y, effective_alpha, clip)
            else:
                # It's a pygame Surface
                tex = self._get_texture(child)
                if tex is not None:
                    a_byte = max(0, min(255, int(effective_alpha * 255)))
                    tex.set_alpha_mod(a_byte)
                    tex.set_blend_mode(BLENDMODE_BLEND)
                    self.renderer.render_copy_f(
                        tex, None,
                        (child_x, child_y, float(tex.w), float(tex.h))
                    )

        if pushed_clip:
            self._pop_clip()

    def _is_identity(self, matrix):
        """Check if a Matrix2D is close to identity."""
        return (abs(matrix.xdx - 1.0) < 0.001 and abs(matrix.ydy - 1.0) < 0.001
                and abs(matrix.xdy) < 0.001 and abs(matrix.ydx) < 0.001)

    def _is_scale_only(self, matrix):
        """Check if a Matrix2D is scale-only (no rotation/shear)."""
        return abs(matrix.xdy) < 0.001 and abs(matrix.ydx) < 0.001

    def _draw_transformed(self, render, x, y, alpha):
        """Draw a render with an affine transform.

        For identity transforms, just recurse normally (no render target needed).
        For scale-only, propagate scale to children's destination rects.
        For rotation/shear, render to texture then use geometry.
        """

        matrix = render.forward
        if matrix is None or self._is_identity(matrix):
            # Identity — no visual transform needed, just recurse
            self._draw_render(render, x, y, alpha, None)
            return

        # Special operations (DISSOLVE, FLATTEN, etc.) must go through
        # _draw_render → _draw_special to be handled correctly. Transforms
        # on these renders are for coordinate mapping, not visual scaling.
        if render.operation != BLIT:
            self._draw_render(render, x, y, alpha, None)
            return

        reverse = render.reverse
        effective_alpha = alpha * render.alpha

        if self._is_scale_only(matrix):
            # Scale only — draw children with scaled positions and sizes.
            # reverse.xdx/ydy are the scale factors (child→parent)
            sx = reverse.xdx if reverse else 1.0
            sy = reverse.ydy if reverse else 1.0
            self._draw_render_scaled(render, x, y, effective_alpha, sx, sy)
            return

        # Complex transform (rotation/shear) — use SDL_RenderCopyExF or fallback
        self._draw_transformed_rtt(render, x, y, alpha)

    def _draw_render_scaled(self, render, x, y, alpha, sx, sy):
        """Draw a render's children with a uniform scale applied to positions and sizes."""

        # Special operations (DISSOLVE, FLATTEN, etc.) must be handled by
        # _draw_render → _draw_special, not by iterating children directly.
        if render.operation != BLIT:
            self._draw_render(render, x, y, alpha, None)
            return

        # Set up clipping (in parent space)
        pushed_clip = False
        if render.xclipping or render.yclipping:
            cw = render.width if render.xclipping else 32767
            ch = render.height if render.yclipping else 32767
            clip_rect = (int(x), int(y), int(cw), int(ch))
            self._push_clip(clip_rect)
            pushed_clip = True

        for child, cx, cy, focus, main in render.children:
            # Transform child position from child space to parent space
            child_x = x + cx * sx
            child_y = y + cy * sy

            if isinstance(child, renpy.display.render.Render):
                if child.operation != BLIT:
                    # Special operation on child — delegate to _draw_render
                    self._draw_render(child, child_x, child_y, alpha, None)
                elif child.forward is not None and not self._is_identity(child.forward):
                    # Nested transform
                    self._draw_transformed(child, child_x, child_y, alpha)
                else:
                    # Recurse into children, propagating scale.
                    # Do NOT use child.surface — it may contain RTT readback
                    # data with corrupted alpha (D3D12 checkerboard artifacts).
                    self._draw_render_scaled(child, child_x, child_y,
                                            alpha * child.alpha, sx, sy)
            else:
                # pygame Surface
                tex = self._get_texture(child)
                if tex is not None:
                    a_byte = max(0, min(255, int(alpha * 255)))
                    tex.set_alpha_mod(a_byte)
                    tex.set_blend_mode(BLENDMODE_BLEND)
                    self.renderer.render_copy_f(
                        tex, None,
                        (child_x, child_y,
                         float(tex.w) * sx, float(tex.h) * sy)
                    )

        if pushed_clip:
            self._pop_clip()

    def _draw_transformed_rtt(self, render, x, y, alpha):
        """Draw a render with complex transform (rotation/shear).

        Prefers SDL_RenderCopyExF for single-surface children (avoids
        render targets which have alpha-clear issues on Xbox D3D12).
        Falls back to render-to-texture + geometry for multi-child subtrees.
        """

        matrix = render.forward
        if matrix is None:
            self._draw_render(render, x, y, alpha, None)
            return

        effective_alpha = alpha * render.alpha
        a_byte = max(0, min(255, int(effective_alpha * 255)))

        pw = float(render.width)
        ph = float(render.height)

        # Extract rotation angle from the forward matrix.
        # forward: xdx=cos(θ) xdy=sin(θ) ydx=-sin(θ) ydy=cos(θ)
        # SDL_RenderCopyExF expects degrees, clockwise.
        angle_rad = math.atan2(matrix.xdy, matrix.xdx)
        angle_deg = math.degrees(angle_rad)

        # Try direct rotation for single-child renders (no RTT needed)
        children = render.children
        if len(children) == 1:
            child, cx, cy, focus, main = children[0]

            # Get the texture to rotate — only use actual leaf surfaces,
            # NOT child.surface which may be corrupted RTT readback data.
            tex = None
            if isinstance(child, renpy.display.render.Render):
                # Skip child.surface — traverse children to find leaf textures
                if len(child.children) == 1:
                    gc = child.children[0][0]
                    if not isinstance(gc, renpy.display.render.Render):
                        tex = self._get_texture(gc)
                    elif len(gc.children) == 1:
                        ggc = gc.children[0][0]
                        if not isinstance(ggc, renpy.display.render.Render):
                            tex = self._get_texture(ggc)
            else:
                tex = self._get_texture(child)

            if tex is not None:
                tex.set_alpha_mod(a_byte)
                if render.over < 1.0:
                    tex.set_blend_mode(BLENDMODE_ADD)
                else:
                    tex.set_blend_mode(BLENDMODE_BLEND)

                # Destination rect centers the texture at the render position
                dst = (x + cx, y + cy, float(tex.w), float(tex.h))
                # Rotate around the center of the destination rect
                center = (float(tex.w) / 2.0, float(tex.h) / 2.0)

                self.renderer.render_copy_ex_f(
                    tex, None, dst, angle_deg, center, 0)
                return

        # Fallback: multi-child or couldn't get texture — draw children
        # directly with combined alpha (accepting slight visual difference
        # vs true composited rotation, but avoids RTT checkerboard)
        for child, cx, cy, focus, main in children:
            if isinstance(child, renpy.display.render.Render):
                self._draw_render(child, x + cx, y + cy, effective_alpha, None)
            else:
                tex = self._get_texture(child)
                if tex is not None:
                    tex.set_alpha_mod(a_byte)
                    tex.set_blend_mode(BLENDMODE_BLEND)
                    self.renderer.render_copy_f(
                        tex, None,
                        (x + cx, y + cy, float(tex.w), float(tex.h))
                    )

    def _render_subtree_to_texture(self, render):
        """Render a subtree into a GPU texture via render target."""

        w = max(1, int(math.ceil(render.width)))
        h = max(1, int(math.ceil(render.height)))
        _dbg("_render_subtree_to_texture: %dx%d" % (w, h))

        if w <= 0 or h <= 0:
            return None

        try:
            target = self.renderer.create_target_texture(w, h)
        except Exception:
            return None

        # Save current target, render subtree, restore
        self.renderer.set_render_target(target)
        _clear_target(self.renderer, w, h)

        # Draw children into the target at (0,0)
        for child, cx, cy, focus, main in render.children:
            if isinstance(child, renpy.display.render.Render):
                self._draw_render(child, cx, cy, 1.0, None)
            else:
                tex = self._get_texture(child)
                if tex is not None:
                    tex.set_alpha_mod(255)
                    tex.set_blend_mode(BLENDMODE_BLEND)
                    self.renderer.render_copy_f(
                        tex, None,
                        (cx, cy, float(tex.w), float(tex.h))
                    )

        self.renderer.set_render_target(None)
        return target

    # -------------------------------------------------------------------------
    # Special effects
    # -------------------------------------------------------------------------

    def _draw_special(self, render, x, y, alpha):
        """Handle DISSOLVE, IMAGEDISSOLVE, PIXELLATE, FLATTEN."""

        op = render.operation
        children = render.children

        if op == DISSOLVE:
            self._draw_dissolve(render, x, y, alpha)
        elif op == IMAGEDISSOLVE:
            self._draw_imagedissolve(render, x, y, alpha)
        elif op == PIXELLATE:
            self._draw_pixellate(render, x, y, alpha)
        elif op == FLATTEN:
            self._draw_flatten(render, x, y, alpha)
        else:
            # Unknown op — try to draw children normally
            for child, cx, cy, focus, main in children:
                if isinstance(child, renpy.display.render.Render):
                    self._draw_render(child, x + cx, y + cy, alpha, None)

    def _draw_dissolve(self, render, x, y, alpha):
        """Cross-fade between bottom and top children.

        Draws directly to the backbuffer without render targets to avoid
        D3D12 checkerboard artifacts. Uses correct alpha math:
        bottom at full alpha, top at complete*alpha, producing
        screen = top*t + bottom*(1-t) for opaque sources.
        """
        children = render.children
        if len(children) < 2:
            return

        complete = render.operation_complete
        effective_alpha = alpha * render.alpha

        bottom_render = children[0][0]
        top_render = children[1][0]

        if self._frame_count <= self._dbg_frames:
            bottom_op = getattr(bottom_render, 'operation', -1) if isinstance(bottom_render, renpy.display.render.Render) else 'surf'
            bottom_nc = len(getattr(bottom_render, 'children', [])) if isinstance(bottom_render, renpy.display.render.Render) else 0
            top_op = getattr(top_render, 'operation', -1) if isinstance(top_render, renpy.display.render.Render) else 'surf'
            top_nc = len(getattr(top_render, 'children', [])) if isinstance(top_render, renpy.display.render.Render) else 0
            _dbg("dissolve: frame=%d complete=%.3f eff_alpha=%.3f "
                 "bottom(op=%s,nc=%d) top(op=%s,nc=%d)" % (
                     self._frame_count, complete, effective_alpha,
                     bottom_op, bottom_nc, top_op, top_nc))

        # Draw bottom scene at full effective alpha
        if isinstance(bottom_render, renpy.display.render.Render):
            self._draw_transformed(bottom_render, x, y, effective_alpha)
        else:
            tex = self._get_texture(bottom_render)
            if tex is not None:
                a = max(0, min(255, int(effective_alpha * 255)))
                tex.set_alpha_mod(a)
                tex.set_blend_mode(BLENDMODE_BLEND)
                self.renderer.render_copy_f(
                    tex, None,
                    (x, y, float(tex.w), float(tex.h))
                )

        # Draw top scene at complete * alpha
        top_alpha = complete * effective_alpha
        if top_alpha > 0.001:
            if isinstance(top_render, renpy.display.render.Render):
                self._draw_transformed(top_render, x, y, top_alpha)
            else:
                tex = self._get_texture(top_render)
                if tex is not None:
                    a = max(0, min(255, int(top_alpha * 255)))
                    tex.set_alpha_mod(a)
                    tex.set_blend_mode(BLENDMODE_BLEND)
                    self.renderer.render_copy_f(
                        tex, None,
                        (x, y, float(tex.w), float(tex.h))
                    )

    def _draw_imagedissolve(self, render, x, y, alpha):
        """Image-dissolve using a mask. CPU fallback via _renpy.imageblend."""
        children = render.children
        if len(children) < 3:
            return

        complete = render.operation_complete
        param = render.operation_parameter

        bottom_render = children[0][0]
        top_render = children[1][0]
        mask_render = children[2][0]

        w = max(1, int(math.ceil(render.width)))
        h = max(1, int(math.ceil(render.height)))
        if w <= 0 or h <= 0:
            return

        # Render all three to surfaces for CPU processing
        bottom_surf = self._render_to_surface(bottom_render, w, h, True)
        top_surf = self._render_to_surface(top_render, w, h, True)
        mask_surf = self._render_to_surface(mask_render, w, h, True)

        if bottom_surf is None or top_surf is None or mask_surf is None:
            return

        # Build the alpha ramp
        step = max(1, int(256 * param))
        position = int(complete * (256 + step))

        ramp = b""
        for i in range(256):
            if i < position - step:
                ramp += b"\xff"
            elif i >= position:
                ramp += b"\x00"
            else:
                v = 255 - int(255 * (i - (position - step)) / step)
                ramp += bytes([max(0, min(255, v))])

        # Use _renpy.imageblend for CPU-based per-pixel mask blending
        dst_surf = pygame.Surface((w, h), pygame.SRCALPHA, 32)
        try:
            renpy.display.module.imageblend(
                bottom_surf, top_surf, dst_surf, mask_surf, ramp)
        except Exception:
            # Fallback: just draw based on completion
            if complete < 0.5:
                dst_surf = bottom_surf
            else:
                dst_surf = top_surf

        # Upload and draw the result
        tex = self.renderer.create_texture_from_surface(dst_surf)
        tex.set_blend_mode(BLENDMODE_BLEND)
        effective_alpha = alpha * render.alpha
        a_byte = max(0, min(255, int(effective_alpha * 255)))
        tex.set_alpha_mod(a_byte)
        self.renderer.render_copy_f(
            tex, None,
            (x, y, float(w), float(h))
        )
        tex.destroy()

    def _draw_pixellate(self, render, x, y, alpha):
        """Pixellate effect using downsample + nearest-neighbor upscale."""
        children = render.children
        if not children:
            return

        child_render = children[0][0]
        param = max(1, int(render.operation_parameter))

        w = max(1, int(math.ceil(render.width)))
        h = max(1, int(math.ceil(render.height)))
        if w <= 0 or h <= 0:
            return

        # Render child to full-size texture
        full_tex = self._flatten_to_texture(child_render, w, h)
        if full_tex is None:
            return

        # Downsample: render to a small target
        small_w = max(1, w // param)
        small_h = max(1, h // param)

        try:
            small_target = self.renderer.create_target_texture(small_w, small_h)
            self.renderer.set_render_target(small_target)
            _clear_target(self.renderer, small_w, small_h)

            # Copy full texture to small target (automatic downscale)
            full_tex.set_scale_mode(SCALEMODE_LINEAR)
            self.renderer.render_copy_f(
                full_tex, None,
                (0.0, 0.0, float(small_w), float(small_h))
            )
            self.renderer.set_render_target(None)

            # Upscale with nearest-neighbor for blocky effect
            small_target.set_scale_mode(SCALEMODE_NEAREST)

            effective_alpha = alpha * render.alpha
            a_byte = max(0, min(255, int(effective_alpha * 255)))
            small_target.set_alpha_mod(a_byte)
            small_target.set_blend_mode(BLENDMODE_BLEND)

            self.renderer.render_copy_f(
                small_target, None,
                (x, y, float(w), float(h))
            )

            small_target.destroy()
        except Exception:
            # Fallback: draw without pixellation
            effective_alpha = alpha * render.alpha
            a_byte = max(0, min(255, int(effective_alpha * 255)))
            full_tex.set_alpha_mod(a_byte)
            full_tex.set_blend_mode(BLENDMODE_BLEND)
            self.renderer.render_copy_f(
                full_tex, None,
                (x, y, float(w), float(h))
            )

        full_tex.destroy()

    def _draw_flatten(self, render, x, y, alpha):
        """Flatten a subtree by drawing children directly with combined alpha.

        Avoids render-to-texture entirely — on Xbox D3D12, render target
        clears don't reliably write alpha=0, causing checkerboard artifacts.
        Per-child alpha is visually identical for non-overlapping children
        and imperceptibly different for overlapping ones.
        """
        effective_alpha = alpha * render.alpha
        blend = BLENDMODE_ADD if render.over < 1.0 else BLENDMODE_BLEND

        for child, cx, cy, focus, main in render.children:
            if isinstance(child, renpy.display.render.Render):
                self._draw_render(child, x + cx, y + cy, effective_alpha, None)
            else:
                tex = self._get_texture(child)
                if tex is not None:
                    a_byte = max(0, min(255, int(effective_alpha * 255)))
                    tex.set_alpha_mod(a_byte)
                    tex.set_blend_mode(blend)
                    self.renderer.render_copy_f(
                        tex, None,
                        (x + cx, y + cy,
                         float(tex.w), float(tex.h))
                    )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _flatten_to_texture(self, render, w, h):
        """Render a render tree to a GPU texture of given size."""
        if w <= 0 or h <= 0:
            return None

        w = max(1, w)
        h = max(1, h)

        try:
            target = self.renderer.create_target_texture(w, h)
        except Exception:
            return None

        self.renderer.set_render_target(target)
        _clear_target(self.renderer, w, h)

        if isinstance(render, renpy.display.render.Render):
            self._draw_render(render, 0, 0, 1.0, None)
        else:
            tex = self._get_texture(render)
            if tex is not None:
                tex.set_alpha_mod(255)
                self.renderer.render_copy_f(tex, None, (0, 0, float(w), float(h)))

        self.renderer.set_render_target(None)
        return target

    def _flatten_to_texture_from_render(self, render, w, h):
        """Flatten a render's children into a target texture."""
        if w <= 0 or h <= 0:
            return None

        w = max(1, w)
        h = max(1, h)

        try:
            target = self.renderer.create_target_texture(w, h)
        except Exception:
            return None

        self.renderer.set_render_target(target)
        _clear_target(self.renderer, w, h)

        for child, cx, cy, focus, main in render.children:
            if isinstance(child, renpy.display.render.Render):
                self._draw_render(child, cx, cy, 1.0, None)
            else:
                tex = self._get_texture(child)
                if tex is not None:
                    tex.set_alpha_mod(255)
                    tex.set_blend_mode(BLENDMODE_BLEND)
                    self.renderer.render_copy_f(
                        tex, None,
                        (cx, cy, float(tex.w), float(tex.h))
                    )

        self.renderer.set_render_target(None)
        return target

    def _render_to_surface(self, render, w, h, alpha=True):
        """Render a render tree to a pygame Surface (for CPU operations)."""
        if w <= 0 or h <= 0:
            return None

        # Render to texture, then read back
        tex = self._flatten_to_texture(render, w, h)
        if tex is None:
            return None

        self.renderer.set_render_target(tex)
        try:
            surf = self.renderer.render_read_pixels()
        except Exception:
            self.renderer.set_render_target(None)
            tex.destroy()
            return None

        self.renderer.set_render_target(None)
        tex.destroy()
        return surf

    def _push_clip(self, rect):
        """Push a clip rectangle onto the stack."""
        self._clip_stack.append(rect)
        self.renderer.set_clip_rect(rect)

    def _pop_clip(self):
        """Pop the clip rectangle stack."""
        if self._clip_stack:
            self._clip_stack.pop()
        if self._clip_stack:
            self.renderer.set_clip_rect(self._clip_stack[-1])
        else:
            self.renderer.set_clip_rect(None)

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
        # new surfaces from render_to_texture on the Render object.
        # On Xbox D3D12, _render_to_surface reads back from a render target
        # that may have checkerboard artifacts in transparent areas. If we
        # cached that surface on render.surface, _draw_render would draw
        # the corrupted surface instead of traversing the render tree.
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
