# -*- coding: utf-8 -*-
"""图片压缩工具

功能
  - 压缩质量：滑块与数字输入框双向绑定（例如 58%）
  - 目标体积：反解压缩参数，逼近指定的文件大小上限
  - 最长边缩放、输出格式转换、批量处理
  - 结果统一输出到源文件所在目录的 compressed\\ 子目录，原文件不改动

两种运行方式
  - 源码运行：依赖同级 libs\\ 目录（Pillow、tkinterdnd2），双击「图片压缩器.vbs」
  - 打包运行：单文件 exe，依赖已内嵌，libs 注入逻辑自动跳过
"""

from __future__ import annotations

import io
import math
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

# 打包后 __file__ 指向临时解压目录（_MEIxxxx），日志与外部资源必须以 exe 所在目录为基准
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent

_LIBS = APP_DIR / "libs"
if not FROZEN and _LIBS.is_dir() and str(_LIBS) not in sys.path:
    sys.path.insert(0, str(_LIBS))


def _log_path() -> Path:
    """日志优先落在程序同级目录；该目录不可写时退到用户数据目录。"""
    candidates = [APP_DIR]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "图片压缩工具")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_bytes(b"")
            probe.unlink()
            return d / "compressor_error.log"
        except Exception:
            continue
    return Path(os.environ.get("TEMP", ".")) / "compressor_error.log"


def log_error(text: str) -> None:
    """pythonw / noconsole 下没有 stdout，异常只能落盘。"""
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write("[%s]\n%s\n\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 依赖加载
# --------------------------------------------------------------------------- #
try:
    from PIL import Image, ImageOps, ImageTk

    Image.MAX_IMAGE_PIXELS = None  # 关闭超大图警告
except Exception as _exc:  # pragma: no cover
    log_error("Pillow 加载失败: %r" % (_exc,))
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb

        _r = _tk.Tk()
        _r.withdraw()
        _mb.showerror("依赖缺失", "无法加载 Pillow：%s\n\n请确认 libs 目录与本程序位于同一文件夹。" % _exc)
        _r.destroy()
    except Exception:
        pass
    raise SystemExit(1)

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_OK = True
except Exception:
    DND_OK = False
    DND_FILES = None
    TkinterDnD = None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
PNG_LO, PNG_HI = 2, 256          # PNG 量化颜色数区间（下限必须允许到 2 色）
PNG_SEARCH_HI = 255              # 搜索上界：255 色为有损，避免触发昂贵的无损编码
PNG_LOSSLESS_Q = 100             # 达到该质量时 PNG 走无损优化（不量化）
PNG_FALLOFF = 2.2                # 质量 -> 颜色数 的幂次（越大低端收缩越快）
JPEG_LO, JPEG_HI = 1, 95         # JPEG / WEBP 质量区间
MAX_ITER = 6                     # 目标体积搜索的最大编码次数
TOLERANCE = 0.03                 # 体积命中容差 ±3%

IMG_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".tif", ".tiff"}

EDGE_CHOICES = ["不缩放", "2560 px", "1920 px", "1600 px", "1280 px", "1080 px", "800 px", "640 px"]
FMT_CHOICES = ["保持原格式", "JPEG", "PNG", "WEBP", "自动（优先小体积）"]
FMT_VALUES = ["auto", "JPEG", "PNG", "WEBP", "smart"]

EXT_OF_FMT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

NATIVE_FMT = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG",
    ".png": "PNG", ".webp": "WEBP",
}


def native_fmt_of(path):
    return NATIVE_FMT.get(Path(path).suffix.lower())


# --------------------------------------------------------------------------- #
# 图像编解码核心
# --------------------------------------------------------------------------- #
def load_image(path) -> Image.Image:
    img = Image.open(path)
    img.load()
    try:
        img = ImageOps.exif_transpose(img)  # 按 EXIF 摆正方向
    except Exception:
        pass
    return img


def apply_resize(img: Image.Image, max_edge: int) -> Image.Image:
    if not max_edge:
        return img
    w, h = img.size
    m = max(w, h)
    if m <= max_edge:
        return img
    s = max_edge / float(m)
    return img.resize((max(1, int(round(w * s))), max(1, int(round(h * s)))), Image.Resampling.LANCZOS)


def flatten_rgb(img: Image.Image) -> Image.Image:
    """把带透明通道的图合成到白底，用于 JPEG。"""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    return img.convert("RGB")


