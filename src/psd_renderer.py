"""
PSD/PSB 文件预览核心模块
策略：优先读取文件内嵌的合并预览图（thumbnail），速度极快，无需解析图层

PSD（version=1）和 PSB（version=2）的 Image Resources 段结构完全相同，
差异仅在图层数据段的偏移量长度（PSD=4字节，PSB=8字节），
而预览图在 Image Resources 段，两者处理方式一致。
"""

import os
import io
import struct
from pathlib import Path
from typing import Optional
from PIL import Image


class PSDRenderer:
    """PSD/PSB 文件快速预览器（基于内嵌缩略图）"""

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

        self._file_path = file_path
        self._preview_image = None
        self._is_psb = (suffix == ".psb")

        # 读取文件头（只读前 26 字节，极快）
        self._meta = self._read_header(file_path)

        # 提取内嵌预览图
        self._preview_image = self._extract_thumbnail(file_path)

        return self._meta

    # ── 读文件头 ──────────────────────────────────────────────────

    def _read_header(self, file_path: str) -> dict:
        """解析 PSD/PSB 文件头，获取宽高/颜色模式/位深，只需读 26 字节"""
        meta = {
            "file_name": Path(file_path).name,
            "file_size": os.path.getsize(file_path),
            "width": 0,
            "height": 0,
            "color_mode": "RGB",
            "bit_depth": "—",
            "layer_count": "—",
        }
        color_mode_map = {
            0: "Bitmap", 1: "Grayscale", 2: "Indexed",
            3: "RGB", 4: "CMYK", 7: "Multichannel",
            8: "Duotone", 9: "Lab",
        }
        try:
            with open(file_path, "rb") as f:
                sig = f.read(4)
                if sig != b"8BPS":
                    raise ValueError("不是有效的 PSD/PSB 文件（签名不符）")
                version = struct.unpack(">H", f.read(2))[0]
                if version not in (1, 2):
                    raise ValueError(f"未知版本号: {version}")
                f.read(6)  # 保留字节
                channels  = struct.unpack(">H", f.read(2))[0]
                height    = struct.unpack(">I", f.read(4))[0]
                width     = struct.unpack(">I", f.read(4))[0]
                depth     = struct.unpack(">H", f.read(2))[0]
                cm_raw    = struct.unpack(">H", f.read(2))[0]

            meta.update({
                "width":      width,
                "height":     height,
                "bit_depth":  f"{depth} bit",
                "color_mode": color_mode_map.get(cm_raw, f"未知({cm_raw})"),
                "format":     "PSB" if self._is_psb else "PSD",
            })
        except Exception:
            pass  # 头信息读取失败不影响预览
        return meta

    # ── 提取内嵌预览图 ────────────────────────────────────────────

    def _extract_thumbnail(self, file_path: str) -> Image.Image:
        """
        从 Image Resources 段提取内嵌预览图。
        PSD/PSB 两者该段结构完全相同，均在文件开头附近，无需遍历图层。
        通常 < 0.3 秒。
        """
        # 方法 1：psd-tools（最可靠）
        try:
            return self._via_psd_tools(file_path)
        except Exception:
            pass

        # 方法 2：纯二进制解析（不依赖 psd-tools 版本）
        try:
            return self._via_binary(file_path)
        except Exception:
            pass

        fmt = "PSB" if self._is_psb else "PSD"
        raise RuntimeError(
            f"该 {fmt} 文件没有内嵌预览图。\n\n"
            "请在 Photoshop 中重新保存，并确保勾选了「存储缩略图」选项：\n"
            "文件 → 存储为（或另存为）→ 勾选「存储缩略图/Thumbnail」"
        )

    def _via_psd_tools(self, file_path: str) -> Image.Image:
        """用 psd-tools 提取缩略图，不触发图层合并"""
        from psd_tools import PSDImage

        psd = PSDImage.open(file_path)
        image_resources = psd.image_resources

        # 资源 ID 1036 = JPEG 缩略图（RGB），1033 = 旧版缩略图（BGR）
        for res_id in (1036, 1033):
            try:
                res = image_resources.get_data(res_id)
                if res is None:
                    continue
                # psd-tools 返回 ThumbnailResource，其 .data 是 PIL Image
                img = getattr(res, "data", None) or getattr(res, "thumbnail", None)
                if img is not None and isinstance(img, Image.Image):
                    return img.convert("RGBA")
            except Exception:
                continue

        # fallback：topil() 读取合并图像数据块（已渲染，不重新合并图层）
        img = psd.topil()
        if img is not None:
            return img.convert("RGBA")

        raise RuntimeError("psd-tools 未能提取预览图")

    def _via_binary(self, file_path: str) -> Image.Image:
        """
        纯二进制解析 Image Resources 段，提取缩略图 JPEG。
        PSD 和 PSB 该段结构完全一致，version 差异不影响此段。
        """
        with open(file_path, "rb") as f:
            # ── 跳过文件头 ──
            f.seek(4 + 2 + 6 + 2 + 4 + 4 + 2 + 2)  # = 26 字节

            # ── 跳过 Color Mode Data 段 ──
            color_mode_len = struct.unpack(">I", f.read(4))[0]
            f.seek(color_mode_len, 1)

            # ── 进入 Image Resources 段 ──
            img_res_len = struct.unpack(">I", f.read(4))[0]
            img_res_end = f.tell() + img_res_len

            while f.tell() < img_res_end - 4:
                marker = f.read(4)
                if marker != b"8BIM":
                    break

                res_id = struct.unpack(">H", f.read(2))[0]

                # Pascal string（名称，通常为空）：长度字节 + 内容，需 2 字节对齐
                ps_len = struct.unpack(">B", f.read(1))[0]
                # 跳过字符串内容，然后补齐到偶数（含长度字节本身）
                skip = ps_len + (1 if (ps_len + 1) % 2 != 0 else 0)
                f.seek(skip, 1)

                data_len = struct.unpack(">I", f.read(4))[0]
                data_start = f.tell()

                if res_id in (1036, 1033):
                    # ThumbnailResource 头（28 字节）：
                    #   format(4) + width(4) + height(4) + widthBytes(4)
                    #   + totalSize(4) + compressedSize(4) + bitsPerPixel(2) + planes(2)
                    f.seek(28, 1)
                    jpeg_len = data_len - 28
                    if jpeg_len > 0:
                        jpeg_data = f.read(jpeg_len)
                        img = Image.open(io.BytesIO(jpeg_data))
                        # 1033（旧格式）是 BGR，需翻转通道
                        if res_id == 1033 and img.mode in ("RGB", "RGBA"):
                            r, g, b = img.split()[:3]
                            img = Image.merge("RGB", (b, g, r))
                        return img.convert("RGBA")

                # 跳到下一个资源（数据长度需对齐到偶数）
                next_pos = data_start + data_len + (data_len % 2)
                f.seek(next_pos, 0)

        raise RuntimeError("二进制解析未找到缩略图资源（资源 ID 1033/1036）")

    # ── 对外接口 ─────────────────────────────────────────────────

    def composite(self) -> Image.Image:
        """返回预览图（供 main.py 调用）"""
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
        self._file_path = None
        self._meta = {}
        self._is_psb = False
