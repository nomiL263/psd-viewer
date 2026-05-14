"""
PSD/PSB 预览器 - 主 GUI 界面

参照 2345看图王 / WPS图片 的加载逻辑：
  1. 打开文件立即读内嵌缩略图显示（< 0.3s，不卡）
  2. 后台线程合并图层得到高清图，完成后无缝替换
  3. 切换文件时立即取消旧任务，旧线程安全等待退出
  4. 大图自动限制传给 UI 的内存用量，防 OOM 闪退
  5. 所有跨线程数据通过 bytes 传递，避免 PIL Image 跨线程引发段错误
"""

import sys
import os
import gc
from pathlib import Path
from typing import Optional, List

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QScrollArea, QStatusBar,
    QSizePolicy, QFrame, QSplitter, QProgressBar, QMessageBox,
    QToolBar,
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QSize, QTimer, QPoint, QMutex,
)
from PySide6.QtGui import (
    QPixmap, QImage, QDragEnterEvent, QDropEvent,
    QAction, QKeySequence, QWheelEvent, QPainter, QColor, QPen,
)
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
from psd_renderer import PSDRenderer


# ══════════════════════════════════════════════
#  线程：第一阶段 —— 读内嵌缩略图（极速）
# ══════════════════════════════════════════════

class ThumbLoadWorker(QThread):
    """
    只读内嵌缩略图 + 文件头，通常 < 0.3s。
    用 bytes 传图，不跨线程传 PIL Image（防段错误）。
    """
    done  = Signal(bytes, int, int, dict)   # (rgba_bytes, w, h, meta)
    error = Signal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path  = file_path
        self._cancelled = False

    def cancel(self): self._cancelled = True

    def run(self):
        try:
            r = PSDRenderer()
            thumb, meta = r.load_thumbnail(self.file_path)
            if self._cancelled:
                return
            if thumb is not None:
                img = thumb.convert("RGBA")
                w, h = img.size
                raw  = img.tobytes("raw", "RGBA")
                del img; gc.collect()
                if not self._cancelled:
                    self.done.emit(raw, w, h, meta)
            else:
                # 没有内嵌缩略图，直接进入高清加载
                if not self._cancelled:
                    self.done.emit(b"", 0, 0, meta)
        except Exception as e:
            if not self._cancelled:
                self.error.emit(str(e))


# ══════════════════════════════════════════════
#  线程：第二阶段 —— 合并图层高清图（后台）
# ══════════════════════════════════════════════

class FullLoadWorker(QThread):
    """
    后台合并所有图层，得到高清图。
    同样用 bytes 传图，防跨线程段错误。
    """
    done  = Signal(bytes, int, int, str)   # (rgba_bytes, w, h, file_path)
    error = Signal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path  = file_path
        self._cancelled = False

    def cancel(self): self._cancelled = True

    def run(self):
        try:
            r     = PSDRenderer()
            image = r.load_full(self.file_path)
            if self._cancelled:
                del image; gc.collect()
                return
            img  = image.convert("RGBA")
            w, h = img.size
            raw  = img.tobytes("raw", "RGBA")
            del img, image; gc.collect()
            if not self._cancelled:
                self.done.emit(raw, w, h, self.file_path)
        except Exception as e:
            if not self._cancelled:
                self.error.emit(str(e))


# ══════════════════════════════════════════════
#  线程：批量读底部缩略图条
# ══════════════════════════════════════════════

class ThumbBarWorker(QThread):
    one_done = Signal(int, bytes, int, int)   # (idx, rgba_bytes, w, h)

    def __init__(self, files: List[str]):
        super().__init__()
        self.files      = files
        self._cancelled = False

    def cancel(self): self._cancelled = True

    def run(self):
        r = PSDRenderer()
        for i, fp in enumerate(self.files):
            if self._cancelled:
                break
            try:
                img = r.load_thumbnail_only(fp)
                if img is not None and not self._cancelled:
                    img = img.convert("RGBA")
                    w, h = img.size
                    raw  = img.tobytes("raw", "RGBA")
                    del img
                    self.one_done.emit(i, raw, w, h)
            except Exception:
                pass


# ══════════════════════════════════════════════
#  工具函数：bytes → QPixmap（主线程内调用）
# ══════════════════════════════════════════════