def keep_alpha(img: Image.Image) -> Image.Image:
    """保留透明通道（WebP 用）。"""
    if img.mode in ("RGB", "RGBA"):
        return img
    if (img.mode == "P" and "transparency" in img.info) or img.mode == "LA":
        return img.convert("RGBA")
    return img.convert("RGB")


def to_display(img: Image.Image) -> Image.Image:
    """转成 ImageTk 可直接显示的 8 位模式。"""
    if img.mode in ("RGB", "RGBA"):
        return img
    if "transparency" in img.info or img.mode == "LA":
        return img.convert("RGBA")
    return img.convert("RGB")


def _jpeg_subsampling(q: int) -> int:
    if q >= 90:
        return 0  # 4:4:4
    if q >= 75:
        return 1  # 4:2:2
    return 2      # 4:2:0


def encode_jpeg(img: Image.Image, q: int, keep_meta: bool, subsampling=None) -> bytes:
    if subsampling is None:
        subsampling = _jpeg_subsampling(int(q))
    buf = io.BytesIO()
    kw = dict(quality=int(q), optimize=True, progressive=True, subsampling=int(subsampling))
    icc = img.info.get("icc_profile")
    if icc:
        kw["icc_profile"] = icc
    if keep_meta and img.info.get("exif"):
        kw["exif"] = img.info["exif"]
    img.save(buf, "JPEG", **kw)
    return buf.getvalue()


def png_colors_for(q: float) -> int:
    """质量百分比 -> 调色板颜色数。

    PNG 索引图的体积主要由索引值的分布集中度决定，颜色数越少压缩率越高，
    因此中低端收缩得比线性更快（幂次 PNG_FALLOFF）。
    """
    q = max(1.0, min(100.0, float(q)))
    n = int(round(PNG_HI * (q / 100.0) ** PNG_FALLOFF))
    return max(PNG_LO, min(PNG_HI, n))


def png_quality_for_colors(colors: int) -> float:
    """颜色数 -> 质量百分比（上述映射的反函数）。"""
    c = max(PNG_LO, min(PNG_HI, int(colors)))
    if c >= PNG_HI:
        return 100.0
    return max(1.0, min(99.0, 100.0 * (c / float(PNG_HI)) ** (1.0 / PNG_FALLOFF)))


def encode_png(img: Image.Image, param: int, keep_meta: bool) -> bytes:
    """param 为颜色数；param >= 256 视为无损，直接优化保存。"""
    buf = io.BytesIO()
    icc = img.info.get("icc_profile")
    kw = dict(optimize=True, compress_level=9)
    if icc:
        kw["icc_profile"] = icc

    if param >= PNG_HI:
        if keep_meta and img.info.get("exif"):
            kw["exif"] = img.info["exif"]
        img.save(buf, "PNG", **kw)
        return buf.getvalue()

    colors = max(PNG_LO, min(PNG_HI, int(param)))

    # 统一入口模式：索引图先还原成真彩色，避免二次量化与透明通道丢失
    if img.mode == "P":
        work = img.convert("RGBA") if "transparency" in img.info else img.convert("RGB")
    else:
        work = img

    has_alpha = work.mode in ("RGBA", "LA")
    if has_alpha:
        pal = work.convert("RGBA").quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
    else:
        pal = work.convert("RGB").quantize(
            colors=colors,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.FLOYDSTEINBERG,
        )
    pal.save(buf, "PNG", **kw)
    return buf.getvalue()


def refine_jpeg_subsampling(base_img: Image.Image, q: int, target: int,
                            keep_meta: bool, current: bytes) -> bytes:
    """体积已落在上限内时，尝试用更精细的色度采样把剩余预算花在画质上。

    quality 是整数档，低质量区每档跨度可达 15%-20%，直接输出会明显小于目标值。
    这里在 quality 不变的前提下逐级提升采样精度，直到刚好贴近上限。
    """
    if not target or len(current) > target:
        return current
    best = current
    start = _jpeg_subsampling(int(q))
    for s in range(start - 1, -1, -1):
        data = encode_jpeg(base_img, q, keep_meta, subsampling=s)
        if len(data) <= target:
            best = data
        else:
            break
    return best


def encode_webp(img: Image.Image, q: int, keep_meta: bool) -> bytes:
    buf = io.BytesIO()
    kw = dict(quality=int(q), method=6)
    icc = img.info.get("icc_profile")
    if icc:
        kw["icc_profile"] = icc
    if keep_meta and img.info.get("exif"):
        kw["exif"] = img.info["exif"]
    img.save(buf, "WEBP", **kw)
    return buf.getvalue()


