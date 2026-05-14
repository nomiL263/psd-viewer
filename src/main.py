"""
PSD/PSB 预览器 - 主 GUI 界面
基于 PySide6 构建
"""

import sys
import os
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFileDialog,
    QScrollArea,
    QStatusBar,
    QSizePolicy,
    QFrame,
    QSplitter,
    QProgressBar,
    QMessageBox,
    QToolBar,
    QSlider,
)
from PySide6.QtCore import (
    Qt,
    QThread,
    Signal,
    QSize,
    QTimer,
    QMimeData,
)
from PySide6.QtGui import (
    QPixmap,
    QImage,
    QDragEnterEvent,
    QDropEvent,
    QAction,
    QKeySequence,
    QIcon,
    QPalette,
    QColor,
    QFont,
    QWheelEvent,
)
from PIL import Image, ImageQt

# 添加 src 目录到 path
sys.path.insert(0, os.path.dirname(__file__))
from psd_renderer import PSDRenderer


# ─────────────────────────────────────────────
# 后台加载线程
# ─────────────────────────────────────────────

class LoadWorker(QThread):
    """后台线程：加载并合并 PSD 文件，避免阻塞 UI"""

    finished = Signal(object, dict)   # (PIL.Image, meta_dict)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path

    def run(self):
        try:
            renderer = PSDRenderer()

            self.progress.emit("正在解析文件结构…")
            meta = renderer.load(self.file_path)

            self.progress.emit(f"正在合并图层（{meta.get('layer_count', '?')} 个图层）…")
            image = renderer.composite()

            self.finished.emit(image, meta)
        except Exception as e:
            self.error.emit(str(e))


# ─────────────────────────────────────────────
# 可缩放图像显示区域
# ─────────────────────────────────────────────

class ImageViewer(QScrollArea):
    """支持鼠标滚轮缩放、拖拽平移的图像查看器"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setWidgetResizable(False)
        self.setStyleSheet("background-color: #2b2b2b; border: none;")

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setWidget(self._label)

        self._pixmap: Optional[QPixmap] = None
        self._scale: float = 1.0
        self._drag_pos = None

        # 拖拽平移
        self._label.setMouseTracking(True)

    # ── 公共接口 ──────────────────────────────

    def set_image(self, pil_image: Image.Image):
        """接收 PIL Image，转换为 QPixmap 并显示"""
        # PIL RGBA → QImage
        data = pil_image.tobytes("raw", "RGBA")
        qimage = QImage(
            data,
            pil_image.width,
            pil_image.height,
            pil_image.width * 4,
            QImage.Format_RGBA8888,
        )
        self._pixmap = QPixmap.fromImage(qimage)
        self._scale = 1.0
        self._fit_to_window()

    def zoom_in(self):
        self._set_scale(min(self._scale * 1.25, 10.0))

    def zoom_out(self):
        self._set_scale(max(self._scale / 1.25, 0.05))

    def zoom_reset(self):
        self._scale = 1.0
        self._update_display()

    def zoom_fit(self):
        self._fit_to_window()

    def clear(self):
        self._pixmap = None
        self._label.clear()
        self._label.resize(0, 0)

    @property
    def scale(self) -> float:
        return self._scale

    # ── 内部方法 ──────────────────────────────

    def _fit_to_window(self):
        if self._pixmap is None:
            return
        vw = self.viewport().width() - 20
        vh = self.viewport().height() - 20
        if vw <= 0 or vh <= 0:
            return
        scale_w = vw / self._pixmap.width()
        scale_h = vh / self._pixmap.height()
        self._scale = min(scale_w, scale_h, 1.0)
        self._update_display()

    def _set_scale(self, scale: float):
        self._scale = scale
        self._update_display()

    def _update_display(self):
        if self._pixmap is None:
            return
        w = int(self._pixmap.width() * self._scale)
        h = int(self._pixmap.height() * self._scale)
        scaled = self._pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())

    # ── 事件 ──────────────────────────────────

    def wheelEvent(self, event: QWheelEvent):
        if self._pixmap is None:
            super().wheelEvent(event)
            return
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._drag_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            delta = event.pos() - self._drag_pos
            self._drag_pos = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._drag_pos = None
            self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 窗口大小变化时，如果当前是适应模式则重新适应
        if self._pixmap and self._scale <= 1.0:
            QTimer.singleShot(0, self._fit_to_window)


# ─────────────────────────────────────────────
# 文件信息面板
# ─────────────────────────────────────────────

class InfoPanel(QFrame):
    """左侧文件信息面板"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(200)
        self.setMaximumWidth(260)
        self.setStyleSheet("""
            QFrame {
                background-color: #1e1e1e;
                border-right: 1px solid #3a3a3a;
            }
            QLabel {
                color: #cccccc;
                font-size: 12px;
                padding: 2px 0;
            }
            QLabel#title {
                color: #ffffff;
                font-size: 13px;
                font-weight: bold;
            }
            QLabel#key {
                color: #888888;
                font-size: 11px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(4)

        title = QLabel("文件信息")
        title.setObjectName("title")
        layout.addWidget(title)

        layout.addSpacing(8)

        # 动态信息标签字典
        self._labels: dict = {}
        fields = [
            ("file_name", "文件名"),
            ("file_size", "文件大小"),
            ("width", "宽度"),
            ("height", "高度"),
            ("color_mode", "颜色模式"),
            ("bit_depth", "位深度"),
            ("layer_count", "图层数"),
        ]

        for key, display in fields:
            key_label = QLabel(display)
            key_label.setObjectName("key")
            layout.addWidget(key_label)

            val_label = QLabel("—")
            val_label.setWordWrap(True)
            self._labels[key] = val_label
            layout.addWidget(val_label)
            layout.addSpacing(4)

        layout.addStretch()

        # 导出按钮
        self._export_btn = QPushButton("导出为 PNG")
        self._export_btn.setEnabled(False)
        self._export_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #106ebe; }
            QPushButton:disabled { background-color: #3a3a3a; color: #666; }
        """)
        layout.addWidget(self._export_btn)

    def update_meta(self, meta: dict):
        """更新显示的元信息"""
        self._labels["file_name"].setText(meta.get("file_name", "—"))

        size = meta.get("file_size", 0)
        if size > 1024 * 1024:
            size_str = f"{size / 1024 / 1024:.1f} MB"
        else:
            size_str = f"{size / 1024:.1f} KB"
        self._labels["file_size"].setText(size_str)

        self._labels["width"].setText(f"{meta.get('width', '—')} px")
        self._labels["height"].setText(f"{meta.get('height', '—')} px")
        self._labels["color_mode"].setText(meta.get("color_mode", "—"))
        self._labels["bit_depth"].setText(f"{meta.get('bit_depth', '—')} bit")
        self._labels["layer_count"].setText(str(meta.get("layer_count", "—")))
        self._export_btn.setEnabled(True)

    def clear(self):
        for lbl in self._labels.values():
            lbl.setText("—")
        self._export_btn.setEnabled(False)

    @property
    def export_button(self) -> QPushButton:
        return self._export_btn


