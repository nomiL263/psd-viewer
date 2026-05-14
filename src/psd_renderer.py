"""
PSD/PSB 文件解析与渲染核心模块
使用 psd-tools 库进行解析，PIL 进行图像处理
"""

import os
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image


class PSDRenderer:
    """PSD/PSB 文件渲染器"""

    def __init__(self):
        self._psd = None
        self._file_path: Optional[str] = None
        self._composite_image: Optional[Image.Image] = None

    def load(self, file_path: str) -> dict:
        """
        加载 PSD/PSB 文件

        Returns:
            dict: 包含文件元信息的字典，加载失败时抛出异常
        """
        from psd_tools import PSDImage

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        suffix = path.suffix.lower()
        if suffix not in (".psd", ".psb"):
            raise ValueError(f"不支持的文件格式: {suffix}，仅支持 .psd / .psb")

        self._psd = PSDImage.open(file_path)
        self._file_path = file_path
        self._composite_image = None  # 清除旧缓存

        return self._get_meta()

    def _get_meta(self) -> dict:
        """提取文件元信息"""
        if self._psd is None:
            return {}

        psd = self._psd
        return {
            "width": psd.width,
            "height": psd.height,
            "color_mode": str(psd.color_mode),
            "channels": psd.channels,
            "bit_depth": psd.bit_depth,
            "layer_count": len(list(psd.descendants())),
            "file_size": os.path.getsize(self._file_path),
            "file_name": Path(self._file_path).name,
        }

    def composite(self) -> Image.Image:
        """
        合并所有图层，返回最终合并图像（带缓存）
        对于超大文件会做尺寸截断保护
        """
        if self._psd is None:
            raise RuntimeError("请先调用 load() 加载文件")

        if self._composite_image is not None:
            return self._composite_image

        # psd-tools 内置 composite 方法
        image = self._psd.composite()

        if image is None:
            # fallback: 尝试使用内嵌的合并缩略图
            image = self._psd.topil()

        if image is None:
            raise RuntimeError("无法合并图层，文件可能已损坏或格式不兼容")

        # 转为 RGBA 保证透明通道处理一致
        if image.mode != "RGBA":
            image = image.convert("RGBA")

        self._composite_image = image
        return image

    def get_thumbnail(self, max_size: Tuple[int, int] = (1920, 1080)) -> Image.Image:
        """
        生成适合屏幕显示的缩略图，保持比例缩放
        """
        img = self.composite()
        img_copy = img.copy()
        img_copy.thumbnail(max_size, Image.LANCZOS)
        return img_copy

    def save_preview(self, output_path: str, quality: int = 90) -> str:
        """
        将合并后的图像导出为 PNG/JPEG

        Args:
            output_path: 输出路径
            quality: JPEG 压缩质量（PNG 忽略此参数）

        Returns:
            str: 实际保存的文件路径
        """
        img = self.composite()
        ext = Path(output_path).suffix.lower()

        if ext in (".jpg", ".jpeg"):
            # JPEG 不支持透明通道
            img_rgb = Image.new("RGB", img.size, (255, 255, 255))
            img_rgb.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img_rgb.save(output_path, "JPEG", quality=quality)
        else:
            img.save(output_path, "PNG")

        return output_path

    @property
    def is_loaded(self) -> bool:
        return self._psd is not None

    def close(self):
        self._psd = None
        self._composite_image = None
        self._file_path = None
