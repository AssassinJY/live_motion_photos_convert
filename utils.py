
import subprocess
import os
import logging

logger = logging.getLogger(__name__)

def run_command(command, check=True):
    """Run a shell command and return the result."""
    logger.info(f"Running command: {command}")
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        logger.error(f"Command failed with code {result.returncode}")
        logger.error(f"Stderr: {result.stderr}")
        logger.error(f"Stdout: {result.stdout}")
        raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
    return result

def convert_heic_to_jpg(input_path, output_path):
    """Convert HEIC to JPG, physically rotating pixels and resetting orientation metadata."""
    # -auto-orient physically rotates the pixels based on EXIF tag
    cmd = [
        'magick',
        input_path,
        '-auto-orient',
        '-quality', '95',
        '-sampling-factor', '4:2:0',
        '-colorspace', 'sRGB',
        output_path
    ]
    run_command(cmd)
    
    # Use exiftool to copy all metadata, but force Orientation to 1 (normal)
    # because we've already rotated the pixels.
    # We delete -Orientation first to clear any conflicting tags in different groups.
    copy_meta_cmd = [
        'exiftool',
        '-overwrite_original',
        '-TagsFromFile', input_path,
        '-all:all',
        '-unsafe',
        '-icc_profile',
        '-Orientation=',
        '-Orientation#=1',
        output_path
    ]
    run_command(copy_meta_cmd)

def get_video_info(path):
    """Get video stream information using ffprobe."""
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=pix_fmt,color_space,color_transfer,color_primaries,bits_per_raw_sample',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        path
    ]
    result = run_command(cmd)
    # Output format: pix_fmt, color_space, color_transfer, color_primaries, bits_per_raw_sample
    parts = result.stdout.strip().split('\n')
    return {
        'pix_fmt': parts[0] if len(parts) > 0 else '',
        'space': parts[1] if len(parts) > 1 else '',
        'transfer': parts[2] if len(parts) > 1 else '',
        'primaries': parts[3] if len(parts) > 3 else '',
        'bits': parts[4] if len(parts) > 4 else '8'
    }

def convert_mov_to_mp4(input_path, output_path):
    """Convert MOV to MP4 (HEVC/H.265), preserving HDR if present."""
    info = get_video_info(input_path)
    is_hdr = '10' in info['bits'] or info['transfer'] in ['arib-std-b67', 'smpte2084']
    
    cmd = [
        'ffmpeg',
        '-y',
        '-i', input_path,
        '-c:v', 'libx265',
        '-tag:v', 'hvc1',
        '-crf', '18',
    ]

    if is_hdr:
        logger.info("HDR content detected, preserving 10-bit and metadata.")
        cmd.extend([
            '-pix_fmt', 'yuv420p10le',
            '-x265-params', (
                f"range=limited:bframes=2:hdr10=1:repeat-headers=1:"
                f"colorprim={info['primaries']}:transfer={info['transfer']}:colormatrix={info['space']}"
            )
        ])
    else:
        # Match the successful wechat sample for SDR
        cmd.extend([
            '-pix_fmt', 'yuvj420p',
            '-x265-params', 'range=full:bframes=2:transfer=bt709:colorprim=smpte432:colormatrix=smpte170m'
        ])

    cmd.extend([
        '-c:a', 'aac',
        '-b:a', '192k',
        '-brand', 'mp42',
        '-movflags', '+faststart',
        output_path
    ])
    run_command(cmd)

