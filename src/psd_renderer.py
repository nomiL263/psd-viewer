"""
PSD/PSB 文件预览核心模块

加载策略（参照 2345看图王/WPS图片）：
  Phase 1 - 极速预览：直接读内嵌缩略图（< 0.3s），立即显示
  Phase 2 - 高清替换：后台 psd-tools composite() 合并图层，完成后无缝替换
  内存保护：单张图超过阈值时自动缩减到安全尺寸再传给 UI
"""

import os
import io
import gc
import struct
import traceback
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image


# 传给 UI 的最大像素边长（防 OOM）
# 8192×8192 RGBA = 256MB，设为 8192 上限
MAX_DISPLAY_PX = 8192

# 单张图内存软上限（字节），超过则等比缩小
MEM_LIMIT_BYTES = 512 * 1024 * 1024   # 512 MB


class PSDRenderer:
    """PSD/PSB 两阶段预览器"""

    def __init__(self):
        self._file_path: Optional[str] = None
        self._is_psb:    bool = False

    # ── 公共：第一阶段，读内嵌缩略图（极速）─────────────────────

    def load_thumbnail(self, file_path: str) -> Tuple[Optional[Image.Image], dict]:
        """
        读文件头 + 内嵌缩略图，通常 < 0.3s。
        返回 (thumbnail_image_or_None, meta_dict)
        """
        self._file_path = file_path
        self._is_psb    = Path(file_path).suffix.lower() == ".psb"

        meta  = self._read_header(file_path)
        thumb = self._read_thumbnail(file_path)
        return thumb, meta

    # ── 公共：第二阶段，合并图层得到高清图──────────────────────

    def load_full(self, file_path: str) -> Image.Image:
        """
        用 psd-tools composite() 合并所有图层，返回高清图。
        耗时取决于文件大小，应在后台线程调用。
        超过内存限制时自动缩减尺寸。
        """
        from psd_tools import PSDImage

        psd   = PSDImage.open(file_path)
        image = psd.composite()

        if image is None:
            image = psd.topil()

        if image is None:
            raise RuntimeError("无法合并图层，文件可能无可见图层")

        # 释放 psd 对象，尽早回收内存
        del psd
        gc.collect()

        if image.mode not in ("RGBA", "RGB"):
            image = image.convert("RGBA")

        # 内存保护：超限则等比缩小
        image = self._safe_resize(image)
        return image

    # ── 内存安全缩放 ─────────────────────────────────────────────

    @staticmethod
    def _safe_resize(image: Image.Image) -> Image.Image:
        """
        若图像超过内存上限或边长上限，等比缩小到安全尺寸。
        使用 LANCZOS 保证质量。
        """
        w, h      = image.size
        channels  = len(image.getbands())
        mem_bytes = w * h * channels

        # 按内存上限计算允许的最大总像素数
        max_px_by_mem = MEM_LIMIT_BYTES // channels  # 像素数

        # 按边长上限
        max_scale_by_edge = min(MAX_DISPLAY_PX / w, MAX_DISPLAY_PX / h, 1.0)
        max_scale_by_mem  = (max_px_by_mem / (w * h)) ** 0.5

        scale = min(max_scale_by_edge, max_scale_by_mem, 1.0)
        if scale < 1.0:
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            image = image.resize((new_w, new_h), Image.LANCZOS)

        return image

    # ── 仅读缩略图（底部缩略图条用）─────────────────────────────

    def load_thumbnail_only(self, file_path: str) -> Optional[Image.Image]:
        """极速读内嵌缩略图，不读 meta，专供缩略图条批量加载"""
        try:
            return self._read_thumbnail(file_path)
        except Exception:
            return None

    # ── 文件头元信息（只读 26 字节）─────────────────────────────

    def _read_header(self, file_path: str) -> dict:
        color_mode_map = {
            0:"Bitmap",  1:"Grayscale", 2:"Indexed",
            3:"RGB",     4:"CMYK",      7:"Multichannel",
            8:"Duotone", 9:"Lab",
        }
        meta = {
            "file_name":   Path(file_path).name,
            "file_size":   os.path.getsize(file_path),
            "width": 0, "height": 0,
            "color_mode":  "RGB",
            "bit_depth":   "—",
            "layer_count": "—",
            "format":      "PSB" if self._is_psb else "PSD",
        }
        try:
            with open(file_path, "rb") as f:
                h = f.read(26)
            if len(h) >= 26 and h[:4] == b"8BPS":
                meta.update({
                    "width":      struct.unpack_from(">I", h, 18)[0],
                    "height":     struct.unpack_from(">I", h, 14)[0],
                    "bit_depth":  f"{struct.unpack_from('>H', h, 22)[0]} bit",
                    "color_mode": color_mode_map.get(
                        struct.unpack_from(">H", h, 24)[0], "未知"),
                })
        except Exception:
            pass
        return meta

    # ── 内嵌缩略图读取（Image Resources 段）────────────────────

    def _read_thumbnail(self, file_path: str) -> Optional[Image.Image]:
        """
        纯二进制，顺序读取 Image Resources 段，
        找到资源 ID 1036（JPEG）或 1033（旧版BGR），返回 PIL Image。
        PSD/PSB 该段结构完全一致。
        """
        try:
            with open(file_path, "rb") as f:
                hdr = f.read(26)
                if len(hdr) < 26 or hdr[:4] != b"8BPS":
                    return None
                # 跳过 Color Mode Data
                cmd_len = struct.unpack(">I", f.read(4))[0]
                if cmd_len:
                    f.seek(cmd_len, 1)
                # 读入整个 Image Resources 段
                res_len  = struct.unpack(">I", f.read(4))[0]
                res_data = f.read(res_len)

            return self._parse_thumbnail(res_data)
        except Exception:
            return None

    def _parse_thumbnail(self, data: bytes) -> Optional[Image.Image]:
        pos, end = 0, len(data)
        while pos + 12 <= end:
            if data[pos:pos+4] != b"8BIM":
                break
            pos += 4
            res_id = struct.unpack_from(">H", data, pos)[0]; pos += 2
            # Pascal string 跳过
            ps_len  = data[pos]; pos += 1
            pos    += ps_len + (1 if (ps_len + 1) % 2 != 0 else 0)
            if pos + 4 > end:
                break
            data_len = struct.unpack_from(">I", data, pos)[0]; pos += 4

            if res_id in (1036, 1033):
                jpeg_start = pos + 28
                jpeg_len   = data_len - 28
                if jpeg_len > 0 and jpeg_start + jpeg_len <= end:
                    try:
                        img = Image.open(io.BytesIO(data[jpeg_start:jpeg_start+jpeg_len]))
                        img.load()
                        if res_id == 1033:
                            r, g, b = img.convert("RGB").split()
                            img = Image.merge("RGB", (b, g, r))
                        return img.convert("RGBA")
                    except Exception:
                        pass

            pos += data_len + (data_len % 2)
        return None
