"""One shaped X11 window, owned exclusively by the feedback daemon."""
class X11Overlay:
    def __init__(self):
        from Xlib import X, display
        from Xlib.ext import shape
        self.connection = display.Display()
        self.window = None
        try:
            if not all(self.connection.has_extension(ext) for ext in ("SHAPE", "XTEST")):
                raise RuntimeError("Visible actions require XShape/XTest")
            self.screen = self.connection.screen()
            self.window = self.screen.root.create_window(
                0, 0, 1, 1, 0, self.screen.root_depth, X.InputOutput, X.CopyFromParent,
                background_pixel=self.screen.black_pixel, override_redirect=1, event_mask=0)
            self.window.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, [])
            self.gc = self.window.create_gc(foreground=self.screen.white_pixel,
                                             background=self.screen.black_pixel)
        except Exception:
            self.close()
            raise

    def show(self, bounds, label):
        from Xlib import X
        from Xlib.ext import shape
        x, y = max(0, int(bounds.x)), max(0, int(bounds.y))
        right = min(self.screen.width_in_pixels, int(bounds.x + bounds.width))
        bottom = min(self.screen.height_in_pixels, int(bounds.y + bounds.height))
        width, height = right - x, bottom - y
        if width <= 0 or height <= 0:
            return False
        self.window.configure(x=x, y=y, width=width, height=height, stack_mode=X.Above)
        edge = min(3, width, height)
        self.window.shape_rectangles(shape.SO.Set, shape.SK.Bounding, X.Unsorted, 0, 0,
            [(0, 0, width, min(20, height)), (0, 0, edge, height),
             (width-edge, 0, edge, height), (0, height-edge, width, edge)])
        self.window.map()
        self.window.clear_area()
        self.window.draw_text(self.gc, 4, min(15, height-1), label.encode("ascii", "replace"))
        self.connection.sync()
        return True

    def hide(self):
        self.window.unmap()
        self.connection.sync()

    def close(self):
        try:
            if self.window is not None:
                self.gc.free()
                self.window.destroy()
            self.connection.close()
        except Exception:
            pass