def inject_motion_photo_metadata(jpg_path, video_size_bytes):
    """Inject Motion Photo XMP/Exif metadata using exiftool.
    
    Injects both v1 (MicroVideo) and v2 (GContainer) format tags for maximum compatibility.
    WeChat and some apps require the v2 GContainer:Directory structure or MicroVideo tags.
    """
    # Locate config file relative to project root
    project_root = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_root, '.exiftool_config')
    
    cmd = [
        'exiftool',
        '-config', config_path,
        '-overwrite_original',
        # Exif tags
        '-Exif:MicroVideo=1',           # Tag 0x8897
        '-Exif:EmbeddedVideo=1',        # Tag 0x9a01
        '-Exif:XiaomiMicroVideo=1',     # Tag 0x889f
        # XMP-GCamera tags (v1 format)
        '-XMP-GCamera:MicroVideo=1',
        '-XMP-GCamera:MicroVideoVersion=1',
        f'-XMP-GCamera:MicroVideoOffset={video_size_bytes}',
        '-XMP-GCamera:MicroVideoPresentationTimestampUs=0',
        # XMP-GCamera tags (redundant for Google Photos)
        '-XMP-GCamera:MotionPhoto=1',
        '-XMP-GCamera:MotionPhotoVersion=1',
        # XMP-Container tags (v2 format)
        '-XMP-Container:Directory[0]Mime=image/jpeg',
        '-XMP-Container:Directory[0]Semantic=Primary',
        '-XMP-Container:Directory[1]Mime=video/mp4',
        '-XMP-Container:Directory[1]Semantic=MotionPhoto',
        f'-XMP-Container:Directory[1]Length={video_size_bytes}',
        '-XMP-Container:Directory[1]Padding=0',
        jpg_path
    ]
    
    run_command(cmd)

def get_file_size(path):
    return os.path.getsize(path)

def extract_livp(livp_path, extract_to):
    """Extract .livp (zip) file and return (image_path, mov_path)."""
    import zipfile
    
    if not os.path.exists(extract_to):
        os.makedirs(extract_to)
        
    image_path = None
    mov_path = None
    
    with zipfile.ZipFile(livp_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)
        for file in zip_ref.namelist():
            lower_file = file.lower()
            if lower_file.endswith(('.heic', '.jpg', '.jpeg')):
                image_path = os.path.abspath(os.path.join(extract_to, file))
            elif lower_file.endswith('.mov'):
                mov_path = os.path.abspath(os.path.join(extract_to, file))

    return image_path, mov_path


