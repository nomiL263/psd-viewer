"""
PSD/PSB 文件预览核心模块

速度优先策略（三级）：
  1. 纯二进制直读（最快）：直接 seek 到 Image Resources 段取 JPEG，不加载任何其他数据
  2. psd-tools 轻量读（备用）：PSDImage.open() 不合并图层，仅取资源
  3. 失败时报错提示用户勾选缩略图

PSD/PSB Image Resources 段结构完全相同，version 差异不影响此段。
"""

import os
import io
import struct
from pathlib import Path
from typing import Optional
from PIL import Image


# ── 读取前多少字节来定位 Image Resources 段 ──
# 实测绝大多数 PSD 文件的 Image Resources 段在文件开头 64KB 以内
_READ_AHEAD = 64 * 1024   # 首次读取 64 KB，足以覆盖大多数文件


class PSDRenderer:
    """PSD/PSB 文件极速预览器（直读内嵌缩略图）"""

    def __init__(self):
        self._file_path: Optional[str] = None
        self._preview_image: Optional[Image.Image] = None
        self._meta: dict = {}
        self._is_psb: bool = False

    def load(self, file_path: str) -> dict:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        suffix = path.suffix.lower()
        if suffix not in (".psd", ".psb"):
            raise ValueError(f"不支持的格式: {suffix}，仅支持 .psd / .psb")

        self._file_path  = file_path
        self._preview_image = None
        self._is_psb     = (suffix == ".psb")

        # 两步并行：读头部元信息 + 提取缩略图（共用同一次文件 IO）
        self._meta, self._preview_image = self._load_all(file_path)
        return self._meta

    # ── 核心：单次 IO 同时拿到 meta + 缩略图 ─────────────────────

    def _load_all(self, file_path: str):
        """
        单次打开文件，顺序读取：
          文件头(26B) → Color Mode Data → Image Resources（含缩略图）
        全程只做顺序读/seek，不解析图层，速度极快。
        """
        color_mode_map = {
            0:"Bitmap", 1:"Grayscale", 2:"Indexed",
            3:"RGB",    4:"CMYK",      7:"Multichannel",
            8:"Duotone",9:"Lab",
        }
        meta = {
            "file_name":   Path(file_path).name,
            "file_size":   os.path.getsize(file_path),
            "width": 0, "height": 0,
            "color_mode":  "RGB",
            "bit_depth":   "—",
            "layer_count": "—",
        }
        image: Optional[Image.Image] = None

        with open(file_path, "rb") as f:
            # ── 1. 文件头 26 字节 ──────────────────────────────
            header = f.read(26)
            if len(header) < 26 or header[:4] != b"8BPS":
                raise ValueError("不是有效的 PSD/PSB 文件")
            version  = struct.unpack_from(">H", header, 4)[0]
            channels = struct.unpack_from(">H", header, 12)[0]
            height   = struct.unpack_from(">I", header, 14)[0]
            width    = struct.unpack_from(">I", header, 18)[0]
            depth    = struct.unpack_from(">H", header, 22)[0]
            cm_raw   = struct.unpack_from(">H", header, 24)[0]

            meta.update({
                "width":      width,
                "height":     height,
                "bit_depth":  f"{depth} bit",
                "color_mode": color_mode_map.get(cm_raw, f"未知({cm_raw})"),
                "format":     "PSB" if self._is_psb else "PSD",
            })

            # ── 2. 跳过 Color Mode Data 段 ────────────────────
            cmd_len = struct.unpack(">I", f.read(4))[0]
            if cmd_len:
                f.seek(cmd_len, 1)

            # ── 3. Image Resources 段 ──────────────────────────
            img_res_len = struct.unpack(">I", f.read(4))[0]
            img_res_end = f.tell() + img_res_len

            # 一次性把整个 Image Resources 段读进内存
            # 通常 < 1 MB，避免反复小块 IO
            img_res_data = f.read(img_res_len)

        # 在内存中解析 Image Resources，速度极快
        image = self._parse_image_resources(img_res_data)

        if image is None:
            # fallback：用 psd-tools（不合并图层）
            image = self._via_psd_tools(file_path)

        return meta, image

    # ── 在内存中解析 Image Resources ─────────────────────────────

    def _parse_image_resources(self, data: bytes) -> Optional[Image.Image]:
        """
        在内存 bytes 中扫描 8BIM 资源块，找到资源 ID 1036（JPEG）或 1033（旧版BGR）。
        全程内存操作，无磁盘 IO，极快。
        """
        pos = 0
        end = len(data)

        while pos + 12 <= end:
            # 4字节标记
            if data[pos:pos+4] != b"8BIM":
                break
            pos += 4

            res_id = struct.unpack_from(">H", data, pos)[0]
            pos += 2

            # Pascal string（对齐到偶数，含长度字节）
            ps_len = data[pos]
            pos += 1
            # 跳过字符串 + 填充到偶数
            total_ps = ps_len + (1 if (ps_len + 1) % 2 != 0 else 0)
            pos += total_ps

            if pos + 4 > end:
                break
            data_len  = struct.unpack_from(">I", data, pos)[0]
            pos += 4
            data_end  = pos + data_len + (data_len % 2)  # 对齐

            if res_id in (1036, 1033):
                # ThumbnailResource 头 28 字节后是 JPEG 数据
                jpeg_start = pos + 28
                jpeg_len   = data_len - 28
                if jpeg_len > 0 and jpeg_start + jpeg_len <= end:
                    jpeg_bytes = data[jpeg_start: jpeg_start + jpeg_len]
                    try:
                        img = Image.open(io.BytesIO(jpeg_bytes))
                        img.load()   # 立即解码，避免延迟
                        if res_id == 1033:   # 旧格式是 BGR
                            r, g, b = img.convert("RGB").split()
                            img = Image.merge("RGB", (b, g, r))
                        return img.convert("RGBA")
                    except Exception:
                        pass

            pos = data_end

        return None

    # ── fallback：psd-tools（不合并图层）────────────────────────

    def _via_psd_tools(self, file_path: str) -> Image.Image:
        try:
            from psd_tools import PSDImage
            psd = PSDImage.open(file_path)

            for res_id in (1036, 1033):
                try:
                    res = psd.image_resources.get_data(res_id)
                    if res is None:
                        continue
                    img = getattr(res, "data", None) or getattr(res, "thumbnail", None)
                    if isinstance(img, Image.Image):
                        return img.convert("RGBA")
                except Exception:
                    continue

            # 最后尝试 topil()（读合并图像块，不重新合并图层）
            img = psd.topil()
            if img is not None:
                return img.convert("RGBA")
        except Exception:
            pass

        fmt = "PSB" if self._is_psb else "PSD"
        raise RuntimeError(
            f"该 {fmt} 文件没有内嵌预览图。\n\n"
            "请在 Photoshop 中重新保存，并确保勾选了「存储缩略图」：\n"
            "文件 → 存储为 → 勾选「存储缩略图 / Thumbnail」"
        )

    # ── 对外接口 ─────────────────────────────────────────────────

    def composite(self) -> Image.Image:
        if self._preview_image is None:
            raise RuntimeError("请先调用 load() 加载文件")
        return self._preview_image

    def get_thumbnail(self, max_size=(1920, 1080)) -> Image.Image:
        img = self.composite().copy()
        img.thumbnail(max_size, Image.LANCZOS)
        return img

    @property
    def is_loaded(self) -> bool:
        return self._preview_image is not None

    def close(self):
        self._preview_image = None
        self._file_path     = None
        self._meta          = {}
        self._is_psb        = False
