"""
PSD/PSB 预览器 - 主 GUI 界面
基于 PySide6 构建

新增功能：
- 文件夹浏览：左右方向键 / 工具栏按钮切换同目录内的 PSD/PSB 文件
- 滚轮缩放：直接滚轮缩放（无需按 Ctrl）
- 左键拖拽：放大后可用左键拖动平移图像
"""

import sys
import os
from pathlib import Path
from typing import Optional, List

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QScrollArea, QStatusBar,
    QSizePolicy, QFrame, QSplitter, QProgressBar, QMessageBox,
    QToolBar,
)
from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer, QPoint
from PySide6.QtGui import (
    QPixmap, QImage, QDragEnterEvent, QDropEvent,
    QAction, QKeySequence, QWheelEvent, QCursor,
)
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
from psd_renderer import PSDRenderer


# ─────────────────────────────────────────────
# 后台加载线程
# ─────────────────────────────────────────────

class LoadWorker(QThread):
    finished = Signal(object, dict)   # (PIL.Image, meta_dict)
    error    = Signal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path

    def run(self):
        try:
            renderer = PSDRenderer()
            meta  = renderer.load(self.file_path)
            image = renderer.composite()
            self.finished.emit(image, meta)
        except Exception as e:
            self.error.emit(str(e))


# ─────────────────────────────────────────────
# 图像查看器（缩放 + 左键拖拽平移）
# ─────────────────────────────────────────────

