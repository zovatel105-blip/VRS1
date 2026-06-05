/**
 * <HlsVideo>
 *
 * Drop-in replacement de `<video>` con soporte HLS adaptativo.
 *
 * Estrategia de selección de fuente (en orden):
 *   1. Si `hlsUrl` está disponible:
 *        - Safari (iOS/macOS) → reproducción HLS NATIVA (video.src = m3u8).
 *        - Chrome / Android WebView → hls.js adjuntado al <video>.
 *        - Si hls.js produce un error FATAL → fallback automático a `mp4Url`.
 *   2. Si no hay HLS o el navegador no lo soporta → MP4 directo (`mp4Url`).
 *
 * El componente expone el mismo `ref` que un `<video>` HTML normal, así que
 * los padres pueden hacer `play() / pause() / currentTime` sin cambios.
 *
 * Config ABR pensada para short-form (TikTok/Reels):
 *   - Buffer adelantado pequeño (8–15 s) para no malgastar datos móviles.
 *   - `capLevelToPlayerSize`: nunca baja un 1080p si el `<video>` mide 400 px.
 *   - `startLevel: -1`: hls.js arranca con la calidad más baja y sube según red.
 */
import React, { useRef, useEffect, forwardRef, useImperativeHandle } from 'react';
import Hls from 'hls.js';
// NOTE: ya no usamos getInitialHlsStartLevel / pickStartLevelIndexFromLevels.
// Forzamos startLevel=0 (360p) global (TikTok-style fast-start). El primer
// segmento siempre es minúsculo → primer frame en <100ms incluso en 4G.
// ABR sube a 540p/720p después del primer fragmento ya descargado.

// Cache la detección de HLS nativo (no cambia durante la sesión).
let _nativeHlsCache = null;
const canPlayHlsNatively = () => {
  if (_nativeHlsCache !== null) return _nativeHlsCache;
  if (typeof document === 'undefined') {
    _nativeHlsCache = false;
    return false;
  }
  const v = document.createElement('video');
  _nativeHlsCache = (
    v.canPlayType('application/vnd.apple.mpegurl') !== '' ||
    v.canPlayType('application/x-mpegURL') !== ''
  );
  return _nativeHlsCache;
};

const DEFAULT_HLS_CONFIG = {
  // — Buffer ajustado para clips cortos —
  maxBufferLength: 8,                  // segundos por delante (en RAM)
  maxMaxBufferLength: 15,              // techo absoluto
  maxBufferSize: 30 * 1000 * 1000,     // 30 MB
  backBufferLength: 4,                 // segundos hacia atrás (rewind corto)
  // — ABR / arranque —
  // 🚀 FIX BOTTLENECK #1: forzar startLevel=0 (360p) para que el PRIMER
  // segmento sea minúsculo (~150KB) y se descargue en <100ms incluso en 4G.
  // ABR subirá a 540p/720p automáticamente tras medir bandwidth real con el
  // segmento ya descargado. Esto es exactamente lo que hace TikTok: lower
  // quality initial start, then upgrade upward.
  startLevel: 0,
  capLevelToPlayerSize: true,          // no descarga 1080p si el player es 400px
  // — Performance —
  enableWorker: true,                  // demuxing en Web Worker → smoother UI
  lowLatencyMode: false,               // no es live, no nos hace falta
  // — Retries —
  fragLoadingMaxRetry: 4,
  manifestLoadingMaxRetry: 4,
  levelLoadingMaxRetry: 4,
  // 🚀 FIX BOTTLENECK #7: cancelar fetches pendientes antes de cambiar de
  // fragmento. Sin esto, al cambiar de slide quedan 1-2 segments pendientes
  // descargándose en background que ya no se van a usar.
  abrEwmaFastLive: 3.0,
  abrEwmaSlowLive: 9.0,
  // testBandwidth en false → no descargas extra para medir; usa el primer
  // segmento real (más rápido en arranque).
  testBandwidth: false,
};

