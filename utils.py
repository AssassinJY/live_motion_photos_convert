
import subprocess
import os
import sys
import logging
import shutil
import tempfile
import json
from pathlib import Path

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


def _tool_exists(name):
    return shutil.which(name) is not None


def _copy_metadata_with_normalized_orientation(input_path, output_path):
    # Keep metadata parity with the old flow while ensuring displayed orientation is normalized.
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


def _read_exiftool_values(input_path, tags):
    cmd = ['exiftool', '-s3'] + tags + [input_path]
    result = run_command(cmd, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_likely_hdr_heic(input_path):
    lower = input_path.lower()
    if not (lower.endswith('.heic') or lower.endswith('.heif')):
        return False
    values = _read_exiftool_values(
        input_path,
        [
            '-XMP-HDRGainMap:HDRGainMapVersion',
            '-QuickTime:AuxiliaryImageType',
            '-ICC-cicp3:TransferCharacteristics',
        ],
    )
    return any(
        'hdrgainmap' in v.lower()
        or 'st 2084' in v.lower()
        or 'bt.2100' in v.lower()
        for v in values
    )


def _read_hdr_headroom(input_path):
    values = _read_exiftool_values(input_path, ['-XMP-HDRGainMap:HDRGainMapHeadroom'])
    if not values:
        return 4.0
    try:
        return max(float(values[0]), 1.0)
    except ValueError:
        return 4.0


def _find_hdr_gainmap_file(temp_dir, base_stem):
    direct = temp_dir / f'{base_stem}-urn:com:apple:photo:2020:aux:hdrgainmap.jpg'
    if direct.exists():
        return str(direct)
    matches = sorted(temp_dir.glob(f'{base_stem}-*hdrgainmap*.jpg'))
    return str(matches[0]) if matches else None


def _ensure_gainmap_has_icc(base_jpg_path, gainmap_path):
    """
    Some Apple HEIC gainmaps are missing ICC profile after decode via heif-convert.
    ultrahdr_app may reject such gainmaps, so copy ICC from base image when needed.
    """
    icc_values = _read_exiftool_values(gainmap_path, ['-ICC_Profile:ProfileDescription'])
    if icc_values:
        return
    run_command(
        [
            'exiftool',
            '-overwrite_original',
            '-TagsFromFile', base_jpg_path,
            '-icc_profile',
            gainmap_path,
        ],
        check=False,
    )


def _convert_heic_to_ultrahdr_jpg(input_path, output_path):
    if not (_tool_exists('heif-convert') and _tool_exists('ultrahdr_app') and _tool_exists('exiftool')):
        return False

    headroom = _read_hdr_headroom(input_path)
    with tempfile.TemporaryDirectory(prefix='ultrahdr_') as td:
        temp_dir = Path(td)
        base_stem = 'base'
        base_jpg = temp_dir / f'{base_stem}.jpg'
        cfg_path = temp_dir / 'gainmap.cfg'

        run_command(['heif-convert', '--with-aux', input_path, str(base_jpg)])
        gainmap_path = _find_hdr_gainmap_file(temp_dir, base_stem)
        if not gainmap_path:
            raise RuntimeError('HDR gain map auxiliary image not found after HEIC decode.')
        _ensure_gainmap_has_icc(str(base_jpg), gainmap_path)

        cfg_path.write_text(
            '\n'.join(
                [
                    f'--maxContentBoost {headroom:.6f}',
                    '--minContentBoost 1',
                    '--gamma 1',
                    '--offsetSdr 0',
                    '--offsetHdr 0',
                    '--hdrCapacityMin 1',
                    f'--hdrCapacityMax {headroom:.6f}',
                    '--useBaseColorSpace 0',
                ]
            ) + '\n',
            encoding='utf-8',
        )

        run_command(
            [
                'ultrahdr_app',
                '-m', '0',
                '-i', str(base_jpg),
                '-g', gainmap_path,
                '-f', str(cfg_path),
                '-z', output_path,
            ]
        )

    _copy_metadata_with_normalized_orientation(input_path, output_path)
    return True


def _extract_mpf_gainmap_jpeg(jpg_path, out_gainmap_path):
    """
    Try to extract MPImage2 (commonly gain map in Ultra HDR JPEG) as a standalone JPEG file.
    Returns True when extraction succeeds.
    """
    cmd = ['exiftool', '-b', '-MPImage2', jpg_path]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return False
    # JPEG SOI marker
    if not result.stdout.startswith(b'\xff\xd8\xff'):
        return False
    with open(out_gainmap_path, 'wb') as f:
        f.write(result.stdout)
    return True


def _is_likely_ultrahdr_jpg(jpg_path):
    lower = jpg_path.lower()
    if not (lower.endswith('.jpg') or lower.endswith('.jpeg')):
        return False
    values = _read_exiftool_values(
        jpg_path,
        [
            '-XMP-HDRGainMap:HDRGainMapVersion',
            '-MPF:NumberOfImages',
        ],
    )
    if any(v.strip() for v in values[:1]):
        return True
    for v in values[1:]:
        if v.isdigit() and int(v) > 1:
            return True
    return False


def _convert_ultrahdr_jpg_to_heic(jpg_path, heic_path):
    """
    Best-effort path for Ultra HDR JPEG -> HEIC:
    - keep a secondary image (MPImage2) in HEIC if present.
    - this preserves more HDR-related payload than plain JPEG decode/re-encode.
    """
    if not (_tool_exists('heif-enc') and _tool_exists('exiftool')):
        return False

    with tempfile.TemporaryDirectory(prefix='ultra_jpg_to_heic_') as td:
        temp_dir = Path(td)
        gainmap_jpg = temp_dir / 'gainmap.jpg'
        has_gainmap = _extract_mpf_gainmap_jpeg(jpg_path, str(gainmap_jpg))

        if has_gainmap:
            cmd = ['heif-enc', '-q', '95', jpg_path, str(gainmap_jpg), '-o', heic_path]
        else:
            cmd = ['heif-enc', '-q', '95', jpg_path, '-o', heic_path]
        run_command(cmd)
    return True

def convert_heic_to_jpg(input_path, output_path):
    """Convert HEIC to JPG, physically rotating pixels and resetting orientation metadata."""
    if _is_likely_hdr_heic(input_path):
        try:
            logger.info('HDR HEIC detected, attempting Ultra HDR JPEG conversion.')
            if _convert_heic_to_ultrahdr_jpg(input_path, output_path):
                return
        except Exception as e:
            logger.warning(f'Ultra HDR conversion failed, fallback to SDR JPEG path: {e}')

    # Fallback/SDR path (legacy behavior).
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
    _copy_metadata_with_normalized_orientation(input_path, output_path)

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

def _safe_non_negative_int(value, default=0):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def get_motion_photo_presentation_timestamp_us(jpg_path):
    """
    Read cover timestamp from Motion Photo JPG (microseconds).
    Prefer MotionPhotoPresentationTimestampUs, fallback to MicroVideoPresentationTimestampUs.
    Returns int or None.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_root, '.exiftool_config')
    cmd = [
        'exiftool',
        '-config', config_path,
        '-s3', '-n',
        '-XMP-GCamera:MotionPhotoPresentationTimestampUs',
        '-XMP-GCamera:MicroVideoPresentationTimestampUs',
        jpg_path
    ]
    result = run_command(cmd, check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            return int(line)
    return None


def get_cover_timestamp_us_from_image_metadata(image_path):
    """
    Read any known cover timestamp from still image metadata.
    Priority:
    1) XMP-GCamera:MotionPhotoPresentationTimestampUs
    2) XMP-GCamera:MicroVideoPresentationTimestampUs
    3) Apple:LivePhotoVideoIndex
    Returns int or None.
    """
    cmd = [
        'exiftool',
        '-s3', '-n',
        '-XMP-GCamera:MotionPhotoPresentationTimestampUs',
        '-XMP-GCamera:MicroVideoPresentationTimestampUs',
        '-LivePhotoVideoIndex',
        image_path
    ]
    result = run_command(cmd, check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def get_apple_live_photo_presentation_timestamp_us(mov_path):
    """
    Best-effort: infer Apple Live Photo cover timestamp from MOV timed metadata track.
    Returns microseconds or None.
    """
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'd',
        '-show_entries', 'stream=start_time,duration,nb_frames',
        '-of', 'json',
        mov_path
    ]
    result = run_command(cmd, check=False)
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout or '{}')
            streams = data.get('streams', [])
            candidates = []
            for stream in streams:
                start_time = stream.get('start_time')
                nb_frames = stream.get('nb_frames')
                duration = stream.get('duration')
                if start_time in (None, 'N/A'):
                    continue
                try:
                    start_time_f = float(start_time)
                except (TypeError, ValueError):
                    continue
                if start_time_f <= 0:
                    continue
                frame_count = _safe_non_negative_int(nb_frames, default=-1)
                if frame_count != 1:
                    continue
                duration_f = None
                if duration not in (None, 'N/A'):
                    try:
                        duration_f = float(duration)
                    except (TypeError, ValueError):
                        duration_f = None
                if duration_f is not None and duration_f > 0.02:
                    continue
                candidates.append(start_time_f)
            if candidates:
                return int(round(min(candidates) * 1_000_000))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Fallback: find tiny data packet with non-zero pts (still-image-time sample commonly 9 bytes).
    cmd2 = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'd',
        '-show_entries', 'packet=pts_time,size',
        '-of', 'json',
        mov_path
    ]
    result2 = run_command(cmd2, check=False)
    if result2.returncode != 0:
        return None
    try:
        data = json.loads(result2.stdout or '{}')
        packets = data.get('packets', [])
        pts_candidates = []
        for packet in packets:
            pts = packet.get('pts_time')
            size = _safe_non_negative_int(packet.get('size'), default=-1)
            if pts in (None, 'N/A') or size <= 0 or size > 16:
                continue
            try:
                pts_f = float(pts)
            except (TypeError, ValueError):
                continue
            if pts_f > 0:
                pts_candidates.append(pts_f)
        if not pts_candidates:
            return None
        return int(round(min(pts_candidates) * 1_000_000))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def inject_motion_photo_metadata(jpg_path, video_size_bytes, presentation_timestamp_us=0):
    """Inject Motion Photo XMP/Exif metadata using exiftool.
    
    Injects both v1 (MicroVideo) and v2 (GContainer) format tags for maximum compatibility.
    WeChat and some apps require the v2 GContainer:Directory structure or MicroVideo tags.
    """
    # Locate config file relative to project root
    project_root = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_root, '.exiftool_config')
    
    ts_us = _safe_non_negative_int(presentation_timestamp_us, default=0)
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
        f'-XMP-GCamera:MicroVideoPresentationTimestampUs={ts_us}',
        # XMP-GCamera tags (redundant for Google Photos)
        '-XMP-GCamera:MotionPhoto=1',
        '-XMP-GCamera:MotionPhotoVersion=1',
        f'-XMP-GCamera:MotionPhotoPresentationTimestampUs={ts_us}',
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
    used_enhanced_hdr_path = False
    if _is_likely_ultrahdr_jpg(jpg_path):
        try:
            logger.info('Ultra HDR JPEG detected, attempting enhanced JPG->HEIC conversion path.')
            used_enhanced_hdr_path = _convert_ultrahdr_jpg_to_heic(jpg_path, heic_path)
        except Exception as e:
            logger.warning(f'Enhanced HDR JPG->HEIC path failed, fallback to standard path: {e}')

    if not used_enhanced_hdr_path:
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
        '-XMP-GCamera:MotionPhotoPresentationTimestampUs=',
        '-XMP-Container:Directory=',
        heic_path
    ]
    run_command(strip_cmd)

    # Best-effort: copy known HDR-related XMP tags when present.
    # Even if some tags are not writable for HEIC on this exiftool build, do not fail conversion.
    hdr_meta_cmd = [
        'exiftool',
        '-overwrite_original',
        '-TagsFromFile', source,
        '-XMP-HDRGainMap:all',
        heic_path
    ]
    run_command(hdr_meta_cmd, check=False)


def convert_mp4_to_mov(mp4_path, mov_path, presentation_timestamp_us=None, content_uuid=None):
    """
    将 MP4 转为 MOV。
    - 当提供 presentation_timestamp_us 时，优先用 AVFoundation 写入 still-image-time timed metadata。
    - 失败时回退到 ffmpeg copy 路径。
    """
    avf_reason = None
    if presentation_timestamp_us is not None:
        ts_us = _safe_non_negative_int(presentation_timestamp_us, default=0)
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts', 'write_live_photo_mov.swift')
        if sys.platform != 'darwin':
            avf_reason = 'non-macOS environment'
        elif not os.path.isfile(script_path):
            avf_reason = f'AVFoundation writer script not found: {script_path}'
        elif not _tool_exists('xcrun') or not _tool_exists('swift'):
            avf_reason = 'xcrun/swift not available'
        else:
            swift_cmd = [
                'xcrun', 'swift', script_path,
                '--input', mp4_path,
                '--output', mov_path,
                '--still-time-seconds', f'{ts_us / 1_000_000.0:.6f}',
            ]
            if content_uuid:
                swift_cmd.extend(['--content-identifier', content_uuid])
            logger.info('Using AVFoundation MOV writer to preserve editable cover frame in Apple Photos.')
            result = run_command(swift_cmd, check=False)
            if result.returncode == 0:
                # AVAssetWriter may leave temp sidecar files (e.g. "*.mov.sb-*").
                for path in Path(mov_path).parent.glob(Path(mov_path).name + '.sb-*'):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                return
            avf_reason = 'AVFoundation writer failed'

    if avf_reason:
        logger.warning(
            'Falling back to ffmpeg MOV copy path (%s). Live Photo pairing should work, '
            'but editable key-frame metadata may be unavailable outside macOS AVFoundation.',
            avf_reason,
        )

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


def set_heic_live_photo_video_index(heic_path, presentation_timestamp_us):
    """
    Store converted cover timestamp in HEIC.
    - Best-effort Apple field: LivePhotoVideoIndex (may be non-writable on some HEICs).
    - Stable fallback field: XMP-GCamera Motion/Micro presentation timestamp.
    """
    ts_us = _safe_non_negative_int(presentation_timestamp_us, default=0)
    apple_cmd = [
        'exiftool',
        '-overwrite_original',
        f'-LivePhotoVideoIndex={ts_us}',
        heic_path
    ]
    run_command(apple_cmd, check=False)

    # Fallback cross-platform storage so timestamp survives future conversions.
    xmp_cmd = [
        'exiftool',
        '-overwrite_original',
        f'-XMP-GCamera:MotionPhotoPresentationTimestampUs={ts_us}',
        f'-XMP-GCamera:MicroVideoPresentationTimestampUs={ts_us}',
        heic_path
    ]
    run_command(xmp_cmd)


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
