
import os
import logging
from utils import convert_heic_to_jpg, convert_mov_to_mp4, inject_motion_photo_metadata, get_file_size

logger = logging.getLogger(__name__)

def create_motion_photo(image_path, mov_path, output_jpg_path):
    """
    Convert Image (HEIC/JPG) + MOV to a Motion Photo JPG using the safe strategy:
    1. Convert Image to JPG (if needed).
    2. Convert MOV to MP4.
    3. Inject Motion Photo XMP/Exif metadata into the JPG.
    4. Append the MP4 to the end of the JPG.
    """
    temp_jpg = output_jpg_path + ".temp.jpg"
    temp_mp4 = output_jpg_path + ".temp.mp4"
    
    try:
        # 1. Convert Image
        # Always convert to JPG to ensure standard format and handle misnamed HEIC files
        logger.info(f"Converting Image: {image_path}")
        convert_heic_to_jpg(image_path, temp_jpg)
        
        # 2. Convert Video
        logger.info(f"Converting Video: {mov_path}")
        convert_mov_to_mp4(mov_path, temp_mp4)
        
        # 3. Get Video Size
        video_size = get_file_size(temp_mp4)
        
        # 4. Inject Metadata into JPG *before* appending
        # This is safer as it ensures exiftool doesn't mess with appended binary data.
        logger.info(f"Injecting Metadata (Offset={video_size})")
        inject_motion_photo_metadata(temp_jpg, video_size)
        
        # 5. Combine JPG and MP4
        logger.info("Combining JPG and MP4")
        with open(output_jpg_path, "wb") as f_out:
            with open(temp_jpg, "rb") as f_jpg:
                f_out.write(f_jpg.read())
            with open(temp_mp4, "rb") as f_mp4:
                f_out.write(f_mp4.read())
                
        logger.info(f"Successfully created Motion Photo: {output_jpg_path}")
        
    except Exception as e:
        logger.error(f"Failed to create motion photo: {e}")
        raise e
    finally:
        # Cleanup
        if os.path.exists(temp_jpg): os.remove(temp_jpg)
        if os.path.exists(temp_mp4): os.remove(temp_mp4)