def bytes_to_pixmap(raw: bytes, w: int, h: int) -> QPixmap:
    qimg = QImage(raw, w, h, w * 4, QImage.Format_RGBA8888)
    qimg._keep = raw   # 防 GC
    return QPixmap.fromImage(qimg)


# ══════════════════════════════════════════════
#  图像查看器
# ══════════════════════════════════════════════

class ImageViewer(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setWidgetResizable(False)
        self.setStyleSheet("background-color:#2b2b2b; border:none;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setWidget(self._label)

        self._pixmap:   Optional[QPixmap] = None
        self._scale:    float  = 1.0
        self._drag_pos: Optional[QPoint] = None
        self._is_fit:   bool   = True

    # ── 接口 ──────────────────────────────────

    def set_pixmap(self, pixmap: QPixmap, keep_view: bool = False):
        """
        keep_view=False：重置到适应窗口（新文件打开时）
        keep_view=True ：保持当前缩放和滚动位置（高清替换缩略图时）
        """
        old_scale = self._scale
        old_h_val = self.horizontalScrollBar().value()
        old_v_val = self.verticalScrollBar().value()
        old_is_fit = self._is_fit

        self._pixmap = pixmap
        if not keep_view:
            self._is_fit = True
            self._fit_to_window()
        else:
            # 按照旧缩放比例重新渲染，然后恢复滚动位置
            self._scale  = old_scale
            self._is_fit = old_is_fit
            self._update_display()
            QTimer.singleShot(0, lambda: (
                self.horizontalScrollBar().setValue(old_h_val),
                self.verticalScrollBar().setValue(old_v_val),
            ))

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

    # ── 内部 ──────────────────────────────────

    def _fit_to_window(self):
        if not self._pixmap:
            return
        vw = max(1, self.viewport().width()  - 4)
        vh = max(1, self.viewport().height() - 4)
        self._scale = min(vw / self._pixmap.width(),
                          vh / self._pixmap.height(), 1.0)
        self._update_display()

    def _set_scale(self, s: float):
        self._scale = s
        self._update_display()

    def _update_display(self):
        if not self._pixmap:
            return
        w = max(1, int(self._pixmap.width()  * self._scale))
        h = max(1, int(self._pixmap.height() * self._scale))
        scaled = self._pixmap.scaled(w, h, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())
        fs = self._fit_scale()
        self.viewport().setCursor(
            Qt.OpenHandCursor if self._scale > fs else Qt.ArrowCursor)

    def _fit_scale(self) -> float:
        if not self._pixmap:
            return 1.0
        return min(
            self.viewport().width()  / self._pixmap.width(),
            self.viewport().height() / self._pixmap.height(), 1.0)

    def _zoom_at(self, factor: float, pos: QPoint):
        old = self._scale
        new = max(0.02, min(16.0, old * factor))
        if abs(new - old) < 1e-6:
            return
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        ix = (hb.value() + pos.x()) / old
        iy = (vb.value() + pos.y()) / old
        self._is_fit = False
        self._scale  = new
        self._update_display()
        hb.setValue(int(ix * new - pos.x()))
        vb.setValue(int(iy * new - pos.y()))

    # ── 事件 ──────────────────────────────────

    def wheelEvent(self, e: QWheelEvent):
        if not self._pixmap:
            super().wheelEvent(e); return
        self._zoom_at(1.15 if e.angleDelta().y() > 0 else 1/1.15,
                      e.position().toPoint())
        e.accept()

    def mousePressEvent(self, e):
        if e.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._drag_pos = e.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None:
            d = e.pos() - self._drag_pos
            self._drag_pos = e.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - d.y())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._drag_pos = None
            self.viewport().setCursor(
                Qt.OpenHandCursor if self._scale > self._fit_scale()
                else Qt.ArrowCursor)
        super().mouseReleaseEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._pixmap and self._is_fit:
            QTimer.singleShot(0, self._fit_to_window)


# ══════════════════════════════════════════════
#  底部缩略图条
# ══════════════════════════════════════════════

THUMB_W, THUMB_H, CARD_H = 100, 80, 100

class ThumbCard(QWidget):
    clicked = Signal(int)

    def __init__(self, index: int, name: str, parent=None):
        super().__init__(parent)
        self.index     = index
        self._pixmap:  Optional[QPixmap] = None
        self._selected = False
        self._hover    = False
        self.setFixedSize(THUMB_W, CARD_H)
        self.setCursor(Qt.PointingHandCursor)

        lbl = QLabel(name, self)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setGeometry(0, THUMB_H + 2, THUMB_W, CARD_H - THUMB_H - 2)
        lbl.setStyleSheet("color:#ccc;font-size:9px;background:transparent;")
        lbl.setWordWrap(False)

    def set_pixmap(self, pm: Optional[QPixmap]):
        self._pixmap = pm
        self.update()

    def set_selected(self, v: bool):
        self._selected = v
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = "#0078d4" if self._selected else ("#3e3e42" if self._hover else "#252526")
        p.fillRect(self.rect(), QColor(bg))
        iw, ih, ix, iy = THUMB_W-4, THUMB_H-4, 2, 2
        if self._pixmap:
            pw, ph = self._pixmap.width(), self._pixmap.height()
            p.drawPixmap(ix+(iw-pw)//2, iy+(ih-ph)//2, self._pixmap)
        else:
            p.setPen(QPen(QColor("#444"), 1))
            p.drawRect(ix, iy, iw, ih)
            p.setPen(QColor("#666"))
            p.drawText(ix, iy, iw, ih, Qt.AlignCenter, "PSD")
        if self._selected:
            p.setPen(QPen(QColor("#fff"), 2))
            p.drawRect(1, 1, THUMB_W-2, CARD_H-2)

    def enterEvent(self, _): self._hover = True;  self.update()
    def leaveEvent(self, _): self._hover = False; self.update()
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.index)


class ThumbnailBar(QScrollArea):
    file_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(CARD_H + 12)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(False)
        self.setStyleSheet("""
            QScrollArea { background:#1a1a1a; border-top:1px solid #3a3a3a; }
            QScrollBar:horizontal { height:8px; background:#2a2a2a; border:none; }
            QScrollBar::handle:horizontal {
                background:#555; border-radius:4px; min-width:20px; }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal { width:0; }
        """)
        self._container = QWidget()
        self._container.setStyleSheet("background:#1a1a1a;")
        self._layout = QHBoxLayout(self._container)
        self._layout.setContentsMargins(6, 4, 6, 4)
        self._layout.setSpacing(4)
        self._layout.addStretch()
        self.setWidget(self._container)
        self._cards: List[ThumbCard] = []
        self._sel:   int = -1

    def reset(self, files: List[str]):
        for c in self._cards:
            self._layout.removeWidget(c)
            c.deleteLater()
        self._cards.clear()
        self._sel = -1
        # 移除 stretch
        while self._layout.count():
            self._layout.takeAt(0)

        for i, fp in enumerate(files):
            c = ThumbCard(i, Path(fp).name, self._container)
            c.clicked.connect(self.file_selected.emit)
            self._layout.addWidget(c)
            self._cards.append(c)
        self._layout.addStretch()
        self._container.setFixedWidth(
            max(len(files) * (THUMB_W+4) + 16, self.width()))

    def set_thumb(self, idx: int, raw: bytes, w: int, h: int):
        if 0 <= idx < len(self._cards) and raw:
            pm = bytes_to_pixmap(raw, w, h)
            # 缩到卡片尺寸
            pm = pm.scaled(THUMB_W-4, THUMB_H-4,
                           Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._cards[idx].set_pixmap(pm)

    def set_selected(self, idx: int):
        if self._sel == idx:
            return
        if 0 <= self._sel < len(self._cards):
            self._cards[self._sel].set_selected(False)
        self._sel = idx
        if 0 <= idx < len(self._cards):
            self._cards[idx].set_selected(True)
            x = idx * (THUMB_W+4) + 6
            self.horizontalScrollBar().setValue(
                max(0, x - self.width()//2))


# ══════════════════════════════════════════════
#  文件信息面板
# ══════════════════════════════════════════════

class InfoPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(180); self.setMaximumWidth(230)
        self.setStyleSheet("""
            QFrame { background:#1e1e1e; border-right:1px solid #3a3a3a; }
            QLabel { color:#ccc; font-size:12px; padding:2px 0; }
            QLabel#t { color:#fff; font-size:13px; font-weight:bold; }
            QLabel#k { color:#888; font-size:11px; }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12,16,12,16); lay.setSpacing(4)
        t = QLabel("文件信息"); t.setObjectName("t"); lay.addWidget(t)
        lay.addSpacing(8)
        self._lbl: dict = {}
        for key, name in [
            ("file_name","文件名"),("file_size","大小"),
            ("width","宽度"),("height","高度"),
            ("color_mode","颜色"),("bit_depth","位深"),
            ("layer_count","图层"),
        ]:
            k = QLabel(name); k.setObjectName("k"); lay.addWidget(k)
            v = QLabel("—"); v.setWordWrap(True)
            self._lbl[key] = v; lay.addWidget(v); lay.addSpacing(3)
        lay.addStretch()
        self._btn = QPushButton("导出为 PNG")
        self._btn.setEnabled(False)
        self._btn.setStyleSheet("""
            QPushButton { background:#0078d4;color:#fff;border:none;
                          border-radius:4px;padding:8px;font-size:12px; }
            QPushButton:hover    { background:#106ebe; }
            QPushButton:disabled { background:#3a3a3a;color:#555; }
        """)
        lay.addWidget(self._btn)

    def update_meta(self, meta: dict):
        self._lbl["file_name"].setText(meta.get("file_name","—"))
        sz = meta.get("file_size", 0)
        self._lbl["file_size"].setText(
            f"{sz/1048576:.1f} MB" if sz>1048576 else f"{sz/1024:.1f} KB")
        self._lbl["width"].setText(f"{meta.get('width','—')} px")
        self._lbl["height"].setText(f"{meta.get('height','—')} px")
        self._lbl["color_mode"].setText(meta.get("color_mode","—"))
        self._lbl["bit_depth"].setText(str(meta.get("bit_depth","—")))
        self._lbl["layer_count"].setText(str(meta.get("layer_count","—")))
        self._btn.setEnabled(True)

    def clear(self):
        for v in self._lbl.values(): v.setText("—")
        self._btn.setEnabled(False)

    @property
    def export_button(self): return self._btn


# ══════════════════════════════════════════════
#  拖放占位
# ══════════════════════════════════════════════

class DropPlaceholder(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setAlignment(Qt.AlignCenter)
        for txt, sty in [
            ("📂",             "font-size:64px;"),
            ("将 PSD / PSB 文件拖放到此处",
             "color:#aaa;font-size:15px;margin-top:12px;"),
            ("或点击工具栏「打开文件」按钮",
             "color:#666;font-size:12px;"),
        ]:
            lb = QLabel(txt); lb.setAlignment(Qt.AlignCenter)
            lb.setStyleSheet(sty); lay.addWidget(lb)


# ══════════════════════════════════════════════
#  状态枚举
# ══════════════════════════════════════════════

class _State:
    IDLE    = "idle"
    THUMB   = "thumb"    # 第一阶段：读缩略图
    FULL    = "full"     # 第二阶段：合并高清
    READY   = "ready"    # 高清已显示


# ══════════════════════════════════════════════
#  主窗口
# ══════════════════════════════════════════════

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PSD / PSB 预览器")
        self.resize(1280, 860)
        self.setMinimumSize(800, 600)
        self.setAcceptDrops(True)

        self._state        = _State.IDLE
        self._current_file = ""
        self._current_pm:  Optional[QPixmap] = None   # 当前显示的 pixmap

        self._thumb_worker:    Optional[ThumbLoadWorker] = None
        self._full_worker:     Optional[FullLoadWorker]  = None
        self._thumbbar_worker: Optional[ThumbBarWorker]  = None

        self._folder_files: List[str] = []
        self._folder_index: int = -1

        self._setup_style()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        QTimer.singleShot(150, self._tick_zoom)

    # ── 样式 ──────────────────────────────────

    def _setup_style(self):
        self.setStyleSheet("""
            QMainWindow { background:#252526; }
            QToolBar {
                background:#2d2d2d; border-bottom:1px solid #3a3a3a;
                spacing:4px; padding:4px;
            }
            QToolBar QToolButton {
                color:#ccc; background:transparent; border:none;
                border-radius:4px; padding:6px 10px; font-size:12px;
            }
            QToolBar QToolButton:hover   { background:#3e3e42; }
            QToolBar QToolButton:pressed { background:#4a4a50; }
            QToolBar QToolButton:disabled{ color:#555; }
            QStatusBar { background:#007acc; color:#fff; font-size:11px; }
            QProgressBar { background:#3e3e42; border:none; height:3px; }
            QProgressBar::chunk { background:#007acc; }
        """)

    # ── 工具栏 ────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar(); tb.setMovable(False)
        self.addToolBar(tb)

        a = QAction("📂  打开文件", self)
        a.setShortcut(QKeySequence.Open)
        a.triggered.connect(self._open_dialog)
        tb.addAction(a); tb.addSeparator()

        self._act_prev = QAction("◀  上一个", self)
        self._act_prev.setShortcut(Qt.Key_Left)
        self._act_prev.setEnabled(False)
        self._act_prev.triggered.connect(self._prev_file)
        tb.addAction(self._act_prev)

        self._nav_lbl = QLabel("  —  ")
        self._nav_lbl.setStyleSheet("color:#aaa;font-size:12px;padding:0 4px;")
        tb.addWidget(self._nav_lbl)

        self._act_next = QAction("下一个  ▶", self)
        self._act_next.setShortcut(Qt.Key_Right)
        self._act_next.setEnabled(False)
        self._act_next.triggered.connect(self._next_file)
        tb.addAction(self._act_next); tb.addSeparator()

        for lbl, sc, fn in [
            ("🔍+  放大",   "Ctrl+=", lambda: self._viewer.zoom_in()),
            ("🔍-  缩小",   "Ctrl+-", lambda: self._viewer.zoom_out()),
            ("⊡  适应窗口", "Ctrl+0", lambda: self._viewer.zoom_fit()),
            ("⊞  100%",    "Ctrl+1", lambda: self._viewer.zoom_reset()),
        ]:
            act = QAction(lbl, self)
            act.setShortcut(QKeySequence(sc))
            act.triggered.connect(fn)
            tb.addAction(act)

        # 高清状态标签
        self._hd_lbl = QLabel("")
        self._hd_lbl.setStyleSheet("color:#f0a050;font-size:11px;padding:0 8px;")
        tb.addWidget(self._hd_lbl)

    # ── 中央布局 ──────────────────────────────

    def _build_central(self):
        outer = QWidget()
        ol    = QVBoxLayout(outer)
        ol.setContentsMargins(0,0,0,0); ol.setSpacing(0)

        sp = QSplitter(Qt.Horizontal); sp.setHandleWidth(1)
        self._info_panel = InfoPanel()
        self._info_panel.export_button.clicked.connect(self._export_png)
        sp.addWidget(self._info_panel)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0,0,0,0); rl.setSpacing(0)
        self._placeholder = DropPlaceholder()
        self._placeholder.setStyleSheet("background:#2b2b2b;")
        rl.addWidget(self._placeholder)
        self._viewer = ImageViewer()
        self._viewer.hide()
        rl.addWidget(self._viewer)
        sp.addWidget(right)
        sp.setStretchFactor(0,0); sp.setStretchFactor(1,1)

        ol.addWidget(sp, 1)

        self._thumb_bar = ThumbnailBar()
        self._thumb_bar.file_selected.connect(self._on_thumb_click)
        ol.addWidget(self._thumb_bar, 0)

        self.setCentralWidget(outer)

    # ── 状态栏 ────────────────────────────────

    def _build_statusbar(self):
        self._status = QStatusBar(); self.setStatusBar(self._status)
        self._prog = QProgressBar()
        self._prog.setRange(0,0); self._prog.setMaximumWidth(120)
        self._prog.hide()
        self._status.addPermanentWidget(self._prog)
        self._zoom_lbl = QLabel("缩放: 100%")
        self._zoom_lbl.setStyleSheet("color:#fff;margin-right:8px;")
        self._status.addPermanentWidget(self._zoom_lbl)
        self._status.showMessage("就绪 — 打开或拖入 PSD/PSB 文件开始预览")

    def _tick_zoom(self):
        self._zoom_lbl.setText(f"缩放: {int(self._viewer.scale*100)}%")
        QTimer.singleShot(150, self._tick_zoom)

    # ── 安全停止线程 ──────────────────────────

    def _stop_worker(self, w: Optional[QThread],
                     timeout_ms: int = 3000) -> None:
        """取消并等待线程退出（最多 timeout_ms ms），安全清理"""
        if w is None:
            return
        try:
            if hasattr(w, "cancel"):
                w.cancel()
            if w.isRunning():
                w.quit()
                if not w.wait(timeout_ms):
                    w.terminate()
                    w.wait(1000)
        except RuntimeError:
            pass   # Qt 对象可能已销毁

    # ── 文件夹扫描 ────────────────────────────

    def _scan_folder(self, file_path: str):
        folder = Path(file_path).parent
        files  = sorted(
            [str(p) for p in folder.iterdir()
             if p.suffix.lower() in (".psd", ".psb")],
            key=str.lower)
        self._folder_files = files
        target = str(Path(file_path).resolve()).lower()
        self._folder_index = next(
            (i for i,f in enumerate(files)
             if str(Path(f).resolve()).lower() == target), 0)

        self._thumb_bar.reset(files)
        self._thumb_bar.set_selected(self._folder_index)
        self._update_nav()

        # 停止旧缩略图条加载，启动新的
        self._stop_worker(self._thumbbar_worker)
        w = ThumbBarWorker(files)
        w.one_done.connect(self._thumb_bar.set_thumb)
        self._thumbbar_worker = w
        w.start()

    def _update_nav(self):
        n, i = len(self._folder_files), self._folder_index
        self._act_prev.setEnabled(n>1 and i>0)
        self._act_next.setEnabled(n>1 and i<n-1)
        self._nav_lbl.setText(f"  {i+1} / {n}  " if n else "  —  ")

    def _prev_file(self):
        if self._folder_index > 0:
            self._folder_index -= 1
            self._load_file(self._folder_files[self._folder_index],
                            scan_folder=False)

    def _next_file(self):
        if self._folder_index < len(self._folder_files)-1:
            self._folder_index += 1
            self._load_file(self._folder_files[self._folder_index],
                            scan_folder=False)

    def _on_thumb_click(self, idx: int):
        if 0 <= idx < len(self._folder_files) and idx != self._folder_index:
            self._folder_index = idx
            self._load_file(self._folder_files[idx], scan_folder=False)

    # ── 文件加载入口 ──────────────────────────

    def _open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 PSD / PSB 文件", "",
            "Photoshop 文件 (*.psd *.psb);;所有文件 (*.*)")
        if path:
            self._load_file(path)

    def _load_file(self, path: str, scan_folder: bool = True):
        # 停止所有正在进行的加载任务
        self._stop_worker(self._thumb_worker)
        self._stop_worker(self._full_worker)
        self._thumb_worker = None
        self._full_worker  = None

        if scan_folder:
            self._scan_folder(path)
        else:
            self._update_nav()
            self._thumb_bar.set_selected(self._folder_index)

        self._current_file = path
        self._state        = _State.THUMB
        self._hd_lbl.setText("")

        # UI 进入加载状态
        self._prog.show()
        self._placeholder.show()
        self._viewer.hide()
        self._viewer.clear()
        self._info_panel.clear()
        self.setWindowTitle(f"PSD / PSB 预览器 — {Path(path).name}")
        self._status.showMessage("读取预览图…")

        # 第一阶段：读缩略图（极速）
        w = ThumbLoadWorker(path)
        w.done.connect(self._on_thumb_done)
        w.error.connect(self._on_thumb_error)
        self._thumb_worker = w
        w.start()

    # ── 第一阶段完成 ──────────────────────────

    def _on_thumb_done(self, raw: bytes, w: int, h: int, meta: dict):
        if self._state != _State.THUMB:
            return   # 已切换文件，丢弃

        self._prog.hide()
        self._info_panel.update_meta(meta)
        self._placeholder.hide()
        self._viewer.show()

        if raw:
            pm = bytes_to_pixmap(raw, w, h)
            self._current_pm = pm
            self._viewer.set_pixmap(pm, keep_view=False)
            n, i = len(self._folder_files), self._folder_index
            nav  = f"  [{i+1}/{n}]" if n>1 else ""
            self._status.showMessage(
                f"{meta.get('file_name')}{nav}  —  "
                f"{meta.get('width')} × {meta.get('height')} px  |  预览图，高清加载中…")
            self._hd_lbl.setText("⏳ 高清加载中…")
        else:
            # 没有内嵌缩略图，跳过显示，直接等高清
            self._status.showMessage("加载高清原图中…")
            self._prog.show()

        # 第二阶段：后台合并高清
        self._state = _State.FULL
        fw = FullLoadWorker(self._current_file)
        fw.done.connect(self._on_full_done)
        fw.error.connect(self._on_full_error)
        self._full_worker = fw
        fw.start()

    def _on_thumb_error(self, msg: str):
        if self._state != _State.THUMB:
            return
        # 缩略图读失败，尝试直接加载高清
        self._state = _State.FULL
        self._status.showMessage("预览图读取失败，加载高清原图中…")
        fw = FullLoadWorker(self._current_file)
        fw.done.connect(self._on_full_done)
        fw.error.connect(self._on_full_error)
        self._full_worker = fw
        fw.start()

    # ── 第二阶段完成 ──────────────────────────

    def _on_full_done(self, raw: bytes, w: int, h: int, file_path: str):
        if file_path != self._current_file:
            return   # 用户已切换，丢弃

        self._prog.hide()
        self._state = _State.READY

        pm = bytes_to_pixmap(raw, w, h)
        self._current_pm = pm

        has_thumb = self._viewer.isVisible() and self._viewer._pixmap is not None
        self._placeholder.hide()
        self._viewer.show()
        # 已有缩略图：保持用户当前缩放/位置替换
        self._viewer.set_pixmap(pm, keep_view=has_thumb)

        n, i = len(self._folder_files), self._folder_index
        nav  = f"  [{i+1}/{n}]" if n>1 else ""
        self._status.showMessage(
            f"{Path(file_path).name}{nav}  —  {w} × {h} px  |  高清原图")
        self._hd_lbl.setText("✓ 高清")

        # 更新信息面板的实际尺寸（高清可能比缩略图大）
        cur_meta = {
            "file_name":   Path(file_path).name,
            "file_size":   os.path.getsize(file_path),
            "width":       w,
            "height":      h,
            "color_mode":  "—",
            "bit_depth":   "—",
            "layer_count": "—",
        }
        self._info_panel.update_meta(cur_meta)

    def _on_full_error(self, msg: str):
        if self._state not in (_State.FULL, _State.THUMB):
            return
        self._prog.hide()
        # 有缩略图时静默显示，仅状态栏提示
        if self._viewer.isVisible() and self._viewer._pixmap is not None:
            self._hd_lbl.setText("预览图")
            self._status.showMessage(f"高清加载失败（仅显示预览图）: {msg}")
        else:
            self._placeholder.show()
            self._viewer.hide()
            self._status.showMessage(f"加载失败: {msg}")
            QMessageBox.critical(self, "加载失败", msg)

    # ── 导出 ──────────────────────────────────

    def _export_png(self):
        if self._current_pm is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出图像", "",
            "PNG 图像 (*.png);;JPEG 图像 (*.jpg)")
        if not path:
            return
        try:
            img = self._current_pm.toImage()
            img.save(path)
            self._status.showMessage(f"已导出: {path}")
            QMessageBox.information(self, "导出成功", f"已保存到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ── 键盘 ──────────────────────────────────

    def keyPressEvent(self, e):
        if   e.key() == Qt.Key_Left:  self._prev_file()
        elif e.key() == Qt.Key_Right: self._next_file()
        else: super().keyPressEvent(e)

    # ── 窗口关闭：等待所有线程安全退出 ────────

    def closeEvent(self, e):
        self._stop_worker(self._thumb_worker)
        self._stop_worker(self._full_worker)
        self._stop_worker(self._thumbbar_worker)
        super().closeEvent(e)

    # ── 拖放 ──────────────────────────────────

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                if url.toLocalFile().lower().endswith((".psd",".psb")):
                    e.acceptProposedAction(); return
        e.ignore()

    def dropEvent(self, e: QDropEvent):
        for url in e.mimeData().urls():
            fp = url.toLocalFile()
            if fp.lower().endswith((".psd",".psb")):
                self._load_file(fp); break


# ══════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════

def main():
    # Windows 下关闭 DPI 自动缩放的副作用，防止高 DPI 屏崩溃
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("PSD Viewer")

    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        w._load_file(sys.argv[1])

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
