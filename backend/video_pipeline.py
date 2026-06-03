"""
Video pipeline for polls.

Procesa los medios (principalmente videos) de una publicación recién creada:
    1. Valida integridad con ffprobe (descarta archivos corruptos).
    2. Genera thumbnail server-side si falta (evita posters rotos).
    3. Transcodifica a 720p H.264+AAC mobile-friendly (+faststart) en
       segundo plano sin bloquear la visibilidad del poll.

State machine que gestiona este módulo:
    processing -> ready   (validación + thumbnail OK, transcoding seguirá async)
    processing -> failed  (validación ffprobe falló en alguna opción de video)

El transcoding nunca cambia el status: si falla, el original sigue sirviéndose
y el poll se mantiene `ready` (fallback transparente).

Los archivos generados se guardan bajo:
    <UPLOAD_DIR>/thumbnails/{option_id}.jpg
    <UPLOAD_DIR>/videos/optimized/{option_id}.mp4

Y se sirven vía el mismo endpoint estático /api/uploads/... que ya existe.
"""

from __future__ import annotations

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Directorios
UPLOAD_DIR = Path(__file__).parent / "uploads"
THUMBNAIL_DIR = UPLOAD_DIR / "thumbnails"
OPTIMIZED_DIR = UPLOAD_DIR / "videos" / "optimized"
HLS_DIR = UPLOAD_DIR / "videos" / "hls"
VS_COMPOSED_DIR = UPLOAD_DIR / "videos" / "vs_composed"  # 🎬 MP4 compuesto VS (1vs1 estilo TikTok Duet)
THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
OPTIMIZED_DIR.mkdir(parents=True, exist_ok=True)
HLS_DIR.mkdir(parents=True, exist_ok=True)
VS_COMPOSED_DIR.mkdir(parents=True, exist_ok=True)

# Semáforo global para limitar ffmpeg concurrente (CPU-bound).
# 2 concurrentes es razonable en contenedores típicos (2 vCPU).
_FFMPEG_SEMAPHORE = asyncio.Semaphore(2)

# Timeouts
_FFPROBE_TIMEOUT = 15.0     # validación rápida
_THUMBNAIL_TIMEOUT = 20.0   # generar un JPG de 1 frame
_TRANSCODE_TIMEOUT = 180.0  # transcoding puede tardar en videos largos
_HLS_TIMEOUT = 300.0        # HLS multi-rendition es más caro (3 ladders)
_VS_COMPOSE_TIMEOUT = 240.0  # composición VS: 2 inputs + re-encode, suele tardar 5-20s

# 🎬 VS Composed canónico (estilo TikTok Duet)
# Output 720x1280 @30fps, h264 main, CRF 23, GOP 30 (1s), +faststart.
# vertical  → hstack (side-by-side): cada lado 360x1280
# horizontal → vstack (top/bottom): cada lado 720x640
_VS_OUTPUT_W = 720
_VS_OUTPUT_H = 1280
_VS_FPS = 30
_VS_GOP = 30  # 1 segundo a 30fps

# HLS bitrate ladder (resolución_alto, video_bitrate_k, max_bitrate_k, audio_bitrate_k)
# Optimizado para móvil/feed social: 360p arranca instantáneo, 720p calidad final.
_HLS_LADDER = [
    {"name": "360p",  "height": 360, "v_bitrate": 700,  "v_maxrate": 900,  "a_bitrate": 96},
    {"name": "540p",  "height": 540, "v_bitrate": 1400, "v_maxrate": 1700, "a_bitrate": 128},
    {"name": "720p",  "height": 720, "v_bitrate": 2500, "v_maxrate": 3000, "a_bitrate": 128},
]
_HLS_SEGMENT_DURATION = 2   # segundos por segmento (.ts); 2s = start-up rápido tipo TikTok


def _asset_key(poll_id, question_id, option_id) -> str:
    """Clave ÚNICA para nombrar los assets transcodificados (HLS/MP4/thumb).

    Los `option_id` de VS NO son únicos: 'a'/'b' se repiten entre preguntas de
    un mismo VS y entre distintos posts. Si nombramos los archivos sólo por
    `option_id`, los assets de un post sobrescriben los de otro (mismo
    /hls/a/...) y todos terminan reproduciendo el último vídeo transcodificado.

    Combinando poll + pregunta + opción la ruta queda única. Para polls
    normales (option_id = UUID) el resultado sigue siendo único.
    """
    parts = [str(poll_id)]
    if question_id:
        parts.append(str(question_id))
    parts.append(str(option_id))
    raw = "_".join(parts)
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


def _local_path_from_url(url: Optional[str]) -> Optional[Path]:
    """Convierte una URL `/api/uploads/...` en un path absoluto en disco.
    Devuelve None si la URL no apunta a un upload local."""
    if not url:
        return None
    m = re.search(r"/api/uploads/(.+?)(?:\?|#|$)", url)
    if not m:
        return None
    return UPLOAD_DIR / m.group(1).lstrip("/")


def _public_url_from_path(path: Path) -> str:
    """Convierte un path absoluto dentro de uploads/ a la URL pública."""
    rel = path.resolve().relative_to(UPLOAD_DIR.resolve())
    return f"/api/uploads/{rel.as_posix()}"


