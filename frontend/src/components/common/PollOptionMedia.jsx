/**
 * <PollOptionMedia>
 *
 * Renderiza el media (vídeo o imagen) de una `option` de un poll.
 *
 * Cambios vs versión anterior:
 * - Poster crossfade: muestra el poster hasta que el video tiene buffer,
 *   luego hace crossfade suave (0.2s) — elimina la pantalla negra inicial.
 * - Background pause: pausa el video cuando la app va a segundo plano
 *   y lo reanuda al volver (igual que TikTok).
 * - onCanPlay en lugar de autoPlay para arrancar solo cuando hay buffer
 *   suficiente — evita el efecto "cámara lenta" por falta de datos.
 */
import React, { useState, useEffect, useRef, useCallback } from 'react';
import { pickPlayableVideoUrl, pickPlayableHlsUrl, pickVideoPosterUrl } from '../../utils/mediaUrl';
import resolveAssetUrl from '../../utils/resolveAssetUrl';
import useCachedSrc from '../../hooks/useCachedSrc';
import { cn } from '../../lib/utils';
import HlsVideo from './HlsVideo';
import videoMemoryManager from '../../services/videoMemoryManager';
// 🚀 FIX BOTTLENECK #3 — Fast-scroll suspension. Cuando el usuario hace
// flick rápido y cascadas de swipes, los slots PREV/NEXT suspenden HLS
// (manifest + primer segmento) para no malgastar bandwidth en contenido
// que va a saltar.
import { useFastScrolling } from '../../utils/scrollVelocityTracker';
// 🆕 Fase C — Defensa en profundidad: cuando el backend NO mandó
// thumbnail_url (típico de VS legacy con vs_questions[].options[] sin
// enriquecer por Fase A/B), generamos el poster client-side desde el
// primer frame del video. Cache LRU + inflight dedup vive en el módulo.
import { generatePosterDataUrl } from '../../utils/canvasPoster';

const VIDEO_MAX_BYTES_DEFAULT = 25 * 1024 * 1024; // 25 MB

const VIDEO_URL_RE = /\.(mp4|mov|webm|avi|m4v)(\?|$)/i;

// ── POSTER TRANSPARENTE (1×1 negro) ────────────────────────────────────────
// SVG inline pasado como `poster=` al <video> para BLOQUEAR el placeholder
// nativo del WebView de Android (círculo/triángulo de "play" sobre fondo
// gris). El WebView solo dibuja su placeholder cuando el <video> NO tiene
// poster. Pasándole este SVG transparente/negro de 1×1 le decimos: "ya
// tienes poster, no pintes el tuyo". El poster VISIBLE (la miniatura real)
// lo seguimos pintando aparte con un <img> encima con z-index>0, así que
// este truco no interfiere con el crossfade.
const TRANSPARENT_POSTER =
  'data:image/svg+xml;charset=utf-8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">' +
    '<rect width="1" height="1" fill="black"/>' +
    '</svg>'
  );

const isVideoOption = (option) => {
  if (!option) return false;
  // 1) Tipo declarado en estructura moderna o plana
  const t = option.media_type || option.media?.type;
  if (typeof t === 'string' && t.toLowerCase().includes('video')) return true;
  // 2) Fallback por extensión de URL — necesario para entradas legacy de VS
  //    (vs_questions) donde NO viene `media_type` y la URL está en
  //    `option.image`. Si la URL parece un video, lo tratamos como video.
  const candidates = [
    option.media?.url,
    option.media?.optimizedUrl,
    option.media?.optimized_media_url,
    option.media_url,
    option.optimized_media_url,
    option.image,
  ];
  return candidates.some((u) => typeof u === 'string' && VIDEO_URL_RE.test(u));
};

