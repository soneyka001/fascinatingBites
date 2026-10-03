import json
import os
import re
import subprocess
import time
import sys
import select
    
import xml.etree.ElementTree as ET
from xml.dom import minidom
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET
from xml.dom import minidom

import mlx_whisper
import requests
from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel, Field

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
BASE_DIR = Path(os.getenv("BASE_DIR", Path.home() / "Desktop/shorts"))
PROCESSED_VOICEOVER_DIR = BASE_DIR / "voiceover"
VOICEOVER_DIR = PROCESSED_VOICEOVER_DIR / "to_be_processed"
SUBVID_DIR = BASE_DIR / "materials" / "subVid"
ASS_DIR = BASE_DIR / "scripts" / "ass"
SCRIPT_DIR = BASE_DIR / "scripts"
SCRIPT_WORDS_DIR = BASE_DIR / "scripts" / "words"
POST_INFO_DIR = BASE_DIR / "postInfo"

# Platform Specific Post Info Directories
YOUTUBE_POST_DIR = POST_INFO_DIR / "YouTube"
TIKTOK_POST_DIR = POST_INFO_DIR / "TikTok"
INSTAGRAM_POST_DIR = POST_INFO_DIR / "Instagram"

SFX_DIR = BASE_DIR / "materials" / "sfx"
GUIDELINES_DIR = BASE_DIR / "materials" / "guidelines"
VIDEO_FILES_DIR = BASE_DIR / "materials" / "videoFiles"

FONT_NAME = "Impact"
FONT_SIZE = 120
WORDS_PER_SCREEN = 3
FPS = 30  # Subtitle & Asset Frame Rate

# --- MODEL FALLBACK PRIORITY LIST ---
FALLBACK_MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]

def ensure_directories() -> None:
    """Creates required folders if they don't exist."""
    dirs = [
        VOICEOVER_DIR, PROCESSED_VOICEOVER_DIR, SUBVID_DIR, ASS_DIR, SCRIPT_DIR, 
        SCRIPT_WORDS_DIR, POST_INFO_DIR, YOUTUBE_POST_DIR, TIKTOK_POST_DIR, 
        INSTAGRAM_POST_DIR, SFX_DIR, GUIDELINES_DIR, VIDEO_FILES_DIR
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)

# --- METADATA SCHEMAS ---
class YouTubeShortsMetadata(BaseModel):
    title: str = Field(description="A clickable, high-CTR YouTube Short title under 60 characters containing at least one relevant emoji.")
    description: str = Field(description="An optimized YouTube Shorts description with an engaging hook, brief context, call-to-action, and 3-5 relevant #hashtags including the #Shorts hashtag.")
    #tags: list[str] = Field(description="List of 10 to 15 relevant SEO tags/keywords for YouTube metadata.")

class TikTokMetadata(BaseModel):
    caption: str = Field(description="Punchy TikTok caption with a strong curiosity hook, concise context, and integrated hashtags.")

class InstagramReelsMetadata(BaseModel):
    caption: str = Field(description="Engaging Instagram Reels caption with a strong first-line hook, line breaks for readability, clear Call-to-Action (e.g., 'Save this', 'Share with a friend'), and integrated hashtags. No new lines.")

class MultiPlatformMetadata(BaseModel):
    youtube_shorts: YouTubeShortsMetadata
    tiktok: TikTokMetadata
    instagram_reels: InstagramReelsMetadata

# --- CREATIVE DIRECTION SCHEMAS ---
class VisualAsset(BaseModel):
    frame_start: int
    frame_end: int
    related_image: str | None = Field(default=None, description="Link to already downloaded/created image if needed to be modified for future scenes.")
    description: str = Field(description="Short description of the visual for file naming.")
    search_tags: str | None = Field(description="Keywords for Pexels/Unsplash/Pixabay searches.", default=None)
    generation_prompt: str | None = Field(description="Detailed prompt for AI Image generation.", default=None)

class SFXAsset(BaseModel):
    frame_start: int
    frame_end: int
    exact_sfx_filename: str = Field(description="Must exactly match an entry from the provided local SFX library index+.mp3 .")

class CreativeDirection(BaseModel):
    guidelines_md: str = Field(description="Markdown formatted text for editing/sound design guidelines.")
    ai_images: list[VisualAsset] = Field(description="AI Image prompts")
    stock_photos: list[VisualAsset] = Field(description="Pixabay/Unsplash Photo queries.")
    stock_videos: list[VisualAsset] = Field(description="Pixabay/Unsplash Video queries.")
    sfx_placements: list[SFXAsset] = Field(description="Placement of local SFX only. Do NOT include recommended/missing downloads here.")

# --- CONSOLIDATED SCHEMA ---
class VideoPackage(BaseModel):
    metadata: MultiPlatformMetadata
    creative_direction: CreativeDirection

# --- UTILITIES ---
def get_short_description(text: str, max_words: int = 3) -> str:
    """Cleans and shortens a description to be used safely in a filename."""
    if not text:
        return "asset"
    clean = re.sub(r'[^\w\s]', '', text)
    words = clean.strip().split()[:max_words]
    return "-".join(words).lower()