def resolve_format(src_path, img: Image.Image, choice: str) -> str:
    """决定输出格式。"""
    if choice in ("JPEG", "PNG", "WEBP"):
        return choice
    ext = Path(src_path).suffix.lower()
    if choice == "auto":
        if ext in (".jpg", ".jpeg", ".jpe"):
            return "JPEG"
        if ext == ".webp":
            return "WEBP"
        return "PNG"
    # smart：在保持视觉质量的前提下选体积更小的容器
    if ext in (".jpg", ".jpeg", ".jpe"):
        return "JPEG"
    return "WEBP"


def quality_to_param(fmt: str, q: float) -> int:
    if fmt == "PNG":
        return PNG_HI if q >= PNG_LOSSLESS_Q else png_colors_for(q)
    return max(1, min(100, int(round(q))))


def param_to_quality(fmt: str, param: int) -> float:
    if fmt == "PNG":
        return 100.0 if param >= PNG_HI else png_quality_for_colors(param)
    return float(param)


def build_encoder(img: Image.Image, fmt: str, keep_meta: bool):
    """返回 (encode_fn, lo, hi)。encode_fn(param) -> bytes"""
    if fmt == "JPEG":
        base = flatten_rgb(img)
        return (lambda p: encode_jpeg(base, p, keep_meta)), JPEG_LO, JPEG_HI
    if fmt == "PNG":
        return (lambda p: encode_png(img, p, keep_meta)), PNG_LO, PNG_SEARCH_HI
    base = keep_alpha(img)
    return (lambda p: encode_webp(base, p, keep_meta)), JPEG_LO, JPEG_HI


def solve_target(encode_fn, lo: int, hi: int, target: int, max_iter: int = MAX_ITER):
    """二分搜索最大的、仍能满足体积上限的参数。

    体积随参数单调递增，故可二分。返回 (param, data, 是否命中目标)。
    """
    top = encode_fn(hi)
    if len(top) <= target:
        return hi, top, True

    best_param, best_data = None, None
    cur_lo, cur_hi = lo, hi
    it = 0
    while cur_lo <= cur_hi and it < max_iter:
        it += 1
        mid = (cur_lo + cur_hi) // 2
        data = encode_fn(mid)
        n = len(data)
        if n <= target:
            if best_param is None or mid > best_param:
                best_param, best_data = mid, data
            cur_lo = mid + 1
            if target - n <= target * TOLERANCE:
                break
        else:
            cur_hi = mid - 1

    if best_param is None:
        data = encode_fn(lo)
        return lo, data, False
    return best_param, best_data, True


# --------------------------------------------------------------------------- #
# 单张处理
# --------------------------------------------------------------------------- #
def process_one(path, opts, want_preview: bool = False) -> dict:
    """按 opts 处理一张图，返回结果字典（data 为编码后的字节）。"""
    src_bytes = os.path.getsize(path)
    src = load_image(path)
    orig_px = src.size
    work = apply_resize(src, opts["max_edge"])
    fmt = resolve_format(path, work, opts["out_format"])
    keep_meta = opts["keep_meta"]

    target = opts["target_bytes"] or 0
    note = ""
    passthrough = False

    if (target > 0 and src_bytes <= target and work is src and fmt == native_fmt_of(path)):
        # 原文件已在上限之内，且无需转格式或缩放：直接沿用，避免重编码带来的画质损失
        data = Path(path).read_bytes()
        hit, quality, param = True, float(opts["quality"]), 0
        passthrough = True
        note = "原文件已小于目标，直接沿用"
    elif target > 0:
        enc, lo, hi = build_encoder(work, fmt, keep_meta)
        param, data, hit = solve_target(enc, lo, hi, target)
        meta_dropped = False
        if not hit:
            # 极限参数仍超标时，尝试丢弃色彩配置文件再搜一次
            enc2, lo2, hi2 = build_encoder(work, fmt, False)
            param2, data2, hit2 = solve_target(enc2, lo2, hi2, target)
            if len(data2) < len(data):
                param, data, hit = param2, data2, hit2
                meta_dropped = True
                note = "已丢弃元数据"
        if hit and fmt == "JPEG" and not meta_dropped:
            # 把剩余体积预算花在色度采样精度上，换取更接近目标的画质
            refined = refine_jpeg_subsampling(flatten_rgb(work), int(param), target, keep_meta, data)
            if len(refined) > len(data):
                data = refined
        quality = param_to_quality(fmt, param)
    else:
        q = opts["quality"]
        param = quality_to_param(fmt, q)
        enc, _, _ = build_encoder(work, fmt, keep_meta)
        data = enc(param)
        hit, quality = True, q

    result = {
        "src": str(path),
        "fmt": fmt,
        "src_bytes": src_bytes,
        "out_bytes": len(data),
        "src_px": orig_px,
        "out_px": work.size,
        "quality": quality,
        "param": param,
        "hit": hit,
        "note": note,
        "passthrough": passthrough,
        "data": data,
    }

    if want_preview:
        pa = to_display(work)
        pa.thumbnail((440, 340), Image.Resampling.LANCZOS)
        try:
            pb = to_display(Image.open(io.BytesIO(data)))
            pb.thumbnail((440, 340), Image.Resampling.LANCZOS)
        except Exception:
            pb = pa
        result["preview_a"] = pa
        result["preview_b"] = pb

    return result