async def _run(cmd: list, timeout: float) -> Tuple[int, bytes, bytes]:
    """Ejecuta un comando externo con timeout. Devuelve (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout, stderr
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise


async def validate_video(path: Path) -> Tuple[bool, str, dict]:
    """Valida un archivo de video con ffprobe. Devuelve (ok, error, info)."""
    if not path.exists() or path.stat().st_size == 0:
        return False, "file_missing_or_empty", {}
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        rc, stdout, stderr = await _run(cmd, _FFPROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return False, "ffprobe_timeout", {}

    if rc != 0:
        err = stderr.decode(errors="ignore")[:200]
        return False, f"ffprobe_failed:{err}", {}

    try:
        info = json.loads(stdout.decode())
    except json.JSONDecodeError:
        return False, "ffprobe_bad_json", {}

    fmt = info.get("format", {})
    streams = info.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    if not has_video:
        return False, "no_video_stream", info

    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return False, "invalid_duration", info

    return True, "ok", {
        "duration": duration,
        "size": int(fmt.get("size", 0) or 0),
        "bitrate": int(fmt.get("bit_rate", 0) or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


async def generate_thumbnail(video_path: Path, option_id: str) -> Optional[str]:
    """Genera un thumbnail server-side en el frame 1s. Devuelve la URL pública
    o None si falla (no es fatal: el client-side fallback sigue siendo válido)."""
    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = THUMBNAIL_DIR / f"{option_id}.jpg"

    cmd = [
        "ffmpeg", "-y",
        "-ss", "1",            # segundo 1 (evita frame negro inicial)
        "-i", str(video_path),
        "-vframes", "1",
        "-vf", "scale=720:-2", # ancho 720, alto proporcional
        "-q:v", "5",           # calidad JPEG razonable (1=mejor,31=peor)
        str(out_path),
    ]
    try:
        async with _FFMPEG_SEMAPHORE:
            rc, _, stderr = await _run(cmd, _THUMBNAIL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"[pipeline] thumbnail timeout for {option_id}")
        return None

    if rc != 0 or not out_path.exists():
        # Reintento: sin el -ss 1 (video muy corto)
        cmd_retry = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vframes", "1", "-vf", "scale=720:-2", "-q:v", "5",
            str(out_path),
        ]
        try:
            async with _FFMPEG_SEMAPHORE:
                rc, _, _ = await _run(cmd_retry, _THUMBNAIL_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        if rc != 0 or not out_path.exists():
            return None

    return _public_url_from_path(out_path)


async def transcode_video_720p(video_path: Path, option_id: str) -> Optional[str]:
    """Transcodifica a 720p H.264 + AAC + faststart (mobile-friendly).
    Devuelve la URL pública del archivo optimizado o None si falla."""
    OPTIMIZED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OPTIMIZED_DIR / f"{option_id}.mp4"

    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        # Video: escalar a máx 720 de alto, manteniendo aspect ratio, alineado par
        "-vf", "scale='min(iw,trunc(iw*720/ih/2)*2)':'min(720,ih)':force_original_aspect_ratio=decrease",
        "-c:v", "libx264",
        "-preset", "fast",       # mejor calidad/tamaño que veryfast, sigue siendo rápido
        "-crf", "23",             # calidad visual objetivo
        "-maxrate", "2000k",
        "-bufsize", "4000k",
        "-profile:v", "main",    # compatible con todos los Android desde 4.1
        "-level", "3.1",          # soportado por todos los móviles desde hace años
        "-pix_fmt", "yuv420p",   # compatibilidad universal (WebView, Safari, Chrome…)
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",           # sample rate estándar, evita incompatibilidades
        "-movflags", "+faststart", # mueve MOOV atom al principio → empieza a reproducir sin bajar todo
        "-max_muxing_queue_size", "1024",
        str(out_path),
    ]
    try:
        async with _FFMPEG_SEMAPHORE:
            rc, _, stderr = await _run(cmd, _TRANSCODE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"[pipeline] transcode timeout for {option_id}")
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        return None

    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        logger.warning(
            f"[pipeline] transcode failed for {option_id}: "
            f"{stderr.decode(errors='ignore')[:200]}"
        )
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        return None

    return _public_url_from_path(out_path)


async def transcode_video_hls(video_path: Path, option_id: str) -> Optional[str]:
    """Transcodifica a HLS ABR (Adaptive Bitrate) con 3 renditions: 360p/540p/720p.
    Genera segmentos de 2s para arranque instantáneo (tipo TikTok).

    Estructura de salida:
        UPLOAD_DIR/videos/hls/<option_id>/
            master.m3u8                  (playlist maestra ABR)
            360p.m3u8 + 360p_NNN.ts ...  (rendition 360p)
            540p.m3u8 + 540p_NNN.ts ...  (rendition 540p)
            720p.m3u8 + 720p_NNN.ts ...  (rendition 720p)

    Devuelve la URL pública de master.m3u8 o None si falla.
    El reproductor pedirá master.m3u8 y elegirá rendition según ancho de banda.

    Optimizaciones clave:
        - Un único ffmpeg con `-filter_complex` (3 escaladores en paralelo,
          decode una sola vez).
        - GOP alineado a 2s (`-g`, `-keyint_min`, `-force_key_frames`) para
          que los switches ABR sean limpios y los segmentos cierren en
          keyframe (requisito HLS).
        - `-sc_threshold 0` desactiva keyframes por cambio de escena (rompe
          la alineación entre renditions).
        - `+faststart` no aplica a HLS (cada .ts ya es independiente).
    """
    out_dir = HLS_DIR / option_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Limpiar restos de runs anteriores (idempotencia)
    for old in out_dir.glob("*"):
        try:
            old.unlink()
        except (FileNotFoundError, IsADirectoryError):
            pass

    # GOP fijo = fps * segment_duration. Asumimos 30fps (común en móvil).
    # Si el source es 24/25/60, ffmpeg respeta `-g` igualmente; lo importante
    # es que sea múltiplo coherente del segment time.
    gop_size = 30 * _HLS_SEGMENT_DURATION  # 60 frames

    # filter_complex: split del decoded stream en N copias y escalar cada una.
    # `force_original_aspect_ratio=decrease` + `pad=...:black` mantiene ratio
    # y rellena con barras negras si hace falta (evita warp).
    splits = "[0:v]split={n}{labels}".format(
        n=len(_HLS_LADDER),
        labels="".join(f"[v{i}]" for i in range(len(_HLS_LADDER))),
    )
    scales = ";".join(
        f"[v{i}]scale=-2:{rendition['height']}:force_original_aspect_ratio=decrease,"
        f"pad=ceil(iw/2)*2:{rendition['height']}:(ow-iw)/2:(oh-ih)/2[v{i}out]"
        for i, rendition in enumerate(_HLS_LADDER)
    )
    filter_complex = f"{splits};{scales}"

    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-filter_complex", filter_complex,
    ]

    # Mapear cada rendition: video escalado + mismo audio
    for i, rendition in enumerate(_HLS_LADDER):
        cmd += [
            "-map", f"[v{i}out]",
            f"-c:v:{i}", "libx264",
            f"-b:v:{i}", f"{rendition['v_bitrate']}k",
            f"-maxrate:v:{i}", f"{rendition['v_maxrate']}k",
            f"-bufsize:v:{i}", f"{rendition['v_maxrate'] * 2}k",
            f"-preset:v:{i}", "veryfast",  # HLS multi-rendition: priorizamos velocidad
            f"-profile:v:{i}", "main",
            f"-level:v:{i}", "3.1",
            f"-pix_fmt:v:{i}", "yuv420p",
            "-map", "0:a:0?",
            f"-c:a:{i}", "aac",
            f"-b:a:{i}", f"{rendition['a_bitrate']}k",
            f"-ar:a:{i}", "44100",
        ]

    # GOP y keyframes alineados al segment duration (CRÍTICO para ABR limpio)
    cmd += [
        "-g", str(gop_size),
        "-keyint_min", str(gop_size),
        "-sc_threshold", "0",
        "-force_key_frames", f"expr:gte(t,n_forced*{_HLS_SEGMENT_DURATION})",
    ]

    # HLS muxer config
    stream_map = " ".join(f"v:{i},a:{i},name:{r['name']}" for i, r in enumerate(_HLS_LADDER))
    cmd += [
        "-f", "hls",
        "-hls_time", str(_HLS_SEGMENT_DURATION),
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(out_dir / "%v_%03d.ts"),
        "-hls_flags", "independent_segments",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", stream_map,
        str(out_dir / "%v.m3u8"),
    ]

    try:
        async with _FFMPEG_SEMAPHORE:
            rc, _, stderr = await _run(cmd, _HLS_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"[pipeline] HLS transcode timeout for {option_id}")
        _cleanup_dir(out_dir)
        return None

    master_path = out_dir / "master.m3u8"
    if rc != 0 or not master_path.exists():
        logger.warning(
            f"[pipeline] HLS transcode failed for {option_id}: "
            f"{stderr.decode(errors='ignore')[:300]}"
        )
        _cleanup_dir(out_dir)
        return None

    # Validar que al menos una rendition tenga segmentos
    has_segments = any(out_dir.glob("*.ts"))
    if not has_segments:
        logger.warning(f"[pipeline] HLS generated but no segments for {option_id}")
        _cleanup_dir(out_dir)
        return None

    return _public_url_from_path(master_path)


def _cleanup_dir(d: Path) -> None:
    """Borra recursivamente un directorio HLS de salida tras un fallo."""
    if not d.exists():
        return
    try:
        for f in d.glob("*"):
            try:
                f.unlink()
            except (FileNotFoundError, IsADirectoryError):
                pass
        d.rmdir()
    except OSError:
        pass

# ──────────────────────────────────────────────────────────────────────────
# 🎬 VS Composed Pipeline (estilo TikTok Duet)
#
# Filosofía: cuando AMBAS opciones de la primera pregunta de un VS son video,
# generamos UN SOLO MP4 con el split-screen ya pre-incrustado (hstack/vstack).
# El feed reproduce ese MP4 con 1 decoder → fluido como TikTok.
# Al votar, el frontend hace switch a streams separados (cinema 3D + winner card).
#
# Esto resuelve el problema central de "2 decoders simultáneos" en el feed.
# Coste: ~5-20s de FFmpeg en background por VS al publicar. Async, no bloquea.
# ──────────────────────────────────────────────────────────────────────────


async def compose_vs_video(
    path_a: Path,
    path_b: Path,
    vs_id: str,
    orientation: str = "vertical",
) -> Optional[str]:
    """Compone dos videos en un único MP4 con split-screen pre-incrustado.

    Parámetros:
        path_a: video del lado A (audio principal — el del creador del VS).
        path_b: video del lado B (audio silenciado).
        vs_id: identificador del VS — define el nombre del archivo de salida.
        orientation:
            - 'vertical'   → hstack (side-by-side): cada lado 360x1280
            - 'horizontal' → vstack (top/bottom):    cada lado 720x640

    Output canónico:
        720x1280 @30fps, h264 main, CRF 23, GOP 30 (1s), +faststart, AAC 128k.
        Duración = min(dur_A, dur_B) (flag `-shortest`).

    Devuelve la URL pública (`/api/uploads/...`) del MP4 compuesto, o None
    si la composición falla (frontend hará fallback a streams separados).
    """
    VS_COMPOSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = VS_COMPOSED_DIR / f"{vs_id}.mp4"

    # Validar inputs existentes
    if not path_a.exists() or not path_b.exists():
        logger.warning(
            f"[vs_compose] {vs_id}: missing inputs "
            f"(a_exists={path_a.exists()}, b_exists={path_b.exists()})"
        )
        return None

    # Definir layout según orientación
    if orientation == "horizontal":
        # vstack (top/bottom): cada lado 720x640 → output 720x1280
        side_w, side_h = _VS_OUTPUT_W, _VS_OUTPUT_H // 2  # 720x640
        stack_filter = "vstack=inputs=2"
    else:
        # vertical / default: hstack (side-by-side): cada lado 360x1280 → output 720x1280
        side_w, side_h = _VS_OUTPUT_W // 2, _VS_OUTPUT_H  # 360x1280
        stack_filter = "hstack=inputs=2"

    # Filter complex:
    #   - Cada input se normaliza a 30fps y se escala con object-fit:cover style
    #     (force_original_aspect_ratio=increase + crop centrado).
    #   - Después hstack/vstack los pega.
    # `setsar=1` evita warnings por sample aspect ratio diferente.
    filter_complex = (
        f"[0:v]fps={_VS_FPS},"
        f"scale={side_w}:{side_h}:force_original_aspect_ratio=increase,"
        f"crop={side_w}:{side_h},setsar=1[a];"
        f"[1:v]fps={_VS_FPS},"
        f"scale={side_w}:{side_h}:force_original_aspect_ratio=increase,"
        f"crop={side_w}:{side_h},setsar=1[b];"
        f"[a][b]{stack_filter}[v]"
    )

    # Comando FFmpeg
    # Audio: SOLO del input A (`-map 0:a?`). El audio de B se descarta.
    # `-shortest` corta el output al final del input más corto.
    # `+faststart` mueve MOOV al inicio → empieza a reproducir sin descargar todo.
    cmd = [
        "ffmpeg", "-y",
        "-i", str(path_a),
        "-i", str(path_b),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a?",                  # audio de A (si existe); B silenciado
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-profile:v", "main",
        "-level", "4.0",                 # 720x1280 cabe en 3.1 también pero 4.0 da margen
        "-pix_fmt", "yuv420p",
        "-r", str(_VS_FPS),
        "-g", str(_VS_GOP),
        "-keyint_min", str(_VS_GOP),
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-shortest",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        str(out_path),
    ]

    try:
        async with _FFMPEG_SEMAPHORE:
            logger.info(f"[vs_compose] {vs_id}: starting composition ({orientation})")
            rc, _, stderr = await _run(cmd, _VS_COMPOSE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"[vs_compose] {vs_id}: timeout after {_VS_COMPOSE_TIMEOUT}s")
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        return None

    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        logger.warning(
            f"[vs_compose] {vs_id}: ffmpeg failed (rc={rc}): "
            f"{stderr.decode(errors='ignore')[:300]}"
        )
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        return None

    public_url = _public_url_from_path(out_path)
    logger.info(
        f"[vs_compose] {vs_id}: OK → {public_url} "
        f"({out_path.stat().st_size // 1024} KB)"
    )
    return public_url


async def compose_vs_video_task(db, vs_id: str) -> None:
    """Orquestador async que decide si componer, ejecuta FFmpeg y actualiza
    el doc en MongoDB. Pensado para correr como BackgroundTask.

    Estados de `composed_status`:
        - 'not_applicable' → al menos un lado NO es video, o falta media_url.
        - 'pending'        → enqueued, pero todavía no arrancó el ffmpeg.
        - 'processing'     → ffmpeg corriendo.
        - 'ready'          → composed_video_url disponible y servible.
        - 'failed'         → ffmpeg falló (frontend usa streams separados).

    El task es tolerante: cualquier excepción deja el doc en 'failed'.
    """
    try:
        # Cargamos el poll (donde vive la primera pregunta) y el vs_experience.
        poll = await db.polls.find_one({"id": vs_id})
        if not poll or poll.get("layout") != "vs":
            logger.warning(f"[vs_compose] {vs_id}: poll not found or not VS layout, skip")
            return

        options = poll.get("options") or []
        if len(options) < 2:
            logger.info(f"[vs_compose] {vs_id}: not enough options ({len(options)}), skip")
            await _set_vs_composed_status(db, vs_id, "not_applicable")
            return

        opt_a, opt_b = options[0], options[1]

        # Ambos lados deben ser video
        if opt_a.get("media_type") != "video" or opt_b.get("media_type") != "video":
            logger.info(
                f"[vs_compose] {vs_id}: not video+video "
                f"(A={opt_a.get('media_type')}, B={opt_b.get('media_type')}), skip"
            )
            await _set_vs_composed_status(db, vs_id, "not_applicable")
            return

        # Resolver paths locales. Si alguno no es upload local, no podemos componer.
        path_a = _local_path_from_url(opt_a.get("media_url"))
        path_b = _local_path_from_url(opt_b.get("media_url"))
        if path_a is None or path_b is None:
            logger.info(
                f"[vs_compose] {vs_id}: non-local media "
                f"(a={opt_a.get('media_url')}, b={opt_b.get('media_url')}), skip"
            )
            await _set_vs_composed_status(db, vs_id, "not_applicable")
            return

        if not path_a.exists() or not path_b.exists():
            logger.warning(
                f"[vs_compose] {vs_id}: local files missing "
                f"(a_exists={path_a.exists()}, b_exists={path_b.exists()})"
            )
            await _set_vs_composed_status(db, vs_id, "failed", error="files_missing")
            return

        # Marcar processing
        await _set_vs_composed_status(db, vs_id, "processing")

        orientation = poll.get("vs_orientation") or "vertical"
        composed_url = await compose_vs_video(path_a, path_b, vs_id, orientation)

        if composed_url:
            # 🚀 HLS ABR del compuesto: la vista PRE-VOTO usa este MP4. Sin HLS
            # se descarga el archivo entero (no se adapta a la red). Generamos
            # master.m3u8 (360p/540p/720p) para arranque sub-segundo + ABR,
            # igual que TikTok. Si falla, el MP4 sigue como fallback.
            composed_hls_url = None
            try:
                composed_local = VS_COMPOSED_DIR / f"{vs_id}.mp4"
                if composed_local.exists():
                    composed_hls_url = await transcode_video_hls(
                        composed_local, f"{vs_id}_composed"
                    )
            except Exception as hls_err:
                logger.warning(
                    f"[vs_compose] {vs_id}: composed HLS transcode failed: {hls_err}"
                )

            composed_set = {
                "composed_video_url": composed_url,
                "composed_hls_url": composed_hls_url,
                "composed_status": "ready",
                "composed_orientation": orientation,
                "composed_completed_at": datetime.utcnow(),
            }
            await db.polls.update_one({"id": vs_id}, {"$set": composed_set})
            # Mirror en vs_experiences (algunos endpoints leen de ahí)
            await db.vs_experiences.update_one({"id": vs_id}, {"$set": composed_set})
            logger.info(
                f"[vs_compose] {vs_id}: marked ready "
                f"(hls={'ok' if composed_hls_url else 'no'})"
            )
        else:
            await _set_vs_composed_status(db, vs_id, "failed", error="ffmpeg_failed")
    except Exception as e:
        logger.error(f"[vs_compose] {vs_id}: unhandled error: {e}", exc_info=True)
        try:
            await _set_vs_composed_status(db, vs_id, "failed", error=f"crash:{str(e)[:120]}")
        except Exception:
            pass


async def _set_vs_composed_status(
    db,
    vs_id: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Helper: actualiza composed_status en `polls` y `vs_experiences` a la vez."""
    update = {"composed_status": status, "composed_updated_at": datetime.utcnow()}
    if error:
        update["composed_error"] = error
    if status == "processing":
        update["composed_started_at"] = datetime.utcnow()
    try:
        await db.polls.update_one({"id": vs_id}, {"$set": update})
    except Exception as e:
        logger.warning(f"[vs_compose] {vs_id}: failed to set polls status: {e}")
    try:
        await db.vs_experiences.update_one({"id": vs_id}, {"$set": update})
    except Exception as e:
        logger.warning(f"[vs_compose] {vs_id}: failed to set vs_experiences status: {e}")