def get_audio_duration(file_path: Path) -> float:
    """Uses ffprobe to extract the exact duration of an audio file in seconds."""
    command = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(file_path)
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        print("  ⚠️ Warning: Could not determine exact audio duration with ffprobe. Defaulting to 60s.")
        return 60.0

def update_sfx_index() -> list[str]:
    """Scans /materials/sfx (including subfolders) and updates missing SFX filenames in the index file."""
    print(" 🔊 Indexing local SFX library...")
    index_file = BASE_DIR / "materials" / "sfx_index.json"

    existing_sfx = set()
    if index_file.exists():
        try:
            existing_sfx = set(json.loads(index_file.read_text(encoding='utf-8')))
        except Exception:
            pass

    actual_sfx = {
        f.stem for f in SFX_DIR.rglob('*') 
        if f.is_file() and f.suffix.lower() in {'.mp3', '.wav'}
    }

    updated_sfx = actual_sfx.union(existing_sfx)

    sfx_list = sorted(list(updated_sfx))
    index_file.write_text(json.dumps(sfx_list, indent=2), encoding='utf-8')
    print(f"  --> SFX library indexed: {len(sfx_list)} items available.")
    return sfx_list

def format_time_ass(seconds):
    """Converts seconds to ASS timestamp format (H:MM:SS.cs)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"

def create_ass_subtitles(words: list[dict], output_ass_path: Path) -> None:
    """Generates the .ass file with word-by-word highlights and scaling."""
    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{FONT_SIZE},&H00FFFFFF&,&H000000FF&,&H00000000&,&H00000000&,1,0,0,0,100,100,0,0,0,16,0,2,5,5,570,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(output_ass_path, 'w', encoding='utf-8') as f:
        f.write(ass_header)
        for i in range(0, len(words), WORDS_PER_SCREEN):
            chunk = words[i:i + WORDS_PER_SCREEN]
            if not chunk:
                continue

            chunk_end = chunk[-1]['end']
            for highlight_idx, highlight_word in enumerate(chunk):
                start_time = highlight_word['start']
                end_time = highlight_word['end']

                if highlight_idx < len(chunk) - 1:
                    end_time = chunk[highlight_idx + 1]['start']
                elif highlight_idx == len(chunk) - 1:
                    end_time = chunk_end

                formatted_text = ""
                for word_idx, w in enumerate(chunk):
                    clean_word = w['word'].strip().upper()
                    if word_idx == highlight_idx:
                        formatted_text += f"{{\\1c&H00FFFF&}}{{\\t(0,100,\\fscx115\\fscy115)}}{clean_word}{{\\fscx100\\fscy100}}{{\\1c&HFFFFFF&}} "
                    else:
                        formatted_text += f"{clean_word} "

                ass_line = f"Dialogue: 0,{format_time_ass(start_time)},{format_time_ass(end_time)},Default,,0,0,0,,{formatted_text.strip()}\n"
                f.write(ass_line)

def generate_transparent_video(
    ass_path: Path, 
    duration: float, 
    output_video_path: Path, 
    fps: int = 30
) -> None:
    """Uses FFmpeg to render the ASS file onto a transparent ProRes 4444 video."""
    if duration <= 0:
        raise ValueError("Duration must be greater than 0.")

    abs_output_path = str(output_video_path.resolve())
    ass_dir = ass_path.parent
    
    # Escape quotes and colons for FFmpeg filtergraph syntax
    escaped_ass_filename = ass_path.name.replace("'", "'\\''").replace(":", "\\:")

    command = [
        'ffmpeg', '-y', '-f', 'lavfi',
        '-i', f'color=size=1080x1920:rate={fps}:color=black@0.0,format=rgba',
        '-filter_complex', f"ass=filename='{escaped_ass_filename}':alpha=1",
        '-c:v', 'prores_ks',               # Use software encoder for alpha channel support
        '-profile:v', '4444',              # ProRes 4444
        '-pix_fmt', 'yuva444p10le',        # 10-bit YUVA format with alpha channel
        '-t', str(duration),
        '-progress', 'pipe:1',
        '-nostats', '-loglevel', 'error', 
        abs_output_path
    ]

    process = subprocess.Popen(
        command, 
        cwd=ass_dir, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.STDOUT, 
        text=True
    )

    log_buffer = []

    if process.stdout:
        for line in process.stdout:
            line_str = line.strip()
            if line_str.startswith("out_time_us="):
                try:
                    current_time = int(line_str.split("=")[1]) / 1_000_000
                    percent = min((current_time / duration) * 100, 100.0)
                    filled = int(30 * (percent / 100))
                    bar = '=' * filled + ('>' if filled < 30 else '') + '-' * (30 - filled - 1)
                    print(f"\r  --> Progress: [{bar}] {percent:.1f}% ({current_time:.1f}s / {duration:.1f}s)", end="", flush=True)
                except ValueError:
                    pass
            elif line_str:
                log_buffer.append(line_str)

    process.wait()

    if process.returncode == 0:
        print("\n  --> Rendering completed successfully.")
    else:
        print("\n  --> Error encountered during FFmpeg rendering:")
        print("\n".join(log_buffer) if log_buffer else "Unknown error occurred.")

# --- RESILIENT MODEL FALLBACK GENERATOR FOR GROQ ---
def generate_content_with_fallback(
    client: Groq,
    models: list[str],
    system_instruction: str,
    prompt: str,
    response_schema,
    temperature: float = 0.5,
    max_retries_per_model: int = 5,
    initial_delay: float = 2.0,
):
    """Iterates through a fallback list of Groq models. If rate limits are hit, switches to the next model."""
    last_exception = None

    for model_name in models:
        delay = initial_delay
        print(f"  🤖 Requesting generation from Groq model: {model_name}...")

        for attempt in range(1, max_retries_per_model + 1):
            try:
                # Groq uses response_format with Pydantic schemas for structured output
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=temperature,
                    response_format={"type": "json_object", "schema": response_schema.model_json_schema()},
                )
                
                # Parse the returned JSON into your Pydantic object
                json_content = response.choices[0].message.content
                parsed_data = response_schema.model_validate_json(json_content)

                print(f"  ✅ Successfully generated response using {model_name}")
                return parsed_data

            except Exception as e:
                last_exception = e
                err_msg = str(e)

                is_rate_or_quota = any(
                    k in err_msg for k in ["429", "503", "UNAVAILABLE", "rate_limit_exceeded", "Quota", "requests per minute"]
                )

                if is_rate_or_quota:
                    if "Quota" in err_msg or "daily" in err_msg.lower():
                        print(f"  ⚠️ Daily quota limit reached for {model_name}. Skipping to next model...")
                        break

                    if attempt < max_retries_per_model:
                        print(f"  ⚠️ Transient error on {model_name} (Attempt {attempt}/{max_retries_per_model}). Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                        delay *= 2
                    else:
                        print(f"  ⚠️ Model {model_name} exhausted retries. Trying fallback model...")
                else:
                    print(f"  ⚠️ Non-quota error with {model_name}: {err_msg}. Trying fallback model...")
                    break

    raise RuntimeError(f"All Groq fallback models failed. Last error: {last_exception}")

# --- STEP 2: GROQ PACKAGE GENERATION ---
def generate_video_package(
    script_text: str,
    words_frames: list[dict],
    sfx_list: list[str],
    video_identifier: str,
    client: Groq,
    models: list[str] = FALLBACK_MODELS,
) -> None:
    yt_path = YOUTUBE_POST_DIR / f"{video_identifier}.json"
    tk_path = TIKTOK_POST_DIR / f"{video_identifier}.json"
    ig_path = INSTAGRAM_POST_DIR / f"{video_identifier}.json"

    creative_dir = VIDEO_FILES_DIR / video_identifier / "creative_files"
    guidelines_file = GUIDELINES_DIR / f"{video_identifier}.md"
    img_json = creative_dir / "images.json"
    photo_json = creative_dir / "photos.json"
    vid_json = creative_dir / "videos.json"
    sfx_txt = creative_dir / "sfx.txt"

    all_files = [yt_path, tk_path, ig_path, guidelines_file, img_json, photo_json, vid_json, sfx_txt]

    if all(f.exists() for f in all_files):
        print(f"  [Step 2/7] All metadata and creative files exist for {video_identifier}. Skipping API call.")
        return

    if not script_text.strip():
        return

    creative_dir.mkdir(parents=True, exist_ok=True)

    compressed_frames = [f"[{wf['start_frame']}-{wf['end_frame']}]{wf['word']}" for wf in words_frames]
    words_dense_str = " ".join(compressed_frames)

    sfx_pool_str = ", ".join(sfx_list[:200]) if sfx_list else "None available"
    if len(sfx_list) > 200:
        sfx_pool_str += " ... (truncated)"

    system_instruction = (
        "You are an elite short-form video content strategist and creative director. "
        "Analyze the script and frame timings to generate optimized metadata and a creative asset map."
    )

    prompt = f"""
