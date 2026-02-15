import os
import sys
import argparse
import logging
import tempfile
import shutil
import subprocess
import uuid

from converter import create_motion_photo
from utils import (
    extract_livp,
    split_motion_photo_jpg,
    get_motion_photo_video_size,
    get_motion_photo_presentation_timestamp_us,
    convert_jpg_to_heic,
    convert_mp4_to_mov,
    inject_heic_makernotes_from_file,
    set_heic_content_identifier,
    set_heic_live_photo_video_index,
    set_mov_content_identifier,
)

# 预提取的 MakerNotes 二进制（项目根目录，JPG→HEIC+MOV 时自动注入）
MAKERNOTES_BIN = os.path.join(os.path.dirname(__file__), "makernotes_apple.bin")

logger = logging.getLogger(__name__)


def jpg_motion_to_heic_mov(jpg_path, output_heic_path=None):
    """
    Motion Photo JPG → HEIC + MOV（Apple Live Photo），保持元数据与 Content Identifier。
    output_heic_path: 指定输出的 HEIC 路径，MOV 自动为同目录同名 .mov；未指定则与输入同名同目录。
    自动使用项目根目录的 makernotes_apple.bin。
    """
    video_size = get_motion_photo_video_size(jpg_path)
    if video_size is None or video_size <= 0:
        raise ValueError(
            "Not a motion photo or missing video size. "
            "JPG must have XMP-GCamera:MicroVideoOffset or Container Directory Item Length."
        )
    if output_heic_path:
        out_dir = os.path.dirname(os.path.abspath(output_heic_path))
        base = os.path.splitext(os.path.basename(output_heic_path))[0]
        heic_path = os.path.join(out_dir, base + ".HEIC")
        mov_path = os.path.join(out_dir, base + ".mov")
    else:
        out_dir = os.path.dirname(os.path.abspath(jpg_path))
        base = os.path.splitext(os.path.basename(jpg_path))[0]
        if base.lower().endswith(".jpg"):
            base = os.path.splitext(base)[0]
        heic_path = os.path.join(out_dir, base + ".HEIC")
        mov_path = os.path.join(out_dir, base + ".mov")
    os.makedirs(out_dir, exist_ok=True)

    content_uuid = str(uuid.uuid4()).upper()
    logger.info("Content Identifier (HEIC+MOV): %s", content_uuid)
    cover_ts_us = get_motion_photo_presentation_timestamp_us(jpg_path)
    logger.info("Motion Photo cover timestamp (us): %s", cover_ts_us if cover_ts_us is not None else "N/A")

    temp_dir = tempfile.mkdtemp(prefix="motion_photo_")
    try:
        static_jpg = os.path.join(temp_dir, "static.jpg")
        embedded_mp4 = os.path.join(temp_dir, "video.mp4")
        split_motion_photo_jpg(jpg_path, static_jpg, embedded_mp4)
        convert_jpg_to_heic(static_jpg, heic_path, metadata_source_path=jpg_path)

        if os.path.isfile(MAKERNOTES_BIN):
            logger.info("Injecting MakerNotes from %s...", os.path.basename(MAKERNOTES_BIN))
            inject_heic_makernotes_from_file(heic_path, MAKERNOTES_BIN)
        else:
            logger.warning("MakerNotes binary not found: %s", MAKERNOTES_BIN)

        convert_mp4_to_mov(embedded_mp4, mov_path, presentation_timestamp_us=cover_ts_us, content_uuid=content_uuid)
        set_heic_content_identifier(heic_path, content_uuid)
        if cover_ts_us is not None:
            set_heic_live_photo_video_index(heic_path, cover_ts_us)
        set_mov_content_identifier(mov_path, content_uuid)
        return heic_path, mov_path
    finally:
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Convert between Live Photo (HEIC+MOV/LIVP) and Motion Photo (JPG). "
        "Input .livp/.heic → output JPG; input .jpg (motion photo) → output HEIC + MOV."
    )
    parser.add_argument(
        "--input", "-i", required=True,
        help="Input file: .livp, .heic (with same-name .mov), or .jpg (motion photo)",
    )
    parser.add_argument(
        "--output", "-o",
        help="For .livp/.heic: output JPG path. For .jpg: output HEIC path (MOV same dir/name with .mov); default: same dir/name as input",
    )
    parser.add_argument("--log", "-l", action="store_true", help="Enable logging output")

    args = parser.parse_args()

    if args.log:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    input_path = args.input
    output_path = args.output

    if not os.path.exists(input_path):
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    temp_dir = None
    try:
        lower = input_path.lower()
        if lower.endswith(".livp"):
            if not output_path:
                output_path = os.path.splitext(input_path)[0] + ".jpg"
            if output_path.lower() == input_path.lower():
                output_path = os.path.splitext(input_path)[0] + "_motion.jpg"
            logger.info("Processing LIVP file: %s", input_path)
            temp_dir = tempfile.mkdtemp()
            image_path, mov_path = extract_livp(input_path, temp_dir)
            if not image_path or not mov_path:
                logger.error("LIVP must contain an image (.heic or .jpg) and a video (.mov)")
                sys.exit(1)
            logger.info("Input image: %s", image_path)
            logger.info("Input MOV: %s", mov_path)
            create_motion_photo(image_path, mov_path, output_path)
            logger.info("Output: %s", output_path)

        elif lower.endswith(".heic"):
            if not output_path:
                output_path = os.path.splitext(input_path)[0] + ".jpg"
            if output_path.lower() == input_path.lower():
                output_path = os.path.splitext(input_path)[0] + "_motion.jpg"
            logger.info("Processing HEIC file: %s", input_path)
            base = os.path.splitext(input_path)[0]
            mov_path = base + ".mov"
            if not os.path.exists(mov_path):
                mov_path = base + ".MOV"
            if not os.path.exists(mov_path):
                logger.error("Matching MOV file not found for: %s", input_path)
                sys.exit(1)
            logger.info("Input image: %s", input_path)
            logger.info("Input MOV: %s", mov_path)
            create_motion_photo(input_path, mov_path, output_path)
            logger.info("Output: %s", output_path)

        elif lower.endswith((".jpg", ".jpeg")):
            logger.info("Processing Motion Photo JPG: %s", input_path)
            heic_path, mov_path = jpg_motion_to_heic_mov(input_path, output_path)

        else:
            logger.error("Unsupported input format. Use .livp, .heic, or .jpg")
            sys.exit(1)

    except subprocess.CalledProcessError:
        sys.exit(1)
    except Exception as e:
        logger.error("Conversion failed: %s", e)
        sys.exit(1)
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    main()
