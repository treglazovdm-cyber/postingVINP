"""Prepare Telegram MP4 metadata and a JPEG thumbnail without re-encoding video."""
import json
import math
import subprocess
import textwrap
import unicodedata


class VideoError(Exception):
    pass


def run(arguments, timeout=45):
    try:
        return subprocess.run(arguments, check=True, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        raise VideoError('Не удалось обработать видео. Проверьте файл и наличие FFmpeg на сервере.') from None


def text_cover(text, directory):
    # This is a deterministic typographic cover, not an AI illustration.
    text = ''.join(c for c in text if unicodedata.category(c)[0] in ('L', 'N', 'P', 'Z') or c.isspace())
    lines = textwrap.wrap(' '.join(text.split()), width=22, max_lines=5, placeholder='…')
    if not lines:
        raise VideoError('Добавьте текст поста для генерации обложки.')
    label = directory / 'cover.txt'
    label.write_text('\n'.join(lines), encoding='utf-8')
    target = directory / 'text-cover.jpg'
    run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'color=c=0x263b32:s=320x180',
         '-vf', "drawbox=x=16:y=18:w=4:h=144:color=0xe3bc72:t=fill,"
         "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
         f"textfile={label}:expansion=none:fontcolor=white:fontsize=19:line_spacing=7:x=32:y=20",
         '-frames:v', '1', '-q:v', '3', '-threads', '1', '-y', str(target)])
    return target


def prepare(source, directory):
    try:
        probe = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)]))
        stream = next(s for s in probe['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'))
        duration = float(probe['format'].get('duration') or stream.get('duration'))
        width, height = int(stream['width']), int(stream['height'])
        if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
            raise ValueError()
    except (ValueError, TypeError, KeyError, StopIteration):
        raise VideoError('Не удалось определить длительность и размеры видео. Проверьте исходный MP4.') from None
    if stream.get('codec_name') != 'h264' or stream.get('pix_fmt') not in ('yuv420p', 'yuvj420p'):
        raise VideoError('Для Telegram нужен MP4 с видео H.264, 8-bit yuv420p. Пересохраните ролик в этом формате.')
    rotation = next((s.get('rotation', 0) for s in stream.get('side_data_list', []) if 'rotation' in s), stream.get('tags', {}).get('rotate', 0))
    if round(float(rotation)) % 180:
        width, height = height, width
    prefix = ['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1']
    # Avoid black opening frames by sampling different points in the clip.
    candidates = []
    for fraction in (.1, .5, .8):
        position = duration * fraction
        gray = run(prefix + ['-ss', str(position), '-i', str(source), '-map', '0:v:0', '-frames:v', '1',
                             '-vf', 'scale=32:32', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'])
        if gray:
            candidates.append((sum(gray) / len(gray), position))
    if not candidates:
        raise VideoError('В видео не удалось прочитать кадры для обложки.')
    position = max(candidates)[1]
    thumbnail = directory / 'thumbnail.jpg'
    run(prefix + ['-ss', str(position), '-i', str(source), '-map', '0:v:0', '-frames:v', '1',
                  '-vf', 'scale=320:320:force_original_aspect_ratio=decrease', '-q:v', '4', '-y', str(thumbnail)])
    if not thumbnail.exists() or thumbnail.stat().st_size >= 200 * 1024:
        raise VideoError('Не удалось создать обложку допустимого размера.')
    ready = directory / 'video.mp4'
    run(prefix + ['-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
                  '-movflags', '+faststart', '-y', str(ready)], timeout=120)
    return ready, thumbnail, {'duration': max(1, math.ceil(duration)), 'width': width,
                              'height': height, 'supports_streaming': 'true'}