async def process_poll_media(db, poll_id: str) -> None:
    """Pipeline completo para un poll. Está pensado para correr como
    BackgroundTask de FastAPI; es tolerante a errores y siempre termina
    dejando al poll en un estado terminal (ready | failed).

    Parámetros:
        db: motor AsyncIOMotor database (pasada explícitamente porque este
            módulo no tiene su propia conexión).
        poll_id: identificador UUID del poll.
    """
    try:
        poll = await db.polls.find_one({"id": poll_id})
        if not poll:
            logger.warning(f"[pipeline] poll {poll_id} not found, aborting")
            return

        # Marcar inicio (idempotente)
        await db.polls.update_one(
            {"id": poll_id},
            {"$set": {
                "status": "processing",
                "processing_started_at": datetime.utcnow(),
            }},
        )

        options = poll.get("options", []) or []
        video_options = [o for o in options if (o.get("media_type") == "video")]

        # 🎬 VS multi-pregunta: cache de resultados de transcoding por option_id
        # para replicar HLS/optimized/thumb a vs_questions[].options[] SIN
        # recodificar lo ya hecho para poll.options (== pregunta 1).
        results_cache: dict = {}

        # ¿Hay videos en vs_questions[].options[] (preguntas 2..N que NO viven
        # en poll.options)? Si los hay, NO podemos cortar temprano aunque
        # poll.options no tenga video.
        _vs_questions_probe = poll.get("vs_questions") or []
        _VIDEO_EXT_RE = r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)"

        def _is_video_opt(o: dict) -> bool:
            if "video" in (o.get("media_type") or "").lower():
                return True
            u = o.get("media_url") or o.get("image") or ""
            return bool(re.search(_VIDEO_EXT_RE, str(u), re.IGNORECASE))

        vs_has_video = any(
            _is_video_opt(o)
            for q in _vs_questions_probe for o in (q.get("options") or [])
        )

        # ¿Es un poll VS? poll.options refleja la PRIMERA pregunta (q0). Para
        # nombrar los assets de q0 con la misma clave única que usará
        # _process_vs_questions_media necesitamos el id de esa primera pregunta.
        is_vs_poll = bool(_vs_questions_probe)
        q0_id = _vs_questions_probe[0].get("id") if is_vs_poll else None

        def _asset_for(opt_dict):
            # VS → clave única poll+q0+option; normal → option_id (UUID, único).
            return _asset_key(poll_id, q0_id, opt_dict["id"]) if is_vs_poll else opt_dict["id"]

        # Si no hay videos en NINGÚN lado, transición inmediata a ready.
        if not video_options and not vs_has_video:
            await db.polls.update_one(
                {"id": poll_id},
                {"$set": {
                    "status": "ready",
                    "processing_completed_at": datetime.utcnow(),
                }},
            )
            return

        # 1) Validación ffprobe de cada video. Si CUALQUIERA falla -> failed.
        for opt in video_options:
            local_path = _local_path_from_url(opt.get("media_url"))
            if local_path is None:
                # URL externa: confiamos en media_validator (ya corrió).
                continue
            ok, reason, _info = await validate_video(local_path)
            if not ok:
                logger.warning(
                    f"[pipeline] poll {poll_id} option {opt.get('id')} "
                    f"failed validation: {reason}"
                )
                await db.polls.update_one(
                    {"id": poll_id},
                    {"$set": {
                        "status": "failed",
                        "processing_completed_at": datetime.utcnow(),
                        "processing_error": f"video_validation:{reason}",
                    }},
                )
                return

        # 2) Thumbnails server-side para cualquier video que no tenga uno.
        #    Esto es rápido (~1s/video), así que lo hacemos antes de marcar ready.
        for opt in video_options:
            local_path = _local_path_from_url(opt.get("media_url"))
            if local_path is None:
                continue
            if opt.get("thumbnail_url"):
                results_cache.setdefault(opt["id"], {})["thumb"] = opt.get("thumbnail_url")
                continue  # ya tiene thumbnail cliente/anterior
            new_thumb = await generate_thumbnail(local_path, _asset_for(opt))
            if new_thumb:
                results_cache.setdefault(opt["id"], {})["thumb"] = new_thumb
                await db.polls.update_one(
                    {"id": poll_id, "options.id": opt["id"]},
                    {"$set": {"options.$.thumbnail_url": new_thumb}},
                )

        # 3) Marcar READY para que el poll ya sea visible en feeds.
        #    El transcoding sigue en background; el cliente verá el original
        #    hasta que `optimized_media_url` esté disponible.
        await db.polls.update_one(
            {"id": poll_id},
            {"$set": {
                "status": "ready",
                "processing_completed_at": datetime.utcnow(),
            }},
        )

        # 4) Transcoding async "fire-and-forget" por opción. Errores no
        #    cambian el status del poll.
        #    Generamos primero MP4 720p (fallback universal, baja CPU) y
        #    luego HLS ABR multi-rendition (start-up rápido, switch adaptativo).
        #    HLS no es bloqueante: si falla, el MP4 sigue sirviéndose como hoy.
        for opt in video_options:
            local_path = _local_path_from_url(opt.get("media_url"))
            if local_path is None:
                continue

            # 4a) MP4 720p (fallback principal)
            try:
                optimized_url = await transcode_video_720p(local_path, _asset_for(opt))
            except Exception as tx_err:
                logger.warning(
                    f"[pipeline] transcode crash for {opt['id']}: {tx_err}"
                )
                optimized_url = None
            if optimized_url:
                results_cache.setdefault(opt["id"], {})["optimized"] = optimized_url
                await db.polls.update_one(
                    {"id": poll_id, "options.id": opt["id"]},
                    {"$set": {"options.$.optimized_media_url": optimized_url}},
                )
                logger.info(
                    f"[pipeline] poll {poll_id} option {opt['id']} optimized -> {optimized_url}"
                )

            # 4b) HLS ABR (360p/540p/720p). Si falla, el MP4 (o el original)
            #     sigue siendo válido — no abortamos ni cambiamos status.
            try:
                hls_url = await transcode_video_hls(local_path, _asset_for(opt))
            except Exception as hls_err:
                logger.warning(
                    f"[pipeline] HLS transcode crash for {opt['id']}: {hls_err}"
                )
                hls_url = None
            if hls_url:
                results_cache.setdefault(opt["id"], {})["hls"] = hls_url
                await db.polls.update_one(
                    {"id": poll_id, "options.id": opt["id"]},
                    {"$set": {"options.$.hls_url": hls_url}},
                )
                logger.info(
                    f"[pipeline] poll {poll_id} option {opt['id']} HLS -> {hls_url}"
                )

        # 5) 🎬 VS multi-pregunta — replicar HLS / MP4-720p / thumbnail a TODAS
        #    las opciones de vs_questions[].options[] (incluye preguntas 2..N
        #    que NO viven en poll.options). Reutiliza results_cache para no
        #    recodificar la pregunta 1. No fatal: los errores se loguean y el
        #    poll permanece `ready` (fallback transparente al MP4 original).
        if vs_has_video:
            try:
                await _process_vs_questions_media(db, poll_id, results_cache)
            except Exception as vs_err:
                logger.warning(
                    f"[pipeline] VS questions media processing failed for {poll_id}: {vs_err}"
                )

    except Exception as e:
        logger.error(f"[pipeline] unhandled error for poll {poll_id}: {e}", exc_info=True)
        # Último recurso: no dejar el poll zombi en processing.
        try:
            await db.polls.update_one(
                {"id": poll_id, "status": "processing"},
                {"$set": {
                    "status": "failed",
                    "processing_completed_at": datetime.utcnow(),
                    "processing_error": f"pipeline_crash:{str(e)[:180]}",
                }},
            )
        except Exception:
            pass


