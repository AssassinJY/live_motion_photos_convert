#!/usr/bin/env python3
"""
批量转换脚本：对指定目录中的文件按类型集中转换。
- livp: livp → jpg（.livp → Motion Photo JPG）
- heic: heic → jpg（.heic+.mov → Motion Photo JPG，HDR HEIC 会尽量转为 Ultra HDR JPG）
- jpg:  jpg → heic（Motion Photo JPG → HEIC + MOV）
"""
import os
import sys
import argparse
import logging
import tempfile
import shutil
import subprocess

from converter import create_motion_photo
from utils import extract_livp
from main import jpg_motion_to_heic_mov

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def has_ultrahdr_toolchain():
    """
    判断是否具备 HEIC HDR -> Ultra HDR JPG 所需工具链。
    不影响主流程：缺失时会自动走 SDR 回退路径。
    """
    return (
        shutil.which("heif-convert") is not None
        and shutil.which("ultrahdr_app") is not None
        and shutil.which("exiftool") is not None
    )


def collect_livp_files(input_dir):
    """收集输入目录下所有 .livp 文件（仅当前目录，不递归）。"""
    files = []
    for name in os.listdir(input_dir):
        if name.lower().endswith(".livp"):
            files.append(os.path.join(input_dir, name))
    return sorted(files)


def collect_heic_pairs(input_dir):
    """收集输入目录下所有 .heic 且存在同名 .mov 的文件对。"""
    pairs = []
    for name in os.listdir(input_dir):
        if not name.lower().endswith(".heic"):
            continue
        base = os.path.splitext(name)[0]
        mov_path = os.path.join(input_dir, base + ".mov")
        if not os.path.isfile(mov_path):
            mov_path = os.path.join(input_dir, base + ".MOV")
        if os.path.isfile(mov_path):
            pairs.append((os.path.join(input_dir, name), mov_path))
    return sorted(pairs, key=lambda x: x[0])


def collect_motion_jpg_files(input_dir):
    """收集输入目录下所有 .jpg/.jpeg 文件（假定为 Motion Photo）。"""
    files = []
    for name in os.listdir(input_dir):
        lower = name.lower()
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            files.append(os.path.join(input_dir, name))
    return sorted(files)


def convert_batch_livp(input_dir, output_dir):
    """批量：LIVP → JPG。返回 (成功数, 失败列表 [(path, error_msg), ...])。"""
    livp_list = collect_livp_files(input_dir)
    if not livp_list:
        print("输入目录下未找到 .livp 文件:", input_dir)
        return 0, []
    total = len(livp_list)
    print(f"共需处理 {total} 个文件")
    os.makedirs(output_dir, exist_ok=True)
    ok = 0
    failed = []
    for idx, livp_path in enumerate(livp_list, 1):
        print(f"[{idx}/{total}] 正在处理: {os.path.basename(livp_path)}")
        base = os.path.splitext(os.path.basename(livp_path))[0]
        out_jpg = os.path.join(output_dir, base + ".jpg")
        try:
            temp_dir = tempfile.mkdtemp(prefix="batch_livp_")
            try:
                image_path, mov_path = extract_livp(livp_path, temp_dir)
                if not image_path or not mov_path:
                    failed.append((livp_path, "LIVP 需包含图片和视频"))
                    continue
                create_motion_photo(image_path, mov_path, out_jpg)
                ok += 1
            finally:
                if os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir)
        except subprocess.CalledProcessError as e:
            failed.append((livp_path, str(e)))
        except Exception as e:
            failed.append((livp_path, str(e)))
    return ok, failed


