"""
PSD/PSB 文件预览核心模块

策略：直读 PSD 内嵌"合并图像数据"（Merged Image Data）
─────────────────────────────────────────────────────────
PSD 格式结构：
  [文件头 26B] [Color Mode Data] [Image Resources] [Layer Info] [合并图像数据]

"合并图像数据" 是 Photoshop 保存时写入的完整高清合并图，
与缩略图不同，它是原始分辨率、原始颜色深度的全尺寸图像。
ACDSee / 2345看图王 / WPS图片等软件都是读这块数据，不解析图层。

读取速度：与读同尺寸 BMP/PNG 相当，无需 psd-tools 合并图层。
"""

import os
import io
import gc
import struct
import zlib
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image


MAX_DISPLAY_PX   = 8192          # 单边最大显示像素
MEM_LIMIT_BYTES  = 600 * 1024 * 1024   # 600 MB 内存软上限


class PSDRenderer:

    def __init__(self):
        self._is_psb: bool = False

    # ══════════════════════════════════════════
    #  主入口：一次调用同时返回高清图 + meta
    # ══════════════════════════════════════════

    def load(self, file_path: str) -> Tuple[Optional[Image.Image], dict]:
        """
        读取 PSD/PSB 文件，返回 (合并图或None, meta_dict)。
        - 有合并图：返回高清 PIL Image
        - 无合并图但文件有效：返回 (None, meta)，调用方显示友好提示
        - 文件损坏/不存在：抛异常
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        if path.suffix.lower() not in (".psd", ".psb"):
            raise ValueError(f"不支持的格式: {path.suffix}")

        self._is_psb = path.suffix.lower() == ".psb"

        with open(file_path, "rb") as f:
            raw = f.read()

        meta  = self._parse_header(raw, file_path)
        image = self._extract_merged_image(raw, meta)

        if image is None:
            image = self._extract_thumbnail(raw)

        # image 仍为 None：文件有效但无图像数据，返回 (None, meta)
        # 调用方（LoadWorker）负责发 no_merged 信号显示友好提示
        if image is None:
            return None, meta

        if image.mode not in ("RGBA", "RGB"):
            image = image.convert("RGBA")
        image = _safe_resize(image)
        return image, meta

    # ══════════════════════════════════════════
    #  仅读缩略图（底部缩略图条用，极速）
    # ══════════════════════════════════════════

    def load_thumbnail_only(self, file_path: str) -> Optional[Image.Image]:
        try:
            with open(file_path, "rb") as f:
                # 只读前 2 MB 就够了（Image Resources 段通常在开头）
                raw = f.read(2 * 1024 * 1024)
            return self._extract_thumbnail(raw)
        except Exception:
            return None

    # ══════════════════════════════════════════
    #  解析文件头
    # ══════════════════════════════════════════

    def _parse_header(self, raw: bytes, file_path: str) -> dict:
        color_mode_map = {
            0:"Bitmap", 1:"Grayscale", 2:"Indexed", 3:"RGB",
            4:"CMYK",   7:"Multichannel", 8:"Duotone", 9:"Lab",
        }
        meta = {
            "file_name":   Path(file_path).name,
            "file_size":   os.path.getsize(file_path),
            "width": 0, "height": 0,
            "color_mode":  "RGB", "bit_depth": "—",
            "layer_count": "—",
            "format":      "PSB" if self._is_psb else "PSD",
        }
        if len(raw) >= 26 and raw[:4] == b"8BPS":
            meta.update({
                "height":     struct.unpack_from(">I", raw, 14)[0],
                "width":      struct.unpack_from(">I", raw, 18)[0],
                "bit_depth":  f"{struct.unpack_from('>H', raw, 22)[0]} bit",
                "color_mode": color_mode_map.get(
                    struct.unpack_from(">H", raw, 24)[0], "未知"),
            })
        return meta

    # ══════════════════════════════════════════
    #  核心：读取内嵌合并图像数据
    # ══════════════════════════════════════════

    def _extract_merged_image(self, raw: bytes,
                              meta: dict) -> Optional[Image.Image]:
        """
        跳过 Color Mode Data、Image Resources、Layer Info 三个段，
        直达"合并图像数据"段，解压并重建为 PIL Image。

        PSD 合并图像数据格式：
          2B  压缩类型（0=Raw, 1=PackBits RLE, 2=ZIP无预测, 3=ZIP有预测）
          [若 RLE：每行字节数 × 行数 × 通道数，每个 2B（PSB 为 4B）]
          [图像数据：按通道平铺，每通道完整行扫描]
        """
        try:
            pos = 26   # 跳过文件头

            # ── Color Mode Data ──
            cmd_len = struct.unpack_from(">I", raw, pos)[0]; pos += 4
            pos += cmd_len

            # ── Image Resources ──
            res_len = struct.unpack_from(">I", raw, pos)[0]; pos += 4
            pos += res_len

            # ── Layer and Mask Info ──
            if self._is_psb:
                lm_len = struct.unpack_from(">Q", raw, pos)[0]; pos += 8
            else:
                lm_len = struct.unpack_from(">I", raw, pos)[0]; pos += 4
            pos += lm_len

            # ── 合并图像数据段 ──
            if pos + 2 > len(raw):
                return None

            compression = struct.unpack_from(">H", raw, pos)[0]; pos += 2

            w        = meta["width"]
            h        = meta["height"]
            channels = 3   # 默认 RGB，多通道时取前3

            if w <= 0 or h <= 0:
                return None

            # 解压数据
            if compression == 0:
                # Raw：直接读
                plane_size = w * h
                data = raw[pos: pos + channels * plane_size]

            elif compression == 1:
                # PackBits RLE
                # 每行字节计数表
                bpc = 4 if self._is_psb else 2   # 每个计数的字节数
                count_table_size = channels * h * bpc
                counts_raw = raw[pos: pos + count_table_size]
                pos += count_table_size

                fmt = ">I" if self._is_psb else ">H"
                row_bytes = [
                    struct.unpack_from(fmt, counts_raw, i * bpc)[0]
                    for i in range(channels * h)
                ]

                planes = []
                rp = pos
                for ch in range(channels):
                    ch_rows = []
                    for row in range(h):
                        idx   = ch * h + row
                        n     = row_bytes[idx]
                        chunk = raw[rp: rp + n]; rp += n
                        ch_rows.append(_unpack_bits(chunk, w))
                    planes.append(b"".join(ch_rows))
                data = b"".join(planes)

            elif compression in (2, 3):
                # ZIP（有/无预测）
                zdata = raw[pos:]
                try:
                    data = zlib.decompress(zdata)
                except Exception:
                    data = zlib.decompress(zdata, -15)

            else:
                return None

            # 重建图像：通道平铺 → 交错 RGBA
            if len(data) < w * h * channels:
                return None

            plane = w * h
            if channels >= 3:
                r_plane = data[0          : plane]
                g_plane = data[plane      : plane*2]
                b_plane = data[plane*2    : plane*3]
                # 交错 RGB
                img_bytes = bytearray(plane * 3)
                img_bytes[0::3] = r_plane
                img_bytes[1::3] = g_plane
                img_bytes[2::3] = b_plane
                img = Image.frombytes("RGB", (w, h), bytes(img_bytes))
                if channels >= 4:
                    a_plane = data[plane*3: plane*4]
                    alpha   = Image.frombytes("L", (w, h), a_plane)
                    img.putalpha(alpha)
                    return img.convert("RGBA")
                return img.convert("RGBA")

        except Exception:
            pass
        return None

    # ══════════════════════════════════════════
    #  Fallback：内嵌缩略图（Image Resources）
    # ══════════════════════════════════════════

    def _extract_thumbnail(self, raw: bytes) -> Optional[Image.Image]:
        try:
            pos = 26
            if pos + 4 > len(raw): return None
            cmd_len = struct.unpack_from(">I", raw, pos)[0]; pos += 4
            pos += cmd_len
            if pos + 4 > len(raw): return None
            res_len = struct.unpack_from(">I", raw, pos)[0]; pos += 4
            res_end = pos + res_len
            res_data = raw[pos: res_end]

            p, end = 0, len(res_data)
            while p + 12 <= end:
                if res_data[p:p+4] != b"8BIM": break
                p += 4
                res_id = struct.unpack_from(">H", res_data, p)[0]; p += 2
                ps_len = res_data[p]; p += 1
                p += ps_len + (1 if (ps_len+1)%2 != 0 else 0)
                if p + 4 > end: break
                dlen = struct.unpack_from(">I", res_data, p)[0]; p += 4
                if res_id in (1036, 1033):
                    js = p + 28; jl = dlen - 28
                    if jl > 0:
                        try:
                            img = Image.open(io.BytesIO(res_data[js:js+jl]))
                            img.load()
                            if res_id == 1033:
                                r,g,b = img.convert("RGB").split()
                                img = Image.merge("RGB",(b,g,r))
                            return img.convert("RGBA")
                        except Exception: pass
                p += dlen + (dlen % 2)
        except Exception: pass
        return None


# ══════════════════════════════════════════════
#  PackBits 解压（RLE）
# ══════════════════════════════════════════════

def _unpack_bits(src: bytes, expected_len: int) -> bytes:
    out = bytearray()
    i   = 0
    while i < len(src) and len(out) < expected_len:
        n = src[i]; i += 1
        if n == 128:
            continue
        elif n > 128:
            count = 257 - n
            if i < len(src):
                out.extend([src[i]] * count); i += 1
        else:
            count = n + 1
            out.extend(src[i:i+count]); i += count
    return bytes(out[:expected_len])


# ══════════════════════════════════════════════
#  内存安全缩放
# ══════════════════════════════════════════════

def _safe_resize(image: Image.Image) -> Image.Image:
    w, h = image.size
    ch   = len(image.getbands())
    scale = min(
        MAX_DISPLAY_PX / w,
        MAX_DISPLAY_PX / h,
        ((MEM_LIMIT_BYTES // ch) / (w * h)) ** 0.5,
        1.0,
    )
    if scale < 1.0:
        image = image.resize(
            (max(1, int(w*scale)), max(1, int(h*scale))),
            Image.LANCZOS)
    return image
