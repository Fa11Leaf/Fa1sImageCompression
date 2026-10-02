# 图片压缩工具

一个 Windows 桌面图片压缩小工具。单个窗口、无多级菜单，用**滑块 / 百分比数字 / 目标体积**三种方式控制压缩程度，实时显示压缩后的真实体积与左右对比预览。

零安装：依赖打进单文件 exe，双击即用；也可以直接用源码运行。

- 系统要求：Windows 10 / 11（64 位）
- 体积：exe 约 18 MB，源码版依赖约 17 MB
- 依赖：Python 3.10+、Pillow、tkinterdnd2（tkinter 为标准库自带）

---

## 功能

| 功能 | 说明 |
|---|---|
| 质量调节 | 滑块与数字框双向绑定，可直接键入 `58` 或 `58%`，回车/失焦生效，越界自动钳制 |
| 目标体积 | 勾选后填写目标上限（KB / MB），程序反解压缩参数逼近该值 |
| 最长边缩放 | 不缩放 / 2560 / 1920 / 1600 / 1280 / 1080 / 800 / 640 px，这是让体积显著下降最有效的手段 |
| 输出格式 | 保持原格式 / JPEG / PNG / WEBP / 自动（优先小体积） |
| 批量处理 | 多选文件、拖入文件或整个文件夹（递归扫描），统一按当前参数处理 |
| 实时预览 | 调整参数后 380 ms 防抖，后台线程真实编码一次，回填准确体积与对比缩略图 |
| 输出策略 | 结果统一写入源文件所在目录的 `compressed\`，重名自动追加 `_1`、`_2`；**原文件绝不改动** |

---

## 使用

### 方式一：直接运行 exe

`release\图片压缩工具.exe` 双击即可。首次启动需要把内嵌依赖解压到 `%TEMP%`，会慢 2–4 秒，之后启动很快。

### 方式二：源码运行

```bat
:: 1. 安装依赖到本目录的 libs\（需要在 PATH 中有带 tkinter 的 Python 3）
setup.bat

:: 2. 启动
::    双击 图片压缩器.vbs（无黑色控制台窗口）
```

也可以手动安装：

```bash
python -m pip install --target ./libs -r requirements.txt
pythonw image_compressor.pyw
```

程序启动时会把同级的 `libs\` 插入 `sys.path`，不写入注册表、不改动系统环境。

---

## 参数说明

### 质量百分比 ≠ 体积百分比

JPEG 的 `quality` 与文件体积**不是线性关系**。quality 从 100 降到 58 通常只减少约 20%–35% 体积，而不是 42%；但从 58 降到 40 可能再砍掉一半。所以滑块上的百分比是**质量百分比**（视觉保真度），不是体积百分比。

### 目标体积是反解出来的

需要"压到 200 KB 以内"时，程序对参数做二分搜索：编码到内存 → 量实际字节数 → 调整参数 → 迭代，收敛到目标体积的 ±3% 以内即停。JPEG 还会在 quality 不变的前提下逐级提升色度采样精度，把剩余体积预算花在画质上。

若最激进的参数仍然超标，程序会明确提示"参数已到底仍未达标"，**不会静默输出一个超标文件**。

### 各格式的实际行为

| 格式 | 质量滑块的作用 |
|---|---|
| JPEG | 直接映射 `quality`，并按值自动切换色度采样：≥90 用 4:4:4，≥75 用 4:2:2，<75 用 4:2:0；全程开启 `optimize` + `progressive` |
| PNG | 100% 时走无损优化（`optimize` + `compress_level=9`）；低于 100% 时按幂函数量化到 2–256 色，有透明通道用 FASTOCTREE，无透明用 MEDIANCUT + Floyd-Steinberg 抖动 |
| WebP | 直接映射 `quality`，`method=6` |

---

## 已知限制

**其一，PNG 有损量化的效果上限很低。** PNG 索引图的体积主要由索引值的分布集中度决定，而不是颜色数本身，所以实测体积呈阶梯分布（示例：1600×1200 的照片，2 色 56 KB / 8 色 205 KB / 16 色 259 KB / 32 色 340 KB，而 48 色到 256 色之间几乎恒定在 364 KB）。滑块在 50%–90% 区间的 PNG 体积变化会很小，只有拉到 30% 以下才明显。这是 PNG 格式的固有特性，任何映射函数都消除不了。**若对 PNG 压缩率有要求，请改选 WEBP 输出**：同一张图 WebP `q=58` 为 266 KB，画质远好于 20 色 PNG。

**其二，JPEG 低质量区每档跨度大。** 整数 quality 的粒度导致低质量区相邻两档体积可能相差 15%–20%（例如 q=16 → 168 KB，q=17 → 212 KB），因此目标体积的命中结果通常略小于目标值。

**其三，质量拉到 83% 以上时输出可能反而大于原文件。** 界面会给出警告，此时应当降低质量或放弃目标限制。

**不支持** HEIC / AVIF / GIF 动图逐帧优化。

---

## 目录结构

```
.
├── image_compressor.pyw   主程序（单文件，约 42 KB）
├── 图片压缩器.vbs         双击启动，无控制台窗口
├── app.ico                应用图标
├── build_exe.py           打包脚本（PyInstaller）
├── version_info.txt       exe 版本信息
├── requirements.txt       运行依赖
├── setup.bat              一键把依赖装进 .\libs
├── LICENSE                本项目许可（MIT）
├── THIRD-PARTY-NOTICES.txt 第三方组件版权与许可声明
├── licenses\              各依赖的完整许可原件
└── libs\                  本地依赖（.gitignore 已排除，需自行安装）
```

---

## 自行打包 exe

```bash
python -m pip install pyinstaller
python build_exe.py
# 产物：..\release\图片压缩工具.exe
```

打包脚本会处理一个易踩的坑：**tkinterdnd2 自带多个平台的 tkdnd 二进制，而 PyInstaller 没有对应的官方 hook**，必须手工把当前平台所需的那一份挂进包内。目录名还随 Tcl 主版本变化（Tcl 8 → `win-x64`，Tcl 9 → `win-x64-tcl9`），Python 3.13+ 自带 Tcl/Tk 9，取的是后者。`build_exe.py` 会按 `PROCESSOR_ARCHITECTURE` 与 `tkinter.TkVersion` 自动选择。

---

## 许可

本项目采用 **MIT 许可**，全文见 [LICENSE](LICENSE)。

打包出的 exe 内嵌了若干第三方组件（Python 运行时、Tcl/Tk、Pillow 及其编解码
库、tkinterdnd2 / tkdnd、PyInstaller bootloader）。它们的版权声明与许可条款见
[THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt)，完整许可原件在 `licenses\`。
分发 exe 时请一并附带这两个文件。

两点值得说明：

- 所有运行期依赖均为**宽松许可**（MIT / MIT-CMU / PSF / BSD 风格），**无 copyleft 传染**，
  因此本项目的许可选择不受限制。
- PyInstaller 本体是 GPLv2，但附有 **Bootloader Exception**，明确允许将它的
  bootloader 嵌入其他程序、并以你自己的条款分发该组合。因此本程序不受 GPL 约束。