def convert_batch_heic_to_jpg(input_dir, output_dir):
    """批量：HEIC+MOV → JPG。返回 (成功数, 失败列表 [(path, error_msg), ...])。"""
    heic_pairs = collect_heic_pairs(input_dir)
    if not heic_pairs:
        print("输入目录下未找到 .heic+.mov 文件对:", input_dir)
        return 0, []
    total = len(heic_pairs)
    print(f"共需处理 {total} 个文件")
    os.makedirs(output_dir, exist_ok=True)
    ok = 0
    failed = []
    for idx, (heic_path, mov_path) in enumerate(heic_pairs, 1):
        print(f"[{idx}/{total}] 正在处理: {os.path.basename(heic_path)} + .mov")
        base = os.path.splitext(os.path.basename(heic_path))[0]
        out_jpg = os.path.join(output_dir, base + ".jpg")
        try:
            create_motion_photo(heic_path, mov_path, out_jpg)
            ok += 1
        except subprocess.CalledProcessError as e:
            failed.append((heic_path, str(e)))
        except Exception as e:
            failed.append((heic_path, str(e)))
    return ok, failed


def convert_batch_jpg_to_heic(input_dir, output_dir):
    """批量：Motion Photo JPG → HEIC + MOV。返回 (成功数, 失败列表 [(path, error_msg), ...])。"""
    jpg_list = collect_motion_jpg_files(input_dir)
    if not jpg_list:
        print("输入目录下未找到 .jpg/.jpeg 文件:", input_dir)
        return 0, []
    total = len(jpg_list)
    print(f"共需处理 {total} 个文件")
    os.makedirs(output_dir, exist_ok=True)
    ok = 0
    failed = []
    for idx, jpg_path in enumerate(jpg_list, 1):
        print(f"[{idx}/{total}] 正在处理: {os.path.basename(jpg_path)}")
        base = os.path.splitext(os.path.basename(jpg_path))[0]
        if base.lower().endswith(".jpg"):
            base = os.path.splitext(base)[0]
        out_heic = os.path.join(output_dir, base + ".HEIC")
        try:
            jpg_motion_to_heic_mov(jpg_path, output_heic_path=out_heic)
            ok += 1
        except subprocess.CalledProcessError as e:
            failed.append((jpg_path, str(e)))
        except Exception as e:
            failed.append((jpg_path, str(e)))
    return ok, failed


def main():
    parser = argparse.ArgumentParser(
        description="批量转换：对指定目录按类型集中转换。"
        " livp: livp→jpg；heic: heic→jpg；jpg: jpg→heic。"
    )
    parser.add_argument(
        "--type", "-t",
        required=True,
        choices=["livp", "heic", "jpg"],
        help="转换类型: livp | heic | jpg",
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="输入目录",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="输出目录；未指定时在输入目录下以转换后格式为名的子目录（jpg/heic）",
    )

    args = parser.parse_args()

    input_dir = os.path.abspath(args.input)
    if not os.path.isdir(input_dir):
        logger.error("输入目录不存在: %s", input_dir)
        sys.exit(1)

    # 默认输出子目录名：按转换后的格式（livp→jpg, heic→jpg, jpg→heic）
    output_format_dir = {"livp": "jpg", "heic": "jpg", "jpg": "heic"}[args.type]
    output_dir = args.output
    if not output_dir:
        output_dir = os.path.join(input_dir, output_format_dir)
    output_dir = os.path.abspath(output_dir)

    # 仅提示，不改变现有批处理行为。
    if args.type in ("livp", "heic"):
        if has_ultrahdr_toolchain():
            print("HDR 支持: 已检测到 Ultra HDR 工具链，HDR HEIC 将尝试输出 Ultra HDR JPG。")
        else:
            print("HDR 支持: 未检测到完整 Ultra HDR 工具链，将使用 SDR 路径（不影响视频嵌入）。")

    if args.type == "livp":
        ok, failed = convert_batch_livp(input_dir, output_dir)
    elif args.type == "heic":
        ok, failed = convert_batch_heic_to_jpg(input_dir, output_dir)
    else:  # jpg
        ok, failed = convert_batch_jpg_to_heic(input_dir, output_dir)

    print(f"完成: 成功 {ok}, 失败 {len(failed)}, 输出目录 {output_dir}")
    if failed:
        print("\n失败的文件:")
        for path, err in failed:
            print(f"  - {path}")
            print(f"    {err}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