# ─────────────────────────────────────────────
# 拖拽覆盖层（文件为空时显示）
# ─────────────────────────────────────────────

class DropPlaceholder(QWidget):
    """当没有文件时，显示拖放提示"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        icon_lbl = QLabel("📂")
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 64px;")
        layout.addWidget(icon_lbl)

        tip1 = QLabel("将 PSD / PSB 文件拖放到此处")
        tip1.setAlignment(Qt.AlignCenter)
        tip1.setStyleSheet("color: #aaa; font-size: 15px; margin-top: 12px;")
        layout.addWidget(tip1)

        tip2 = QLabel("或点击工具栏「打开文件」按钮")
        tip2.setAlignment(Qt.AlignCenter)
        tip2.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(tip2)


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

        self._setup_style()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

    # ── 样式 ──────────────────────────────────

    def _setup_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #252526; }
            QToolBar {
                background-color: #2d2d2d;
                border-bottom: 1px solid #3a3a3a;
                spacing: 4px;
                padding: 4px;
            }
            QToolBar QToolButton {
                color: #cccccc;
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 12px;
            }
            QToolBar QToolButton:hover { background-color: #3e3e42; }
            QToolBar QToolButton:pressed { background-color: #4a4a50; }
            QStatusBar {
                background-color: #007acc;
                color: white;
                font-size: 11px;
            }
            QProgressBar {
                background-color: #3e3e42;
                border: none;
                height: 3px;
            }
            QProgressBar::chunk { background-color: #007acc; }
        """)

    # ── 工具栏 ────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        act_open = QAction("📂  打开文件", self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self._open_file_dialog)
        tb.addAction(act_open)

        tb.addSeparator()

        act_zoom_in = QAction("🔍+  放大", self)
        act_zoom_in.setShortcut(QKeySequence("Ctrl+="))
        act_zoom_in.triggered.connect(lambda: self._viewer.zoom_in() if hasattr(self, '_viewer') else None)
        tb.addAction(act_zoom_in)

        act_zoom_out = QAction("🔍-  缩小", self)
        act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        act_zoom_out.triggered.connect(lambda: self._viewer.zoom_out() if hasattr(self, '_viewer') else None)
        tb.addAction(act_zoom_out)

        act_zoom_fit = QAction("⊡  适应窗口", self)
        act_zoom_fit.setShortcut(QKeySequence("Ctrl+0"))
        act_zoom_fit.triggered.connect(lambda: self._viewer.zoom_fit() if hasattr(self, '_viewer') else None)
        tb.addAction(act_zoom_fit)

        act_zoom_100 = QAction("⊞  100%", self)
        act_zoom_100.setShortcut(QKeySequence("Ctrl+1"))
        act_zoom_100.triggered.connect(lambda: self._viewer.zoom_reset() if hasattr(self, '_viewer') else None)
        tb.addAction(act_zoom_100)

    # ── 中央布局 ──────────────────────────────

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)

        # 左侧信息面板
        self._info_panel = InfoPanel()
        self._info_panel.export_button.clicked.connect(self._export_png)
        splitter.addWidget(self._info_panel)

        # 右侧：堆叠（占位 / 图像查看器）
        self._right_stack = QWidget()
        right_layout = QVBoxLayout(self._right_stack)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._placeholder = DropPlaceholder()
        self._placeholder.setStyleSheet("background-color: #2b2b2b;")
        right_layout.addWidget(self._placeholder)

        self._viewer = ImageViewer()
        self._viewer.hide()
        right_layout.addWidget(self._viewer)

        splitter.addWidget(self._right_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)

    # ── 状态栏 ────────────────────────────────

    def _build_statusbar(self):
        self._status = QStatusBar()
        self.setStatusBar(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # 不确定进度
        self._progress.setMaximumWidth(120)
        self._progress.hide()
        self._status.addPermanentWidget(self._progress)

        self._zoom_label = QLabel("缩放: 100%")
        self._zoom_label.setStyleSheet("color: white; margin-right: 8px;")
        self._status.addPermanentWidget(self._zoom_label)

        self._status.showMessage("就绪 — 打开或拖入 PSD/PSB 文件开始预览")

    # ── 文件操作 ──────────────────────────────

    def _open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开 PSD / PSB 文件",
            "",
            "Photoshop 文件 (*.psd *.psb);;所有文件 (*.*)",
        )
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        if self._worker and self._worker.isRunning():
            return  # 正在加载中，忽略

        self._progress.show()
        self._status.showMessage("正在加载…")
        self._placeholder.show()
        self._viewer.hide()
        self._viewer.clear()
        self._info_panel.clear()
        self.setWindowTitle(f"PSD / PSB 预览器 — {Path(path).name}")

        self._worker = LoadWorker(path)
        self._worker.progress.connect(lambda msg: self._status.showMessage(msg))
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
        self._update_zoom_label()
        self._status.showMessage(
            f"已加载：{meta.get('file_name')}  —  "
            f"{meta.get('width')} × {meta.get('height')} px  |  "
            f"{meta.get('layer_count')} 个图层"
        )

    def _on_load_error(self, error_msg: str):
        self._progress.hide()
        self._placeholder.show()
        self._viewer.hide()
        self._status.showMessage(f"加载失败: {error_msg}")
        QMessageBox.critical(self, "预览失败", f"{error_msg}")

    def _export_png(self):
        if self._current_image is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 PNG", "", "PNG 图像 (*.png);;JPEG 图像 (*.jpg)"
        )
        if not path:
            return
        try:
            img = self._current_image
            ext = Path(path).suffix.lower()
            if ext in (".jpg", ".jpeg"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "RGBA":
                    bg.paste(img, mask=img.split()[3])
                else:
                    bg.paste(img)
                bg.save(path, "JPEG", quality=92)
            else:
                img.save(path, "PNG")
            self._status.showMessage(f"已导出到: {path}")
            QMessageBox.information(self, "导出成功", f"文件已保存到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ── 缩放标签更新 ──────────────────────────

    def _update_zoom_label(self):
        if hasattr(self, "_viewer"):
            pct = int(self._viewer.scale * 100)
            self._zoom_label.setText(f"缩放: {pct}%")
        QTimer.singleShot(200, self._update_zoom_label)

    # ── 拖放支持 ──────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            for url in urls:
                fp = url.toLocalFile()
                if fp.lower().endswith((".psd", ".psb")):
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
    app.setOrganizationName("PSDViewer")

    # 高 DPI 支持
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    window = MainWindow()
    window.show()

    # 支持命令行传入文件路径，双击文件打开
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
        if os.path.isfile(file_path):
            window._load_file(file_path)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