Analyze the script and frame mappings (FPS: {FPS}) to generate metadata and a creative asset map.

RULES for CREATIVE DIRECTION:
1. Visual JSONs (images, photos, videos) must feature NON-OVERLAPPING frame ranges. Change visual elements ~1 per second. Do NOT download the same image/video 2+ times, add a notification to guidelines with all the timecodes.
2. SFX Placement must exclusively map local filenames from AVAILABLE LOCAL SFX.
3. Guidelines (Markdown) must include editing instructions, reasoning, frame ranges, and missing SFX recommendations.
4. CRITICAL: For EVERY item in `ai_images`, you MUST provide a detailed `generation_prompt`. For `stock_photos` and `stock_videos`, you MUST provide `search_tags`.

--- SCRIPT START ---
{script_text}
--- SCRIPT END ---

--- AVAILABLE LOCAL SFX ---
[{sfx_pool_str}]

--- COMPRESSED WORD-FRAME MAP ([start-end]word) ---
{words_dense_str}
"""
    print("  [Step 2/7] Generating Metadata & Creative Direction...", flush=True)
    try:
        # Call the updated Groq fallback handler
        data: VideoPackage = generate_content_with_fallback(
            client=client,
            models=models,
            system_instruction=system_instruction,
            prompt=prompt,
            response_schema=VideoPackage,
            temperature=0.5,
        )

        yt_path.write_text(data.metadata.youtube_shorts.model_dump_json(indent=2), encoding="utf-8")
        tk_path.write_text(data.metadata.tiktok.model_dump_json(indent=2), encoding="utf-8")
        ig_path.write_text(data.metadata.instagram_reels.model_dump_json(indent=2), encoding="utf-8")

        guidelines_file.write_text(data.creative_direction.guidelines_md, encoding="utf-8")
        img_json.write_text(json.dumps([asset.model_dump() for asset in data.creative_direction.ai_images], indent=2), encoding="utf-8")
        photo_json.write_text(json.dumps([asset.model_dump() for asset in data.creative_direction.stock_photos], indent=2), encoding="utf-8")
        vid_json.write_text(json.dumps([asset.model_dump() for asset in data.creative_direction.stock_videos], indent=2), encoding="utf-8")

        with open(sfx_txt, "w", encoding="utf-8") as f:
            for sfx in data.creative_direction.sfx_placements:
                f.write(f"{sfx.frame_start},{sfx.frame_end},{sfx.exact_sfx_filename}\n")

        print(f"  --> Successfully generated video package for: {video_identifier}")
    except Exception as e:
        print(f"  --> Error generating video package: {e}")

# --- STEP 3: STOCK ASSET DOWNLOADING ---
def download_pixabay_assets(video_identifier: str, api_key: str | None = None) -> None:
    """Downloads stock videos and photos concurrently."""
    resolved_key = api_key or os.getenv("PIXABAY_API_KEY")
    if not resolved_key:
        print("  [Step 3/7] Warning: No Pixabay API key found. Skipping stock downloads.")
        return

    creative_dir = VIDEO_FILES_DIR / video_identifier / "creative_files"
    vid_json_path = creative_dir / "videos.json"
    photo_json_path = creative_dir / "photos.json"

    assets_to_download = []

    try:
        if vid_json_path.exists():
            vids = json.loads(vid_json_path.read_text(encoding="utf-8"))
            assets_to_download.extend([(v, "video") for v in vids])
        if photo_json_path.exists():
            photos = json.loads(photo_json_path.read_text(encoding="utf-8"))
            assets_to_download.extend([(p, "photo") for p in photos])
    except Exception as e:
        print(f"  --> Error reading stock asset configuration: {e}")
        return

    if not assets_to_download:
        return

    print(f"  [Step 3/7] Downloading {len(assets_to_download)} Pixabay stock assets concurrently...")

    def fetch_single_asset(args: tuple) -> None:
        asset, asset_type = args
        search_tag = asset.get("search_tags") or asset.get("description")
        if not search_tag:
            return

        start_frame = asset.get("frame_start", 0)
        end_frame = asset.get("frame_end", 0)
        short_desc = get_short_description(asset.get("description") or search_tag)

        try:
            query = requests.utils.quote(search_tag)
            if asset_type == "video":
                url = f"https://pixabay.com/api/videos/?key={resolved_key}&q={query}&per_page=3"
            else:
                url = f"https://pixabay.com/api/?key={resolved_key}&q={query}&per_page=3&image_type=photo"

            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()

            if data.get("hits"):
                if asset_type == "video":
                    asset_url = data["hits"][0]["videos"]["large"]["url"]
                    ext = ".mp4"
                else:
                    asset_url = data["hits"][0]["largeImageURL"]
                    ext = ".jpg"

                file_name = VIDEO_FILES_DIR / video_identifier / f"{start_frame}to{end_frame}_{short_desc}_stock{ext}"

                if file_name.exists():
                    return

                with requests.get(asset_url, stream=True, timeout=15) as r:
                    r.raise_for_status()
                    with open(file_name, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                print(f"  --> Downloaded stock {asset_type}: {file_name.name}")
            else:
                print(f"  ⚠️ No Pixabay {asset_type} found for '{search_tag}'")
        except Exception as item_err:
            print(f"  ⚠️ Skipping {asset_type} '{search_tag}': {item_err}")

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(fetch_single_asset, assets_to_download))

# --- STEP 4: LOCAL AI IMAGE GENERATION ---
def generate_ai_images_cli(video_identifier: str, client: genai.Client | None = None) -> None:
    """Generates images locally using the mflux-generate CLI tool to the numbered video root."""
    import sys # Ensure sys is imported for the progress bar
    video_dir = VIDEO_FILES_DIR / video_identifier
    creative_dir = video_dir / "creative_files"
    img_json_path = creative_dir / "images.json"

    if not img_json_path.exists():
        return

    image_requests = json.loads(img_json_path.read_text(encoding="utf-8"))
    if not image_requests:
        return

    total_images = len(image_requests)
    print(f"  [Step 4/7] Generating {total_images} AI images locally via mflux-generate CLI...")

    for idx, asset in enumerate(image_requests):
        prompt = asset.get("generation_prompt") or asset.get("description")
        if not prompt:
            print(f"  ⚠️ [{idx+1}/{total_images}] Missing prompt and description, skipping.")
            continue

        start_frame = asset.get("frame_start", 0)
        end_frame = asset.get("frame_end", 0)
        short_desc = get_short_description(asset.get("description") or prompt)

        file_name = video_dir / f"{start_frame}to{end_frame}_{short_desc}_ai.jpeg"
        
        if file_name.exists():
            print(f"  --> [{idx+1}/{total_images}] Skipping existing local image: {file_name.name}")
            continue

        # Draw the dynamic progress bar
        bar_length = 20
        filled = int((idx / total_images) * bar_length)
        bar = '█' * filled + '-' * (bar_length - filled)
        sys.stdout.write(f"\r  [{bar}] {idx}/{total_images} | Generating: {short_desc[:25]}...")
        sys.stdout.flush()

        cmd = [
            "mflux-generate",
            "--model", "schnell",
            "--quantize", "4",
            "--prompt", f"{prompt}, 9:16 vertical short video background",
            "--steps", "4",
            "--width", "576",
            "--height", "1024",
            "--output", str(file_name)
        ]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Clear the progress line before printing success to avoid visual glitches
            sys.stdout.write("\r" + " " * 80 + "\r")
            print(f"  --> [{idx+1}/{total_images}] Saved local AI image: {file_name.name}")
        except subprocess.CalledProcessError as err:
            sys.stdout.write("\r" + " " * 80 + "\r")
            print(f"  ⚠️ [{idx+1}/{total_images}] Failed generating local image '{file_name.name}' via CLI: {err}")
            
    print("  ✅ Finished generating all AI images.")

# --- STEP 7: DAVINCI RESOLVE TIMELINE GENERATION ---
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
import re
from urllib.parse import quote

def create_resolve_timeline(video_identifier: str, audio_path: Path, total_duration: float, fps: int = FPS) -> Path:
    """Creates an FCP7 XML (.xml) timeline file that imports natively into DaVinci Resolve."""
    print("  [Step 7/7] Exporting DaVinci Resolve FCPXML Timeline...", flush=True)
    total_frames = int(round(total_duration * fps))
    if total_frames <= 0:
        total_frames = 1800  # Default 60 seconds fallback

    video_dir = VIDEO_FILES_DIR / video_identifier
    creative_dir = video_dir / "creative_files"
    output_xml_path = video_dir / f"{video_identifier}_timeline.xml"

    # 1. Parse V1 & V2 Visual Clips
    v1_clips = []
    if video_dir.exists():
        for item in video_dir.iterdir():
            if item.is_file() and item.suffix.lower() in {'.jpeg', '.jpg', '.png', '.mp4', '.mov'}:
                m = re.match(r"^(\d+)to(\d+)_(.*)$", item.name)
                if m:
                    s_frame = int(m.group(1))
                    e_frame = int(m.group(2))
                    v1_clips.append({
                        "path": item,
                        "start": s_frame,
                        "end": e_frame,
                        "is_video": item.suffix.lower() in {'.mp4', '.mov'}
                    })
    v1_clips.sort(key=lambda x: x["start"])

    # 2. Parse A2+ SFX Placements
    sfx_clips = []
    sfx_txt = creative_dir / "sfx.txt"
    if sfx_txt.exists():
        lines = sfx_txt.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                try:
                    s_frame = int(parts[0])
                    e_frame = int(parts[1])
                    sfx_name = parts[2].strip()

                    if e_frame <= s_frame:
                        e_frame = s_frame + int(fps)

                    found_sfx = None
                    search_dirs = [SFX_DIR, creative_dir] if 'SFX_DIR' in globals() else [creative_dir]
                    
                    for search_dir in search_dirs:
                        if not search_dir.exists():
                            continue
                        for sfx_file in search_dir.rglob("*"):
                            if sfx_file.is_file():
                                if (sfx_file.name.lower() == sfx_name.lower() or 
                                    sfx_file.stem.lower() == sfx_name.lower() or
                                    sfx_file.stem.lower() == Path(sfx_name).stem.lower()):
                                    found_sfx = sfx_file
                                    break
                        if found_sfx:
                            break

                    if found_sfx:
                        sfx_clips.append({
                            "path": found_sfx,
                            "start": s_frame,
                            "end": e_frame
                        })
                    else:
                        print(f"  [Warning] Could not find SFX file matching: {sfx_name}")
                except ValueError:
                    continue

    # 3. Construct Apple XMEML XML Structure
    xmeml = ET.Element("xmeml", version="5")
    sequence = ET.SubElement(xmeml, "sequence", id=f"seq-{video_identifier}")
    ET.SubElement(sequence, "name").text = video_identifier
    ET.SubElement(sequence, "duration").text = str(total_frames)

    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(fps)
    ET.SubElement(rate, "ntsc").text = "FALSE"

    media = ET.SubElement(sequence, "media")
    video_node = ET.SubElement(media, "video")

    format_node = ET.SubElement(video_node, "format")
    sample_node = ET.SubElement(format_node, "samplecharacteristics")
    ET.SubElement(sample_node, "width").text = "1080"
    ET.SubElement(sample_node, "height").text = "1920"
    s_rate = ET.SubElement(sample_node, "rate")
    ET.SubElement(s_rate, "timebase").text = str(fps)
    ET.SubElement(s_rate, "ntsc").text = "FALSE"

    # --- Helper to append clips to tracks ---
    def append_clip_to_track(track, clip, clip_id, file_id, track_type, is_bg=False, apply_zoom=False, apply_audio_level=False):
        duration = max(1, clip["end"] - clip["start"])
        clip_dur_str = str(duration)
        
        clip_item = ET.SubElement(track, "clipitem", id=clip_id)
        ET.SubElement(clip_item, "name").text = clip["path"].name
        ET.SubElement(clip_item, "duration").text = clip_dur_str
        c_rate = ET.SubElement(clip_item, "rate")
        ET.SubElement(c_rate, "timebase").text = str(fps)
        ET.SubElement(c_rate, "ntsc").text = "FALSE"
        ET.SubElement(clip_item, "start").text = str(clip["start"])
        ET.SubElement(clip_item, "end").text = str(clip["end"])
        ET.SubElement(clip_item, "in").text = "0"
        ET.SubElement(clip_item, "out").text = clip_dur_str

        file_node = ET.SubElement(clip_item, "file", id=file_id)
        ET.SubElement(file_node, "name").text = clip["path"].name
        file_url = f"file://localhost{quote(str(clip['path'].resolve()))}"
        ET.SubElement(file_node, "pathurl").text = file_url
        
        f_rate = ET.SubElement(file_node, "rate")
        ET.SubElement(f_rate, "timebase").text = str(fps)
        ET.SubElement(f_rate, "ntsc").text = "FALSE"

        # ИСПРАВЛЕНИЕ 1: Явный таймкод для всех файлов
        f_timecode = ET.SubElement(file_node, "timecode")
        tc_rate = ET.SubElement(f_timecode, "rate")
        ET.SubElement(tc_rate, "timebase").text = str(fps)
        ET.SubElement(tc_rate, "ntsc").text = "FALSE"
        ET.SubElement(f_timecode, "string").text = "00:00:00:00"
        ET.SubElement(f_timecode, "frame").text = "0"
        ET.SubElement(f_timecode, "displayformat").text = "NDF"

        if track_type == "video":
            # ИСПРАВЛЕНИЕ 2: Для картинок длительность исходника = 864000
            source_duration = "864000" if not clip.get("is_video", True) else clip_dur_str
            ET.SubElement(file_node, "duration").text = source_duration

            f_media = ET.SubElement(file_node, "media")
            f_video = ET.SubElement(f_media, "video")
            
            # ИСПРАВЛЕНИЕ 3: Дублируем duration внутри тега video
            ET.SubElement(f_video, "duration").text = source_duration
            
            f_sample = ET.SubElement(f_video, "samplecharacteristics")
            ET.SubElement(f_sample, "width").text = "1080"
            ET.SubElement(f_sample, "height").text = "1920"
            
            if "subs.mov" in clip["path"].name.lower():
                ET.SubElement(f_sample, "alphatype").text = "straight"

            # V1: Enlarge and Blur Background
            if is_bg:
                filter_scale = ET.SubElement(clip_item, "filter")
                effect_scale = ET.SubElement(filter_scale, "effect")
                ET.SubElement(effect_scale, "effectid").text = "basic"
                ET.SubElement(effect_scale, "effectcategory").text = "motion"
                ET.SubElement(effect_scale, "effecttype").text = "motion"
                ET.SubElement(effect_scale, "mediatype").text = "video"
                param_scale = ET.SubElement(effect_scale, "parameter")
                ET.SubElement(param_scale, "parameterid").text = "scale"
                ET.SubElement(param_scale, "value").text = "300"
                
                filter_blur = ET.SubElement(clip_item, "filter")
                effect_blur = ET.SubElement(filter_blur, "effect")
                ET.SubElement(effect_blur, "effectid").text = "Gaussian Blur"
                ET.SubElement(effect_blur, "effectcategory").text = "Blur"
                ET.SubElement(effect_blur, "effecttype").text = "filter"
                ET.SubElement(effect_blur, "mediatype").text = "video"
                param_blur = ET.SubElement(effect_blur, "parameter")
                ET.SubElement(param_blur, "parameterid").text = "radius"
                ET.SubElement(param_blur, "value").text = "50"

            # V2: Dynamic Zoom (Pictures only)
            elif apply_zoom and not clip.get("is_video", False):
                filter_scale = ET.SubElement(clip_item, "filter")
                effect_scale = ET.SubElement(filter_scale, "effect")
                ET.SubElement(effect_scale, "effectid").text = "basic"
                ET.SubElement(effect_scale, "effectcategory").text = "motion"
                ET.SubElement(effect_scale, "effecttype").text = "motion"
                ET.SubElement(effect_scale, "mediatype").text = "video"
                param_scale = ET.SubElement(effect_scale, "parameter")
                ET.SubElement(param_scale, "parameterid").text = "scale"
                
                kf1 = ET.SubElement(param_scale, "keyframe")
                ET.SubElement(kf1, "when").text = str(clip["start"])
                ET.SubElement(kf1, "value").text = "100"
                
                kf2 = ET.SubElement(param_scale, "keyframe")
                ET.SubElement(kf2, "when").text = str(clip["end"])
                ET.SubElement(kf2, "value").text = "115"

        elif track_type == "audio":
            ET.SubElement(file_node, "duration").text = clip_dur_str
            f_media = ET.SubElement(file_node, "media")
            f_audio = ET.SubElement(f_media, "audio")
            ET.SubElement(f_audio, "channelcount").text = "2"
            
            if apply_audio_level:
                filter_audio = ET.SubElement(clip_item, "filter")
                effect_audio = ET.SubElement(filter_audio, "effect")
                ET.SubElement(effect_audio, "effectid").text = "audiolevels"
                ET.SubElement(effect_audio, "effectcategory").text = "audiolevels"
                ET.SubElement(effect_audio, "effecttype").text = "audiolevels"
                ET.SubElement(effect_audio, "mediatype").text = "audio"
                param_audio = ET.SubElement(effect_audio, "parameter")
                ET.SubElement(param_audio, "parameterid").text = "level"
                ET.SubElement(param_audio, "value").text = "0.446"

        source_track = ET.SubElement(clip_item, "sourcetrack")
        ET.SubElement(source_track, "mediatype").text = track_type
        ET.SubElement(source_track, "trackindex").text = "1"

    # --- Build Video Tracks ---
    track_v1 = ET.SubElement(video_node, "track") # V1 Background
    track_v2 = ET.SubElement(video_node, "track") # V2 Foreground

    for idx, clip in enumerate(v1_clips, start=1):
        append_clip_to_track(track_v1, clip, f"v1-clip-{idx}", f"file-v1-{idx}", "video", is_bg=True)
        append_clip_to_track(track_v2, clip, f"v2-clip-{idx}", f"file-v2-{idx}", "video", apply_zoom=True)

    # Track V3: Transparent Subtitles MOV Overlay
    subs_mov_path = SUBVID_DIR / f"{audio_path.stem}subs.mov"
    if subs_mov_path.exists():
        track_v3 = ET.SubElement(video_node, "track")
        subs_clip = {"path": subs_mov_path, "start": 0, "end": total_frames, "is_video": True}
        append_clip_to_track(track_v3, subs_clip, "v3-subs", "file-v3-subs", "video")

    # --- Build Audio Tracks ---
    audio_node = ET.SubElement(media, "audio")

    if audio_path.exists():
        track_a1 = ET.SubElement(audio_node, "track")
        vo_clip = {"path": audio_path, "start": 0, "end": total_frames}
        append_clip_to_track(track_a1, vo_clip, "a1-vo", "file-a1-vo", "audio")

    if sfx_clips:
        sfx_clips.sort(key=lambda x: x["start"])
        track_available_at = [] 
        routed_sfx = []              

        for clip in sfx_clips:
            assigned_track = -1
            for i, free_frame in enumerate(track_available_at):
                if clip["start"] >= free_frame:
                    assigned_track = i
                    track_available_at[i] = clip["end"]
                    break
            
            if assigned_track == -1:
                assigned_track = len(track_available_at)
                track_available_at.append(clip["end"])
            
            routed_sfx.append((clip, assigned_track))

        track_elements = [ET.SubElement(audio_node, "track") for _ in track_available_at]

        for idx, (clip, track_idx) in enumerate(routed_sfx, start=1):
            append_clip_to_track(
                track=track_elements[track_idx], 
                clip=clip, 
                clip_id=f"sfx-{idx}-trk-{track_idx}", 
                file_id=f"file-sfx-{idx}", 
                track_type="audio", 
                apply_audio_level=True
            )

    # Save formatted XML
    xml_str = ET.tostring(xmeml, encoding="utf-8")
    pretty_xml = minidom.parseString(xml_str).toprettyxml(indent="  ")
    doctype = '<!DOCTYPE xmeml PUBLIC "-//Apple//DTD XMEML//EN" "http://www.apple.com/DTDs/Apple-xmeml-1.0.dtd">\n'
    header, _, rest = pretty_xml.partition('\n')
    final_xml = f"{header}\n{doctype}{rest}"

    output_xml_path.write_text(final_xml, encoding="utf-8")
    print(f"  --> Saved timeline: {output_xml_path.name}")
    return output_xml_path


# --- MAIN WORKFLOW ---

def process_audio_file(
    audio_path: Path, index: int, total_files: int, sfx_library: list[str], client: groq.Client
) -> None:
    file_start_time = time.time()
    filename = audio_path.name
    base_filename_no_ext = audio_path.stem
    subs_base_name = f"{base_filename_no_ext}subs"
    video_identifier = base_filename_no_ext

    txt_output_path = SCRIPT_DIR / f"{base_filename_no_ext}.txt"
    word_frames_path = SCRIPT_WORDS_DIR / f"{base_filename_no_ext}_word_frames.json"
    ass_path = ASS_DIR / f"{subs_base_name}.ass"
    output_video_path = SUBVID_DIR / f"{subs_base_name}.mov"

    print("-" * 50)
    print(f" Processing File [{index}/{total_files}]: {filename}")
    print("-" * 50)

    full_text = ""
    total_duration = 0.0
    all_words = []
    words_frames = []
    

    # 1. Transcription and Word-Frame Mapping
    if txt_output_path.exists() and word_frames_path.exists():
        print("  [Step 1/7] Text & frame map exist. Skipping Whisper transcription.")
        full_text = txt_output_path.read_text(encoding='utf-8')
        words_frames = json.loads(word_frames_path.read_text(encoding='utf-8'))
        for wf in words_frames:
            all_words.append({'word': wf['word'], 'start': wf['start_frame'] / FPS, 'end': wf['end_frame'] / FPS})
        if all_words:
            total_duration = all_words[-1].get('end', 0.0) + 0.5
    else:
        print("  [Step 1/7] Transcribing and mapping frames...", flush=True)
        step_start = time.time()
        result = mlx_whisper.transcribe(str(audio_path), word_timestamps=True, path_or_hf_repo="mlx-community/whisper-turbo")

        for segment in result.get('segments', []):
            for word in segment.get('words', []):
                all_words.append(word)
                words_frames.append({
                    "word": word["word"].strip(),
                    "start_frame": int(round(word["start"] * FPS)),
                    "end_frame": int(round(word["end"] * FPS))
                })

        if not all_words:
            print(f"  ⚠️ No words found in {filename}. Skipping remaining steps.")
            return

        total_duration = all_words[-1].get('end', 0.0) + 0.5
        full_text = result.get('text', '').strip()

        txt_output_path.write_text(full_text, encoding='utf-8')
        word_frames_path.write_text(json.dumps(words_frames, indent=2), encoding='utf-8')
        print(f"  --> Done in {time.time() - step_start:.1f}s. Exported Word-Frame JSON.")

    if total_duration == 0.0:
        total_duration = get_audio_duration(audio_path)

    # 2. Gemini Package Generation (with Model Fallbacks)
    generate_video_package(full_text, words_frames, sfx_library, video_identifier, client)

    # 3. Stock Asset Downloading (Pixabay)
    download_pixabay_assets(video_identifier)

    # --- 4. Local AI Image Generation ---
    generate_ai_images_cli(video_identifier, client)

    # 5. Build Subtitle File (.ass)
    if not ass_path.exists():
        print("  [Step 5/7] Building ASS subtitle file...", flush=True)
        create_ass_subtitles(all_words, ass_path)
    else:
        print(f"  [Step 5/7] Skipping ASS build: {ass_path.name} exists.")

    # 6. Generate Transparent Subtitles Video (FFmpeg)
    if not output_video_path.exists():
        print("  [Step 6/7] Rendering transparent video via FFmpeg...", flush=True)
        generate_transparent_video(ass_path, total_duration, output_video_path)
    else:
        print(f"  [Step 6/7] Skipping video render: {output_video_path.name} already exists.")

    # 7. Create DaVinci Resolve Timeline
    create_resolve_timeline(video_identifier, audio_path, total_duration)

    print(f"✅ Finished processing {filename} in {time.time() - file_start_time:.1f}s total!\n")
    
    if index < total_files:
        print(" ⏳ Cooling down for 12 seconds to respect API rate limits...")
        time.sleep(12)
    
    # Move the completed .wav file out of the 'to_be_processed' folder
    """destination_path = VOICEOVER_DIR / audio_path.name
    try:
        audio_path.rename(destination_path)
        print(f"  --> Successfully moved {filename} to {PROCESSED_VOICEOVER_DIR}")
    except Exception as e:
        print(f"  ⚠️ Failed to move {filename}: {e}")"""

def main() -> None:
    ensure_directories()

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("❌ Error: GROQ_API_KEY is missing from environment variables.")
        return

    # Instantiate Groq Client
    client = Groq(api_key=api_key)
    sfx_library = update_sfx_index()

    audio_files = [
        f for f in sorted(VOICEOVER_DIR.iterdir()) 
        if f.suffix.lower() in {'.mp3', '.wav', '.m4a'} and f.is_file()
    ]

    total_files = len(audio_files)
    if not total_files:
        print("No voiceover audio files found to process.")
        return

    print("\n==========================================")
    print(f" Found {total_files} file(s) to process")
    print("==========================================\n")

    for index, file_path in enumerate(audio_files, start=1):
        process_audio_file(file_path, index, total_files, sfx_library, client)

    print("🎉 All queued files have been processed successfully.")

if __name__ == "__main__":
    main()