const PollOptionMedia = ({
  option,
  className,
  style,
  videoProps = {},
  imgProps = {},
  showPlaceholderOnError = true,
  cacheVideo = true,
  videoMaxBytes = VIDEO_MAX_BYTES_DEFAULT,
  videoRef: externalVideoRef = null,
  distanceFromActive = 0,
  isHighBandwidth = true,
  // postId + layout son opcionales y solo se usan para identificar la entrada
  // en videoMemoryManager (accounting global de cuántos <video> hay vivos).
  postId = null,
  layout = 'default',
}) => {
  const isVideo = isVideoOption(option);
  const internalVideoRef = useRef(null);
  const videoEl = externalVideoRef || internalVideoRef;

  // URLs originales
  const rawVideoSrc = isVideo ? pickPlayableVideoUrl(option) : null;
  const rawHlsSrc = isVideo ? pickPlayableHlsUrl(option) : null;
  const rawPosterSrc = pickVideoPosterUrl(option);
  const rawImageSrc = !isVideo
    ? resolveAssetUrl(
        option?.media?.url ||
        option?.media_url ||
        option?.media?.thumbnail ||
        option?.thumbnail_url ||
        option?.image
      )
    : null;

  // Offline-first: sustituir por URI local cacheada cuando exista
  const cachedVideoSrc = useCachedSrc(rawVideoSrc, {
    enabled: cacheVideo && !!rawVideoSrc,
    maxBytes: videoMaxBytes,
  });
  const cachedPosterSrc = useCachedSrc(rawPosterSrc, { enabled: !!rawPosterSrc });
  const cachedImageSrc = useCachedSrc(rawImageSrc, { enabled: !!rawImageSrc });

  // Estrategia de selección de fuente para el reproductor:
  //   1) Si tenemos MP4 cacheado en filesystem → preferimos ese (offline OK,
  //      cero latencia de manifest HLS). Saltamos HLS porque sería tonto
  //      hacer ABR cuando ya tenemos el archivo entero en disco.
  //   2) Si no, y hay HLS disponible → usamos HLS para tener ABR en tiempo
  //      real (calidad se adapta al ancho de banda, cambia rendition sin
  //      pausar). hls.js en Chrome/Android, nativo en Safari/iOS.
  //   3) Si no hay HLS → MP4 remoto plano.
  const hasCachedMp4 = !!cachedVideoSrc;
  const mp4SrcForPlayer = cachedVideoSrc || rawVideoSrc;
  // 🚀 FIX BOTTLENECK #3 — durante fast-scroll (flick + cascada), los
  // slots PREV/NEXT (distance > 0) suspenden HLS. El slot activo
  // (distance === 0) carga HLS siempre. Cuando el tracker libera el flag
  // (250ms de idle), los slots vecinos reanudan automáticamente.
  // Nota: si hay MP4 cacheado en disco, NO suspendemos — es offline-first,
  // cero coste de red.
  const isFastScrolling = useFastScrolling();
  const suspendHls = isFastScrolling && distanceFromActive > 0 && !hasCachedMp4;
  const hlsSrcForPlayer = hasCachedMp4 || suspendHls ? null : rawHlsSrc;
  const videoSrc = mp4SrcForPlayer; // para checks de "hay algo que reproducir"

  // ── POSTER CANVAS FALLBACK (Fase C) ───────────────────────────────────────
  // Si el backend NO mandó poster (rawPosterSrc=null) y SÍ tenemos un video,
  // generamos uno client-side desde el primer keyframe. Cache LRU global
  // por URL en el módulo → al hacer swipe back no se regenera.
  //
  // Esto cubre 3 casos:
  //   a) VS legacy ya en BD: vs_questions[].options[] sin thumbnail_url
  //      porque se creó antes de Fase A (y aún no se corrió Fase B).
  //   b) Backend con thumbnail roto / 404 (que pickVideoPosterUrl ya
  //      filtraría como inexistente más adelante en el ciclo de vida).
  //   c) Cualquier flujo futuro que olvide enriquecer la opción.
  //
  // Esto NO sustituye Fase A/B — el thumbnail server-side es siempre más
  // rápido (HTTP cacheable, no consume CPU del cliente). Es solo el plan B.
  const [canvasPoster, setCanvasPoster] = useState(null);
  useEffect(() => {
    // Solo intentamos canvas si:
    //   - Es video (no tiene sentido para imagen)
    //   - No hay poster del backend (raw o cacheado)
    //   - Tenemos URL del video para extraer frame
    if (!isVideo) { setCanvasPoster(null); return; }
    if (rawPosterSrc) { setCanvasPoster(null); return; }
    if (!rawVideoSrc) { setCanvasPoster(null); return; }
    // Solo en slot activo o adyacente — no malgastemos CPU decodificando
    // posters de slots a distancia > 2 que el usuario quizá nunca verá.
    if (distanceFromActive > 2) { setCanvasPoster(null); return; }

    let cancelled = false;
    generatePosterDataUrl(rawVideoSrc).then((data) => {
      if (cancelled) return;
      if (data) setCanvasPoster(data);
    });
    return () => { cancelled = true; };
  }, [isVideo, rawPosterSrc, rawVideoSrc, distanceFromActive]);

  // El poster final es, en orden de preferencia:
  //   1) Versión cacheada del poster del backend (RAM/disk)
  //   2) Poster crudo del backend (URL HTTP)
  //   3) Poster generado client-side desde canvas (Fase C fallback)
  const posterSrc = cachedPosterSrc || rawPosterSrc || canvasPoster;
  const imageSrc = cachedImageSrc || rawImageSrc;

  // ── POSTER CROSSFADE ──────────────────────────────────────────────────────
  // isBuffered: true cuando el video tiene datos suficientes para reproducir
  // sin congelarse. Hasta entonces mostramos el poster encima.
  // El crossfade (opacity transition 0.2s) elimina el salto visual.
  const [isBuffered, setIsBuffered] = useState(false);
  // hasFirstFrame: true cuando el primer frame del video YA está pintado en
  // GPU (no solo decodificado en buffer). Usamos requestVideoFrameCallback
  // (donde existe) para saberlo con precisión. Esto es lo que evita el
  // "negro de 1-2 frames" típico entre que se quita el poster y aparece el
  // primer frame real del video.
  const [hasFirstFrame, setHasFirstFrame] = useState(false);
  const [videoStatus, setVideoStatus] = useState('loading');
  const [imgStatus, setImgStatus] = useState('loading');

  // Reset cuando cambian las URLs
  useEffect(() => {
    if (videoSrc && lastVideoSrcRef.current !== videoSrc) {
      lastVideoSrcRef.current = videoSrc;
      everHadFrameRef.current = false;
      setVideoStatus('loading');
      setIsBuffered(false);
      setHasFirstFrame(false);
    }
  }, [videoSrc]);
  useEffect(() => {
    if (imageSrc) setImgStatus('loading');
  }, [imageSrc]);

  // ── 🚀 IMG.DECODE() del poster: garantiza decode antes del paint ──────────
  //
  // El poster JPEG/WebP se descarga via <img src=...> pero el browser solo
  // lo DECODIFICA cuando va a pintarlo. Decode de JPEG 1080×1920 toma
  // 30-80ms en gama media. Si el slot pasa de PREV/NEXT a CUR durante esos
  // 30-80ms, el browser hace decode sync mientras render → frame drop.
  //
  // img.decode() es una Promise API que decodifica en background y resuelve
  // cuando la imagen está lista para pintar inmediatamente. Lo hacemos en
  // distance <= 1 (CUR + vecinos) para que estén pre-decoded antes del swipe.
  useEffect(() => {
    if (!posterSrc) return;
    if (distanceFromActive > 1) return;
    if (typeof Image === 'undefined') return;
    let cancelled = false;
    const img = new Image();
    img.src = posterSrc;
    if (typeof img.decode === 'function') {
      img.decode().catch(() => { /* decode fail OK */ });
    } else if (img.complete) {
      // Ya en cache; no hacemos nada
    }
    return () => { cancelled = true; void cancelled; };
  }, [posterSrc, distanceFromActive]);

  // ── 🚀 P2: HELPER ÚNICO DE PLAY ──────────────────────────────────────────
  // Antes había 3 triggers de v.play() dispersos (effect distanceFromActive,
  // onCanPlay, onLoadedData) con la misma guarda copy-pasted. Ahora todos
  // llaman a esta función. La guarda es:
  //   1) Slot debe ser ACTIVO (distance === 0) — otros slots no reproducen.
  //   2) Video debe estar PAUSADO — no relanzamos play sobre play.
  //   3) readyState >= 2 (HAVE_CURRENT_DATA) — al menos primer frame.
  //      Si no, el evento de buffer (canplay/loadeddata) lo llamará luego.
  // Mantenemos los 3 triggers como red de seguridad: distintos WebViews
  // disparan onCanPlay y onLoadedData en orden distinto.
  const tryPlayIfActive = useCallback(() => {
    const v = videoEl?.current;
    if (!v) return;
    if (distanceFromActive !== 0) return;
    if (!v.paused) return;
    if (v.readyState < 2) return;
    v.play().catch(() => {});
  }, [videoEl, distanceFromActive]);

  // ── 🚀 PLAY/PAUSE DRIVEN BY distanceFromActive (FIX CRÍTICO TikTok-style)
  //
  // CAUSA RAÍZ encontrada: `onCanPlay` solo dispara UNA VEZ por carga. Si
  // dispara cuando el slot está aún a distance=1 (preloaded), el play() se
  // omite (porque la condición `distanceFromActive === 0` no se cumple).
  // Cuando el slot pasa a CUR (distance=0), NADA llama a play() de nuevo
  // → el video preloaded se queda quieto en el primer frame mientras el
  // usuario ve resultado: pantalla congelada / poster.
  //
  // Fix: drive play/pause desde el cambio de distanceFromActive, no del
  // evento canplay. Si el video ya está listo (readyState >= 2 = HAVE_CURRENT_DATA),
  // play inmediatamente. Si no, onCanPlay lo arrancará cuando llegue datos.
  //
  // Esto es lo que hace que TikTok 1vs1 se sienta instantáneo: al hacer
  // swipe, ambos videos del nuevo slot YA están bufferizados Y comienzan a
  // reproducirse simultáneamente sin esperar a otro evento de buffer.
  useEffect(() => {
    if (!isVideo) return;
    const v = videoEl?.current;
    if (!v) return;

    if (distanceFromActive === 0) {
      // Slot ACTIVO: arrancar reproducción ya. Si aún no hay datos
      // suficientes, onCanPlay lo gestionará en cuanto lleguen.
      // 2 intentos: inmediato (si ya está listo) y tras 1 frame (por si
      // React acaba de re-renderizar y el video aún no terminó su effect).
      tryPlayIfActive();
      const rafId = requestAnimationFrame(tryPlayIfActive);
      return () => cancelAnimationFrame(rafId);
    } else {
      // Slot NO activo: pausar para liberar decoder hardware (crítico en
      // Android gama media donde solo hay 2-4 decoders H.264 simultáneos).
      // Mantenemos el buffer ya descargado (no llamamos .load() ni .src='').
      if (!v.paused) {
        try { v.pause(); } catch (_) {}
      }
      return undefined;
    }
  }, [isVideo, distanceFromActive, videoEl, videoSrc, tryPlayIfActive]);

  // ── 🚀 WARM-PLAY: forzar decode del primer frame en el <video> del slot
  //
  // Cuando el slot está a distance=1 (preloaded, no activo aún), el browser
  // descarga bytes (preload="auto") pero NO decodifica ni renderiza el
  // primer frame en GPU hasta que se llama .play(). Resultado: cuando el
  // usuario hace swipe, ve "negro" 1-3 frames mientras el decoder produce
  // el primer frame.
  //
  // Truco TikTok: hacer un play() + pause() inmediato en distance=1 para
  // forzar al decoder a producir y pintar el primer frame. Cuando el slot
  // pase a distance=0, el frame YA está en pantalla y solo necesitamos
  // reanudar la reproducción.
  //
  // Solo se hace UNA VEZ por carga de URL — evitamos re-disparos.
  const warmedRef = useRef(null);
  const everHadFrameRef = useRef(false);
  const lastVideoSrcRef = useRef(null);
  useEffect(() => {
    if (!isVideo) return;
    if (distanceFromActive !== 1) return;
    if (warmedRef.current === videoSrc) return;
    const v = videoEl?.current;
    if (!v) return;

    let cancelled = false;
    const warm = async () => {
      // Esperar a tener al menos metadata (readyState >= 1)
      if (v.readyState < 1) {
        await new Promise((resolve) => {
          const onMeta = () => { v.removeEventListener('loadedmetadata', onMeta); resolve(); };
          v.addEventListener('loadedmetadata', onMeta);
          // Failsafe: 800ms
          setTimeout(() => { v.removeEventListener('loadedmetadata', onMeta); resolve(); }, 800);
        });
      }
      if (cancelled) return;
      try {
        // Asegurar muted=true (browsers bloquean autoplay con sonido).
        v.muted = true;
        const playPromise = v.play();
        if (playPromise && typeof playPromise.then === 'function') {
          await playPromise;
        }
        if (cancelled) return;
        // Pausar inmediatamente — solo queríamos forzar el primer frame.
        // Si en este tiempo (1-2 frames) el slot pasó a activo, la pausa
        // será inmediatamente revertida por el effect de play/pause
        // arriba.
        if (distanceFromActive !== 0) {
          v.pause();
        }
        warmedRef.current = videoSrc;
      } catch (_) {
        // play() puede fallar (autoplay policy en navegadores estrictos).
        // No es fatal — el video arrancará por la ruta normal en
        // distance=0. Solo perdemos el frame warmup en este caso.
      }
    };

    // Damos un pequeño respiro (1 frame) para no competir con animaciones
    // del swipe que aún está en curso cuando entramos en distance=1.
    const rafId = requestAnimationFrame(warm);
    return () => { cancelled = true; cancelAnimationFrame(rafId); };
  }, [isVideo, distanceFromActive, videoSrc, videoEl]);

  // ── 🚀 PRIMER FRAME RENDERIZADO via requestVideoFrameCallback
  //
  // Sabemos con exactitud CUÁNDO el primer frame del video está pintado en
  // GPU (no solo cuando hay buffer). Esto elimina el "flash negro" entre el
  // poster desapareciendo y el video apareciendo: en lugar de fiarnos de
  // `canplay` (buffer suficiente, frame puede estar a 1-2 ticks de renderizar),
  // esperamos al primer rVFC para decir hasFirstFrame=true.
  //
  // Fallback: si el browser no soporta rVFC (Safari <17 viejo), usamos
  // `playing` event como aproximación.
  useEffect(() => {
    if (!isVideo) return;
    const v = videoEl?.current;
    if (!v) return;

    let rvfcHandle = null;
    if (typeof v.requestVideoFrameCallback === 'function') {
      rvfcHandle = v.requestVideoFrameCallback(() => {
        setHasFirstFrame(true);
      });
    } else {
      // Fallback: marcar hasFirstFrame al primer evento 'playing'
      const onPlaying = () => { setHasFirstFrame(true); };
      v.addEventListener('playing', onPlaying, { once: true });
      return () => v.removeEventListener('playing', onPlaying);
    }
    return () => {
      if (rvfcHandle != null && typeof v.cancelVideoFrameCallback === 'function') {
        try { v.cancelVideoFrameCallback(rvfcHandle); } catch (_) {}
      }
    };
  }, [isVideo, videoSrc, videoEl]);

  // ── BACKGROUND PAUSE ─────────────────────────────────────────────────────
  // TikTok pausa video Y audio cuando el usuario minimiza la app.
  // AudioManager ya maneja el audio; aquí manejamos el video.
  useEffect(() => {
    if (!isVideo) return;
    const v = videoEl?.current;
    if (!v) return;

    const handleVisibility = () => {
      if (document.hidden) {
        v.pause();
      } else {
        // Solo reanudar si el post está activo (distanceFromActive === 0)
        if (distanceFromActive === 0 && !v.paused) return;
        if (distanceFromActive === 0) {
          v.play().catch(() => {});
        }
      }
    };

    document.addEventListener('visibilitychange', handleVisibility);
    return () => document.removeEventListener('visibilitychange', handleVisibility);
  }, [isVideo, distanceFromActive, videoEl]);

  // ── REGISTRO PASIVO EN videoMemoryManager ───────────────────────────────
  // El manager NO controla play/pause/preload (eso lo hace este componente
  // con onCanPlay + distanceFromActive). Solo lleva la cuenta global de
  // cuántos <video> hay vivos al mismo tiempo y limpia huérfanos cuyos
  // padres se desmontaron sin avisar. Esto evita leaks acumulados tras
  // muchos swipes / cambios de feed / navegación.
  useEffect(() => {
    if (!isVideo) return;
    const v = videoEl?.current;
    if (!v) return;

    // Generar key estable. option.id siempre existe; postId mejora unicidad
    // entre publicaciones que compartan el mismo option.id (no debería pasar,
    // pero por si acaso).
    const optionId = option?.id || option?._id || 'unknown';
    const pId = postId || option?.poll_id || option?.postId || 'p';
    const key = `${pId}_${optionId}`;

    videoMemoryManager.registerVideo(v, {
      postId: pId,
      optionId,
      layout,
      isActive: distanceFromActive === 0,
      isVisible: distanceFromActive <= 1,
      priority: distanceFromActive === 0 ? 'high' : 'medium',
      passive: true, // ← clave: solo accounting, no toca el elemento
    });

    return () => {
      videoMemoryManager.unregisterVideo(key);
    };
    // Re-registramos si cambia el <video> de elemento (HLS remount, etc).
    // distanceFromActive NO va aquí — sería tirar/registrar en cada swipe.
    // Si necesitamos actualizar isActive/isVisible, lo hacemos en otro effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVideo, option?.id, layout, postId]);

  // Actualizar isActive/isVisible cuando cambia distanceFromActive (sin
  // re-registrar — solo mutamos la entrada del registry).
  useEffect(() => {
    if (!isVideo) return;
    const optionId = option?.id || option?._id || 'unknown';
    const pId = postId || option?.poll_id || option?.postId || 'p';
    const key = `${pId}_${optionId}`;
    const entry = videoMemoryManager.activeVideos.get(key);
    if (entry) {
      entry.isActive = distanceFromActive === 0;
      entry.isVisible = distanceFromActive <= 1;
      entry.lastAccessed = Date.now();
    }
  }, [isVideo, distanceFromActive, option?.id, postId]);

  // ── No hay media ──────────────────────────────────────────────────────────
  if (!isVideo && !imageSrc) {
    return (
      <div
        className={cn(
          'relative w-full h-full bg-gradient-to-br from-purple-900 to-pink-900',
          className
        )}
        style={style}
      />
    );
  }

  // ── Modo VIDEO ────────────────────────────────────────────────────────────
  if (isVideo) {
    // 🚀 FIX GAP #2 (Android decoder pool): para layout VS no podemos
    // mantener 6 <video> vivos (PREV/CUR/NEXT × 2 lados) — Android limita
    // a 2–4 decoders H.264 simultáneos en hardware. Para VS solo montamos
    // el <video> en el slot activo y el vecino inmediato (<=1). El resto
    // muestra solo poster (gap silencioso, no pantalla negra).
    //
    // 🚀 P1 OPTIMIZATION: Para layouts normales bajamos de <=3 a <=2.
    // Antes: CUR + PREV + NEXT + NEXT+1 + NEXT+2 + NEXT+3 = potencialmente
    // 5 <video> vivos a la vez (cuando entran/salen del viewport). Ahora:
    // CUR + PREV + NEXT + NEXT+1 + NEXT+2 = 5 → 3 efectivos activos.
    // Android WebView gama media solo soporta 2-4 decoders H.264 hw
    // simultáneos; pasar el límite causa freezing del video activo.
    // Con distance=2 mantenemos un buffer de pre-warm sin saturar.
    const videoTagMaxDistance = layout === 'vs' ? 1 : 2;
    const shouldRenderVideoTag = distanceFromActive <= videoTagMaxDistance;

    if (!videoSrc || !shouldRenderVideoTag) {
      return (
        <div
          className={cn(
            'relative w-full h-full overflow-hidden bg-gradient-to-br from-gray-700 to-gray-900',
            className
          )}
          style={style}
        >
          {posterSrc && (
            <img
              src={posterSrc}
              alt=""
              draggable={false}
              // Slot lejano (distance > 3): el <video> ya no se monta,
              // solo se muestra el poster. Lo bajamos a prioridad mínima
              // para que NUNCA compita con thumbnails del slot activo/+1.
              // `loading="lazy"` deja al browser decidir cuándo descargar
              // según viewport — incluso este poster puede saltarse.
              fetchpriority="low"
              loading="lazy"
              decoding="async"
              className="w-full h-full object-cover"
              onError={(e) => { e.currentTarget.style.display = 'none'; }}
            />
          )}
        </div>
      );
    }

    return (
      <div
        className={cn(
          // 🎨 FIX BLACK-SCREEN: gradiente brand como fallback cuando el
          // poster del backend tarda en descargar. ANTES: el contenedor
          // heredaba `bg-black` del card → pantalla NEGRA mientras el
          // poster cargaba (0-500ms en primer acceso). AHORA: el usuario
          // SIEMPRE ve un gradiente coherente con la marca, incluso si el
          // poster nunca llega (offline, error 404, etc.). El poster se
          // pinta encima cuando carga; el primer frame de video se pinta
          // encima del poster cuando llega.
          'relative w-full h-full overflow-hidden bg-gradient-to-br from-purple-950 via-fuchsia-950 to-pink-950',
          className,
        )}
        style={style}
      >
        {/* Poster visible hasta que el video tiene SU PRIMER FRAME pintado.
            Hace crossfade con el video cuando hasFirstFrame=true.
            Usamos hasFirstFrame (rVFC) en lugar de isBuffered (canplay) para
            eliminar el flash negro de 1-2 frames entre "hay buffer" y "hay
            primer frame pintado". Si el browser no soporta rVFC, cae a
            'playing' event como aproximación. */}
        {posterSrc && (
          <img
            src={posterSrc}
            alt=""
            draggable={false}
            // 🚀 FIX BLACK-SCREEN: Fetch Priority + eager loading agresivos.
            // ANTES: distance≥2 usaba loading="lazy" → poster NO se descargaba
            //   hasta entrar en viewport. Al hacer fast-scroll, el slot pasaba
            //   de distance=3 → distance=0 sin haber descargado el poster
            //   nunca → PANTALLA NEGRA hasta que el fetch completara
            //   (200-500ms en 4G).
            // AHORA: TODOS los posters en DOM (distance ≤ 3) cargan eager.
            //   Son tiny (~10-50KB), descargar 7 en paralelo cuesta nada.
            //   Para distance ≤ 1 además se piden con priority="high" para
            //   ganar bandwidth al resto. distance=0 además decoding="sync"
            //   para que el browser bloquee y pinte ya.
            fetchpriority={distanceFromActive <= 1 ? 'high' : 'auto'}
            decoding={distanceFromActive === 0 ? 'sync' : 'async'}
            loading="eager"
            className="absolute inset-0 w-full h-full object-cover pointer-events-none"
            style={{
              opacity: hasFirstFrame ? 0 : 1,
              transition: 'opacity 0.2s ease',
              zIndex: 1,
            }}
          />
        )}

        <HlsVideo
          ref={videoEl}
          hlsUrl={hlsSrcForPlayer}
          mp4Url={mp4SrcForPlayer}
          distanceFromActive={distanceFromActive}
          // 🚀 FIX GAP #3 (VS bitrate cap): en layout VS el <video> ocupa
          // ~mitad de pantalla; pedir 1080p en cada lado es desperdicio.
          // Cap a 720p → ~40% menos bytes y ~50% menos tiempo de decode
          // sin pérdida visible. Para layouts normales (full-screen),
          // dejamos que ABR + capLevelToPlayerSize decidan sin techo.
          maxHeightCap={layout === 'vs' ? 720 : null}
          // Poster TRANSPARENTE para bloquear el placeholder nativo del
          // WebView Android (▶ sobre gris). El poster VISIBLE real es el
          // <img> de arriba; este sólo silencia el del WebView.
          poster={TRANSPARENT_POSTER}
          onCanPlay={() => {
            // Video tiene suficiente buffer para reproducir sin congelarse
            // (readyState 3 = HAVE_FUTURE_DATA). Mantenemos esta guarda como
            // fallback si el browser saltó loadeddata (algunos WebView lo hacen).
            setIsBuffered(true);
            tryPlayIfActive();
          }}
          onWaiting={() => {
            // Video se quedó sin datos → mostrar poster de nuevo
            setIsBuffered(false);
            // No reseteamos hasFirstFrame: el frame YA está pintado.
            // El crossfade del poster usa hasFirstFrame, así que el
            // poster solo vuelve si videoSrc cambia (re-load completo).
          }}
          onPlaying={() => {
            // Video tiene datos de nuevo → ocultar poster
            setIsBuffered(true);
            setHasFirstFrame(true);
          }}
          onLoadedData={() => {
            setVideoStatus('loaded');
            // 🚀 FIX BOTTLENECK #4 — arranca el play en readyState 2
            // (HAVE_CURRENT_DATA = primer frame ya decodificado), sin
            // esperar a readyState 3 (HAVE_FUTURE_DATA = ~500ms de buffer).
            // Esto recorta ~80-150ms al primer frame visible en cada swipe.
            // Es el equivalente a `bufferForPlaybackMs` bajo de ExoPlayer.
            tryPlayIfActive();
          }}
          onError={() => setVideoStatus('error')}
          // 🔄 Si HLS falla fatal, hls.js cae a MP4 automáticamente (lo
          // gestiona <HlsVideo>). Aquí solo loggeamos para debug.
          onHlsError={(data) => {
            if (process.env.NODE_ENV !== 'production') {
              // eslint-disable-next-line no-console
              console.debug('[PollOptionMedia] HLS fatal → fallback MP4', data);
            }
          }}
          className={cn(
            'w-full h-full object-cover',
            videoStatus === 'error' && showPlaceholderOnError && 'opacity-0'
          )}
          style={{
            // CLAVE: visibility hidden + opacity 0 hasta que haya primer
            // frame REAL pintado (no solo bufferizado). visibility:hidden
            // evita que el WebView pinte NADA del <video> (ni el placeholder);
            // opacity:0 da el crossfade suave cuando volvemos a `visible`.
            // El usuario nunca llega a ver el <video> antes del primer
            // frame renderizado en GPU (detectado via requestVideoFrameCallback).
            visibility: hasFirstFrame ? 'visible' : 'hidden',
            opacity: hasFirstFrame ? 1 : 0,
            transition: 'opacity 0.2s ease',
            transform: 'translateZ(0)',
            backfaceVisibility: 'hidden',
            WebkitBackfaceVisibility: 'hidden',
            willChange: distanceFromActive <= 1 ? 'transform, opacity' : 'auto',
            zIndex: 0,
            ...(videoProps.style || {}),
          }}
          preload={(() => {
            if (videoProps.preload) return videoProps.preload;
            if (distanceFromActive <= 1) return 'auto';
            if (distanceFromActive === 2 && isHighBandwidth) return 'metadata';
            return 'none';
          })()}
          muted
          playsInline
          loop
          // NO autoPlay — usamos onCanPlay para arrancar cuando hay buffer
          // eslint-disable-next-line react/no-unknown-property
          webkit-playsinline="true"
          // eslint-disable-next-line react/no-unknown-property
          x5-playsinline="true"
          {...videoProps}
          // autoPlay siempre false — override cualquier valor del padre
          autoPlay={false}
        />

        {/* Fallback si el video falla y hay poster */}
        {videoStatus === 'error' && showPlaceholderOnError && posterSrc && (
          <img
            src={posterSrc}
            alt=""
            draggable={false}
            // Video falló → el poster pasa a ser EL contenido visible.
            // Si es el slot activo, prioridad alta (es lo único que ve el
            // usuario); si es lejano, baja. `decoding="sync"` en activo
            // evita un segundo flash mientras el browser decodifica.
            fetchpriority={distanceFromActive === 0 ? 'high' : 'low'}
            decoding={distanceFromActive === 0 ? 'sync' : 'async'}
            loading={distanceFromActive <= 1 ? 'eager' : 'lazy'}
            className="absolute inset-0 w-full h-full object-cover pointer-events-none"
            style={{ zIndex: 2 }}
            onError={(e) => { e.currentTarget.style.display = 'none'; }}
          />
        )}
        {/* Fallback sin poster → gradiente */}
        {videoStatus === 'error' && showPlaceholderOnError && !posterSrc && (
          <div
            className="absolute inset-0 bg-gradient-to-br from-purple-900 via-fuchsia-800 to-pink-900 pointer-events-none"
            style={{ zIndex: 2 }}
          />
        )}
      </div>
    );
  }

  // ── Modo IMAGEN ───────────────────────────────────────────────────────────
  return (
    <div
      className={cn('relative w-full h-full overflow-hidden', className)}
      style={style}
    >
      <img
        src={imageSrc}
        alt=""
        draggable={false}
        onLoad={() => setImgStatus('loaded')}
        onError={() => setImgStatus('error')}
        className={cn(
          'w-full h-full object-cover',
          imgStatus === 'error' && 'opacity-0'
        )}
        {...imgProps}
      />
      {imgStatus === 'error' && (
        <div className="absolute inset-0 bg-gradient-to-br from-gray-700 to-gray-900 pointer-events-none" />
      )}
    </div>
  );
};

export default PollOptionMedia;
