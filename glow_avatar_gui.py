"""
GlowAvatar — простое Tkinter-окно для подготовки "светящихся" на HDR-экранах
аватарок (см. README.md).

Запуск:  python glow_avatar_gui.py
"""
from __future__ import annotations

import copy
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from PIL import Image, ImageTk

import glow_core

WORKING_SIZE = 512          # разрешение для интерактивного редактирования маски
UNDO_LIMIT = 25
DEFAULT_STOPS = 2.0
DEFAULT_OUTPUT_SIZE = 800

CAVEAT_TEXT = (
    "Свечение НЕ видно на этом (обычном) мониторе — предпросмотр просто\n"
    "показывает слегка засвеченные зоны. Реальный эффект проверяется после\n"
    "загрузки на LinkedIn, на экране с HDR (iPhone/iPad, MacBook, HDR-монитор\n"
    "Windows с включённым HDR)."
)

smoothstep = glow_core.smoothstep


class GlowAvatarApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GlowAvatar — светящиеся аватарки")
        self.root.resizable(False, False)

        self.photo_arr: np.ndarray | None = None       # WORKING_SIZExWORKING_SIZEx3 uint8
        self.mask: np.ndarray | None = None             # WORKING_SIZExWORKING_SIZE float32
        self.undo_stack: list[np.ndarray] = []
        self.source_path: str | None = None

        self.tool = tk.StringVar(value="brush")
        self.brush_radius = tk.IntVar(value=40)
        self.brush_softness = tk.DoubleVar(value=0.6)
        self.brush_strength = tk.DoubleVar(value=1.0)
        self.gradient_invert = tk.BooleanVar(value=False)
        self.wand_tolerance = tk.IntVar(value=40)
        self.auto_threshold = tk.IntVar(value=235)
        self.show_mask = tk.BooleanVar(value=True)
        self.stops = tk.DoubleVar(value=DEFAULT_STOPS)
        self.whiten = tk.DoubleVar(value=0.0)
        self.output_size = tk.StringVar(value=str(DEFAULT_OUTPUT_SIZE))

        self._drag_start: tuple[int, int] | None = None
        self._preview_shape_id: int | None = None
        self._tk_img: ImageTk.PhotoImage | None = None

        self._build_ui()
        self._update_nits_label()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.grid(row=0, column=0, sticky="nsew")

        # --- Canvas ---
        self.canvas = tk.Canvas(
            outer, width=WORKING_SIZE, height=WORKING_SIZE,
            background="#202020", highlightthickness=1, highlightbackground="#555",
        )
        self.canvas.grid(row=0, column=0, rowspan=20, padx=(0, 10))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        side = ttk.Frame(outer)
        side.grid(row=0, column=1, sticky="n")
        r = 0

        ttk.Button(side, text="Открыть фото…", command=self._open_image).grid(
            row=r, column=0, columnspan=2, sticky="we", pady=(0, 8)
        )
        r += 1

        ttk.Label(side, text="Инструмент", font=("", 9, "bold")).grid(
            row=r, column=0, columnspan=2, sticky="w"
        )
        r += 1
        for value, label in [
            ("brush", "Кисть (осветлять)"),
            ("eraser", "Ластик"),
            ("gradient-linear", "Градиент — линейный"),
            ("gradient-radial", "Градиент — радиальный"),
            ("magic-wand", "Волшебная палочка (по цвету)"),
        ]:
            ttk.Radiobutton(side, text=label, value=value, variable=self.tool).grid(
                row=r, column=0, columnspan=2, sticky="w"
            )
            r += 1

        r = self._add_slider(side, r, "Радиус кисти", self.brush_radius, 4, 200)
        r = self._add_slider(
            side, r, "Мягкость края (кисть / палочка)", self.brush_softness, 0.0, 1.0
        )
        r = self._add_slider(
            side, r, "Сила мазка / заливки", self.brush_strength, 0.05, 1.0
        )

        ttk.Checkbutton(
            side, text="Инвертировать градиент", variable=self.gradient_invert
        ).grid(row=r, column=0, columnspan=2, sticky="w", pady=(4, 0))
        r += 1

        r = self._add_slider(
            side, r, "Допуск цвета (волшебная палочка)", self.wand_tolerance, 5, 150, integer=True
        )

        ttk.Separator(side).grid(row=r, column=0, columnspan=2, sticky="we", pady=8)
        r += 1

        ttk.Label(side, text="Авто-маска по яркости", font=("", 9, "bold")).grid(
            row=r, column=0, columnspan=2, sticky="w"
        )
        r += 1
        r = self._add_slider(side, r, "Порог яркости", self.auto_threshold, 100, 255, integer=True)
        ttk.Button(
            side, text="Построить маску по яркости (заменяет текущую)",
            command=self._apply_auto_mask,
        ).grid(row=r, column=0, columnspan=2, sticky="we")
        r += 1

        ttk.Separator(side).grid(row=r, column=0, columnspan=2, sticky="we", pady=8)
        r += 1

        btns = ttk.Frame(side)
        btns.grid(row=r, column=0, columnspan=2, sticky="we")
        ttk.Button(btns, text="Undo (Ctrl+Z)", command=self._undo).pack(side="left", expand=True, fill="x")
        ttk.Button(btns, text="Инвертировать", command=self._invert_mask).pack(side="left", expand=True, fill="x")
        ttk.Button(btns, text="Очистить", command=self._clear_mask).pack(side="left", expand=True, fill="x")
        r += 1

        ttk.Checkbutton(
            side, text="Показывать маску поверх фото", variable=self.show_mask,
            command=self._redraw,
        ).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0))
        r += 1

        ttk.Separator(side).grid(row=r, column=0, columnspan=2, sticky="we", pady=8)
        r += 1

        ttk.Label(side, text="Целевая яркость свечения", font=("", 9, "bold")).grid(
            row=r, column=0, columnspan=2, sticky="w"
        )
        r += 1
        r = self._add_slider(side, r, "Стопы экспозиции", self.stops, 0.5, 4.3, on_change=self._update_nits_label)
        self.nits_label = ttk.Label(side, text="")
        self.nits_label.grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        r = self._add_slider(side, r, "Отбеливание при максимуме", self.whiten, 0.0, 1.0)
        ttk.Label(
            side, text="(0 = цвет не трогаем, только ярче; выше — насыщенный\n"
                        "цвет физически не светится так же сильно, как белый)",
            foreground="#888", justify="left",
        ).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1

        ttk.Separator(side).grid(row=r, column=0, columnspan=2, sticky="we", pady=8)
        r += 1

        row = ttk.Frame(side)
        row.grid(row=r, column=0, columnspan=2, sticky="we")
        ttk.Label(row, text="Размер выходного файла, px:").pack(side="left")
        ttk.Entry(row, textvariable=self.output_size, width=6).pack(side="left", padx=4)
        r += 1

        ttk.Button(side, text="Экспортировать…", command=self._export).grid(
            row=r, column=0, columnspan=2, sticky="we", pady=(8, 8)
        )
        r += 1

        ttk.Label(side, text=CAVEAT_TEXT, foreground="#888", justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w"
        )

        self.root.bind_all("<Control-z>", lambda e: self._undo())

    def _add_slider(self, parent, row, label, var, lo, hi, integer=False, on_change=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1

        scale = ttk.Scale(
            parent, from_=lo, to=hi, variable=var, orient="horizontal",
            command=lambda v: on_change() if on_change else None,
        )
        scale.grid(row=row, column=0, columnspan=2, sticky="we")
        return row + 1

    # --------------------------------------------------------------- image

    def _open_image(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Изображения", "*.jpg *.jpeg *.png *.bmp *.webp"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            arr = glow_core.load_square_srgb_uint8(path, WORKING_SIZE)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка", f"Не удалось открыть изображение:\n{exc}")
            return
        self.source_path = path
        self.photo_arr = arr
        self.mask = np.zeros((WORKING_SIZE, WORKING_SIZE), dtype=np.float32)
        self.undo_stack.clear()
        self._redraw()

    def _require_image(self) -> bool:
        if self.photo_arr is None:
            messagebox.showinfo("Нет фото", "Сначала открой фото.")
            return False
        return True

    # ---------------------------------------------------------------- mask

    def _push_undo(self) -> None:
        if self.mask is None:
            return
        self.undo_stack.append(self.mask.copy())
        if len(self.undo_stack) > UNDO_LIMIT:
            self.undo_stack.pop(0)

    def _undo(self) -> None:
        if not self.undo_stack:
            return
        self.mask = self.undo_stack.pop()
        self._redraw()

    def _invert_mask(self) -> None:
        if not self._require_image():
            return
        self._push_undo()
        self.mask = 1.0 - self.mask
        self._redraw()

    def _clear_mask(self) -> None:
        if not self._require_image():
            return
        self._push_undo()
        self.mask[:] = 0.0
        self._redraw()

    def _apply_auto_mask(self) -> None:
        if not self._require_image():
            return
        self._push_undo()
        lum = (
            0.2126 * self.photo_arr[..., 0]
            + 0.7152 * self.photo_arr[..., 1]
            + 0.0722 * self.photo_arr[..., 2]
        )
        t = self.auto_threshold.get()
        low, high = max(t - 20, 0), min(t + 20, 255)
        self.mask = smoothstep(low, high, lum).astype(np.float32)
        self._redraw()

    # ------------------------------------------------------------- painting

    def _canvas_to_working(self, event) -> tuple[int, int]:
        x = int(np.clip(event.x, 0, WORKING_SIZE - 1))
        y = int(np.clip(event.y, 0, WORKING_SIZE - 1))
        return x, y

    def _paint_dab(self, cx: int, cy: int) -> None:
        radius = max(1, self.brush_radius.get())
        softness = self.brush_softness.get()
        strength = self.brush_strength.get()
        inner = radius * (1.0 - softness)

        x0, x1 = max(0, cx - radius), min(WORKING_SIZE, cx + radius + 1)
        y0, y1 = max(0, cy - radius), min(WORKING_SIZE, cy + radius + 1)
        if x1 <= x0 or y1 <= y0:
            return
        yy, xx = np.mgrid[y0:y1, x0:x1]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        kernel = 1.0 - smoothstep(inner, radius, dist)
        kernel *= strength

        region = self.mask[y0:y1, x0:x1]
        if self.tool.get() == "brush":
            self.mask[y0:y1, x0:x1] = np.maximum(region, kernel)
        else:  # eraser
            self.mask[y0:y1, x0:x1] = np.maximum(region - kernel, 0.0)

    def _apply_magic_wand(self, x: int, y: int) -> None:
        strength = self.brush_strength.get()
        feather = self.brush_softness.get()
        tolerance = self.wand_tolerance.get()
        wand = glow_core.magic_wand_mask(self.photo_arr, x, y, tolerance, feather)
        self.mask = np.maximum(self.mask, wand * strength)

    def _apply_gradient(self, x0, y0, x1, y1) -> None:
        yy, xx = np.mgrid[0:WORKING_SIZE, 0:WORKING_SIZE].astype(np.float32)
        if self.tool.get() == "gradient-radial":
            radius = max(1.0, math.hypot(x1 - x0, y1 - y0))
            dist = np.sqrt((xx - x0) ** 2 + (yy - y0) ** 2)
            value = 1.0 - smoothstep(0.0, radius, dist)
        else:  # linear
            dx, dy = x1 - x0, y1 - y0
            length2 = max(1.0, dx * dx + dy * dy)
            t = ((xx - x0) * dx + (yy - y0) * dy) / length2
            value = np.clip(t, 0.0, 1.0)
        if self.gradient_invert.get():
            value = 1.0 - value
        self.mask = np.maximum(self.mask, value.astype(np.float32))

    def _on_press(self, event) -> None:
        if not self._require_image():
            return
        self._push_undo()
        x, y = self._canvas_to_working(event)
        self._drag_start = (x, y)
        tool = self.tool.get()
        if tool in ("brush", "eraser"):
            self._paint_dab(x, y)
            self._redraw()
        elif tool == "magic-wand":
            self._apply_magic_wand(x, y)
            self._redraw()

    def _on_drag(self, event) -> None:
        if self.photo_arr is None or self._drag_start is None:
            return
        x, y = self._canvas_to_working(event)
        tool = self.tool.get()
        if tool in ("brush", "eraser"):
            self._paint_dab(x, y)
            self._redraw()
        elif tool.startswith("gradient"):
            self._draw_gradient_preview(self._drag_start, (x, y))
        # magic-wand: срабатывает только по клику, drag игнорируется

    def _on_release(self, event) -> None:
        if self.photo_arr is None or self._drag_start is None:
            return
        x, y = self._canvas_to_working(event)
        tool = self.tool.get()
        if tool.startswith("gradient"):
            self._clear_gradient_preview()
            x0, y0 = self._drag_start
            if (x0, y0) != (x, y):
                self._apply_gradient(x0, y0, x, y)
                self._redraw()
        self._drag_start = None

    def _draw_gradient_preview(self, start, end) -> None:
        self._clear_gradient_preview()
        x0, y0 = start
        x1, y1 = end
        if self.tool.get() == "gradient-radial":
            radius = math.hypot(x1 - x0, y1 - y0)
            self._preview_shape_id = self.canvas.create_oval(
                x0 - radius, y0 - radius, x0 + radius, y0 + radius, outline="#00c8ff"
            )
        else:
            self._preview_shape_id = self.canvas.create_line(
                x0, y0, x1, y1, fill="#00c8ff", width=2, arrow="last"
            )

    def _clear_gradient_preview(self) -> None:
        if self._preview_shape_id is not None:
            self.canvas.delete(self._preview_shape_id)
            self._preview_shape_id = None

    # ---------------------------------------------------------------- draw

    def _redraw(self) -> None:
        if self.photo_arr is None:
            self.canvas.delete("all")
            return
        if self.show_mask.get():
            display = glow_core.render_mask_overlay(self.photo_arr, self.mask)
        else:
            display = self.photo_arr
        img = Image.fromarray(display)
        self._tk_img = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_img)

    def _update_nits_label(self, *_args) -> None:
        nits = glow_core.target_nits_for_mask_value(1.0, self.stops.get())
        self.nits_label.config(text=f"пик свечения ≈ {nits:.0f} нит")

    # -------------------------------------------------------------- export

    def _export(self) -> None:
        if not self._require_image():
            return
        try:
            size = int(self.output_size.get())
            if size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Ошибка", "Размер выходного файла должен быть положительным числом.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".jpg",
            filetypes=[("JPEG", "*.jpg")],
            initialfile="glow_avatar.jpg",
        )
        if not path:
            return

        try:
            glow_core.export_glow_jpeg(
                path, self.photo_arr, self.mask, self.stops.get(), size,
                whiten=self.whiten.get(),
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка экспорта", str(exc))
            return

        messagebox.showinfo(
            "Готово",
            f"Сохранено: {path}\n\nСвечение будет видно только после загрузки на "
            "LinkedIn и только на HDR-экране.",
        )


def main() -> None:
    root = tk.Tk()
    GlowAvatarApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
