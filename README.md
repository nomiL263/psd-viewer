# PSD / PSB 预览器

一个轻量的 Windows 桌面程序，无需安装 Photoshop 即可快速预览 `.psd` / `.psb` 文件。

## 功能

- 拖放或点击打开 PSD/PSB 文件
- 合并所有图层，展示最终合成效果
- 鼠标滚轮缩放（Ctrl + 滚轮）、中键拖拽平移
- 显示文件元信息（尺寸、图层数、色彩模式等）
- 一键导出为 PNG / JPEG
- 暗色 UI，高 DPI 支持

---

## 快速开始（运行源码）

### 环境要求

- Python 3.10+
- Windows 10/11（推荐）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 运行

```bash
python src/main.py
# 或传入文件路径（支持双击关联打开）
python src/main.py "C:\path\to\file.psd"
```

---

## 打包为 exe

### 方式一：双击批处理（Windows）

```
双击运行 build.bat
```

### 方式二：Python 脚本（跨平台）

```bash
python build.py
```

打包完成后，可执行文件位于 `dist/PSD_Viewer.exe`。

---

## 键盘快捷键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+O` | 打开文件 |
| `Ctrl+=` | 放大 |
| `Ctrl+-` | 缩小 |
| `Ctrl+0` | 适应窗口 |
| `Ctrl+1` | 100% 原始大小 |
| 中键拖拽 | 平移图像 |
| `Ctrl+滚轮` | 缩放图像 |

---

## 目录结构

```
psd-viewer/
├── src/
│   ├── main.py          # GUI 主窗口（PySide6）
│   └── psd_renderer.py  # PSD 解析 & 渲染核心（psd-tools）
├── assets/
│   └── icon.ico         # 程序图标（可替换）
├── requirements.txt
├── build.bat            # Windows 一键打包
├── build.py             # Python 打包脚本
└── README.md
```

---

## 依赖说明

| 库 | 用途 |
|----|------|
| `psd-tools` | 解析 PSD/PSB 格式，合并图层 |
| `Pillow` | 图像处理与格式转换 |
| `PySide6` | Qt 6 GUI 框架 |
| `PyInstaller` | 打包为独立 exe |

---

## 注意事项

- 超大文件（>500MB）首次加载可能需要数秒
- PSB（大型文档）支持取决于 psd-tools 版本，建议使用最新版
- 打包后 exe 体积约 80-120 MB（含 Qt 运行时）