async def _process_vs_questions_media(db, poll_id: str, results_cache: Optional[dict] = None) -> None:
    """Replica HLS / MP4-720p / thumbnail a TODAS las opciones de vídeo que
    viven en ``polls.vs_questions[].options[]`` (y espeja en
    ``vs_experiences.questions[].options[]``).

    Motivo: el pipeline estándar (process_poll_media) sólo escribe en
    ``poll.options``, que para un VS es ÚNICAMENTE la primera pregunta. El feed
    VS, sin embargo, renderiza desde ``vs_questions[].options[]`` — por eso, sin
    esta réplica, las opciones VS no tienen ``.m3u8`` y NO hacen ABR (no se
    adaptan a la velocidad de la red, no arrancan instantáneas).

    ``results_cache`` (opcional) trae los resultados ya calculados para la
    pregunta 1 (mismas option_id que poll.options) → evita recodificar.
    """
    results_cache = results_cache if results_cache is not None else {}

    poll = await db.polls.find_one(
        {"id": poll_id}, {"vs_questions": 1, "vs_id": 1}
    )
    if not poll:
        return
    questions = poll.get("vs_questions") or []
    if not questions:
        return
    vs_id = poll.get("vs_id") or poll_id

    _VIDEO_EXT_RE = r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)"

    for q_index, q in enumerate(questions):
        q_id = q.get("id")
        if not q_id:
            continue
        for opt in (q.get("options") or []):
            opt_id = opt.get("id")
            media_url = opt.get("media_url") or opt.get("image")
            if not opt_id or not media_url:
                continue

            is_video = (
                "video" in (opt.get("media_type") or "").lower()
                or bool(re.search(_VIDEO_EXT_RE, str(media_url), re.IGNORECASE))
            )
            if not is_video:
                continue

            # ⚠️ Los option_id de VS ('a'/'b') se repiten entre preguntas y
            # entre posts. La clave de asset DEBE ser única por poll+pregunta+
            # opción para que cada vídeo tenga su propio /hls/<key>/.
            asset_id = _asset_key(poll_id, q_id, opt_id)

            # Sólo la PRIMERA pregunta (q_index==0) comparte option_id con
            # poll.options, así que sólo ahí es válido reutilizar el cache que
            # llenó process_poll_media. En q1..N el mismo option_id 'a'/'b'
            # corresponde a OTRO vídeo → NO reutilizar (evita el cross-mezclado).
            cached = (results_cache.get(opt_id) or {}) if q_index == 0 else {}
            optimized_url = cached.get("optimized")
            hls_url = cached.get("hls")
            thumb_url = cached.get("thumb")

            # Si no estaba en cache, generamos los assets ahora. Si es una URL
            # externa (sin path local) no podemos transcodificar; dejamos lo que haya.
            local_path = _local_path_from_url(media_url)
            if local_path is not None:
                already_has_thumb = bool(thumb_url or opt.get("thumbnail_url"))
                if not already_has_thumb:
                    try:
                        thumb_url = await generate_thumbnail(local_path, asset_id)
                    except Exception as e:
                        logger.warning(f"[pipeline] VS thumb gen failed {asset_id}: {e}")
                if optimized_url is None:
                    try:
                        optimized_url = await transcode_video_720p(local_path, asset_id)
                    except Exception as e:
                        logger.warning(f"[pipeline] VS 720p transcode failed {asset_id}: {e}")
                if hls_url is None:
                    try:
                        hls_url = await transcode_video_hls(local_path, asset_id)
                    except Exception as e:
                        logger.warning(f"[pipeline] VS HLS transcode failed {asset_id}: {e}")
                # Cachear para reusar si la misma option_id se repite.
                results_cache[opt_id] = {
                    "optimized": optimized_url,
                    "hls": hls_url,
                    "thumb": thumb_url,
                }

            # Construir el $set sólo con lo que tengamos.
            set_polls = {}
            set_vsexp = {}
            if hls_url:
                set_polls["vs_questions.$[q].options.$[o].hls_url"] = hls_url
                set_vsexp["questions.$[q].options.$[o].hls_url"] = hls_url
            if optimized_url:
                set_polls["vs_questions.$[q].options.$[o].optimized_media_url"] = optimized_url
                set_vsexp["questions.$[q].options.$[o].optimized_media_url"] = optimized_url
            if thumb_url and not opt.get("thumbnail_url"):
                set_polls["vs_questions.$[q].options.$[o].thumbnail_url"] = thumb_url
                set_vsexp["questions.$[q].options.$[o].thumbnail_url"] = thumb_url

            if not set_polls:
                continue

            try:
                await db.polls.update_one(
                    {"id": poll_id},
                    {"$set": set_polls},
                    array_filters=[{"q.id": q_id}, {"o.id": opt_id}],
                )
            except Exception as e:
                logger.warning(
                    f"[pipeline] VS vs_questions update failed {poll_id}/{q_id}/{opt_id}: {e}"
                )
            try:
                await db.vs_experiences.update_one(
                    {"id": vs_id},
                    {"$set": set_vsexp},
                    array_filters=[{"q.id": q_id}, {"o.id": opt_id}],
                )
            except Exception as e:
                logger.warning(
                    f"[pipeline] VS vs_experiences mirror failed {vs_id}/{q_id}/{opt_id}: {e}"
                )

            logger.info(
                f"[pipeline] VS {poll_id} q={q_id} opt={opt_id} "
                f"HLS={'ok' if hls_url else '-'} OPT={'ok' if optimized_url else '-'}"
            )