class ImageViewer(QScrollArea):
    """
    - 滚轮直接缩放（无需 Ctrl）
    - 左键拖拽平移（放大后）
    - 中键拖拽平移（任何时候）
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setWidgetResizable(False)
        self.setStyleSheet("background-color: #2b2b2b; border: none;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setWidget(self._label)

        self._pixmap: Optional[QPixmap] = None
        self._scale: float  = 1.0
        self._drag_pos: Optional[QPoint] = None
        self._is_fit: bool  = True   # 当前是否处于"适应窗口"状态

    # ── 公共接口 ────────────────────────────────

    def set_image(self, pil_image: Image.Image):
        # 如果图像超过 4K，先降采样到 4K 再转 QPixmap，渲染更快
        MAX_PX = 4096
        if pil_image.width > MAX_PX or pil_image.height > MAX_PX:
            pil_image = pil_image.copy()
            pil_image.thumbnail((MAX_PX, MAX_PX), Image.BILINEAR)

        # 确保是 RGBA
        if pil_image.mode != "RGBA":
            pil_image = pil_image.convert("RGBA")

        data   = pil_image.tobytes("raw", "RGBA")
        qimage = QImage(data, pil_image.width, pil_image.height,
                        pil_image.width * 4, QImage.Format_RGBA8888)
        # 保留 data 引用防止被 GC 回收
        qimage._data_ref = data
        self._pixmap  = QPixmap.fromImage(qimage)
        self._is_fit  = True
        self._fit_to_window()

    def zoom_in(self):
        self._is_fit = False
        self._set_scale(min(self._scale * 1.25, 16.0))

    def zoom_out(self):
        self._is_fit = False
        self._set_scale(max(self._scale / 1.25, 0.02))

    def zoom_reset(self):
        self._is_fit = False
        self._scale  = 1.0
        self._update_display()

    def zoom_fit(self):
        self._is_fit = True
        self._fit_to_window()

    def clear(self):
        self._pixmap = None
        self._label.clear()
        self._label.resize(0, 0)

    @property
    def scale(self) -> float:
        return self._scale

    # ── 内部 ────────────────────────────────────

    def _fit_to_window(self):
        if self._pixmap is None:
            return
        vw = self.viewport().width()  - 4
        vh = self.viewport().height() - 4
        if vw <= 0 or vh <= 0:
            return
        self._scale = min(vw / self._pixmap.width(),
                          vh / self._pixmap.height(), 1.0)
        self._update_display()

    def _set_scale(self, scale: float):
        self._scale = scale
        self._update_display()

    def _update_display(self):
        if self._pixmap is None:
            return
        w      = max(1, int(self._pixmap.width()  * self._scale))
        h      = max(1, int(self._pixmap.height() * self._scale))
        scaled = self._pixmap.scaled(w, h, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())
        # 更新光标：放大超过适应比例时显示手型
        if self._scale > self._fit_scale():
            self.viewport().setCursor(Qt.OpenHandCursor)
        else:
            self.viewport().setCursor(Qt.ArrowCursor)

    def _fit_scale(self) -> float:
        if self._pixmap is None:
            return 1.0
        vw = self.viewport().width()
        vh = self.viewport().height()
        if vw <= 0 or vh <= 0:
            return 1.0
        return min(vw / self._pixmap.width(),
                   vh / self._pixmap.height(), 1.0)

    # ── 以鼠标位置为中心缩放 ────────────────────

    def _zoom_at(self, factor: float, mouse_pos: QPoint):
        """以鼠标所在位置为锚点缩放"""
        old_scale  = self._scale
        new_scale  = max(0.02, min(16.0, old_scale * factor))
        if abs(new_scale - old_scale) < 1e-6:
            return

        # 计算鼠标在图像坐标系的位置
        h_bar = self.horizontalScrollBar()
        v_bar = self.verticalScrollBar()
        img_x = (h_bar.value() + mouse_pos.x()) / old_scale
        img_y = (v_bar.value() + mouse_pos.y()) / old_scale

        self._is_fit = False
        self._scale  = new_scale
        self._update_display()

        # 恢复锚点（让鼠标下方的像素保持不动）
        h_bar.setValue(int(img_x * new_scale - mouse_pos.x()))
        v_bar.setValue(int(img_y * new_scale - mouse_pos.y()))

    # ── 事件 ────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent):
        if self._pixmap is None:
            super().wheelEvent(event)
            return
        delta  = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1 / 1.15
        self._zoom_at(factor, event.position().toPoint())
        event.accept()

    def mousePressEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._drag_pos = event.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            delta = event.pos() - self._drag_pos
            self._drag_pos = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._drag_pos = None
            if self._scale > self._fit_scale():
                self.viewport().setCursor(Qt.OpenHandCursor)
            else:
                self.viewport().setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pixmap and self._is_fit:
            QTimer.singleShot(0, self._fit_to_window)


# ─────────────────────────────────────────────
# 文件信息面板
# ─────────────────────────────────────────────

class InfoPanel(QFrame):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(200)
        self.setMaximumWidth(260)
        self.setStyleSheet("""
            QFrame { background-color: #1e1e1e; border-right: 1px solid #3a3a3a; }
            QLabel { color: #cccccc; font-size: 12px; padding: 2px 0; }
            QLabel#title { color: #ffffff; font-size: 13px; font-weight: bold; }
            QLabel#key   { color: #888888; font-size: 11px; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(4)

        title = QLabel("文件信息")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addSpacing(8)

        self._labels: dict = {}
        for key, display in [
            ("file_name",  "文件名"),
            ("file_size",  "文件大小"),
            ("width",      "宽度"),
            ("height",     "高度"),
            ("color_mode", "颜色模式"),
            ("bit_depth",  "位深度"),
            ("layer_count","图层数"),
        ]:
            lk = QLabel(display); lk.setObjectName("key")
            layout.addWidget(lk)
            lv = QLabel("—"); lv.setWordWrap(True)
            self._labels[key] = lv
            layout.addWidget(lv)
            layout.addSpacing(4)

        layout.addStretch()

        self._export_btn = QPushButton("导出为 PNG")
        self._export_btn.setEnabled(False)
        self._export_btn.setStyleSheet("""
            QPushButton { background-color:#0078d4; color:white; border:none;
                          border-radius:4px; padding:8px; font-size:12px; }
            QPushButton:hover    { background-color:#106ebe; }
            QPushButton:disabled { background-color:#3a3a3a; color:#666; }
        """)
        layout.addWidget(self._export_btn)

    def update_meta(self, meta: dict):
        self._labels["file_name"].setText(meta.get("file_name", "—"))
        sz = meta.get("file_size", 0)
        self._labels["file_size"].setText(
            f"{sz/1024/1024:.1f} MB" if sz > 1024*1024 else f"{sz/1024:.1f} KB")
        self._labels["width"].setText(f"{meta.get('width','—')} px")
        self._labels["height"].setText(f"{meta.get('height','—')} px")
        self._labels["color_mode"].setText(meta.get("color_mode", "—"))
        self._labels["bit_depth"].setText(str(meta.get("bit_depth", "—")))
        self._labels["layer_count"].setText(str(meta.get("layer_count", "—")))
        self._export_btn.setEnabled(True)

    def clear(self):
        for lbl in self._labels.values():
            lbl.setText("—")
        self._export_btn.setEnabled(False)

    @property
    def export_button(self):
        return self._export_btn


# ─────────────────────────────────────────────
# 拖放占位提示
# ─────────────────────────────────────────────

class DropPlaceholder(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        for text, style in [
            ("📂", "font-size:64px;"),
            ("将 PSD / PSB 文件拖放到此处",
             "color:#aaa;font-size:15px;margin-top:12px;"),
            ("或点击工具栏「打开文件」按钮",
             "color:#666;font-size:12px;"),
        ]:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(style)
            layout.addWidget(lbl)


# ─────────────────────────────────────────────
# 主窗口
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PSD / PSB 预览器")
        self.resize(1200, 800)
        self.setMinimumSize(800, 600)
        self.setAcceptDrops(True)

        self._current_image: Optional[Image.Image] = None
        self._worker: Optional[LoadWorker] = None

        # ── 文件夹浏览状态 ──
        self._folder_files: List[str] = []   # 当前目录内所有 PSD/PSB
        self._folder_index: int = -1          # 当前文件在列表中的位置

        # ── 预加载缓存（相邻文件） ──
        self._preload_cache: dict = {}        # {file_path: PIL.Image}
        self._preload_worker: Optional[LoadWorker] = None

        self._setup_style()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

    # ── 样式 ────────────────────────────────────

    def _setup_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color:#252526; }
            QToolBar {
                background-color:#2d2d2d; border-bottom:1px solid #3a3a3a;
                spacing:4px; padding:4px;
            }
            QToolBar QToolButton {
                color:#cccccc; background:transparent; border:none;
                border-radius:4px; padding:6px 10px; font-size:12px;
            }
            QToolBar QToolButton:hover    { background-color:#3e3e42; }
            QToolBar QToolButton:pressed  { background-color:#4a4a50; }
            QToolBar QToolButton:disabled { color:#555; }
            QStatusBar { background-color:#007acc; color:white; font-size:11px; }
            QProgressBar { background-color:#3e3e42; border:none; height:3px; }
            QProgressBar::chunk { background-color:#007acc; }
        """)

    # ── 工具栏 ──────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        # 打开
        act_open = QAction("📂  打开文件", self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self._open_file_dialog)
        tb.addAction(act_open)

        tb.addSeparator()

        # ← 上一个
        self._act_prev = QAction("◀  上一个", self)
        self._act_prev.setShortcut(QKeySequence(Qt.Key_Left))
        self._act_prev.setEnabled(False)
        self._act_prev.triggered.connect(self._prev_file)
        tb.addAction(self._act_prev)

        # 文件计数标签
        self._nav_label = QLabel("  —  ")
        self._nav_label.setStyleSheet("color:#aaa; font-size:12px; padding:0 4px;")
        tb.addWidget(self._nav_label)

        # → 下一个
        self._act_next = QAction("下一个  ▶", self)
        self._act_next.setShortcut(QKeySequence(Qt.Key_Right))
        self._act_next.setEnabled(False)
        self._act_next.triggered.connect(self._next_file)
        tb.addAction(self._act_next)

        tb.addSeparator()

        # 缩放
        act_zoom_in = QAction("🔍+  放大", self)
        act_zoom_in.setShortcut(QKeySequence("Ctrl+="))
        act_zoom_in.triggered.connect(lambda: self._viewer.zoom_in())
        tb.addAction(act_zoom_in)

        act_zoom_out = QAction("🔍-  缩小", self)
        act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        act_zoom_out.triggered.connect(lambda: self._viewer.zoom_out())
        tb.addAction(act_zoom_out)

        act_zoom_fit = QAction("⊡  适应窗口", self)
        act_zoom_fit.setShortcut(QKeySequence("Ctrl+0"))
        act_zoom_fit.triggered.connect(lambda: self._viewer.zoom_fit())
        tb.addAction(act_zoom_fit)

        act_zoom_100 = QAction("⊞  100%", self)
        act_zoom_100.setShortcut(QKeySequence("Ctrl+1"))
        act_zoom_100.triggered.connect(lambda: self._viewer.zoom_reset())
        tb.addAction(act_zoom_100)

    # ── 中央布局 ────────────────────────────────

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)

        self._info_panel = InfoPanel()
        self._info_panel.export_button.clicked.connect(self._export_png)
        splitter.addWidget(self._info_panel)

        right = QWidget()
        rl    = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self._placeholder = DropPlaceholder()
        self._placeholder.setStyleSheet("background-color:#2b2b2b;")
        rl.addWidget(self._placeholder)

        self._viewer = ImageViewer()
        self._viewer.hide()
        rl.addWidget(self._viewer)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

    # ── 状态栏 ──────────────────────────────────

    def _build_statusbar(self):
        self._status = QStatusBar()
        self.setStatusBar(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setMaximumWidth(120)
        self._progress.hide()
        self._status.addPermanentWidget(self._progress)

        self._zoom_label = QLabel("缩放: 100%")
        self._zoom_label.setStyleSheet("color:white; margin-right:8px;")
        self._status.addPermanentWidget(self._zoom_label)

        self._status.showMessage("就绪 — 打开或拖入 PSD/PSB 文件开始预览")
        QTimer.singleShot(200, self._tick_zoom_label)

    def _tick_zoom_label(self):
        pct = int(self._viewer.scale * 100)
        self._zoom_label.setText(f"缩放: {pct}%")
        QTimer.singleShot(200, self._tick_zoom_label)

    # ── 文件夹浏览 ──────────────────────────────

    def _scan_folder(self, file_path: str):
        """扫描 file_path 所在目录，收集所有 PSD/PSB 并定位当前文件"""
        folder = Path(file_path).parent
        files  = sorted(
            [str(p) for p in folder.iterdir()
             if p.suffix.lower() in (".psd", ".psb")],
            key=lambda x: x.lower()
        )
        self._folder_files = files
        try:
            self._folder_index = files.index(str(Path(file_path).resolve()))
        except ValueError:
            # 路径大小写不一致时做模糊匹配
            lp = str(Path(file_path).resolve()).lower()
            self._folder_index = next(
                (i for i, f in enumerate(files) if f.lower() == lp), 0)
        self._update_nav()

    def _update_nav(self):
        n   = len(self._folder_files)
        idx = self._folder_index
        has = n > 1
        self._act_prev.setEnabled(has and idx > 0)
        self._act_next.setEnabled(has and idx < n - 1)
        if n > 0:
            self._nav_label.setText(f"  {idx + 1} / {n}  ")
        else:
            self._nav_label.setText("  —  ")

    def _prev_file(self):
        if self._folder_index > 0:
            self._folder_index -= 1
            self._load_file(self._folder_files[self._folder_index],
                            scan_folder=False)

    def _next_file(self):
        if self._folder_index < len(self._folder_files) - 1:
            self._folder_index += 1
            self._load_file(self._folder_files[self._folder_index],
                            scan_folder=False)

    # ── 文件加载 ────────────────────────────────

    def _open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 PSD / PSB 文件", "",
            "Photoshop 文件 (*.psd *.psb);;所有文件 (*.*)")
        if path:
            self._load_file(path)

    def _load_file(self, path: str, scan_folder: bool = True):
        if self._worker and self._worker.isRunning():
            return

        if scan_folder:
            self._scan_folder(path)
        else:
            self._update_nav()

        # 命中预加载缓存：直接显示，无需等待
        if path in self._preload_cache:
            cached_img = self._preload_cache[path]
            self._progress.hide()
            self._current_image = cached_img
            # 元信息单独快速读取（只读 26 字节头）
            try:
                r = PSDRenderer()
                meta = r._read_header(path)
                meta["file_name"] = Path(path).name
                meta["file_size"] = os.path.getsize(path)
            except Exception:
                meta = {"file_name": Path(path).name, "file_size": 0,
                        "width": "—", "height": "—", "color_mode": "—",
                        "bit_depth": "—", "layer_count": "—"}
            self._info_panel.update_meta(meta)
            self._placeholder.hide()
            self._viewer.show()
            self._viewer.set_image(cached_img)
            self.setWindowTitle(f"PSD / PSB 预览器 — {Path(path).name}")
            n   = len(self._folder_files)
            idx = self._folder_index
            nav = f"  [{idx+1}/{n}]" if n > 1 else ""
            self._status.showMessage(
                f"已加载：{meta.get('file_name')}{nav}  —  "
                f"{meta.get('width')} × {meta.get('height')} px")
            QTimer.singleShot(100, self._preload_neighbors)
            return

        # 未命中缓存：正常后台加载
        self._progress.show()
        self._status.showMessage("正在加载…")
        self._placeholder.show()
        self._viewer.hide()
        self._viewer.clear()
        self._info_panel.clear()
        self.setWindowTitle(f"PSD / PSB 预览器 — {Path(path).name}")

        self._worker = LoadWorker(path)
        self._worker.finished.connect(self._on_load_finished)
        self._worker.error.connect(self._on_load_error)
        self._worker.start()

    def _on_load_finished(self, image: Image.Image, meta: dict):
        self._progress.hide()
        self._current_image = image
        self._info_panel.update_meta(meta)
        self._placeholder.hide()
        self._viewer.show()
        self._viewer.set_image(image)

        n   = len(self._folder_files)
        idx = self._folder_index
        nav = f"  [{idx+1}/{n}]" if n > 1 else ""
        self._status.showMessage(
            f"已加载：{meta.get('file_name')}{nav}  —  "
            f"{meta.get('width')} × {meta.get('height')} px")

        # 加载完成后，后台预加载前后相邻文件
        QTimer.singleShot(100, self._preload_neighbors)

    def _preload_neighbors(self):
        """后台静默预加载相邻文件，切换时直接从缓存取"""
        idx   = self._folder_index
        files = self._folder_files
        candidates = []
        if idx + 1 < len(files):
            candidates.append(files[idx + 1])
        if idx - 1 >= 0:
            candidates.append(files[idx - 1])

        # 只预加载还没缓存的
        to_load = [p for p in candidates if p not in self._preload_cache]
        if not to_load:
            return

        target = to_load[0]

        def _do_preload():
            try:
                r = PSDRenderer()
                r.load(target)
                img = r.composite()
                self._preload_cache[target] = img
                # 缓存最多保留 3 个
                if len(self._preload_cache) > 3:
                    oldest = next(iter(self._preload_cache))
                    del self._preload_cache[oldest]
            except Exception:
                pass

        import threading
        threading.Thread(target=_do_preload, daemon=True).start()

    def _on_load_error(self, error_msg: str):
        self._progress.hide()
        self._placeholder.show()
        self._viewer.hide()
        self._status.showMessage(f"加载失败: {error_msg}")
        QMessageBox.critical(self, "预览失败", error_msg)

    def _export_png(self):
        if self._current_image is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出图像", "",
            "PNG 图像 (*.png);;JPEG 图像 (*.jpg)")
        if not path:
            return
        try:
            img = self._current_image
            if Path(path).suffix.lower() in (".jpg", ".jpeg"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
                bg.save(path, "JPEG", quality=92)
            else:
                img.save(path, "PNG")
            self._status.showMessage(f"已导出到: {path}")
            QMessageBox.information(self, "导出成功", f"已保存到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ── 键盘事件（方向键切换） ────────────────────

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Left:
            self._prev_file()
        elif key == Qt.Key_Right:
            self._next_file()
        else:
            super().keyPressEvent(event)

    # ── 拖放 ────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith((".psd", ".psb")):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            fp = url.toLocalFile()
            if fp.lower().endswith((".psd", ".psb")):
                self._load_file(fp)
                break


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PSD Viewer")
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    window = MainWindow()
    window.show()

    if len(sys.argv) > 1:
        fp = sys.argv[1]
        if os.path.isfile(fp):
            window._load_file(fp)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