def get_motion_photo_video_size(jpg_path):
    """
    从 Motion Photo JPG 的 XMP/Exif 中读取末尾嵌入视频的字节数。
    兼容：Google MicroVideoOffset、XMP-Container Directory[1].Length、
    以及 Xiaomi 等使用的 XMP-GContainer DirectoryItemLength（单值或列表的最后一格为视频长度）。
    返回 int，若不是 motion photo 或读取失败则返回 None。
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_root, '.exiftool_config')
    # 先尝试带 config 的 Google/标准标签
    cmd = [
        'exiftool',
        '-config', config_path,
        '-s', '-n',
        '-XMP-GCamera:MicroVideoOffset',
        '-XMP-Container:Directory1Length',
        jpg_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            value = line.split(':')[-1].strip()
            if value.isdigit():
                return int(value)
    # 再尝试无 config 的 DirectoryItemLength（Xiaomi/MVIMG 等用 GContainer 单值或列表）
    cmd2 = [
        'exiftool',
        '-s', '-n',
        '-DirectoryItemLength',
        jpg_path
    ]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    if result2.returncode == 0 and result2.stdout.strip():
        for line in result2.stdout.strip().splitlines():
            value = line.split(':')[-1].strip()
            # 可能是 "4319558" 或 "static_len, video_len"
            parts = [p.strip() for p in value.split(',')]
            if parts:
                last = parts[-1]
                if last.isdigit():
                    return int(last)
    return None


def split_motion_photo_jpg(jpg_path, out_static_jpg_path, out_video_path):
    """
    按 Motion Photo 标准将 JPG 拆成：静态图（纯 JPG）和末尾嵌入的视频（MP4）。
    """
    video_size = get_motion_photo_video_size(jpg_path)
    if video_size is None or video_size <= 0:
        raise ValueError(f"Not a motion photo or missing video size: {jpg_path}")

    file_size = os.path.getsize(jpg_path)
    static_size = file_size - video_size
    if static_size <= 0:
        raise ValueError(f"Invalid motion photo: video size {video_size} >= file size {file_size}")

    with open(jpg_path, 'rb') as f:
        static_data = f.read(static_size)
        video_data = f.read()

    if len(video_data) != video_size:
        raise ValueError(f"Unexpected trailing bytes: expected {video_size}, got {len(video_data)}")

    with open(out_static_jpg_path, 'wb') as f:
        f.write(static_data)
    with open(out_video_path, 'wb') as f:
        f.write(video_data)

    return out_static_jpg_path, out_video_path


def convert_jpg_to_heic(jpg_path, heic_path, metadata_source_path=None):
    """
    使用 ImageMagick (magick) 将 JPG 转为 HEIC，并尽量保留元数据。
    若提供 metadata_source_path，则用 exiftool 从该文件复制元数据到 HEIC（会去掉 Motion Photo 相关标签）。
    """
    cmd = [
        'magick',
        jpg_path,
        '-auto-orient',
        '-quality', '95',
        heic_path
    ]
    run_command(cmd)

    source = metadata_source_path or jpg_path
    strip_cmd = [
        'exiftool',
        '-overwrite_original',
        '-TagsFromFile', source,
        '-all:all',
        '-unsafe',
        '-icc_profile',
        '-MicroVideo=',
        '-EmbeddedVideo=',
        '-XiaomiMicroVideo=',
        '-XMP-GCamera:MicroVideo=',
        '-XMP-GCamera:MicroVideoVersion=',
        '-XMP-GCamera:MicroVideoOffset=',
        '-XMP-GCamera:MicroVideoPresentationTimestampUs=',
        '-XMP-GCamera:MotionPhoto=',
        '-XMP-GCamera:MotionPhotoVersion=',
        '-XMP-Container:Directory=',
        heic_path
    ]
    run_command(strip_cmd)


def convert_mp4_to_mov(mp4_path, mov_path):
    """
    使用 ffmpeg 将 MP4 转为 MOV，保留流与元数据。
    """
    cmd = [
        'ffmpeg',
        '-y',
        '-i', mp4_path,
        '-c', 'copy',
        '-map_metadata', '0',
        '-movflags', '+faststart',
        mov_path
    ]
    run_command(cmd)


def inject_heic_makernotes_from_file(heic_path, makernotes_bin_path):
    """
    将预先提取的 MakerNotes 二进制块注入目标 HEIC。
    使用项目内 makernotes_apple.bin（从模板 HEIC 一次性提取），不再依赖模板图片。
    """
    cmd = [
        'exiftool',
        '-overwrite_original',
        f'-MakerNotes<={makernotes_bin_path}',
        heic_path
    ]
    run_command(cmd)


def set_heic_content_identifier(heic_path, content_uuid):
    """
    在 HEIC 的 [MakerNotes] 中设置 Content Identifier 为指定 UUID（与 Apple 导出一致）。
    需先通过 inject_heic_makernotes_from_file 注入 MakerNotes 块。
    用于与同组 MOV 的 Content Identifier 一致，供系统识别 Live Photo 配对。
    """
    cmd = [
        'exiftool',
        '-overwrite_original',
        f'-ContentIdentifier={content_uuid}',
        heic_path
    ]
    run_command(cmd)


def set_mov_content_identifier(mov_path, content_uuid):
    """
    在 MOV 的 [QuickTime Keys] 中设置 Content Identifier 为指定 UUID。
    与 HEIC 的 Content Identifier 保持一致，供系统识别 Live Photo 配对。
    """
    cmd = [
        'exiftool',
        '-overwrite_original',
        f'-Keys:ContentIdentifier={content_uuid}',
        mov_path
    ]
    run_command(cmd)