# ──────────────────────────────────────────────────────────────────────────
# Reaper: rescata polls que quedaron en `processing` más de X minutos.
# Suele pasar si el proceso backend se reinicia mientras transcodificaba.
# ──────────────────────────────────────────────────────────────────────────

async def reap_stuck_polls(db, max_age_minutes: int = 10) -> int:
    """Marca como `failed` cualquier poll en `processing` más antiguo que
    `max_age_minutes`. Devuelve el número de polls afectados."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    result = await db.polls.update_many(
        {
            "status": "processing",
            "$or": [
                {"processing_started_at": {"$lt": cutoff}},
                {"processing_started_at": {"$exists": False},
                 "created_at": {"$lt": cutoff}},
            ],
        },
        {
            "$set": {
                "status": "failed",
                "processing_completed_at": datetime.utcnow(),
                "processing_error": "reaper_timeout",
            }
        },
    )
    if result.modified_count:
        logger.warning(f"[reaper] rescued {result.modified_count} stuck polls")
    return result.modified_count


async def reaper_loop(db, interval_seconds: int = 300, max_age_minutes: int = 10) -> None:
    """Loop infinito: corre el reaper cada `interval_seconds` (5 min por defecto).
    Pensado para arrancarse con asyncio.create_task() en startup."""
    logger.info(f"[reaper] loop started (every {interval_seconds}s, threshold {max_age_minutes}min)")
    # Una pasada al arrancar para limpiar fantasmas tras reinicio.
    try:
        await reap_stuck_polls(db, max_age_minutes=max_age_minutes)
    except Exception as e:
        logger.error(f"[reaper] startup pass failed: {e}")
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await reap_stuck_polls(db, max_age_minutes=max_age_minutes)
        except asyncio.CancelledError:
            logger.info("[reaper] loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[reaper] iteration failed: {e}")
            # Pausa defensiva para no entrar en busy loop si algo falla
            await asyncio.sleep(interval_seconds)


# ──────────────────────────────────────────────────────────────────────────
# Backfill: regenera thumbnails y genera versiones optimizadas (720p H.264)
# para polls ya READY que fueron subidos antes de tener ffmpeg o la pipeline.
# Corre en background, no bloquea requests y procesa uno por uno con pausas
# para no saturar el CPU del servidor en picos de tráfico.
# ──────────────────────────────────────────────────────────────────────────

async def backfill_missing_video_assets(
    db,
    batch_size: int = 5,
    sleep_between_batches: float = 10.0,
    sleep_between_items: float = 2.0,
    include_hls: bool = False,
) -> int:
    """Procesa polls READY con opciones de vídeo que les faltan thumbnail
    o `optimized_media_url`. Devuelve el número total de opciones procesadas.

    Es idempotente: skippea opciones que ya tienen ambos campos.

    Parámetros:
        include_hls: si True, también genera HLS para opciones que ya tienen
            optimized_media_url pero no hls_url. Por defecto False — HLS es
            ~3x más caro que MP4 y queremos un flag manual para activar el
            rebackfill en batch.
    """
    processed = 0
    try:
        # Buscar polls ready con opciones de vídeo que les falte algo.
        # Incluye 3 estados:
        #   - thumbnail_url ausente
        #   - optimized_media_url ausente
        #   - hls_url ausente (solo relevante si include_hls=True)
        missing_clauses = [
            {"thumbnail_url": {"$in": [None, ""]}},
            {"thumbnail_url": {"$exists": False}},
            {"optimized_media_url": {"$in": [None, ""]}},
            {"optimized_media_url": {"$exists": False}},
        ]
        if include_hls:
            missing_clauses.extend([
                {"hls_url": {"$in": [None, ""]}},
                {"hls_url": {"$exists": False}},
            ])
        cursor = db.polls.find(
            {
                "status": "ready",
                # 🚫 Excluir VS: sus option_id ('a'/'b') NO son únicos y este
                # backfill genérico nombra assets por option_id → colisión. Los
                # VS se procesan por su propio path (process_poll_media +
                # _process_vs_questions_media) con claves únicas.
                "layout": {"$ne": "vs"},
                "options": {
                    "$elemMatch": {
                        "media_type": "video",
                        "$or": missing_clauses,
                    }
                },
            },
            {"id": 1, "options": 1},
        ).limit(batch_size)

        polls_to_fix = await cursor.to_list(length=batch_size)
        if not polls_to_fix:
            return 0

        logger.info(f"[backfill] processing {len(polls_to_fix)} polls with missing video assets")

        for poll in polls_to_fix:
            poll_id = poll["id"]
            options = poll.get("options") or []
            for opt in options:
                if opt.get("media_type") != "video":
                    continue

                local_path = _local_path_from_url(opt.get("media_url"))
                if local_path is None or not local_path.exists():
                    # Archivo no está en disco (puede ser externo o ya borrado)
                    continue

                # 1) Thumbnail si falta
                if not opt.get("thumbnail_url"):
                    try:
                        new_thumb = await generate_thumbnail(local_path, opt["id"])
                        if new_thumb:
                            await db.polls.update_one(
                                {"id": poll_id, "options.id": opt["id"]},
                                {"$set": {"options.$.thumbnail_url": new_thumb}},
                            )
                            logger.info(
                                f"[backfill] thumbnail for {poll_id}/{opt['id']} -> {new_thumb}"
                            )
                    except Exception as e:
                        logger.warning(f"[backfill] thumb failed for {opt['id']}: {e}")

                # 2) Optimized video si falta
                if not opt.get("optimized_media_url"):
                    try:
                        optimized_url = await transcode_video_720p(local_path, opt["id"])
                        if optimized_url:
                            await db.polls.update_one(
                                {"id": poll_id, "options.id": opt["id"]},
                                {"$set": {"options.$.optimized_media_url": optimized_url}},
                            )
                            logger.info(
                                f"[backfill] optimized {poll_id}/{opt['id']} -> {optimized_url}"
                            )
                            processed += 1
                    except Exception as e:
                        logger.warning(f"[backfill] transcode failed for {opt['id']}: {e}")

                # 3) HLS si falta (sólo si include_hls=True)
                if include_hls and not opt.get("hls_url"):
                    try:
                        hls_url = await transcode_video_hls(local_path, opt["id"])
                        if hls_url:
                            await db.polls.update_one(
                                {"id": poll_id, "options.id": opt["id"]},
                                {"$set": {"options.$.hls_url": hls_url}},
                            )
                            logger.info(
                                f"[backfill] HLS {poll_id}/{opt['id']} -> {hls_url}"
                            )
                            processed += 1
                    except Exception as e:
                        logger.warning(f"[backfill] HLS failed for {opt['id']}: {e}")

                # Pausa entre opciones para no saturar CPU
                await asyncio.sleep(sleep_between_items)

        return processed
    except Exception as e:
        logger.error(f"[backfill] batch failed: {e}", exc_info=True)
        return processed


async def backfill_loop(
    db,
    interval_seconds: int = 30,
    batch_size: int = 5,
    include_hls: bool = True,
) -> None:
    """Loop infinito: cada `interval_seconds` procesa un batch de polls con
    assets de vídeo faltantes. Pensado para correr tras startup y recuperar
    vídeos históricos sin bloquear el servicio.

    🚀 FIX BOTTLENECK #5: `include_hls=True` por defecto. Antes el HLS NUNCA
    se generaba para vídeos legacy (default era False) → solo 1 de cada 23
    vídeos tenía HLS y el feed servía MP4 plano (sin ABR, sin fast-start de
    360p, primer byte 1-2s en 4G). Ahora el backfill genera HLS para todos
    los vídeos sin él, dándoles arranque sub-segundo estilo TikTok.
    """
    logger.info(
        f"[backfill] loop started (every {interval_seconds}s, "
        f"batch={batch_size}, include_hls={include_hls})"
    )
    # Esperar un poco al arranque para dejar que el servidor se estabilice
    await asyncio.sleep(20)
    while True:
        try:
            processed = await backfill_missing_video_assets(
                db, batch_size=batch_size, include_hls=include_hls,
            )
            if processed == 0:
                # Nada que hacer: esperar más antes de la siguiente pasada
                await asyncio.sleep(interval_seconds * 4)
            else:
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("[backfill] loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[backfill] iteration failed: {e}")
            await asyncio.sleep(interval_seconds * 2)