def output_path_for(src, fmt: str, out_dir: Path) -> Path:
    stem = Path(src).stem
    ext = EXT_OF_FMT.get(fmt, ".jpg")
    p = out_dir / (stem + ext)
    i = 1
    while p.exists():
        p = out_dir / ("%s_%d%s" % (stem, i, ext))
        i += 1
    return p


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
PAD = 8


def human(nbytes: float) -> str:
    n = float(nbytes)
    if n < 1024:
        return "%d B" % int(n)
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.2f MB" % (n / (1024 * 1024))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.files = []
        self.q = queue.Queue()
        self.job = None
        self.rz_job = None
        self.busy = False          # 保留：预览线程占用标记
        self.running = False       # 批量任务进行中
        self.cancel = False
        self.preview_seq = 0       # 预览任务序号，用于丢弃过期结果
        self.preview_lock = threading.Lock()
        self.thumb_a = None
        self.thumb_b = None
        self.last_out_dir = None
        self._last_size = None

        root.title("图片压缩工具")
        # 实际尺寸在 main() 里按内容请求值自适应，这里只兜底
        root.minsize(520, 620)

        self._build_ui()
        self._setup_dnd()
        root.after(60, self._pump)

    # ---------------- 界面构建 ---------------- #
    def _build_ui(self):
        style = ttk.Style()
        for name in ("vista", "winnative", "clam"):
            if name in style.theme_names():
                style.theme_use(name)
                break
        try:
            import tkinter.font as tkfont

            for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(fname).configure(family="Microsoft YaHei UI", size=9)
        except Exception:
            pass
        style.configure("Hint.TLabel", foreground="#6b6b6b")
        style.configure("Run.TButton", padding=(12, 7))

        main = ttk.Frame(self.root, padding=PAD)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # --- 文件选择 ---
        bar = ttk.Frame(main)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="添加图片", command=self.pick_files).pack(side="left")
        ttk.Button(bar, text="添加文件夹", command=self.pick_folder).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="清空", command=self.clear_files).pack(side="left", padx=(6, 0))
        self.dnd_hint = ttk.Label(
            bar,
            text="可将文件 / 文件夹拖入窗口" if DND_OK else "拖拽不可用",
            style="Hint.TLabel",
        )
        self.dnd_hint.pack(side="right")

        # --- 文件队列 ---
        lf = ttk.LabelFrame(main, text="待处理文件", padding=6)
        lf.grid(row=1, column=0, sticky="nsew", pady=(PAD, 0))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(
            lf,
            height=4,
            activestyle="none",
            exportselection=False,
            selectmode="extended",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#d8d8d8",
            highlightcolor="#8ab4f8",
        )
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # --- 压缩质量 ---
        qf = ttk.LabelFrame(main, text="压缩质量", padding=6)
        qf.grid(row=2, column=0, sticky="ew", pady=(PAD, 0))
        qf.columnconfigure(0, weight=1)

        row = ttk.Frame(qf)
        row.grid(row=0, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)
        self.q_var = tk.DoubleVar(value=58.0)
        self.q_show = tk.StringVar(value="58")
        self.scale = ttk.Scale(row, from_=1, to=100, orient="horizontal", variable=self.q_var, command=self._on_scale)
        self.scale.grid(row=0, column=0, sticky="ew")
        self.q_entry = ttk.Entry(row, width=6, justify="right", textvariable=self.q_show)
        self.q_entry.grid(row=0, column=1, padx=(10, 2))
        ttk.Label(row, text="%").grid(row=0, column=2)
        self.q_entry.bind("<Return>", self._commit_quality)
        self.q_entry.bind("<FocusOut>", self._commit_quality)

        self.q_hint = ttk.Label(qf, text="质量百分比：数值越低体积越小，100% 为近乎无损", style="Hint.TLabel")
        self.q_hint.grid(row=1, column=0, sticky="w", pady=(4, 0))

        # --- 目标体积 ---
        tf = ttk.LabelFrame(main, text="目标体积", padding=6)
        tf.grid(row=3, column=0, sticky="ew", pady=(PAD, 0))
        tf.columnconfigure(4, weight=1)

        self.use_target = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tf, text="限制最大体积", variable=self.use_target, command=self._on_params_changed
        ).grid(row=0, column=0, sticky="w")

        self.tgt_text = tk.StringVar(value="200")
        self.tgt_entry = ttk.Entry(tf, width=8, justify="right", textvariable=self.tgt_text)
        self.tgt_entry.grid(row=0, column=1, padx=(12, 4))
        self.tgt_unit = ttk.Combobox(tf, width=4, state="readonly", values=("KB", "MB"))
        self.tgt_unit.current(0)
        self.tgt_unit.grid(row=0, column=2)
        self.tgt_entry.bind("<Return>", self._on_params_changed)
        self.tgt_entry.bind("<FocusOut>", self._on_params_changed)
        self.tgt_unit.bind("<<ComboboxSelected>>", self._on_params_changed)

        ttk.Label(tf, text="勾选后自动反解质量参数；1 KB = 1024 字节", style="Hint.TLabel").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(4, 0)
        )

        # --- 尺寸与格式 ---
        gf = ttk.LabelFrame(main, text="尺寸与格式", padding=6)
        gf.grid(row=4, column=0, sticky="ew", pady=(PAD, 0))
        gf.columnconfigure(1, weight=1)
        gf.columnconfigure(3, weight=1)

        ttk.Label(gf, text="最长边").grid(row=0, column=0, sticky="w")
        self.edge_box = ttk.Combobox(gf, state="readonly", values=EDGE_CHOICES, width=10)
        self.edge_box.current(0)
        self.edge_box.grid(row=0, column=1, sticky="w", padx=(6, 12))
        self.edge_box.bind("<<ComboboxSelected>>", self._on_params_changed)

        ttk.Label(gf, text="输出格式").grid(row=0, column=2, sticky="w")
        self.fmt_box = ttk.Combobox(gf, state="readonly", values=FMT_CHOICES, width=16)
        self.fmt_box.current(0)
        self.fmt_box.grid(row=0, column=3, sticky="w", padx=(6, 0))
        self.fmt_box.bind("<<ComboboxSelected>>", self._on_params_changed)

        self.keep_meta = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            gf, text="保留 EXIF 拍摄信息（会略微增大体积）", variable=self.keep_meta, command=self._on_params_changed
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # --- 预览 ---
        pf = ttk.LabelFrame(main, text="预览对比", padding=6)
        pf.grid(row=5, column=0, sticky="nsew", pady=(PAD, 0))
        pf.columnconfigure(0, weight=1)
        pf.columnconfigure(1, weight=1)
        # tk.Canvas 的默认宽度是 378px，必须显式给初值，否则会把窗口撑得很宽
        self.canvas_a = tk.Canvas(pf, width=220, height=132, bg="#f5f5f5",
                                  highlightthickness=1, highlightbackground="#dcdcdc")
        self.canvas_a.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.canvas_b = tk.Canvas(pf, width=220, height=132, bg="#f5f5f5",
                                  highlightthickness=1, highlightbackground="#dcdcdc")
        self.canvas_b.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.info_a = ttk.Label(pf, text="原图 —", style="Hint.TLabel", anchor="center")
        self.info_a.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.info_b = ttk.Label(pf, text="压缩后 —", style="Hint.TLabel", anchor="center")
        self.info_b.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        # --- 结果与操作 ---
        bot = ttk.Frame(main)
        bot.grid(row=6, column=0, sticky="ew", pady=(PAD, 0))
        bot.columnconfigure(0, weight=1)

        self.summary = ttk.Label(bot, text="请添加图片", anchor="w")
        self.summary.grid(row=0, column=0, columnspan=3, sticky="ew")

        self.pb = ttk.Progressbar(bot, mode="determinate")
        self.pb.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 8))

        self.status = ttk.Label(bot, text="", style="Hint.TLabel", anchor="w")
        self.status.grid(row=2, column=0, sticky="w")

        self.btn_out = ttk.Button(bot, text="打开输出目录", command=self.open_outdir, state="disabled")
        self.btn_out.grid(row=2, column=1, padx=(6, 6))

        self.btn_run = ttk.Button(bot, text="压缩并保存", command=self.run_batch, style="Run.TButton")
        self.btn_run.grid(row=2, column=2)

        self._update_controls_state()

    def _setup_dnd(self):
        if not DND_OK:
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as exc:
            log_error("注册拖拽失败: %r" % (exc,))

    # ---------------- 参数读取 ---------------- #
    def _edge_value(self) -> int:
        idx = self.edge_box.current()
        if idx <= 0:
            return 0
        try:
            return int(EDGE_CHOICES[idx].split()[0])
        except Exception:
            return 0

    def _fmt_value(self) -> str:
        idx = self.fmt_box.current()
        return FMT_VALUES[idx] if 0 <= idx < len(FMT_VALUES) else "auto"

    def _target_bytes(self):
        if not self.use_target.get():
            return 0
        raw = self.tgt_text.get().strip().replace("，", "").replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            return 0
        if v <= 0:
            return 0
        mult = 1024 * 1024 if self.tgt_unit.get() == "MB" else 1024
        return int(v * mult)

    def _collect_opts(self) -> dict:
        return {
            "quality": float(self.q_var.get()),
            "target_bytes": self._target_bytes(),
            "max_edge": self._edge_value(),
            "out_format": self._fmt_value(),
            "keep_meta": bool(self.keep_meta.get()),
        }

    def _update_controls_state(self):
        # 启用目标体积时，质量滑块仅供参考，置灰
        state = "disabled" if self.use_target.get() else "normal"
        for w in (self.scale, self.q_entry):
            try:
                w.configure(state=state)
            except Exception:
                pass

        fmt = self._fmt_value()
        if self.use_target.get():
            self.q_hint.configure(text="已按目标体积自动反解质量参数，滑块数值仅作参考")
        elif fmt == "PNG":
            n = png_colors_for(float(self.q_var.get()))
            if n >= PNG_HI:
                self.q_hint.configure(text="PNG 无损优化，体积通常只能下降 5%~15%")
            else:
                self.q_hint.configure(
                    text="PNG 有损：量化至 %d 色（索引图体积下降有限，若要显著变小请选 WEBP）" % n
                )
        else:
            self.q_hint.configure(text="质量百分比：数值越低体积越小，100% 为近乎无损")

        if not DND_OK:
            self.dnd_hint.configure(text="拖拽不可用")

    # ---------------- 文件管理 ---------------- #
    def _scan_folder(self, folder) -> list:
        found = []
        for root_dir, _dirs, names in os.walk(folder):
            for n in names:
                if Path(n).suffix.lower() in IMG_EXTS:
                    found.append(os.path.join(root_dir, n))
        return sorted(found)

    def add_paths(self, paths):
        added = 0
        for p in paths:
            if os.path.isdir(p):
                items = self._scan_folder(p)
            elif os.path.isfile(p) and Path(p).suffix.lower() in IMG_EXTS:
                items = [p]
            else:
                items = []
            for it in items:
                if it not in self.files:
                    self.files.append(it)
                    self.listbox.insert("end", "%s  (%s)" % (os.path.basename(it), human(os.path.getsize(it))))
                    added += 1
        if self.listbox.size() and not self.listbox.curselection():
            self.listbox.selection_set(0)
        if added:
            self.status.configure(text="已添加 %d 张" % added)
            self._kick_preview()

    def pick_files(self):
        paths = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[("图片", "*.jpg *.jpeg *.jpe *.png *.webp *.bmp *.tif *.tiff"), ("所有文件", "*.*")],
        )
        if paths:
            self.add_paths(list(paths))

    def pick_folder(self):
        d = filedialog.askdirectory(title="选择文件夹")
        if d:
            self.add_paths([d])

    def clear_files(self):
        self.files.clear()
        self.listbox.delete(0, "end")
        self.thumb_a = self.thumb_b = None
        self._render_empty()
        self.summary.configure(text="请添加图片")
        self.status.configure(text="")

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self.add_paths([p for p in paths if p])

    def _current_file(self):
        sel = self.listbox.curselection()
        if sel:
            i = sel[0]
            if 0 <= i < len(self.files):
                return self.files[i]
        return self.files[0] if self.files else None

    def _on_select(self, _event=None):
        self._kick_preview()

    # ---------------- 参数变更 -> 预览 ---------------- #
    def _on_scale(self, _val=None):
        v = int(round(self.q_var.get()))
        self.q_show.set(str(v))
        self._on_params_changed()

    def _commit_quality(self, _event=None):
        raw = self.q_show.get().strip().rstrip("%").rstrip("％").strip()
        try:
            v = float(raw)
        except ValueError:
            v = None
        if v is None:
            self.q_show.set(str(int(round(self.q_var.get()))))
            return
        v = max(1.0, min(100.0, v))
        self.q_var.set(v)
        self.q_show.set("%g" % v)
        self._on_params_changed()

    def _on_params_changed(self, _event=None):
        self._update_controls_state()
        if self.job:
            try:
                self.root.after_cancel(self.job)
            except Exception:
                pass
        self.job = self.root.after(380, self._kick_preview)

    def _kick_preview(self):
        self.job = None
        path = self._current_file()
        if not path:
            self._render_empty()
            return
        self.preview_seq += 1
        seq = self.preview_seq
        opts = self._collect_opts()
        self.status.configure(text="计算中…")
        threading.Thread(target=self._preview_worker, args=(seq, path, opts), daemon=True).start()

    def _preview_worker(self, seq, path, opts):
        # 串行化：同一时刻只跑一次编码；已过期的任务直接放弃，避免快速拖动时线程堆积
        with self.preview_lock:
            if seq != self.preview_seq:
                return
            try:
                res = process_one(path, opts, want_preview=True)
            except Exception as exc:
                log_error("预览失败 %s\n%s" % (path, traceback.format_exc()))
                self.q.put(("error", path, str(exc)))
                return
        if seq != self.preview_seq:
            return
        self.q.put(("preview", path, res))

    # ---------------- 消息泵 ---------------- #
    def _pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "preview":
                    self._show_preview(msg[1], msg[2])
                elif kind == "error":
                    self.status.configure(text="处理失败：%s" % msg[2])
                elif kind == "progress":
                    self.pb.configure(value=msg[1])
                    self.status.configure(text=msg[2])
                elif kind == "done":
                    self._on_batch_done(msg[1])
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def _show_preview(self, path, res):
        if self._current_file() != path:
            return  # 结果已过期
        self.thumb_a = res.get("preview_a")
        self.thumb_b = res.get("preview_b")
        self._redraw_thumbs()

        ratio = res["src_bytes"] / float(res["out_bytes"]) if res["out_bytes"] else 0
        delta = (1 - res["out_bytes"] / float(res["src_bytes"])) * 100 if res["src_bytes"] else 0
        txt = "%s → %s  (↓%.1f%%)" % (human(res["src_bytes"]), human(res["out_bytes"]), delta)
        if res["note"]:
            txt += "  " + res["note"]
        if not res["hit"]:
            txt += "  ⚠ 参数已到底仍未达标，建议缩小尺寸或改用 WEBP"
        elif res["out_bytes"] > res["src_bytes"]:
            txt += "  ⚠ 输出大于原文件，请降低质量或关闭目标限制"
        self.summary.configure(text=txt)

        if res.get("passthrough"):
            qtxt = "未重编码"
        elif res["fmt"] == "PNG":
            qtxt = "无损" if res["param"] >= PNG_HI else "索引 %d 色（约 %.0f%%）" % (res["param"], res["quality"])
        else:
            qtxt = "质量 %.0f%%" % res["quality"]
        self.status.configure(text="%s · %s · %s" % (res["fmt"], qtxt, "%.1f×" % ratio if ratio else "—"))

        if res["src_px"] != res["out_px"]:
            self.info_a.configure(text="原图 %d×%d" % res["src_px"])
            self.info_b.configure(text="输出 %d×%d · %s" % (res["out_px"][0], res["out_px"][1], human(res["out_bytes"])))
        else:
            self.info_a.configure(text="原图 %d×%d · %s" % (res["src_px"][0], res["src_px"][1], human(res["src_bytes"])))
            self.info_b.configure(text="输出 %d×%d · %s" % (res["out_px"][0], res["out_px"][1], human(res["out_bytes"])))

    def _render_empty(self):
        for cv in (self.canvas_a, self.canvas_b):
            cv.delete("all")
        self.info_a.configure(text="原图 —")
        self.info_b.configure(text="压缩后 —")

    # ---------------- 缩略图绘制 ---------------- #
    def _draw(self, canvas, img):
        canvas.delete("all")
        if img is None:
            return
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw <= 1:
            cw = 240
        if ch <= 1:
            ch = 150
        view = img.copy()
        view.thumbnail((max(20, cw - 10), max(20, ch - 10)), Image.Resampling.LANCZOS)
        ph = ImageTk.PhotoImage(view)
        canvas.create_image(cw // 2, ch // 2, image=ph)
        canvas.image = ph  # 保持引用

    def _redraw_thumbs(self):
        self._draw(self.canvas_a, self.thumb_a)
        self._draw(self.canvas_b, self.thumb_b)

    def _on_configure(self, event):
        if event.widget is not self.root:
            return
        size = (event.width, event.height)
        if size == self._last_size:
            return
        self._last_size = size
        if self.rz_job:
            try:
                self.root.after_cancel(self.rz_job)
            except Exception:
                pass
        self.rz_job = self.root.after(220, self._redraw_thumbs)

    # ---------------- 批量处理 ---------------- #
    def run_batch(self):
        if self.running:
            self.cancel = True
            self.status.configure(text="正在停止…")
            return
        if not self.files:
            messagebox.showinfo("提示", "请先添加图片。")
            return

        opts = self._collect_opts()
        if opts["target_bytes"] == 0 and opts["quality"] <= 0:
            return

        self.running = True
        self.cancel = False
        self.btn_run.configure(text="停止")
        self.pb.configure(maximum=len(self.files), value=0)
        files = list(self.files)
        threading.Thread(target=self._batch_worker, args=(files, opts), daemon=True).start()

    def _batch_worker(self, files, opts):
        ok, fail, skipped = 0, 0, 0
        out_dir = None
        total_in, total_out = 0, 0
        errors = []

        for i, path in enumerate(files, 1):
            if self.cancel:
                skipped = len(files) - i + 1
                break
            self.q.put(("progress", i - 1, "正在压缩 %d/%d：%s" % (i, len(files), os.path.basename(path))))
            try:
                res = process_one(path, opts)
                d = Path(path).parent / "compressed"
                d.mkdir(parents=True, exist_ok=True)
                out_dir = d
                dst = output_path_for(path, res["fmt"], d)
                with open(dst, "wb") as fh:
                    fh.write(res["data"])
                ok += 1
                total_in += res["src_bytes"]
                total_out += res["out_bytes"]
            except Exception as exc:
                fail += 1
                errors.append("%s: %s" % (os.path.basename(path), exc))
                log_error("压缩失败 %s\n%s" % (path, traceback.format_exc()))
            self.q.put(("progress", i, None))

        self.q.put(("done", {
            "ok": ok, "fail": fail, "skipped": skipped,
            "out_dir": out_dir, "total_in": total_in, "total_out": total_out,
            "errors": errors,
        }))

    def _on_batch_done(self, info):
        self.running = False
        self.btn_run.configure(text="压缩并保存")
        self.last_out_dir = info["out_dir"]
        if self.last_out_dir:
            self.btn_out.configure(state="normal")

        parts = ["成功 %d 张" % info["ok"]]
        if info["fail"]:
            parts.append("失败 %d 张" % info["fail"])
        if info["skipped"]:
            parts.append("已取消 %d 张" % info["skipped"])
        if info["total_in"]:
            parts.append("%s → %s" % (human(info["total_in"]), human(info["total_out"])))
        self.status.configure(text=" · ".join(parts))

        if info["ok"] and info["total_in"]:
            saved = (1 - info["total_out"] / float(info["total_in"])) * 100
            self.summary.configure(text="已完成 %d 张，总体积减少 %.1f%%" % (info["ok"], saved))
        if info["errors"]:
            messagebox.showwarning("部分文件处理失败", "\n".join(info["errors"][:8]))

    def open_outdir(self):
        d = self.last_out_dir
        if not d or not Path(d).is_dir():
            p = self._current_file()
            d = (Path(p).parent / "compressed") if p else None
        if d and Path(d).is_dir():
            try:
                os.startfile(str(d))  # noqa: S606  (Windows)
            except Exception as exc:
                messagebox.showinfo("提示", "无法打开目录：%s" % exc)
        else:
            messagebox.showinfo("提示", "还没有输出目录，请先执行压缩。")


# --------------------------------------------------------------------------- #
def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    enable_dpi_awareness()
    root = TkinterDnD.Tk() if DND_OK else tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    app = App(root)
    root.bind("<Configure>", app._on_configure)

    # 按内容请求尺寸定窗口大小，并收进屏幕可用区域，避免底部按钮被裁掉
    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = max(520, min(int(w), sw - 80))
    h = max(600, min(int(h), sh - 120))
    root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error(traceback.format_exc())
        try:
            import tkinter.messagebox as mb

            mb.showerror("程序错误", traceback.format_exc()[-1800:])
        except Exception:
            pass
