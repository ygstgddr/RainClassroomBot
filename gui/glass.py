"""Small, native Tk widgets for the glass desktop theme.

Only the decorative background is rasterized. Tables, text, focus handling and
keyboard interaction remain native widgets; no transparency is applied to text.
"""
import math
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageDraw, ImageFilter, ImageTk


INK = '#243349'
MUTED = '#748299'
PANEL = '#f8fafd'
BLUE = '#007aff'


class GlassBackdrop(tk.Canvas):
    """Render a softly lit desktop and frosted cards only after a resize settles."""

    def __init__(self, master, **kwargs):
        super().__init__(master, highlightthickness=0, bd=0, bg='#eaf1fb', **kwargs)
        self._job = None
        self._image = None
        self._background = self.create_image(0, 0, anchor='nw')
        self.bind('<Configure>', self._schedule, add='+')

    def _schedule(self, event=None):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(65, self._paint)

    def _paint(self):
        self._job = None
        w, h = max(1, self.winfo_width()), max(1, self.winfo_height())
        if w < 100 or h < 100:
            return
        # Rendering the diffuse light at quarter resolution keeps resizing cheap.
        sw, sh = max(1, w // 4), max(1, h // 4)
        light = Image.new('RGB', (sw, sh), '#edf2fb')
        pixels = light.load()
        for y in range(sh):
            for x in range(sw):
                a = math.exp(-(((x-sw*.08)/(sw*.7))**2 + ((y-sh*.18)/(sh*.6))**2)*2)
                b = math.exp(-(((x-sw*.93)/(sw*.6))**2 + ((y-sh*.72)/(sh*.8))**2)*2)
                c = math.exp(-(((x-sw*.52)/(sw*.7))**2 + ((y-sh*.98)/(sh*.35))**2)*2)
                pixels[x, y] = (int(238-27*a+2*b-14*c), int(244-8*a-24*b-7*c), int(252-2*a-2*b-5*c))
        canvas = light.resize((w, h), Image.Resampling.BICUBIC).convert('RGBA')
        cards = [(20, 20, 208, h-20, 26, (250, 252, 255, 160)),
                 (230, 218, w-22, h-22, 25, (248, 250, 253, 244))]
        space, available = 14, w-252
        cw = (available-2*space)/3
        for n in range(3):
            left = 230 + n*(cw+space)
            cards.append((left, 112, left+cw, 200, 20, (252, 253, 255, 202)))
        shadow = Image.new('RGBA', (w, h))
        draw = ImageDraw.Draw(shadow)
        for x1, y1, x2, y2, radius, _ in cards:
            if x2 > x1 and y2 > y1:
                draw.rounded_rectangle((x1+1, y1+7, x2+1, y2+7), radius, fill=(61, 92, 140, 23))
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
        surfaces = Image.new('RGBA', (w, h))
        draw = ImageDraw.Draw(surfaces)
        for x1, y1, x2, y2, radius, color in cards:
            if x2 > x1 and y2 > y1:
                draw.rounded_rectangle((x1, y1, x2, y2), radius, fill=color, outline=(255, 255, 255, 225), width=1)
        canvas.alpha_composite(surfaces)
        self._image = ImageTk.PhotoImage(canvas, master=self)
        self.itemconfigure(self._background, image=self._image)
        self.tag_lower(self._background)


class GlassButton(tk.Canvas):
    """Rounded button with mouse/keyboard parity and ttk-compatible state()."""

    def __init__(self, master, text='', command=None, accent=False, quiet=False, width=None, height=40, **kwargs):
        self._font = tkfont.Font(root=master, family='Microsoft YaHei UI', size=10, weight='bold' if accent else 'normal')
        self._text, self._command = text, command
        self._accent, self._quiet = accent, quiet
        self._disabled, self._hover, self._selected = False, False, False
        self._fixed_width = width
        self._height = height
        self._photo = None
        self._draw_signature = None
        kwargs.setdefault('bg', PANEL)
        super().__init__(master, width=width or self._font.measure(text)+30, height=height,
                         bd=0, highlightthickness=0, takefocus=1, cursor='hand2', **kwargs)
        self.bind('<Enter>', lambda event: self._enter(True))
        self.bind('<Leave>', lambda event: self._enter(False))
        self.bind('<Button-1>', self._click)
        self.bind('<space>', self._click)
        self.bind('<Return>', self._click)
        self.bind('<FocusIn>', lambda event: self._draw())
        self.bind('<FocusOut>', lambda event: self._draw())
        self.bind('<Configure>', lambda event: self._draw())
        self._draw()

    def _enter(self, value):
        self._hover = value
        self._draw()

    def _click(self, event=None):
        if not self._disabled:
            self.focus_set()
            if self._command:
                self._command()
        return 'break'

    def invoke(self):
        return self._click()

    def state(self, statespec=None):
        if statespec is not None:
            if 'disabled' in statespec:
                self._disabled = True
            if '!disabled' in statespec:
                self._disabled = False
            self._draw()
        return ('disabled',) if self._disabled else ()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if 'text' in kwargs:
            self._text = kwargs.pop('text')
            if self._fixed_width is None:
                kwargs['width'] = self._font.measure(self._text)+30
        if 'command' in kwargs:
            self._command = kwargs.pop('command')
        if 'state' in kwargs:
            self._disabled = kwargs.pop('state') == 'disabled'
        if 'selected' in kwargs:
            self._selected = kwargs.pop('selected')
        result = super().configure(**kwargs)
        if hasattr(self, '_photo'):
            self._draw()
        return result

    config = configure

    def cget(self, key):
        if key == 'text':
            return self._text
        if key == 'state':
            return 'disabled' if self._disabled else 'normal'
        return super().cget(key)

    def _draw(self):
        w = self._fixed_width or self._font.measure(self._text)+30
        h = self._height
        focused = self.focus_get() == self
        signature = (self._text, w, h, self._disabled, self._hover, self._selected, focused, self._accent, self._quiet)
        if signature == self._draw_signature:
            return
        self._draw_signature = signature
        if self._accent:
            fill = '#b2cdef' if self._disabled else ('#0067dd' if self._hover else BLUE)
            border, foreground = fill, '#ffffff'
        elif self._selected:
            fill, border, foreground = '#e0edff', '#d1e3fb', '#1165c9'
        else:
            fill = '#eaf1fb' if self._hover and not self._disabled else ('#f0f4fa' if self._quiet else '#ffffff')
            border, foreground = '#e2e9f2', '#9ca8b9' if self._disabled else INK
        scale = 2
        bitmap = Image.new('RGBA', (w*scale, h*scale))
        draw = ImageDraw.Draw(bitmap)
        draw.rounded_rectangle((1, 1, w*scale-2, h*scale-2), radius=12*scale,
                               fill=fill, outline='#7fb5ff' if focused else border,
                               width=2*scale if focused else scale)
        bitmap = bitmap.resize((w, h), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(bitmap, master=self)
        self.delete('all')
        self.create_image(0, 0, anchor='nw', image=self._photo)
        self.create_text(w/2, h/2, text=self._text, fill=foreground, font=self._font)
        super().configure(cursor='arrow' if self._disabled else 'hand2')