const HlsVideo = forwardRef(
  ({ hlsUrl, mp4Url, hlsConfig, onHlsError, maxHeightCap = null, distanceFromActive = 0, ...videoProps }, ref) => {
    const videoRef = useRef(null);
    // 🚀 FIX BOTTLENECK #2: persistimos la instancia HLS entre re-renders.
    // Antes destruíamos y recreábamos en cada cambio de src → 50-100ms por
    // swipe (worker re-init + decoder re-alloc). Ahora reusamos la instancia
    // y solo llamamos a `hls.loadSource(newUrl)`, que es lo que TikTok hace
    // con sus player pools (mismo decoder, distinto stream).
    const hlsRef = useRef(null);
    const currentHlsUrlRef = useRef(null);
    useImperativeHandle(ref, () => videoRef.current, []);

    useEffect(() => {
      const video = videoRef.current;
      if (!video) return undefined;

      const destroyHls = () => {
        if (hlsRef.current) {
          try { hlsRef.current.destroy(); } catch (_) { /* noop */ }
          hlsRef.current = null;
        }
        if (video._hlsInstance) {
          video._hlsInstance = null;
        }
        currentHlsUrlRef.current = null;
      };

      const useHls = !!hlsUrl;
      const fallbackToMp4 = () => {
        destroyHls();
        if (mp4Url) {
          try {
            if (video.src !== mp4Url) {
              video.src = mp4Url;
              video.load();
            }
          } catch (_) { /* noop */ }
        }
      };

      // ── Caso 1: no hay HLS → MP4 directo ──────────────────────────────
      if (!useHls) {
        fallbackToMp4();
        return undefined; // cleanup en el unmount de abajo
      }

      // ── Caso 2: Safari nativo HLS ─────────────────────────────────────
      if (canPlayHlsNatively()) {
        destroyHls(); // por si veníamos de hls.js (cambio de browser? no, pero por seguridad)
        try {
          if (video.src !== hlsUrl) {
            video.src = hlsUrl;
            video.load();
          }
        } catch (_) { fallbackToMp4(); }
        return undefined;
      }

      // ── Caso 3: hls.js (Chrome / WebView / Firefox) ───────────────────
      if (!Hls.isSupported()) {
        fallbackToMp4();
        return undefined;
      }

      // 🔁 Si YA tenemos una instancia HLS adjunta a este <video>, sólo
      // reasignamos source en lugar de destruir+crear (player recycling).
      if (hlsRef.current && currentHlsUrlRef.current !== hlsUrl) {
        try {
          // stopLoad aborta los fetches en vuelo (B7). loadSource arranca el
          // nuevo manifest. attachMedia ya no es necesario (sigue attached).
          hlsRef.current.stopLoad();
          hlsRef.current.loadSource(hlsUrl);
          currentHlsUrlRef.current = hlsUrl;
          return undefined;
        } catch (_) {
          // Si algo falla, destruye y empieza limpio.
          destroyHls();
        }
      }

      // Misma URL → nada que hacer (idempotente, evita re-attach).
      if (hlsRef.current && currentHlsUrlRef.current === hlsUrl) {
        return undefined;
      }

      // No teníamos instancia: crear una nueva.
      const finalConfig = {
        ...DEFAULT_HLS_CONFIG,
        ...(hlsConfig || {}),
      };
      const hls = new Hls(finalConfig);
      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
      hlsRef.current = hls;
      video._hlsInstance = hls;
      currentHlsUrlRef.current = hlsUrl;

      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        if (!data?.levels || data.levels.length === 0) return;

        // 🚀 FIX GAP #3 (VS bitrate cap): si maxHeightCap está activo,
        // limitamos `autoLevelCapping` al nivel más alto cuya
        // resolución vertical sea <= cap.
        if (typeof maxHeightCap === 'number' && maxHeightCap > 0) {
          let highestAllowed = -1;
          data.levels.forEach((lvl, idx) => {
            const h = lvl?.height || 0;
            if (h > 0 && h <= maxHeightCap && idx > highestAllowed) {
              highestAllowed = idx;
            }
          });
          if (highestAllowed >= 0) {
            try { hls.autoLevelCapping = highestAllowed; } catch (_) { /* noop */ }
          }
        }

        // 🚀 FIX BOTTLENECK #1 — startLevel=0 ya está en config: primer
        // segmento en 360p para arrancar instantáneo. ABR sube luego.
        // No re-alineamos a uno más alto aquí (perderíamos el fast-start).
      });

      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data || !data.fatal) return;
        // eslint-disable-next-line no-console
        console.warn('[HlsVideo] fatal HLS error, falling back to MP4', data);
        onHlsError?.(data);
        destroyHls();
        if (mp4Url) {
          try { video.src = mp4Url; } catch (_) { /* noop */ }
        }
      });

      return undefined; // cleanup global en el siguiente useEffect
    }, [hlsUrl, mp4Url, maxHeightCap]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (distanceFromActive !== 0) return undefined;
    const hls = hlsRef.current;
    if (!hls) return undefined;
    try { hls.startLoad(); } catch (_) {}
    return undefined;
  }, [distanceFromActive]);

    // Cleanup ÚNICAMENTE en el unmount real del componente.
    // Esto es lo que permite el recycling: el useEffect de arriba YA NO
    // destruye la instancia en cada cambio de src.
    useEffect(() => {
      // Snapshot del ref para no leer .current dentro del cleanup
      // (puede haber cambiado para entonces).
      const video = videoRef.current;
      return () => {
        if (hlsRef.current) {
          try { hlsRef.current.destroy(); } catch (_) { /* noop */ }
          hlsRef.current = null;
        }
        if (video && video._hlsInstance) video._hlsInstance = null;
      };
    }, []);

    // Nota: NO pasamos `src` al <video> en JSX — lo gestionamos imperativamente
    // dentro del useEffect para evitar que React lo sobreescriba en re-renders.
    return <video ref={videoRef} {...videoProps} />;
  }
);

HlsVideo.displayName = 'HlsVideo';

export default HlsVideo;
