import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { motion, useAnimation, PanInfo } from 'framer-motion';
import PollCard from './PollCard';
import MusicPlayer from './MusicPlayer';
import MusicDisplay from './MusicDisplay';
import CustomLogo from './CustomLogo';
import CommentsModal from './CommentsModal';
import PostDetailModal from './PostDetailModal';
import ShareModal from './ShareModal';
import PostManagementMenu from './PostManagementMenu';
import FeedMenu from './FeedMenu';
import StoriesViewer from './StoriesViewer';
import VotersModal from './VotersModal';
import ChallengeParticipantsModal from './ChallengeParticipantsModal';
import MediaPrefetcher from './MediaPrefetcher';
import { useFollow } from '../contexts/FollowContext';
import { useAuth } from '../contexts/AuthContext';
import { useShare } from '../hooks/useShare';
import { useTranslation } from '../hooks/useTranslation';
import { useViewTracking } from '../hooks/useViewTracking';
import { cn } from '../lib/utils';
import AppConfig from '../config/config';
import { resolveAssetUrl } from '../utils/resolveAssetUrl';
import { pickPlayableVideoUrl, pickVideoPosterUrl, pickImageUrl } from '../utils/mediaUrl';
import { ChevronUp, ChevronDown, Heart, MessageCircle, Send, Bookmark, MoreHorizontal, CheckCircle, User, Home, Search, Plus, Mail, Trophy, Share2, Music, X, Swords } from 'lucide-react';
import VoteIcon from './icons/VoteIcon';
import { Button } from './ui/button';
import { Avatar, AvatarImage, AvatarFallback } from './ui/avatar';
import { useToast } from '../hooks/use-toast';
import audioManager from '../services/AudioManager';
import realMusicService from '../services/realMusicService';
import LayoutRenderer from './layouts/LayoutRenderer';
import DoubleTapVoteAnimation from './DoubleTapVoteAnimation';
import PollOptionMedia from './common/PollOptionMedia';
import feedMenuService from '../services/feedMenuService';
import storyService from '../services/storyService';
import { useNavPreference } from '../hooks/useNavPreference';
import { useTikTok } from '../contexts/TikTokContext';
import useNetworkStatus from '../hooks/useNetworkStatus';
import { setFastScrolling } from '../utils/scrollVelocityTracker';

// Helper function to render text with clickable hashtags
const renderTextWithHashtags = (text, navigate) => {
  if (!text) return null;
  
  // Split text by hashtags while keeping the hashtags
  const parts = text.split(/(#\w+)/g);
  
  return parts.map((part, index) => {
    if (part.startsWith('#')) {
      // This is a hashtag, make it clickable
      return (
        <span
          key={`hashtag-${part}-${index}`}
          className="text-white font-semibold hover:underline cursor-pointer"
          onClick={(e) => {
            e.stopPropagation();
            navigate(`/search?q=${encodeURIComponent(part.substring(1))}&filter=hashtags`);
          }}
        >
          {part}
        </span>
      );
    }
    // Regular text
    return <span key={`text-${index}`}>{part}</span>;
  });
};

// Componente UserButton clickeable
const UserButton = ({ user, percentage, isSelected, isWinner, onClick, onUserClick, optionIndex }) => (
  <div className="absolute flex flex-col items-center gap-2 z-20 bottom-4 right-4">
    {/* Avatar del usuario - clickeable */}
    <button
      onClick={(e) => {
        e.stopPropagation();
        onUserClick(user);
      }}
      className="group relative"
    >
      <Avatar className={cn(
        "w-12 h-12 transition-all duration-200 ring-2",
        isSelected 
          ? "ring-blue-400 shadow-lg shadow-blue-500/50" 
          : isWinner
            ? "ring-green-400 shadow-lg shadow-green-500/50"
            : "ring-white/30 shadow-lg"
      )}>
        <AvatarImage src={user.avatar} className="object-cover" />
        <AvatarFallback className="bg-gradient-to-br from-blue-500 to-purple-600 text-white flex items-center justify-center">
          <User className="w-5 h-5" />
        </AvatarFallback>
      </Avatar>
      
      {/* Verificación overlay */}
      {user.verified && (
        <div className="absolute -bottom-1 -right-1 bg-white rounded-full p-0.5">
          <CheckCircle className="w-4 h-4 text-blue-500 fill-current" />
        </div>
      )}
      
      {/* Hover tooltip */}
      <div className="absolute -top-10 left-1/2 transform -translate-x-1/2 opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none">
        <div className="bg-black/80 text-white text-xs px-2 py-1 rounded backdrop-blur-sm">
          @{user.username}
        </div>
      </div>
    </button>
    
    {/* Nombre de usuario */}
  </div>
);

// 🚀 OPTIMIZACIÓN CRÍTICA TikTok-style: TikTokPollCard memoizado
//
// CAUSA RAÍZ del lag durante el swipe: el padre TikTokScrollView llama
// setTranslateOffset(...) en CADA touchmove (60+ veces/s). Eso re-renderiza
// el padre, que vuelve a montar el array `slots.map(...)`, que pasa props
// a TikTokPollCard. Si TikTokPollCard NO está memoizado, se re-renderiza
// 60 veces/s — junto con su árbol (PollOptionMedia, VSLayout, MediaToolbar,
// CommentsModal lazy, etc.). En un VS con 2 videos + 3 preguntas eso son
// miles de operaciones por segundo durante un swipe.
//
// React.memo con shallow compare es suficiente: las únicas props que
// cambian durante el swipe son las internas de TikTokScrollView (que NO
// se pasan al card). poll / isActive / distanceFromActive solo cambian
// al COMMITear el swipe (al final), no durante. Las callbacks deben
// venir envueltas en useCallback desde el padre — si no, fallarían el
// shallow compare. (Ya se pasan así desde la página padre).
const TikTokPollCardInner = ({
  poll, 
  onVote, 
  onLike, 
  onShare, 
  onComment, 
  onSave, 
  onCreatePoll, 
  isActive, 
  index, 
  total, 
  showLogo = true, 
  shouldPreload = true, 
  isVisible = true, 
  onUpdatePoll, 
  onDeletePoll, 
  isOwnProfile, 
  currentUser: authUser, 
  savedPolls, 
  setSavedPolls,
  commentedPolls,
  setCommentedPolls,
  sharedPolls,
  setSharedPolls,
  // 🚀 NEW: Performance optimization props
  optimizeVideo = false,
  renderPriority = 'medium',
  shouldUnload = false,
  layout = null,
  // 🚀 NUEVO: Distancia al post activo y ancho de banda para preload TikTok-style
  distanceFromActive = 0,
  isHighBandwidth = true,
  // 🔒 NEW: Callback para notificar cuando un modal se abre/cierra
  onModalStateChange = null,
  // 📜 NEW: Mostrar hint de scroll solo para usuarios nuevos
  showScrollHint = false,
  // 🎵 Audio detail page context
  fromAudioDetailPage = false,
  currentAudio = null
}) => {
  const [isMusicPlaying, setIsMusicPlaying] = useState(false);
  const [showCommentsModal, setShowCommentsModal] = useState(false);
  const [showPostDetailModal, setShowPostDetailModal] = useState(false);
  const [showVotersModal, setShowVotersModal] = useState(false);
  const [showChallengeParticipants, setShowChallengeParticipants] = useState(false);
  const [audioContextActivated, setAudioContextActivated] = useState(false);
  const { isBottomNav } = useNavPreference();
  const { hideRightNavigation } = useTikTok();
  const { t } = useTranslation();
  // 🎯 LAYOUT MODE: refleja la preferencia del usuario (botones sociales a un lado vs abajo).
  // Debe permanecer constante aunque la barra inferior se oculte (vista inmersiva en
  // Profile/Audio/PostViewer/etc.) — los botones sociales laterales deben seguir visibles
  // en todas las páginas que renderizan TikTokScrollView, no solo en /feed.
  const isBottomNavVisible = isBottomNav;
  // 📐 Visibilidad REAL de la barra inferior (para reservar 56px de padding cuando aplica).
  const isBottomNavBarShown = isBottomNav && !hideRightNavigation;
  const [isCommentsExpanded, setIsCommentsExpanded] = useState(false);
  const [isVotersExpanded, setIsVotersExpanded] = useState(false);

  // 🚀 FIX F5 — Lazy-mount de modales con persistencia.
  // ANTES: Los 5 modales (Comments/PostDetail/Voters/ChallengeParticipants/
  //   Share) se montaban en cada slot SIEMPRE. × 3 slots = 15 árboles de
  //   modal con sus hooks corriendo en cada swipe (~5-15ms desperdiciados).
  // AHORA: solo se monta tras la PRIMERA apertura, y permanece montado
  //   después para preservar las animaciones de exit de AnimatePresence
  //   (que está dentro de cada modal). En el 95% de la sesión un usuario
  //   no abre la mayoría de estos modales → 0 coste.
  const hasOpenedCommentsRef = useRef(false);
  const hasOpenedPostDetailRef = useRef(false);
  const hasOpenedVotersRef = useRef(false);
  const hasOpenedChallengeParticipantsRef = useRef(false);
  const hasOpenedShareRef = useRef(false);
  if (showCommentsModal) hasOpenedCommentsRef.current = true;
  if (showPostDetailModal) hasOpenedPostDetailRef.current = true;
  if (showVotersModal) hasOpenedVotersRef.current = true;
  if (showChallengeParticipants) hasOpenedChallengeParticipantsRef.current = true;

  // 🗳️ VS posts: el snapshot `polls.total_votes` no se actualiza al votar
  // en el endpoint VS. VSLayout dispara `vs:statsUpdate` con el conteo real
  // tras el fetch a /api/vs/{vs_id}. Guardamos el override para mostrarlo
  // en el botón social de votos.
  const [vsTotalsOverride, setVsTotalsOverride] = useState({});
  // 🎨 VS posts: lado votado por el usuario en la PRIMERA pregunta del post
  // (a=lila #A855F7, b=azul #3B82F6). Se usa para colorear el botón social
  // de votos. VSLayout dispara `vs:userVote` al votar / al prefetch.
  const [vsUserVote, setVsUserVote] = useState({});
  useEffect(() => {
    const onStats = (e) => {
      const { pollId, totalVotes } = e?.detail || {};
      if (!pollId || typeof totalVotes !== 'number') return;
      setVsTotalsOverride(prev => {
        const cur = prev[pollId];
        // Mantenemos siempre el valor MÁS ALTO conocido para evitar que el
        // contador "baje" por carreras de eventos durante votos optimistas.
        if (typeof cur === 'number' && cur >= totalVotes) return prev;
        return { ...prev, [pollId]: totalVotes };
      });
    };
    const onUserVote = (e) => {
      const { pollId, votedSide } = e?.detail || {};
      if (!pollId || (votedSide !== 'a' && votedSide !== 'b')) return;
      setVsUserVote(prev => ({ ...prev, [pollId]: votedSide }));
    };
    window.addEventListener('vs:statsUpdate', onStats);
    window.addEventListener('vs:userVote', onUserVote);
    return () => {
      window.removeEventListener('vs:statsUpdate', onStats);
      window.removeEventListener('vs:userVote', onUserVote);
    };
  }, []);
  const getDisplayedTotalVotes = (p) => {
    const override = vsTotalsOverride[p?.id];
    const base = Number(p?.totalVotes) || 0;
    return typeof override === 'number' ? Math.max(override, base) : base;
  };
  const getVoteButtonColor = (p) => {
    // Solo colorear cuando es VS y el usuario ya votó en este post.
    const isVS = p?.layout === 'vs' || !!p?.vs_id;
    if (!isVS) return null;
    const side = vsUserVote[p?.id];
    if (side === 'a') return '#A855F7'; // Twyk lila (purple-500)
    if (side === 'b') return '#3B82F6'; // Twyk azul (blue-500)
    return null;
  };
  
  // Carousel state for multiple options
  // When coming from AudioDetailPage, start at the slide matching the current audio
  const getInitialSlide = () => {
    if (fromAudioDetailPage && currentAudio?.id && poll.options) {
      const audioId = currentAudio.id;
      const matchIdx = poll.options.findIndex(opt => 
        opt.extracted_audio_id === audioId || 
        opt.extracted_audio_id === `user_audio_${audioId}` ||
        `user_audio_${opt.extracted_audio_id}` === audioId
      );
      if (matchIdx >= 0) return matchIdx;
    }
    return 0;
  };
  const [currentSlide, setCurrentSlide] = useState(getInitialSlide);
  const [touchStart, setTouchStart] = useState(null);
  const [touchEnd, setTouchEnd] = useState(null);
  
  // 🎵 NUEVO: Estado para thumbnail dinámico del carrusel con audio original
  const [carouselThumbnail, setCarouselThumbnail] = useState(null);
  
  // 🎵 NUEVO: Estado para audio dinámico del carrusel con audio original
  const [carouselAudioId, setCarouselAudioId] = useState(null);
  const [carouselAudioData, setCarouselAudioData] = useState(null); // Full audio data for current slide
  
  // Story state for author avatar ring
  const [authorHasStories, setAuthorHasStories] = useState(false);
  const [authorStoriesData, setAuthorStoriesData] = useState(null);
  const [showAuthorStoryViewer, setShowAuthorStoryViewer] = useState(false);
  
  // 👁️ View tracking - Registra vista después de 2 segundos si el poll está activo y visible
  useViewTracking(poll.id, isActive && isVisible);
  
  // Touch handlers for carousel navigation
  const handleTouchStart = (e) => {
    setTouchStart(e.targetTouches[0].clientX);
  };

  const handleTouchEnd = (e) => {
    if (!touchStart || !e.changedTouches[0]) return;
    
    const touchEnd = e.changedTouches[0].clientX;
    const touchDiff = touchStart - touchEnd;
    
    // Minimum swipe distance
    if (Math.abs(touchDiff) < 50) return;
    
    if (touchDiff > 0) {
      // Swipe left - next slide
      setCurrentSlide(prev => Math.min(prev + 1, (poll.options?.length || 1) - 1));
    } else {
      // Swipe right - previous slide
      setCurrentSlide(prev => Math.max(prev - 1, 0));
    }
    
    setTouchStart(null);
  };
  
  // 🎵 NUEVO: Handler para cuando cambia el thumbnail del carrusel con audio original
  const handleCarouselThumbnailChange = (thumbnailUrl) => {
    console.log('🖼️ TikTokScrollView: Thumbnail del carrusel actualizado:', thumbnailUrl);
    setCarouselThumbnail(thumbnailUrl);
  };
  
  // 🎵 NUEVO: Handler para cuando cambia el audio del carrusel con audio original
  const handleCarouselAudioChange = (audioData) => {
    // audioData puede ser un objeto completo o null
    const audioId = audioData?.id || null;
    console.log('🎵 TikTokScrollView: Audio del carrusel actualizado:', {
      audioData,
      extractedId: audioId
    });
    setCarouselAudioId(audioId);
    setCarouselAudioData(audioData); // Save full audio data for UI display
  };
  
  // 🔄 Reset carousel thumbnail y audio cuando cambia el poll
  useEffect(() => {
    setCarouselThumbnail(null);
    setCarouselAudioId(null);
    setCarouselAudioData(null);
  }, [poll.id]);
  
  // 🔄 Cerrar el modal de comentarios cuando el card deja de estar activo (scroll)
  useEffect(() => {
    if (!isActive && showCommentsModal) {
      setShowCommentsModal(false);
    }
    if (!isActive && showPostDetailModal) {
      setShowPostDetailModal(false);
    }
  }, [isActive, showCommentsModal, showPostDetailModal]);

  // 🏆 Listener para abrir comentarios desde la VSWinnerCard
  useEffect(() => {
    const handleOpenComments = (e) => {
      if (!isActive) return;
      if (e?.detail?.pollId && e.detail.pollId === poll?.id) {
        setShowCommentsModal(true);
      }
    };
    window.addEventListener('vs:openComments', handleOpenComments);
    return () => window.removeEventListener('vs:openComments', handleOpenComments);
  }, [isActive, poll?.id]);
  
  // Feed menu state
  const [isNotificationEnabled, setIsNotificationEnabled] = useState(false);
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  
  // 🔒 Notificar al padre cuando un modal se abre/cierra para bloquear el swipe
  useEffect(() => {
    const isAnyModalOpen = showCommentsModal || showPostDetailModal || showVotersModal || isMenuOpen;
    if (onModalStateChange) {
      onModalStateChange(isAnyModalOpen);
    }
  }, [showCommentsModal, showPostDetailModal, showVotersModal, isMenuOpen, onModalStateChange]);
  
  const navigate = useNavigate();
  const { followUser, unfollowUser, isFollowing, followsMe, getFollowStatus, followStateVersion } = useFollow();
  const { user: currentUser } = useAuth();
  const { toast } = useToast();
  const { shareModal, sharePoll, closeShareModal } = useShare();

  // Feed menu handlers
  const handleNotInterested = async (pollId) => {
    try {
      await feedMenuService.markNotInterested(pollId);
      return { success: true };
    } catch (error) {
      console.error('Error marking as not interested:', error);
      throw error;
    }
  };

  const handleHideUser = async (authorId) => {
    try {
      await feedMenuService.hideUser(authorId);
      return { success: true };
    } catch (error) {
      console.error('Error hiding user:', error);
      throw error;
    }
  };

  const handleToggleNotifications = async (authorId) => {
    try {
      const result = await feedMenuService.toggleNotifications(authorId);
      setIsNotificationEnabled(result.notifications_enabled);
      return { success: true };
    } catch (error) {
      console.error('Error toggling notifications:', error);
      throw error;
    }
  };

  const handleReport = async (pollId, reportData) => {
    try {
      await feedMenuService.reportContent(pollId, reportData);
      return { success: true };
    } catch (error) {
      console.error('Error submitting report:', error);
      throw error;
    }
  };

  // Handle avatar click - open stories if unviewed, go to profile if all viewed
  const handleAvatarClick = (e) => {
    e.stopPropagation();
    
    // If has stories and has unviewed stories, open story viewer
    if (authorStoriesData && authorStoriesData.has_unviewed) {
      audioManager.pause(); // Pause feed audio when opening stories
      setShowAuthorStoryViewer(true);
    } else {
      // Navigate to profile if no stories or all stories viewed
      handleUserClick(poll.authorUser || { username: poll.author?.username || poll.author?.display_name || 'usuario' });
    }
  };
  
  // Handle story viewer close - reload stories to update viewed status
  const handleStoryViewerClose = async () => {
    setShowAuthorStoryViewer(false);
    audioManager.resume(); // Resume feed audio when closing stories
    // Reload stories to update viewed status
    try {
      if (authorUserId) {
        const storiesResponse = await storyService.getUserStories(authorUserId);
        if (storiesResponse && storiesResponse.total_stories > 0) {
          setAuthorHasStories(true);
          setAuthorStoriesData(storiesResponse);
        } else {
          setAuthorHasStories(false);
          setAuthorStoriesData(null);
        }
      }
    } catch (error) {
      console.error('Error reloading author stories:', error);
    }
  };



  // Debug logging for save functionality
  useEffect(() => {
    console.log('🔖 TikTokScrollView: onSave prop received:', typeof onSave, !!onSave);
  }, [onSave]);

  // Get user ID from poll author
  const getAuthorUserId = () => {
    // Primero intentar con el objeto author si existe (priorizar UUID)
    if (poll.author && poll.author.id) {
      return poll.author.id;
    }
    // Luego intentar con authorUser UUID (legacy support)
    if (poll.authorUser && poll.authorUser.id) {
      return poll.authorUser.id;
    }
    // Solo usar username si no hay UUID disponible
    if (poll.author && poll.author.username) {
      return poll.author.username;
    }
    if (poll.authorUser && poll.authorUser.username) {
      return poll.authorUser.username;
    }
    // Convert author display name to username format como fallback
    const displayName = poll.author?.display_name || poll.author?.username || poll.authorUser?.displayName || 'unknown';
    return displayName.toLowerCase().replace(/\s+/g, '_');
  };

  const authorUserId = getAuthorUserId();

  // 🚀 FIX F6 — Gate de fetches HTTP por swipe.
  // ANTES: estos 2 efectos disparaban fetch a `getFollowStatus` y
  //   `getUserStories` cada vez que `authorUserId` cambiaba — es decir,
  //   en CADA swipe, incluso en los slots PREV/NEXT (inactivos). Cuando
  //   el usuario hacía fast-scroll, 4-6 fetch HTTP se acumulaban robando
  //   ancho de banda al HLS del nuevo video → primer frame retardado.
  // AHORA: sólo se disparan si la card está ACTIVA (es la visible) y
  //   tras 300ms de estabilidad (debounce). En un fast-scroll de 5 posts
  //   seguidos en <300ms, sólo el último dispara los fetch.
  // Check follow status when component mounts or follow state changes
  useEffect(() => {
    if (!isActive || !authorUserId || !currentUser || authorUserId === currentUser.id) return;
    const t = setTimeout(() => {
      getFollowStatus(authorUserId);
    }, 300);
    return () => clearTimeout(t);
  }, [isActive, authorUserId, currentUser, getFollowStatus, followStateVersion]);

  // Load author stories status — gated igual que follow status
  useEffect(() => {
    if (!isActive || !authorUserId) return;
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        const storiesResponse = await storyService.getUserStories(authorUserId);
        if (cancelled) return;
        if (storiesResponse && storiesResponse.total_stories > 0) {
          setAuthorHasStories(true);
          setAuthorStoriesData(storiesResponse);
        } else {
          setAuthorHasStories(false);
          setAuthorStoriesData(null);
        }
      } catch (error) {
        if (cancelled) return;
        console.error('Error loading author stories:', error);
        setAuthorHasStories(false);
        setAuthorStoriesData(null);
      }
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [isActive, authorUserId]);

  // 🚀 FIX F1 — RACE CONDITION DE AUDIO ELIMINADA
  // ANTES: este efecto + el efecto del padre (línea ~2013) competían por
  //   audioManager.play/stop con un setTimeout(100) artificial entre stop y
  //   play. Resultado: 100-200ms de silencio por swipe + race condition.
  // AHORA: el control real de audio (play/stop) vive SOLO en el padre
  //   (TikTokScrollView, efecto en activeIndex/polls). Este efecto se reduce
  //   a su única función legítima: actualizar el flag UI `isMusicPlaying`
  //   para que el icono musical refleje el estado. Sin setTimeout, sin
  //   await artificiales, sin console.log spam (6/swipe eliminados).
  useEffect(() => {
    const hasExtractedAudio = poll.layout === 'off' && poll.options?.some(opt => opt.extracted_audio_id);
    const hasMusic = !!(poll.music && poll.music.preview_url && !hasExtractedAudio);
    // El icono musical sólo "vibra" en el card activo que efectivamente
    // tiene música. Cuando deja de estar activo, apagamos el flag.
    setIsMusicPlaying(isActive && hasMusic);
  }, [isActive, poll.music?.preview_url, poll.id, poll.layout, poll.options]);

  // Activar audio context en primera interacción
  useEffect(() => {
    const activateOnFirstInteraction = async () => {
      if (!audioContextActivated) {
        const activated = await audioManager.activateAudioContext();
        setAudioContextActivated(activated);
      }
    };

    // Activar en cualquier click o touch
    document.addEventListener('click', activateOnFirstInteraction, { once: true });
    document.addEventListener('touchstart', activateOnFirstInteraction, { once: true });

    return () => {
      document.removeEventListener('click', activateOnFirstInteraction);
      document.removeEventListener('touchstart', activateOnFirstInteraction);
    };
  }, [audioContextActivated]);

  const handleVote = (optionId) => {
    if (!poll.userVote) {
      onVote(poll.id, optionId);
    }
  };

  const handleUserClick = (user) => {
    // If it's the current user, navigate without param so isOwnProfile works
    if (authUser && (user.id === authUser.id || user.username === authUser.username)) {
      navigate('/profile');
    } else {
      navigate(`/profile/${user.username}`);
    }
  };

  const handleFollowUser = async (user) => {
    const userId = user.id || user.username;
    
    try {
      const result = await followUser(userId);
      if (result.success) {
        toast({
          title: "¡Siguiendo!",
          description: `Ahora sigues a @${user.username || user.displayName}`,
          duration: 2000,
        });
      } else {
        toast({
          title: "Error",
          description: result.error || "Error al seguir al usuario",
          variant: "destructive",
          duration: AppConfig.TOAST_DURATION,
        });
      }
    } catch (error) {
      console.error('Error following user:', error);
      toast({
        title: "Error",
        description: "Error al seguir al usuario",
        variant: "destructive",
        duration: AppConfig.TOAST_DURATION,
      });
    }
  };

  const handleMusicToggle = (playing) => {
    setIsMusicPlaying(playing);
  };

  const formatNumber = (num) => {
    // Handle undefined, null, or non-numeric values
    if (num === undefined || num === null || isNaN(num)) {
      return '0';
    }
    
    const numValue = Number(num);
    if (numValue >= 1000000) {
      return `${(numValue / 1000000).toFixed(1)}M`;
    }
    if (numValue >= 1000) {
      return `${(numValue / 1000).toFixed(1)}K`;
    }
    return numValue.toString();
  };

  const getPercentage = (votes) => {
    if (poll.totalVotes === 0) return 0;
    return Math.round((votes / poll.totalVotes) * 100);
  };

  const getWinningOption = () => {
    if (!poll.options || poll.options.length === 0) {
      return null;
    }
    return poll.options.reduce((max, option) => 
      option.votes > max.votes ? option : max
    );
  };

  const winningOption = getWinningOption();

  // Si algún bottom sheet está medio abierto, comprimir el post
  const isPostMiniature = isBottomNavVisible && (
    (showCommentsModal && !isCommentsExpanded) ||
    (showVotersModal && !isVotersExpanded)
  );

  // 🎯 Helper: renderiza los botones de acción (like, comentar, compartir, guardar, menú, música)
  // sideMode=true → estilo TikTok: columna vertical en lateral derecho, SIN marco
  // sideMode=false → estilo actual: fila horizontal en la parte inferior, CON marco
  const renderActionButtons = (sideMode) => {
    const iconCls = sideMode ? "w-[23px] h-[23px]" : "w-5 h-5";
    const txtCls = sideMode ? "text-[8px]" : "text-sm";
    const btnLayout = sideMode
      ? "flex flex-col items-center gap-0.5 hover:scale-110 transition-all duration-200 h-auto p-0 bg-transparent hover:bg-transparent [&_svg]:size-[23px]"
      : "flex items-center gap-1 hover:scale-105 transition-all duration-200 h-auto p-1.5 rounded-lg backdrop-blur-sm";
    const dropShadowStyle = sideMode ? { filter: 'drop-shadow(0 2px 4px rgba(0,0,0,0.7))' } : undefined;

    // 🏷️ Helper: en sideMode (barra de navegación inferior activa) cuando el
    // contador está en 0 mostramos un texto-placeholder en lugar de "0".
    //   Heart  → "be 1st"
    //   Comment → "add 1st"
    //   Share  → "share"
    //   Save   → "save"
    const getCountLabel = (count, placeholder) => {
      const n = Number(count) || 0;
      if (sideMode && n === 0) return placeholder;
      return formatNumber(n);
    };

    return (
      <>
        {/* Like */}
        <Button
          variant="ghost"
          size="sm"
          onClick={(e) => {
            e.stopPropagation();
            onLike(poll.id);
          }}
          className={cn(
            btnLayout,
            sideMode
              ? "text-white hover:text-white"
              : cn("text-white hover:text-red-400 bg-black/20", poll.userLiked && "bg-red-500/20")
          )}
          style={dropShadowStyle}
        >
          <Heart className={cn(
            `${iconCls} flex-shrink-0 transition-all duration-200`,
            poll.userLiked && "fill-current scale-110 text-red-500"
          )} />
          <span className={`font-medium ${txtCls} whitespace-nowrap text-white`}>{getCountLabel(poll.likes, t('feed.actions.like0'))}</span>
        </Button>

        {/* Comment */}
        <Button
          variant="ghost"
          size="sm"
          onClick={(e) => {
            e.stopPropagation();
            setShowCommentsModal(true);
            const commentsEnabled = poll.comments_enabled !== false && poll.commentsEnabled !== false;
            if (commentsEnabled) {
              setCommentedPolls(prev => {
                const newSet = new Set(prev);
                newSet.add(poll.id);
                return newSet;
              });
            }
          }}
          className={cn(
            btnLayout,
            sideMode
              ? "text-white hover:text-white"
              : ((commentedPolls.has(poll.id) || poll.userCommented)
                  ? "text-white bg-blue-500/20 hover:text-blue-300"
                  : "text-white bg-black/20 hover:text-blue-400")
          )}
          style={dropShadowStyle}
        >
          <MessageCircle className={cn(
            `${iconCls} flex-shrink-0`,
            (commentedPolls.has(poll.id) || poll.userCommented) && "fill-current text-blue-400"
          )} />
          {(poll.comments_enabled !== false && poll.commentsEnabled !== false) && (
            <span className={`font-medium ${txtCls} whitespace-nowrap text-white`}>{getCountLabel(poll.comments, t('feed.actions.comment0'))}</span>
          )}
        </Button>

        {/* Share */}
        <Button
          variant="ghost"
          size="sm"
          onClick={async (e) => {
            e.stopPropagation();
            const registerShareInBackend = async () => {
              const token = localStorage.getItem('token');
              if (token) {
                try {
                  const response = await fetch(`${process.env.REACT_APP_BACKEND_URL}/api/polls/${poll.id}/share`, {
                    method: 'POST',
                    headers: {
                      'Authorization': `Bearer ${token}`,
                      'Content-Type': 'application/json'
                    }
                  });
                  if (response.ok) {
                    const result = await response.json();
                    console.log('🔗 TikTokScrollView: Poll shared successfully, new count:', result.shares);
                    setSharedPolls(prev => {
                      const newSet = new Set(prev);
                      newSet.add(poll.id);
                      return newSet;
                    });
                  }
                } catch (error) {
                  console.error('🔗 TikTokScrollView: Error sharing poll:', error);
                }
              }
            };
            if (navigator.share) {
              navigator.share({
                title: poll.question || 'Vota en esta encuesta',
                text: 'Mira esta increíble votación',
                url: `${window.location.origin}/poll/${poll.id}`,
              }).then(async () => {
                await registerShareInBackend();
                onShare && onShare(poll.id);
              }).catch((error) => {
                if (error.name !== 'AbortError') {
                  sharePoll(poll);
                }
              });
            } else {
              sharePoll(poll);
            }
          }}
          className={cn(
            btnLayout,
            sideMode
              ? "text-white hover:text-white"
              : ((sharedPolls.has(poll.id) || poll.userShared)
                  ? "text-white bg-green-500/20 hover:text-green-300"
                  : "text-white bg-black/20 hover:text-green-400")
          )}
          style={dropShadowStyle}
        >
          <Share2 className={cn(
            `${iconCls} flex-shrink-0`,
            (sharedPolls.has(poll.id) || poll.userShared) && "fill-current text-green-400"
          )} />
          <span className={`font-medium ${txtCls} whitespace-nowrap text-white`}>{getCountLabel(poll.shares, t('feed.actions.share'))}</span>
        </Button>

        {/* Save */}
        {onSave && (
          <Button
            variant="ghost"
            size="sm"
            onClick={async (e) => {
              e.preventDefault();
              e.stopPropagation();
              const isCurrentlySaved = savedPolls.has(poll.id);
              try {
                if (isCurrentlySaved) {
                  const response = await fetch(`${process.env.REACT_APP_BACKEND_URL}/api/polls/${poll.id}/save`, {
                    method: 'DELETE',
                    headers: {
                      'Authorization': `Bearer ${localStorage.getItem('token')}`,
                      'Content-Type': 'application/json'
                    }
                  });
                  if (response.ok) {
                    setSavedPolls(prev => {
                      const newSet = new Set(prev);
                      newSet.delete(poll.id);
                      return newSet;
                    });
                  }
                } else {
                  onSave(poll.id);
                  setSavedPolls(prev => {
                    const newSet = new Set(prev);
                    newSet.add(poll.id);
                    return newSet;
                  });
                }
              } catch (error) {
                console.error('🔖 TikTokScrollView: Error with save/unsave:', error);
              }
            }}
            className={cn(
              btnLayout,
              "cursor-pointer pointer-events-auto z-50",
              sideMode
                ? "text-white hover:text-white"
                : ((savedPolls.has(poll.id) || poll.isSaved)
                    ? "text-white bg-yellow-500/20 hover:text-yellow-300"
                    : "text-white bg-black/20 hover:text-yellow-400")
            )}
            style={{ pointerEvents: 'auto', ...(dropShadowStyle || {}) }}
          >
            <Bookmark className={cn(
              `${iconCls} flex-shrink-0`,
              (savedPolls.has(poll.id) || poll.isSaved) && "fill-current text-yellow-400"
            )} />
            <span className={`font-medium ${txtCls} whitespace-nowrap text-white`}>{getCountLabel(poll.saves_count || 0, t('feed.actions.save'))}</span>
          </Button>
        )}

        {/* Feed Menu - Only shown for other users' posts */}
        {(() => {
          const shouldShowMenu = currentUser && (
            (poll.author?.id && poll.author.id !== currentUser.id) ||
            (poll.authorUser?.id && poll.authorUser.id !== currentUser.id)
          );
          return shouldShowMenu;
        })() && (
          <FeedMenu
            poll={poll}
            onNotInterested={handleNotInterested}
            onHideUser={handleHideUser}
            onToggleNotifications={handleToggleNotifications}
            onReport={handleReport}
            isNotificationEnabled={isNotificationEnabled}
            onOpenChange={setIsMenuOpen}
            className={sideMode
              ? "flex items-center justify-center text-white hover:text-gray-300 hover:scale-110 transition-all duration-200 h-auto p-0 bg-transparent"
              : "flex items-center justify-center text-white hover:text-gray-300 hover:scale-105 transition-all duration-200 h-auto p-1.5 rounded-lg bg-black/20 backdrop-blur-sm"}
          />
        )}

        {/* Post Management Menu - Only shown for own posts */}
        {onUpdatePoll && onDeletePoll && authUser && poll.author?.id === authUser.id && (
          <PostManagementMenu
            poll={poll}
            onUpdate={onUpdatePoll}
            onDelete={onDeletePoll}
            currentUser={authUser}
            isOwnProfile={isOwnProfile}
            onOpenChange={setIsMenuOpen}
            className={sideMode
              ? "flex items-center justify-center text-white hover:text-purple-400 hover:scale-110 transition-all duration-200 h-auto p-0 bg-transparent"
              : "flex items-center justify-center text-white hover:text-purple-400 hover:scale-105 transition-all duration-200 h-auto p-1.5 rounded-lg bg-black/20 backdrop-blur-sm"}
          />
        )}

        {/* Music Player - disco rotativo - SOLO en modo bottom (no se mueve al lateral) */}
        {!sideMode && poll.music && (() => {
          const hasExtractedAudio = poll.layout === 'off' && poll.options?.some(opt => opt.extracted_audio_id);
          const displayMusic = hasExtractedAudio && carouselAudioData
            ? { ...poll.music, title: carouselAudioData.title || poll.music?.title, artist: carouselAudioData.artist || poll.music?.artist, cover: carouselAudioData.cover || poll.music?.cover, preview_url: carouselAudioData.preview_url || poll.music?.preview_url, id: carouselAudioData.id || poll.music?.id }
            : poll.music;
          return (
            <MusicPlayer
              music={displayMusic}
              isVisible={isActive}
              onTogglePlay={handleMusicToggle}
              autoPlay={!hasExtractedAudio}
              loop={true}
              authorAvatar={carouselThumbnail || poll.author?.avatar_url}
              authorUsername={poll.author?.username || poll.author?.display_name}
              overrideAudioId={carouselAudioId}
              forceUseAvatar={!!carouselThumbnail}
              className="flex-shrink-0 ml-auto"
            />
          );
        })()}
      </>
    );
  };

  return (
    <div className="w-full h-full flex flex-col relative bg-black overflow-hidden">
      
      {/* Contenedor del post con transformación miniatura */}
      <div 
        className="absolute inset-0 transition-all duration-500 ease-out origin-top"
        style={isPostMiniature ? {
          top: '8px',
          left: 0,
          right: 0,
          bottom: 'auto',
          width: '100%',
            // 🛠️ El post miniatura ocupa el 60% del viewport.
            // Modal medio-abierto + barra inferior (input + emojis) = 40% del viewport.
            // Sheet modal = calc(40dvh - 113px), input bar ≈ 105px + 8px gap.
            height: '60dvh',
          borderRadius: '20px',
          overflow: 'hidden',
          zIndex: 101,
          pointerEvents: 'none',
          boxShadow: '0 10px 40px rgba(0,0,0,0.35), 0 4px 12px rgba(0,0,0,0.2)',
        } : {}}
      >

      {/* Header - Fixed at top with safe area */}
      <div className={`absolute top-0 left-0 right-0 z-30 bg-gradient-to-b from-black/80 to-transparent px-4 pt-safe-4 pb-8 ${isPostMiniature ? 'pointer-events-auto' : ''}`}
           style={{ paddingTop: 'max(1rem, var(--safe-area-inset-top))' }}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">

            {/* 🏆 CHALLENGE: Avatares de participantes reemplazan el avatar del autor */}
            {poll.is_challenge && poll.participants && poll.participants.length > 0 ? (
              <>
                {/* Participant avatars + text (replaces author avatar) */}
                <button
                  className="flex items-center gap-2"
                  onClick={(e) => {
                    e.stopPropagation();
                    setShowChallengeParticipants(true);
                  }}
                >
                  {/* Overlapping circular avatars - diagonal stack like Instagram Reels */}
                  <div className="relative flex-shrink-0" style={{ width: '48px', height: '48px' }}>
                    {/* Avatar 2 - top-right, behind, with crescent mask cutout */}
                    {poll.participants[1] && (
                      <div
                        className="rounded-full overflow-hidden bg-white absolute"
                        style={{ 
                          zIndex: 1, 
                          top: '0px', 
                          right: '0px', 
                          width: '30px', 
                          height: '30px',
                          WebkitMaskImage: 'radial-gradient(circle at 0px 30px, transparent 19px, black 20px)',
                          maskImage: 'radial-gradient(circle at 0px 30px, transparent 19px, black 20px)'
                        }}
                      >
                        {poll.participants[1].avatar_url ? (
                          <img
                            src={resolveAssetUrl(poll.participants[1].avatar_url)}
                            alt={poll.participants[1].username || ''}
                            className="w-full h-full object-cover"
                          />
                        ) : (
                          <div className="w-full h-full flex items-center justify-center bg-gray-50 text-gray-400">
                            <User className="w-3 h-3" />
                          </div>
                        )}
                      </div>
                    )}
                    {/* Avatar 1 - bottom-left, in front, no border */}
                    {poll.participants[0] && (
                      <div
                        className="rounded-full overflow-hidden bg-white absolute"
                        style={{ zIndex: 2, bottom: '0px', left: '0px', width: '36px', height: '36px' }}
                      >
                        {poll.participants[0].avatar_url ? (
                          <img
                            src={resolveAssetUrl(poll.participants[0].avatar_url)}
                            alt={poll.participants[0].username || ''}
                            className="w-full h-full object-cover"
                          />
                        ) : (
                          <div className="w-full h-full flex items-center justify-center bg-gray-50 text-gray-400">
                            <User className="w-4 h-4" />
                          </div>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Participant names */}
                  <div>
                    <span className="text-white text-sm font-bold leading-tight drop-shadow-md">
                      {(() => {
                        const total = poll.participants.length;
                        const p1 = poll.participants[0];
                        const name1 = p1.username || p1.display_name || 'usuario';
                        if (total === 2) {
                          const p2 = poll.participants[1];
                          const name2 = p2.username || p2.display_name || 'usuario';
                          return `${name1} vs ${name2}`;
                        }
                        const rest = total - 1;
                        return `${name1} y ${rest} persona${rest > 1 ? 's' : ''} más`;
                      })()}
                    </span>
                    <p className="text-sm text-white/70">{poll.timeAgo}</p>
                  </div>
                </button>
              </>
            ) : (
              <>
                {/* NORMAL POST: Avatar del autor con botón seguir */}
                <div className="group relative">
                  {/* Avatar para navegar al perfil o abrir historias */}
                  <button
                    onClick={handleAvatarClick}
                    className="w-12 h-12 rounded-full relative"
                  >
                    {/* Anillo con centro transparente */}
                    {authorHasStories && (
                      <div className={`absolute inset-0 rounded-full ${
                        authorStoriesData?.has_unviewed
                          ? 'bg-gradient-to-tr from-[#6366F1] via-[#8B5CF6] to-[#B061FF]'
                          : 'bg-gray-300'
                      }`}
                        style={{
                          WebkitMaskImage: 'radial-gradient(circle, transparent 22.5px, black 23px)',
                          maskImage: 'radial-gradient(circle, transparent 22.5px, black 23px)'
                        }}
                      />
                    )}
                    {/* Avatar - siempre mismo tamaño */}
                    <div className="absolute rounded-full overflow-hidden" style={{ inset: '3.5px' }}>
                      <Avatar className="w-full h-full rounded-full">
                        <AvatarImage 
                          src={poll.author?.avatar_url && poll.author.avatar_url !== null ? resolveAssetUrl(poll.author.avatar_url) : undefined} 
                          className="object-cover" 
                        />
                        <AvatarFallback className="bg-gray-50 text-gray-400 flex items-center justify-center">
                          <User className="w-4 h-4" />
                        </AvatarFallback>
                      </Avatar>
                    </div>
                  </button>

                  {/* Botón separado para seguir */}
                  {!isFollowing(authorUserId) && currentUser && authorUserId !== currentUser.id && (
                    <button
                      onClick={(e) => {
                        console.log('🎯 PLUS BUTTON CLICKED!');
                        e.stopPropagation();
                        const userToFollow = poll.authorUser || { 
                          username: (poll.author?.username || poll.author?.display_name || 'unknown').toLowerCase().replace(/\s+/g, '_'),
                          displayName: poll.author?.display_name || poll.author?.username || 'Usuario',
                          id: authorUserId 
                        };
                        console.log('📝 userToFollow object:', userToFollow);
                        handleFollowUser(userToFollow);
                      }}
                      className="absolute bottom-1 right-1 rounded-full p-[3px] shadow-lg cursor-pointer transition-all duration-200 hover:scale-125 hover:opacity-90 active:scale-95"
                      style={{ background: 'linear-gradient(135deg, #6366F1, #8B5CF6)' }}
                    >
                      <Plus className="w-3.5 h-3.5 text-white stroke-[3]" />
                    </button>
                  )}
                  
                  {/* Hover tooltip para seguir */}
                  {!isFollowing(authorUserId) && currentUser && authorUserId !== currentUser.id && (
                    <div className="absolute -top-12 left-1/2 transform -translate-x-1/2 opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none z-50">
                      <div className="bg-blue-800/90 text-white text-xs px-3 py-1.5 rounded-lg backdrop-blur-sm whitespace-nowrap border border-blue-600/30 shadow-lg">
                        <div className="font-medium">@{poll.authorUser?.username || poll.author?.username || poll.author?.display_name || 'usuario'}</div>
                        <div className="text-blue-300 text-[10px]">{followsMe(authorUserId) ? 'Seguir también' : 'Seguir usuario'}</div>
                      </div>
                    </div>
                  )}
                  
                  {/* Indicador de siguiendo con animación */}
                  {isFollowing(authorUserId) && (
                    <div 
                      className="absolute bottom-1 right-1 bg-white rounded-full p-[3px] shadow-lg"
                      style={{
                        animation: 'followBounce 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275) forwards'
                      }}
                    >
                      <CheckCircle className="w-3.5 h-3.5 text-indigo-500" />
                    </div>
                  )}
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="text-white font-semibold text-base">{poll.author?.display_name || poll.author?.username || poll.authorUser?.displayName || 'Usuario'}</h3>
                  </div>
                  <p className="text-sm text-white/70">{poll.timeAgo}</p>
                </div>
              </>
            )}

          </div>

        </div>
        
        <div className="mt-1">
          <h2 
            className="text-white text-sm leading-tight text-left line-clamp-2 cursor-pointer active:opacity-70"
            onClick={(e) => {
              e.stopPropagation();
              setShowPostDetailModal(true);
            }}
          >
            {renderTextWithHashtags(poll.title, navigate)}
          </h2>
        </div>


      </div>

      {/* Main content - Layout Renderer or Challenge Layout */}
      <div className="absolute inset-0 w-full h-full"
           style={{
             top: 0,
             bottom: 0,
             left: 'env(safe-area-inset-left, 0)',
             right: 'env(safe-area-inset-right, 0)'
           }}>
        {/* 🏆 CHALLENGE LAYOUT - Muestra contenido de múltiples participantes */}
        {/* Si el challenge tiene un layout estándar de contenido (off, vertical, etc.), 
            usar LayoutRenderer normal. Solo usar el layout challenge-específico para layouts 
            tipo vs-horizontal, 1vs1, stack, etc. */}
        {poll.is_challenge && !['off', 'vertical', 'horizontal', 'triptych-vertical', 'triptych-horizontal', 'grid-2x2', 'grid-3x2', 'horizontal-3x2'].includes(poll.layout) ? (
          <div className="w-full h-full flex flex-col">
            {/* Contenido del Challenge - Layout con barras de porcentaje como el feed */}
            <div className="flex-1 flex">
              {(() => {
                // Calcular total de votos y porcentajes
                const totalVotes = poll.options.reduce((sum, opt) => sum + (opt.votes || 0), 0);
                const getPercentage = (votes) => {
                  if (totalVotes === 0) return 0;
                  return Math.round((votes / totalVotes) * 100);
                };
                
                // Determinar ganador
                const maxVotes = Math.max(...poll.options.map(o => o.votes || 0));
                const winningOptionId = poll.options.find(o => (o.votes || 0) === maxVotes && maxVotes > 0)?.id;
                const hasUserVoted = !!poll.userVote;
                
                if (poll.options.length === 2) {
                  // Layout 1vs1 - Dos contenidos lado a lado
                  return (
                    <>
                      {poll.options.map((option, optIdx) => {
                        const percentage = getPercentage(option.votes || 0);
                        const isWinner = option.id === winningOptionId && hasUserVoted;
                        const isSelected = poll.userVote === option.id;
                        
                        return (
                          <div 
                            key={option.id || optIdx}
                            className={cn(
                              "flex-1 relative overflow-hidden",
                              optIdx === 0 ? "border-r-2 border-white/30" : ""
                            )}
                          >
                            <DoubleTapVoteAnimation
                              onDoubleTap={() => !hasUserVoted && handleVote(option.id)}
                              disabled={hasUserVoted}
                            >
                            {/* Media del participante (offline-first cacheable) */}
                            <PollOptionMedia
                              option={option}
                              className="w-full h-full"
                              distanceFromActive={isActive ? 0 : 99}
                              videoProps={{
                                autoPlay: true,
                                loop: true,
                                muted: true,
                                playsInline: true,
                              }}
                            />
                            
                            {/* Mentioned Users */}
                            {option.mentioned_users?.length > 0 && (
                              <div className="absolute bottom-16 left-2 right-2 z-20 flex flex-wrap gap-1 items-center">
                                {option.mentioned_users.slice(0, 2).map((mentionedUser, mIdx) => (
                                  <button
                                    key={mentionedUser.id || mIdx}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      const username = mentionedUser.username || mentionedUser.display_name?.toLowerCase().replace(/\s+/g, '_');
                                      if (username) navigate(`/profile/${username}`);
                                    }}
                                    className="flex items-center bg-white/20 px-1.5 py-0.5 rounded-full backdrop-blur-sm cursor-pointer hover:bg-white/30 transition-all duration-200"
                                  >
                                    <Avatar className="w-4 h-4 mr-1 border border-white/50">
                                      <AvatarImage src={resolveAssetUrl(mentionedUser.avatar_url)} />
                                      <AvatarFallback className="bg-gray-400 text-white text-[8px] flex items-center justify-center">
                                        <User className="w-2 h-2" />
                                      </AvatarFallback>
                                    </Avatar>
                                    <span className="text-[10px] text-white font-medium">
                                      @{(mentionedUser.username || mentionedUser.display_name)?.slice(0, 10)}
                                    </span>
                                  </button>
                                ))}
                                {option.mentioned_users.length > 2 && (
                                  <div className="flex items-center bg-white/20 px-1.5 py-0.5 rounded-full backdrop-blur-sm">
                                    <span className="text-[10px] text-white/90">+{option.mentioned_users.length - 2}</span>
                                  </div>
                                )}
                              </div>
                            )}

                            {/* Barra de porcentaje - estilo igual que polls normales */}
                            {hasUserVoted && percentage > 0 && (
                              <div 
                                className={cn(
                                  "absolute inset-x-0 bottom-0 rounded-t-lg transition-all",
                                  isWinner 
                                    ? "bg-green-500/15"
                                    : isSelected 
                                      ? "bg-blue-500/15"
                                      : "bg-white/10"
                                )}
                                style={{ 
                                  height: `${Math.max(percentage, 15)}%`
                                }}
                              >
                                {isWinner && (
                                  <div className="absolute top-2 left-1/2 -translate-x-1/2">
                                    <Trophy className="w-4 h-4 text-green-300" />
                                  </div>
                                )}
                              </div>
                            )}
                            </DoubleTapVoteAnimation>
                          </div>
                        );
                      })}
                    </>
                  );
                } else {
                  // Layout para más de 2 participantes - Grid
                  return (
                    <div className="w-full h-full grid grid-cols-2 gap-1">
                      {poll.options.map((option, optIdx) => {
                        const percentage = getPercentage(option.votes || 0);
                        const isWinner = option.id === winningOptionId && hasUserVoted;
                        const isSelected = poll.userVote === option.id;
                        
                        return (
                          <div 
                            key={option.id || optIdx}
                            className="relative overflow-hidden"
                          >
                            <DoubleTapVoteAnimation
                              onDoubleTap={() => !hasUserVoted && handleVote(option.id)}
                              disabled={hasUserVoted}
                            >
                            {option.media?.type?.includes('video') || option.media?.url ? (
                              <PollOptionMedia
                                option={option}
                                className="w-full h-full"
                                distanceFromActive={isActive ? 0 : 99}
                                videoProps={{
                                  autoPlay: true,
                                  loop: true,
                                  muted: true,
                                  playsInline: true,
                                }}
                              />
                            ) : (
                              <div className="w-full h-full bg-gradient-to-br from-purple-900 to-pink-900" />
                            )}
                            
                            {/* Mentioned Users */}
                            {option.mentioned_users?.length > 0 && (
                              <div className="absolute bottom-12 left-1 right-1 z-20 flex flex-wrap gap-0.5 items-center">
                                {option.mentioned_users.slice(0, 2).map((mentionedUser, mIdx) => (
                                  <button
                                    key={mentionedUser.id || mIdx}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      const username = mentionedUser.username || mentionedUser.display_name?.toLowerCase().replace(/\s+/g, '_');
                                      if (username) navigate(`/profile/${username}`);
                                    }}
                                    className="flex items-center bg-white/20 px-1 py-0.5 rounded-full backdrop-blur-sm cursor-pointer hover:bg-white/30 transition-all duration-200"
                                  >
                                    <Avatar className="w-3 h-3 mr-0.5 border border-white/50">
                                      <AvatarImage src={resolveAssetUrl(mentionedUser.avatar_url)} />
                                      <AvatarFallback className="bg-gray-400 text-white text-[7px] flex items-center justify-center">
                                        <User className="w-2 h-2" />
                                      </AvatarFallback>
                                    </Avatar>
                                    <span className="text-[9px] text-white font-medium">
                                      @{(mentionedUser.username || mentionedUser.display_name)?.slice(0, 8)}
                                    </span>
                                  </button>
                                ))}
                                {option.mentioned_users.length > 2 && (
                                  <div className="flex items-center bg-white/20 px-1 py-0.5 rounded-full backdrop-blur-sm">
                                    <span className="text-[9px] text-white/90">+{option.mentioned_users.length - 2}</span>
                                  </div>
                                )}
                              </div>
                            )}

                            {/* Barra de porcentaje - estilo igual que polls normales */}
                            {hasUserVoted && percentage > 0 && (
                              <div 
                                className={cn(
                                  "absolute inset-x-0 bottom-0 rounded-t-lg transition-all",
                                  isWinner 
                                    ? "bg-green-500/15"
                                    : isSelected 
                                      ? "bg-blue-500/15"
                                      : "bg-white/10"
                                )}
                                style={{ 
                                  height: `${Math.max(percentage, 15)}%`
                                }}
                              >
                                {isWinner && (
                                  <div className="absolute top-2 left-1/2 -translate-x-1/2">
                                    <Trophy className="w-4 h-4 text-green-300" />
                                  </div>
                                )}
                              </div>
                            )}
                            </DoubleTapVoteAnimation>
                          </div>
                        );
                      })}
                    </div>
                  );
                }
              })()}
            </div>
          </div>
        ) : (
          /* Layout normal para polls regulares */
          <LayoutRenderer 
            poll={poll}
            onVote={(optionId) => handleVote(optionId)}
            isActive={isActive}
            currentSlide={currentSlide}
            onSlideChange={setCurrentSlide}
            handleTouchStart={handleTouchStart}
            handleTouchEnd={handleTouchEnd}
            onThumbnailChange={handleCarouselThumbnailChange}
            onAudioChange={handleCarouselAudioChange}
            index={index}
            showLogo={showLogo}
            // 🚀 PERFORMANCE: Layout-specific optimization props
            optimizeVideo={optimizeVideo}
            renderPriority={renderPriority}
            shouldPreload={shouldPreload}
            isVisible={isVisible}
            shouldUnload={shouldUnload}
            distanceFromActive={distanceFromActive}
            isHighBandwidth={isHighBandwidth}
            layout={layout}
            // 🧭 NAV: pass nav-bar state so layouts (e.g. VSLayout)
            // can position overlays based on visible nav bar.
            isBottomNavVisible={isBottomNavBarShown}
          />
        )}
      </div>

      {/* Bottom info and actions - Enhanced with safe area */}
      <div className="absolute bottom-0 left-0 right-0 z-30 bg-gradient-to-t from-black/90 via-black/70 to-transparent px-4 pt-8 pointer-events-none"
           style={{ 
             paddingBottom: isPostMiniature ? 'max(0.5rem, var(--safe-area-inset-bottom))' : (isBottomNavBarShown ? 'calc(56px + max(0.5rem, var(--safe-area-inset-bottom)))' : (isBottomNavVisible ? 'max(0.5rem, var(--safe-area-inset-bottom))' : 'max(1.5rem, var(--safe-area-inset-bottom))')),
             paddingLeft: 'max(1rem, var(--safe-area-inset-left))',
             paddingRight: 'max(1rem, var(--safe-area-inset-right))'
           }}>
        {/* Solo mostrar votos si show_vote_count es true (o por defecto si no existe) */}
        {/* Cuando isBottomNavVisible, el contador se mueve al lateral derecho */}
        {!isBottomNavVisible && (poll.show_vote_count !== false && poll.showVoteCount !== false) && (
          <div className="mb-4 pointer-events-auto">
            {poll.is_challenge ? (
              /* 🏆 CHALLENGE: Mostrar estado en vez de conteo de votos */
              <div className="inline-flex items-center px-4 py-2 rounded-full bg-black/20 backdrop-blur-sm">
                <span className="text-white/90 font-semibold text-sm">
                  {(() => {
                    const totalVotes = poll.options?.reduce((sum, opt) => sum + (opt.votes || 0), 0) || 0;
                    if (totalVotes === 0) return "Sé el primero en decidir";
                    
                    const sortedOptions = [...(poll.options || [])].sort((a, b) => (b.votes || 0) - (a.votes || 0));
                    const maxVotes = sortedOptions[0]?.votes || 0;
                    const secondVotes = sortedOptions[1]?.votes || 0;
                    
                    if (maxVotes === secondVotes) return "Empate";
                    
                    const winnerName = sortedOptions[0]?.participant_username || sortedOptions[0]?.text || "Participante";
                    return `${winnerName} va ganando`;
                  })()}
                </span>
              </div>
            ) : (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setShowVotersModal(true);
                }}
                className="inline-flex items-center px-4 py-2 rounded-full bg-black/20 backdrop-blur-sm text-white/90 font-semibold text-sm hover:text-white transition-colors cursor-pointer"
              >
                {formatNumber(getDisplayedTotalVotes(poll))} {t('vs.votos')}
              </button>
            )}
          </div>
        )}

        {!isBottomNavVisible && (() => {
          const hasHighInteractions = (poll.likes >= 1000 || poll.comments >= 1000 || poll.shares >= 1000 || (poll.saves_count || 0) >= 1000);
          return (
            <div className={`flex items-center -ml-2 pointer-events-auto flex-nowrap ${hasHighInteractions ? 'gap-0.5' : 'gap-3'}`}>
              {renderActionButtons(false)}
            </div>
          );
        })()}

      {/* Título de la música - Contenedor separado debajo de los botones (estilo Twyk) */}
      {poll.music && !isMenuOpen && !isBottomNavVisible && (() => {
        const hasExtractedAudio = poll.layout === 'off' && poll.options?.some(opt => opt.extracted_audio_id);
        const displayMusic = hasExtractedAudio && carouselAudioData
          ? { ...poll.music, title: carouselAudioData.title || poll.music?.title, artist: carouselAudioData.artist || poll.music?.artist, id: carouselAudioData.id || poll.music?.id }
          : poll.music;
        return (
        <div className="absolute left-0 right-0 z-40 px-4 pointer-events-none"
             style={{ 
               bottom: isBottomNavVisible ? 'calc(52px + max(0.15rem, var(--safe-area-inset-bottom)))' : 'max(0.15rem, var(--safe-area-inset-bottom))',
               paddingLeft: 'max(1rem, var(--safe-area-inset-left))',
               paddingRight: 'max(1rem, var(--safe-area-inset-right))'
             }}>
          <div 
            onClick={(e) => {
              e.stopPropagation();
              if (displayMusic?.id) {
                let audioId = displayMusic.id;
                if (displayMusic.isOriginal || displayMusic.source === 'User Upload') {
                  audioId = audioId.startsWith('user_audio_') ? audioId : `user_audio_${audioId}`;
                }
                navigate(`/audio/${audioId}`);
              }
            }}
            className="flex items-center gap-1.5 text-white cursor-pointer hover:text-gray-200 transition-colors duration-200 ml-1 w-fit pointer-events-auto" 
            style={{ textShadow: '0 1px 3px rgba(0,0,0,0.8)' }}
          >
            <Music className={`${isBottomNavVisible ? 'w-3 h-3' : 'w-3.5 h-3.5'} flex-shrink-0`} style={{ filter: 'drop-shadow(0 1px 2px rgba(0,0,0,0.8))' }} />
            {(() => {
              const fullTitle = `${displayMusic.title} - ${displayMusic.artist}`;
              const isLong = fullTitle.length > 25;
              return (
                <div className="marquee-wrapper">
                  <span 
                    className={`${isBottomNavVisible ? 'text-[10px]' : 'text-xs'} font-light${isLong ? ' animate-marquee' : ''}`}
                    style={{ textShadow: '0 1px 3px rgba(0,0,0,0.8)', whiteSpace: 'nowrap' }}
                  >
                    {fullTitle}
                  </span>
                </div>
              );
            })()}
          </div>
        </div>
        );
      })()}

      </div>{/* Cierre del overlay de botones inferior */}

      {/* 🎯 BOTONES SOCIALES ESTILO TIKTOK - Lateral derecho, centrados verticalmente, sin marco */}
      {/* Solo cuando la barra de navegación inferior está activa */}
      {isBottomNavVisible && !isPostMiniature && (
        <div
          className="absolute z-30 flex flex-col items-center gap-5 pointer-events-auto"
          style={{
            right: 'max(0.5rem, var(--safe-area-inset-right))',
            top: '50%',
            transform: 'translateY(-50%)'
          }}
        >
          {/* Contador de votos compacto al inicio (estilo TikTok) */}
          {(poll.show_vote_count !== false && poll.showVoteCount !== false) && (
            poll.is_challenge ? (
              <div className="flex flex-col items-center gap-0.5 text-white" style={{ filter: 'drop-shadow(0 2px 4px rgba(0,0,0,0.7))' }}>
                {(() => {
                  const totalVotes = poll.options?.reduce((sum, opt) => sum + (opt.votes || 0), 0) || 0;
                  const sortedOptions = [...(poll.options || [])].sort((a, b) => (b.votes || 0) - (a.votes || 0));
                  const maxVotes = sortedOptions[0]?.votes || 0;
                  const secondVotes = sortedOptions[1]?.votes || 0;
                  const isTie = totalVotes > 0 && maxVotes === secondVotes;
                  const showTrophy = totalVotes > 0 && !isTie;
                  return (
                    <>
                      {showTrophy ? (
                        <Trophy className="w-[40px] h-[40px] flex-shrink-0" strokeWidth={1.75} />
                      ) : (
                        <VoteIcon className="w-[40px] h-[40px] flex-shrink-0" strokeWidth={320} filled={false} />
                      )}
                      <span className="text-[11px] font-semibold whitespace-nowrap text-center px-1 max-w-[72px] truncate">
                        {(() => {
                          if (totalVotes === 0) return t('feed.actions.voteFirst');
                          if (isTie) return t('feed.actions.tie');
                          const winnerName = sortedOptions[0]?.participant_username || sortedOptions[0]?.text || t('feed.actions.participant');
                          return t('feed.actions.winner', { name: winnerName });
                        })()}
                      </span>
                    </>
                  );
                })()}
              </div>
            ) : (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setShowVotersModal(true);
                }}
                className="flex flex-col items-center gap-0.5 hover:scale-110 transition-all duration-200 cursor-pointer text-white"
                style={{ filter: 'drop-shadow(0 2px 4px rgba(0,0,0,0.7))' }}
              >
                {(() => {
                  const voteColor = getVoteButtonColor(poll);
                  const hasVoted = !!voteColor;
                  return (
                    <span style={{ color: voteColor || '#fff', display: 'inline-flex', transition: 'color 200ms' }}>
                      <VoteIcon className="w-[40px] h-[40px] flex-shrink-0" strokeWidth={320} filled={hasVoted} />
                    </span>
                  );
                })()}
                <span className={`text-[8px] font-medium whitespace-nowrap leading-none text-white`}>
                  {(() => {
                    const total = Number(getDisplayedTotalVotes(poll)) || 0;
                    return total === 0 ? t('feed.actions.vote') : formatNumber(total);
                  })()}
                </span>
              </button>
            )
          )}
          {renderActionButtons(true)}
        </div>
      )}

      {/* Reproductor de música (disco) - posicionado abajo derecha cuando sideMode (no se mueve al lateral con los sociales) */}
      {isBottomNavVisible && !isPostMiniature && poll.music && (() => {
        const hasExtractedAudio = poll.layout === 'off' && poll.options?.some(opt => opt.extracted_audio_id);
        const displayMusic = hasExtractedAudio && carouselAudioData
          ? { ...poll.music, title: carouselAudioData.title || poll.music?.title, artist: carouselAudioData.artist || poll.music?.artist, cover: carouselAudioData.cover || poll.music?.cover, preview_url: carouselAudioData.preview_url || poll.music?.preview_url, id: carouselAudioData.id || poll.music?.id }
          : poll.music;
        return (
          <div
            className="absolute z-30 pointer-events-auto"
            style={{
              right: 'max(0.5rem, var(--safe-area-inset-right))',
              bottom: isBottomNavBarShown ? 'calc(56px + max(0.75rem, var(--safe-area-inset-bottom)))' : 'max(0.75rem, var(--safe-area-inset-bottom))'
            }}
          >
            <MusicPlayer
              music={displayMusic}
              isVisible={isActive}
              onTogglePlay={handleMusicToggle}
              autoPlay={!hasExtractedAudio}
              loop={true}
              authorAvatar={carouselThumbnail || poll.author?.avatar_url}
              authorUsername={poll.author?.username || poll.author?.display_name}
              overrideAudioId={carouselAudioId}
              forceUseAvatar={!!carouselThumbnail}
              className="flex-shrink-0"
            />
          </div>
        );
      })()}

      {/* Scroll hints - Solo para usuarios nuevos que no han hecho scroll */}
      {showScrollHint && (
        <div className="absolute bottom-32 left-1/2 transform -translate-x-1/2 flex flex-col items-center gap-3 z-20"
             style={{ 
               bottom: 'max(8rem, calc(8rem + var(--safe-area-inset-bottom)))'
             }}>
          <div className="animate-bounce">
            <ChevronDown className="w-8 h-8 text-white/80" />
          </div>
          <div className="text-white/80 text-sm font-medium bg-black/60 px-4 py-2 rounded-full backdrop-blur-sm border border-white/20">
            {t('vs.deslizaParaVer')}
          </div>
        </div>
      )}

      </div>{/* Cierre del contenedor miniatura */}

      {/* Modal de comentarios — lazy-mount (F5) */}
      {hasOpenedCommentsRef.current && (
        <CommentsModal
          isOpen={showCommentsModal}
          onClose={() => { setShowCommentsModal(false); setIsCommentsExpanded(false); }}
          pollId={poll.id}
          pollTitle={poll.title}
          pollAuthor={poll.author?.display_name || poll.author?.username || 'Usuario'}
          commentsEnabled={poll.comments_enabled !== false && poll.commentsEnabled !== false}
          onExpandChange={setIsCommentsExpanded}
        />
      )}

      {/* Modal de detalle del post (al tocar título) — lazy-mount (F5) */}
      {hasOpenedPostDetailRef.current && (
        <PostDetailModal
          isOpen={showPostDetailModal}
          onClose={() => setShowPostDetailModal(false)}
          poll={poll}
          isFollowing={isFollowing(authorUserId) || (currentUser && authorUserId === currentUser.id)}
          onFollow={() => {
            const userToFollow = poll.authorUser || { 
              username: (poll.author?.username || poll.author?.display_name || 'unknown').toLowerCase().replace(/\s+/g, '_'),
              displayName: poll.author?.display_name || poll.author?.username || 'Usuario',
              id: authorUserId 
            };
            handleFollowUser(userToFollow);
          }}
          commentsEnabled={poll.comments_enabled !== false && poll.commentsEnabled !== false}
        />
      )}

      {/* Modal de votantes — lazy-mount (F5) */}
      {hasOpenedVotersRef.current && (
        <VotersModal
          isOpen={showVotersModal}
          onClose={() => { setShowVotersModal(false); setIsVotersExpanded(false); }}
          pollId={poll.id}
          poll={poll}
          onExpandChange={setIsVotersExpanded}
        />
      )}

      {/* Modal de participantes del challenge — lazy-mount (F5) */}
      {poll.is_challenge && hasOpenedChallengeParticipantsRef.current && (
        <ChallengeParticipantsModal
          isOpen={showChallengeParticipants}
          onClose={() => setShowChallengeParticipants(false)}
          participants={poll.participants || []}
          challengeTitle={poll.title}
        />
      )}

      {/* Modal de compartir — lazy-mount (F5) */}
      {(() => {
        if (shareModal.isOpen) hasOpenedShareRef.current = true;
        return hasOpenedShareRef.current ? (
          <ShareModal
            isOpen={shareModal.isOpen}
            onClose={closeShareModal}
            content={shareModal.content}
          />
        ) : null;
      })()}
      
      {/* Story Viewer - Portal to escape stacking context */}
      {showAuthorStoryViewer && authorStoriesData && createPortal(
        <StoriesViewer
          storiesGroups={[authorStoriesData]}
          onClose={handleStoryViewerClose}
          initialUserIndex={0}
        />,
        document.body
      )}
    </div>
  );
};

// 🚀 React.memo wrapping — el shallow compare es exactamente lo que
// necesitamos: props del card son estables durante el swipe; solo cambian
// al COMMITear (cambio de slot). Esto reduce los renders durante el drag
// de ~60/s a 0.
const TikTokPollCard = React.memo(TikTokPollCardInner);
TikTokPollCard.displayName = 'TikTokPollCard';

// — TikTokPollCard y helpers internos se mantienen igual que en tu versión original —
// (No se modifican: renderActionButtons, UserButton, renderTextWithHashtags, etc.)
// Solo se exporta TikTokScrollView con los cambios de fluidez.

// ─── DEV LOG ────────────────────────────────────────────────────────────────
const DEV = process.env.NODE_ENV === 'development';
const log = DEV ? console.log.bind(console) : () => {};

// ─── TikTokScrollView ────────────────────────────────────────────────────────
const TikTokScrollView = ({
  polls,
  onVote, onLike, onShare, onComment, onSave, onExitTikTok, onCreatePoll,
  onLoadMore, isLoadingMore = false, isInitialLoading = false,
  hasMoreContent = true, showLogo = true, showCloseButton = false,
  closeOnBack = false, showActiveChallengesButton = false,
  initialIndex = 0, fromAudioDetailPage = false, currentAudio = null,
  onUseSound = null, onUpdatePoll = null, onDeletePoll = null,
  isOwnProfile = false, onIndexChange = null, onActiveIndexChange = null,
  emptyMessage = 'No hay publicaciones disponibles',
  emptySubMessage = 'Vuelve más tarde para ver nuevo contenido',
  onSwipeStart = null, storiesOverlayOpen = false, onRefresh = null,
}) => {
  const containerRef = useRef(null);
  const [activeIndex, setActiveIndex] = useState(initialIndex);
  const [lastActiveIndex, setLastActiveIndex] = useState(initialIndex);
  const [savedPolls, setSavedPolls] = useState(new Set());
  const [commentedPolls, setCommentedPolls] = useState(new Set());
  const [sharedPolls, setSharedPolls] = useState(new Set());
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [showScrollHint, setShowScrollHint] = useState(() => !localStorage.getItem('hasScrolledFeed'));
  const [isTransitioning, setIsTransitioning] = useState(false);
  const [translateOffset, setTranslateOffset] = useState(0);

  // ── Velocity-aware swipe ──────────────────────────────────────────────────
  // Duración de la transición adaptativa según velocidad del swipe.
  // Swipe rápido → 180ms, swipe lento → 280ms. Igual que TikTok.
  const [transitionDuration, setTransitionDuration] = useState(250);
  const swipeStartTimeRef = useRef(null);

  // Pull-to-refresh
  const [pullDistance, setPullDistance] = useState(0);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const pullStartYRef = useRef(null);
  const isPullingRef = useRef(false);
  const PTR_THRESHOLD = 80;
  const PTR_MAX = 140;

  const { user: currentUser } = useAuth();
  const { isMetered } = useNetworkStatus();
  const isHighBandwidth = !isMetered;
  const navigate = useNavigate();
  const { t } = useTranslation();

  // ─── VIRTUAL 3-SLOT POOL ──────────────────────────────────────────────────
  const SLOT_PREV = 0;
  const SLOT_CUR  = 1;
  const SLOT_NEXT = 2;

  const slotPolls = useMemo(() => {
    if (!polls || polls.length === 0) return [null, null, null];
    const prev = activeIndex > 0 ? polls[activeIndex - 1] : null;
    const cur  = polls[activeIndex] || null;
    const next = activeIndex < polls.length - 1 ? polls[activeIndex + 1] : null;
    return [prev, cur, next];
  }, [polls, activeIndex]);

  const tapeRef = useRef(null);
  const touchStartYRef = useRef(null);
  const touchCurrentYRef = useRef(null);
  const isAnimatingRef = useRef(false);

  // ─── PRE-DECODE next video(s) ─────────────────────────────────────────────
  // 🚀 FIX GAP #1 (VS Illusion of Instant): cuando el +1 es un VS, el slide
  // visible inicialmente contiene 2 videos (lado A y lado B). Pre-decodificar
  // solo el primero deja al segundo lado en "loading" durante el swipe →
  // pantalla negra o poster mientras uno ya está vivo. Ahora devolvemos
  // hasta N URLs y creamos un <video> oculto por cada una, en paralelo.
  //
  // Reglas:
  //   - Post normal (no VS): 1 sola URL (la primera de options).
  //   - VS clásico (1 pregunta): hasta 2 URLs (opciones 0 y 1 de la 1ª
  //     pregunta). Son las que el usuario verá nada más soltar el dedo.
  //   - VS multi-pregunta: solo las 2 de la 1ª pregunta (las demás se
  //     prefetchan vía eagerPrefetchNextPost / feedMediaPrefetcher; aquí
  //     priorizamos lo que se ve en t=0).
  //
  // Aún limitamos a 2 para no saturar decoder pool en gama media (Android
  // suele permitir 2–4 decoders H.264 hw simultáneos en background).
  const getNextVideoUrls = useCallback(() => {
    if (!polls || activeIndex >= polls.length - 1) return [];
    const nextPoll = polls[activeIndex + 1];
    if (!nextPoll) return [];

    const out = [];
    const pushIfVideo = (opt) => {
      if (!opt) return;
      const url = pickPlayableVideoUrl(opt);
      if (url && !out.includes(url)) out.push(url);
    };

    // 1) VS: la 1ª pregunta visible (vs_questions[0] o options) — ambos lados.
    if (nextPoll.layout === 'vs') {
      const firstQ = Array.isArray(nextPoll.vs_questions) && nextPoll.vs_questions.length > 0
        ? nextPoll.vs_questions[0]
        : { options: nextPoll.options || [] };
      const qOpts = Array.isArray(firstQ?.options) ? firstQ.options : [];
      for (const opt of qOpts) {
        pushIfVideo(opt);
        if (out.length >= 2) break;
      }
      // Si la 1ª pregunta es imagen, intentar con la primera con video
      // (para que al menos haya 1 pre-decodificado).
      if (out.length === 0 && Array.isArray(nextPoll.vs_questions)) {
        outer: for (const q of nextPoll.vs_questions) {
          const opts = Array.isArray(q?.options) ? q.options : [];
          for (const opt of opts) {
            pushIfVideo(opt);
            if (out.length >= 1) break outer;
          }
        }
      }
      return out;
    }

    // 2) Post normal: la primera URL de video (igual que antes).
    const baseOptions = Array.isArray(nextPoll.options) ? nextPoll.options : [];
    for (const opt of baseOptions) {
      pushIfVideo(opt);
      if (out.length >= 1) break;
    }
    return out;
  }, [polls, activeIndex]);

  // Map url → HTMLVideoElement para poder limpiarlos en el cleanup.
  const preDecodeVideosRef = useRef(new Map());
  useEffect(() => {
    if (!isHighBandwidth) return;
    const urls = getNextVideoUrls();
    if (!urls || urls.length === 0) return;

    const existing = preDecodeVideosRef.current;
    const wanted = new Set(urls);
    // 1) Eliminar los que ya no queremos (sobraron de un activeIndex previo)
    for (const [oldUrl, oldVideo] of existing.entries()) {
      if (!wanted.has(oldUrl)) {
        try { oldVideo.pause(); oldVideo.src = ''; oldVideo.load(); } catch (_) {}
        try { document.body.removeChild(oldVideo); } catch (_) {}
        existing.delete(oldUrl);
      }
    }

    const cancelers = [];
    // 2) Crear los nuevos
    for (const url of urls) {
      if (existing.has(url)) continue;
      const video = document.createElement('video');
      video.src = url;
      video.muted = true;
      video.playsInline = true;
      video.preload = 'auto';
      video.style.cssText = 'position:fixed;top:-9999px;left:-9999px;width:1px;height:1px;pointer-events:none;';
      document.body.appendChild(video);
      existing.set(url, video);
      // Forzar decode del primer keyframe en GPU
      video.play().then(() => { video.pause(); }).catch(() => {});

      // Range 256KB para arrancar el buffer (TikTok-style, prioridad HIGH).
      try {
        let cancelFn = null;
        import('../services/priorityFetchQueue').then(({ default: queue }) => {
          const { cancel } = queue.enqueue(url, {
            priority: 'high',
            init: {
              headers: { Range: 'bytes=0-262143' },
              cache: 'force-cache',
            },
          });
          cancelFn = cancel;
        }).catch(() => {});
        cancelers.push(() => { try { cancelFn?.(); } catch (_) {} });
      } catch (_) {}
    }

    return () => {
      // Cancelar Ranges en cola
      for (const c of cancelers) { try { c(); } catch (_) {} }
    };
  }, [getNextVideoUrls, isHighBandwidth]);

  // Cleanup global al desmontar TikTokScrollView (cambio de feed, etc.)
  useEffect(() => {
    return () => {
      const existing = preDecodeVideosRef.current;
      for (const [, v] of existing.entries()) {
        try { v.pause(); v.src = ''; v.load(); } catch (_) {}
        try { document.body.removeChild(v); } catch (_) {}
      }
      existing.clear();
    };
  }, []);

  // ─── 🚀 PRE-DECODE POSTERS del +1 con img.decode() ────────────────────────
  // El poster JPEG se descarga sí — pero NO se decodifica en memoria hasta
  // que se renderiza. Decodificar un JPEG 1080×1920 en CPU cuesta ~30-80ms
  // en gama media → si el swipe ocurre antes, el browser tiene que decode
  // sync mientras renderiza → frame drop visible.
  //
  // img.decode() es una Promise API que decode en background y resuelve
  // cuando la imagen está lista para paint. Lo hacemos para los posters
  // del +1 (siguiente post) — cuando el slot pase a CUR, el poster ya
  // está pre-decodificado y aparece instantáneo.
  //
  // Para layouts VS hay 2 posters (lados A y B), así que decode ambos.
  const decodedPostersRef = useRef(new Set());
  useEffect(() => {
    if (!polls || activeIndex >= polls.length - 1) return;
    const nextPoll = polls[activeIndex + 1];
    if (!nextPoll) return;

    const collectPosterUrls = (poll) => {
      const urls = [];
      const addFromOption = (opt) => {
        if (!opt) return;
        const url = pickVideoPosterUrl(opt) || pickImageUrl(opt);
        if (url && !urls.includes(url)) urls.push(url);
      };
      // Para VS: posters de la 1ª pregunta (los 2 lados visibles al swipe)
      if (poll.layout === 'vs') {
        const firstQ = Array.isArray(poll.vs_questions) && poll.vs_questions.length > 0
          ? poll.vs_questions[0]
          : { options: poll.options || [] };
        const qOpts = Array.isArray(firstQ?.options) ? firstQ.options : [];
        qOpts.forEach(addFromOption);
      } else {
        // Post normal: solo la 1ª opción (la primera que se ve)
        if (Array.isArray(poll.options) && poll.options[0]) {
          addFromOption(poll.options[0]);
        }
      }
      return urls;
    };

    const posterUrls = collectPosterUrls(nextPoll);
    if (posterUrls.length === 0) return;

    // Decode en background. Ignoramos errores (la imagen puede fallar de
    // descargar — no es crítico, el browser decodifica on-demand al render).
    posterUrls.forEach((url) => {
      if (decodedPostersRef.current.has(url)) return;
      const img = new Image();
      img.src = url;
      // decode() devuelve Promise — el browser hace decode en thread aparte
      // (si lo soporta) o en main thread pero NO bloquea render.
      if (typeof img.decode === 'function') {
        img.decode().then(() => {
          decodedPostersRef.current.add(url);
        }).catch(() => {
          // Decode falló (img no cargó, CORS, etc.) — no es crítico
        });
      } else {
        // Fallback: simplemente cargar via Image constructor (warm HTTP cache)
        img.onload = () => decodedPostersRef.current.add(url);
      }
    });

    // Limpiamos el Set cuando crece demasiado (>50 entries) para no
    // mantener referencias innecesarias. No removemos imágenes individuales
    // — están en HTTP cache del browser, no consumen memoria de imágenes
    // decodificadas (esas se gestionan automáticamente por el browser).
    if (decodedPostersRef.current.size > 50) {
      decodedPostersRef.current.clear();
    }
  }, [polls, activeIndex]);

  // ─── PRE-CONNECT next audio ───────────────────────────────────────────────
  useEffect(() => {
    if (!polls || activeIndex >= polls.length - 1) return;
    const nextPoll = polls[activeIndex + 1];
    if (!nextPoll?.music?.preview_url) return;
    if (!isHighBandwidth) return;
    if (typeof audioManager.preconnect === 'function') {
      audioManager.preconnect(nextPoll.music.preview_url);
    }
  }, [polls, activeIndex, isHighBandwidth]);

  // ─── CENTRALIZED AUDIO MANAGER ───────────────────────────────────────────
  useEffect(() => {
    if (!polls || polls.length === 0) return;
    const activePoll = polls[activeIndex];
    if (!activePoll) return;

    let cancelled = false;
    // Reducido a 16ms (1 frame) en lugar de 60ms para respuesta inmediata
    const id = setTimeout(async () => {
      if (cancelled) return;
      try {
        const hasExtractedAudio = activePoll.layout === 'off' && activePoll.options?.some(opt => opt.extracted_audio_id);
        const hasMusic = activePoll.music?.preview_url && !hasExtractedAudio;
        if (hasMusic) {
          if (!audioManager.isPlayingPost(activePoll.id)) {
            await audioManager.stop();
            if (cancelled) return;
            await audioManager.play(activePoll.music.preview_url, { startTime: 0, loop: true, volume: 0.7, postId: activePoll.id });
          }
        } else {
          if (audioManager.isPlaying) await audioManager.stop();
        }
      } catch (err) {
        log('Audio sync error:', err);
      }
    }, 16); // 1 frame en lugar de 60ms

    return () => { cancelled = true; clearTimeout(id); };
  }, [activeIndex, polls]);

  // ─── BACKGROUND PAUSE — videos ───────────────────────────────────────────
  // AudioManager ya maneja el audio en background.
  // Aquí pausamos los videos del slot activo cuando la app se minimiza.
  useEffect(() => {
    const handleVisibility = () => {
      if (document.hidden) {
        // Pausar todos los videos activos
        document.querySelectorAll('video').forEach(v => {
          if (!v.paused) v.pause();
        });
      } else {
        // Reanudar solo el video del slot activo
        document.querySelectorAll('[data-slot-active="true"] video').forEach(v => {
          if (v.paused) v.play().catch(() => {});
        });
      }
    };
    document.addEventListener('visibilitychange', handleVisibility);
    return () => document.removeEventListener('visibilitychange', handleVisibility);
  }, []);

  // ─── VELOCITY-AWARE SWIPE (TikTok-style) ──────────────────────────────────
  // Duración de la transición según velocidad del dedo (px/ms) y la distancia
  // restante hasta el snap. Más rápido el dedo o menos distancia → menos ms.
  // Curva easeOutQuint para que arranque rápido y termine con suavidad real,
  // dando la sensación "ligera" característica de TikTok.
  const TRANSITION_MS_FAST = 160;   // flick rápido
  const TRANSITION_MS_SLOW = 220;   // swipe deliberado
  const TRANSITION_MS_MIN  = 140;   // techo inferior

  const goToIndex = useCallback((newIndex, opts = {}) => {
    if (isAnimatingRef.current) return;
    if (newIndex < 0 || newIndex >= polls.length) return;
    if (newIndex === activeIndex) return;

    const { velocity = 0, remainingDvh = 100 } = opts;

    isAnimatingRef.current = true;
    if (onSwipeStart) onSwipeStart();

    const direction = newIndex > activeIndex ? 'next' : 'prev';
    const targetOffset = direction === 'next' ? -100 : 100;

    if (showScrollHint && newIndex > 0) {
      setShowScrollHint(false);
      localStorage.setItem('hasScrolledFeed', 'true');
    }

    // Carga silenciosa — sin spinner, igual que TikTok
    if (onLoadMore && hasMoreContent && !isLoadingMore) {
      const remaining = polls.length - newIndex;
      if (remaining <= 8) onLoadMore();
    }

    // Duración: si hay velocidad, calcula tiempo real para recorrer lo que queda
    // (en dvh → px). Limitada entre MIN y SLOW. Si no hay velocidad, usa SLOW.
    let transMs = TRANSITION_MS_SLOW;
    if (velocity > 0) {
      const vh = window.innerHeight || 1;
      const remainingPx = (Math.abs(remainingDvh) / 100) * vh;
      // velocity en px/ms → tiempo natural = px / velocity, suavizado un 25%
      const natural = (remainingPx / velocity) * 1.25;
      transMs = Math.max(TRANSITION_MS_MIN, Math.min(TRANSITION_MS_SLOW, natural));
      // Flick muy rápido → asegura sensación crujiente
      if (velocity > 1.2) transMs = Math.min(transMs, TRANSITION_MS_FAST);
    }

    setTransitionDuration(transMs);
    setIsTransitioning(true);
    setTranslateOffset(targetOffset);

    setActiveIndex(newIndex);
    setLastActiveIndex(newIndex);
    eagerPrefetchedIndexRef.current = -1;

    if (typeof onActiveIndexChange === 'function') {
      try { onActiveIndexChange(newIndex); } catch (_) {}
    }

    setTimeout(() => {
      setIsTransitioning(false);
      setTranslateOffset(0);
      isAnimatingRef.current = false;
    }, transMs);
  }, [activeIndex, polls.length, onSwipeStart, onActiveIndexChange, showScrollHint, onLoadMore, hasMoreContent, isLoadingMore]);

  // Listener "Siguiente duelo"
  useEffect(() => {
    const handleNextPost = (e) => {
      const sourcePollId = e?.detail?.pollId;
      if (sourcePollId && polls[activeIndex]?.id !== sourcePollId) return;
      if (activeIndex < polls.length - 1) goToIndex(activeIndex + 1);
    };
    window.addEventListener('vs:nextPost', handleNextPost);
    return () => window.removeEventListener('vs:nextPost', handleNextPost);
  }, [activeIndex, polls, goToIndex]);

  // ─── GESTOS TÁCTILES (TikTok-style) ─────────────────────────────────────
  // Umbral más bajo + commit por velocidad ("flick"): si el dedo se mueve
  // rápido al soltar, cambiamos de post aunque hayas arrastrado poco.
  const SWIPE_FRACTION_THRESHOLD = 0.12;   // 12% (antes 20%)
  const EDGE_RESISTANCE = 0.20;
  const VELOCITY_COMMIT_PX_MS = 0.45;      // flick → cambia post aunque haya poca distancia
  // Buffer corto de muestras (últimos ~80ms) para calcular velocidad instantánea
  const velocitySamplesRef = useRef([]);

  // ─── EAGER PREFETCH (TikTok-style: kick off al tocar, no al soltar) ─────
  // TikTok eleva la prioridad del +1 en onSwipeStart (dedo apenas tocó).
  // Antes, lo hacíamos en el useEffect [activeIndex] que sólo corre DESPUÉS
  // del setTimeout(transMs) en goToIndex → llegaba 150–300ms tarde.
  // Ahora disparamos prefetch del +1 (vídeo + poster + audio) en cuanto el
  // dedo toca la pantalla. eagerPrefetchedIndexRef evita re-disparos
  // redundantes durante el mismo touchmove.
  const eagerPrefetchedIndexRef = useRef(-1);

  const eagerPrefetchNextPost = useCallback((targetIndex) => {
    if (typeof targetIndex !== 'number' || targetIndex < 0) return;
    if (!polls || targetIndex >= polls.length) return;
    if (eagerPrefetchedIndexRef.current === targetIndex) return;
    eagerPrefetchedIndexRef.current = targetIndex;

    // Native (Capacitor): disk-persistent cache del +1 (vídeo + audio)
    try {
      import('../services/feedMediaPrefetcher').then(({ default: prefetcher }) => {
        prefetcher.prefetchVideosAroundIndex(polls, targetIndex, 0);
        prefetcher.prefetchAudiosAroundIndex(polls, targetIndex, 0);
      }).catch(() => {});
    } catch (_) {}

    // Web + native: cache HTTP/LRU del poster + 1er vídeo del +1
    try {
      Promise.all([
        import('../services/mediaCacheService'),
        import('../utils/mediaUrl'),
      ]).then(([cacheMod, mediaMod]) => {
        const cache = cacheMod.default;
        const { pickPlayableVideoUrl, pickVideoPosterUrl, pickImageUrl } = mediaMod;
        const conn = navigator?.connection;
        const isCellular = conn?.type === 'cellular' || ['2g', '3g'].includes(conn?.effectiveType || '');
        const videoMaxBytes = isCellular ? 0 : 10 * 1024 * 1024;
        const imageMaxBytes = 2 * 1024 * 1024;
        const p = polls[targetIndex];
        if (!p) return;

        // 🚀 FIX GAP #6 (VS multi-pregunta en touch prefetch): igual que
        // MediaPrefetcher / feedMediaPrefetcher, hay que iterar también
        // poll.vs_questions[].options. De lo contrario, las preguntas
        // 2..N del +1 VS no se cachean en touchstart y se siente lento
        // cuando el usuario hace swipe horizontal dentro del nuevo post.
        const collectAllOpts = (poll) => {
          const all = [];
          if (Array.isArray(poll?.options)) {
            for (const o of poll.options) if (o) all.push(o);
          }
          if (Array.isArray(poll?.vs_questions)) {
            for (const q of poll.vs_questions) {
              const qOpts = Array.isArray(q?.options) ? q.options : [];
              for (const o of qOpts) if (o) all.push(o);
            }
          }
          return all;
        };

        const allOpts = collectAllOpts(p);
        if (allOpts.length === 0) return;
        let videoPrefetched = 0;
        const seenVideoUrls = new Set();
        for (const option of allOpts) {
          const thumbUrl = pickVideoPosterUrl(option) || pickImageUrl(option);
          if (thumbUrl) cache.prefetch(thumbUrl, { maxBytes: imageMaxBytes });
          if (videoMaxBytes > 0 && option.media_type === 'video') {
            const videoUrl = pickPlayableVideoUrl(option);
            if (videoUrl && !seenVideoUrls.has(videoUrl)) {
              seenVideoUrls.add(videoUrl);
              cache.prefetch(videoUrl, { maxBytes: videoMaxBytes });
              videoPrefetched += 1;
              // Para VS, prefetchamos hasta 2 vídeos (los 2 lados de la
              // 1ª pregunta — lo que ve el usuario al instante).
              // Para post normal, basta 1 (el primero).
              const maxVideos = p.layout === 'vs' ? 2 : 1;
              if (videoPrefetched >= maxVideos) break;
            }
          }
        }
      }).catch(() => {});
    } catch (_) {}
  }, [polls]);

  // Reset al cambiar de post activo → permite eager-prefetchar el nuevo +1
  useEffect(() => {
    eagerPrefetchedIndexRef.current = -1;
    // Si llegamos aquí por wheel/teclado/programático y había un video
    // pausado por HLS stopLoad, asumimos que el cambio de activeIndex ya
    // lo ha vuelto irrelevante (el video pasó a PREV). No queremos
    // reanudarlo después porque ya no se ve. Limpiamos la ref.
    pausedHlsElRef.current = null;
  }, [activeIndex]);

  // ── PAUSA DE SEGMENTOS HLS NO-CRÍTICOS DEL VIDEO ACTIVO ───────────────────
  //
  // Del spec TikTok (punto #7):
  //   onSwipeStart → "Cancela descarga de segmentos no críticos del video
  //                  actual"
  //
  // Cuando el usuario apenas toca la pantalla para hacer swipe, el video
  // activo SIGUE descargando segmentos del MP4/HLS que ya no va a necesitar
  // (porque está a punto de pasar al siguiente). Esos bytes compiten por
  // bandwidth con el video +1 que SÍ va a importar en ~150ms.
  //
  // Solución:
  //   - Si el video activo usa hls.js → `hls.stopLoad()` detiene la
  //     descarga de fragmentos manteniendo el buffer ya descargado intacto.
  //     El usuario puede seguir viendo lo que tiene; solo paramos lo nuevo.
  //   - Si usa HLS nativo (Safari/iOS) → no podemos abortar (lo controla
  //     internamente Webkit), pero el `video.pause()` natural al cambiar de
  //     post desencadenará la pausa del loader nativo.
  //   - Si usa MP4 plano → no hacemos nada (el browser ya gestiona prioridad
  //     según `<video>` visible vs no visible).
  //
  // Guardamos referencia al elemento que pausamos para poder reanudarlo
  // (`startLoad`) si el swipe se cancela y el usuario vuelve al mismo video.
  const pausedHlsElRef = useRef(null);

  const pauseActiveVideoHlsLoading = useCallback(() => {
    try {
      const activeVideo = document.querySelector('[data-slot-active="true"] video');
      if (!activeVideo) return;
      const hls = activeVideo._hlsInstance;
      if (hls && typeof hls.stopLoad === 'function') {
        hls.stopLoad();
        pausedHlsElRef.current = activeVideo;
        if (process.env.NODE_ENV !== 'production') {
          // eslint-disable-next-line no-console
          console.debug('[TikTokScroll] HLS stopLoad (swipe start) on active video');
        }
      }
    } catch (_) { /* noop */ }
  }, []);

  const resumeActiveVideoHlsLoading = useCallback(() => {
    try {
      const el = pausedHlsElRef.current;
      pausedHlsElRef.current = null;
      if (!el) return;
      // Solo reanudamos si sigue siendo el video activo (si el swipe
      // completó y cambió el activeIndex, el viejo activo ya no nos
      // importa — debe quedarse parado para no seguir descargando bytes
      // del post que el usuario abandonó).
      if (el.getAttribute && el.closest?.('[data-slot-active="true"]')) {
        const hls = el._hlsInstance;
        if (hls && typeof hls.startLoad === 'function') {
          hls.startLoad();
          if (process.env.NODE_ENV !== 'production') {
            // eslint-disable-next-line no-console
            console.debug('[TikTokScroll] HLS startLoad (swipe cancelled)');
          }
        }
      }
    } catch (_) { /* noop */ }
  }, []);

  const handleTapePointerDown = useCallback((e) => {
    if (isModalOpen || storiesOverlayOpen) return;
    if (isAnimatingRef.current) return;
    touchStartYRef.current = e.touches ? e.touches[0].clientY : e.clientY;
    touchCurrentYRef.current = touchStartYRef.current;
    // Guardar timestamp para calcular velocidad del swipe
    swipeStartTimeRef.current = Date.now();
    velocitySamplesRef.current = [{ y: touchStartYRef.current, t: swipeStartTimeRef.current }];

    // 🚀 BYPASS REACT: marcamos drag activo y desactivamos transición en el
    // tape vía DOM directo. Cualquier re-render accidental durante el drag
    // verá `dragActiveRef=true` y mantendrá el transform actual.
    dragActiveRef.current = true;
    currentDragOffsetRef.current = 0;
    currentPullRef.current = 0;
    if (tapeRef.current) {
      tapeRef.current.style.transition = 'none';
    }

    // 🚀 TikTok-style: prefetch del +1 al instante (90%+ de swipes van forward).
    // Esto ahorra ~150–300ms vs esperar a que termine la animación de swipe.
    if (activeIndex + 1 < polls.length) {
      eagerPrefetchNextPost(activeIndex + 1);
    }

    // 🛑 Spec TikTok punto #7: paramos la descarga de segmentos no-críticos
    //    del video ACTUAL para que el bandwidth pase al +1 que el usuario
    //    está a punto de ver. El buffer ya descargado se mantiene → si el
    //    swipe se cancela, reanudamos con `resumeActiveVideoHlsLoading()`.
    pauseActiveVideoHlsLoading();

    if (onRefresh && !isRefreshing && activeIndex === 0) {
      pullStartYRef.current = touchStartYRef.current;
      isPullingRef.current = false;
    }
  }, [isModalOpen, storiesOverlayOpen, onRefresh, isRefreshing, activeIndex, polls.length, eagerPrefetchNextPost, pauseActiveVideoHlsLoading]);

  // 🚀 BYPASS REACT durante el drag — actualiza el transform directamente
  // via ref. Evita re-renders del componente padre + cascade a los 3 cards
  // en cada touchmove (60+/s = 60+ renders/s perdidos a tiempo de paint).
  // Esto es exactamente lo que hace TikTok: durante el drag, ningún
  // componente React se renderiza; solo se mueve el contenedor con CSS
  // transform desde un listener nativo.
  //
  // `currentDragOffsetRef` guarda el offset actual del drag para que el
  // touchend pueda leerlo sin depender de state asíncrono.
  const currentDragOffsetRef = useRef(0);
  const currentPullRef = useRef(0);
  const dragActiveRef = useRef(false);

  const applyTapeTransform = useCallback((offsetDvh, pullPx = 0) => {
    const el = tapeRef.current;
    if (!el) return;
    // translate3d para forzar layer GPU. Sin transition (drag activo).
    el.style.transition = 'none';
    el.style.transform = `translate3d(0, calc(-100dvh + ${offsetDvh}dvh + ${pullPx}px), 0)`;
  }, []);

  const handleTapePointerMove = useCallback((e) => {
    if (touchStartYRef.current === null) return;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;
    touchCurrentYRef.current = clientY;

    // Muestrear para velocidad instantánea (mantener solo últimos 80ms)
    const now = Date.now();
    const samples = velocitySamplesRef.current;
    samples.push({ y: clientY, t: now });
    while (samples.length > 1 && now - samples[0].t > 80) samples.shift();

    const deltaPx = clientY - touchStartYRef.current;

    if (onRefresh && !isRefreshing && activeIndex === 0 && deltaPx > 0) {
      isPullingRef.current = true;
      const pullPx = Math.min(deltaPx * 0.5, PTR_MAX);
      currentPullRef.current = pullPx;
      // Actualizar el indicador de pull-to-refresh (mantiene React aquí
      // porque cambia el spinner visualmente; pero solo cuando hay pull).
      setPullDistance(pullPx);
      // El tape también se desplaza con el pull
      applyTapeTransform(0, pullPx);
      return;
    }

    const vh = window.innerHeight || 1;
    let offsetDvh = (deltaPx / vh) * 100;

    const atStart = activeIndex === 0;
    const atEnd = activeIndex >= polls.length - 1;
    if ((atStart && deltaPx > 0) || (atEnd && deltaPx < 0)) {
      offsetDvh *= EDGE_RESISTANCE;
    }

    // 🚀 BYPASS REACT: actualizar el DOM directamente, NO setState aquí.
    // React no se entera del drag → 0 re-renders durante el swipe.
    currentDragOffsetRef.current = offsetDvh;
    dragActiveRef.current = true;
    applyTapeTransform(offsetDvh, 0);
  }, [onRefresh, isRefreshing, activeIndex, polls.length, applyTapeTransform]);

  const handleTapePointerUp = useCallback(async (e) => {
    if (touchStartYRef.current === null) return;

    const startY = touchStartYRef.current;
    const endY = touchCurrentYRef.current ?? startY;
    const deltaPx = endY - startY;

    // Velocidad instantánea de los últimos ~80ms (px/ms). Detección de "flick".
    const samples = velocitySamplesRef.current;
    let instantVelocity = 0; // siempre positiva (módulo)
    let signedVelocity = 0;
    if (samples.length >= 2) {
      const first = samples[0];
      const last = samples[samples.length - 1];
      const dt = Math.max(1, last.t - first.t);
      signedVelocity = (last.y - first.y) / dt;
      instantVelocity = Math.abs(signedVelocity);
    }

    touchStartYRef.current = null;
    touchCurrentYRef.current = null;
    swipeStartTimeRef.current = null;
    velocitySamplesRef.current = [];

    // 🚀 BYPASS REACT: liberar el flag ANTES del setState para que el
    // próximo re-render use los valores de state (no del ref de drag).
    // El DOM sigue con el transform bypassed; React lo sobreescribirá con
    // el target (con transition activa) → el browser anima del actual al
    // target. Esa es la "snap-to" animation.
    dragActiveRef.current = false;

    if (isPullingRef.current) {
      isPullingRef.current = false;
      pullStartYRef.current = null;
      if (pullDistance >= PTR_THRESHOLD && !isRefreshing) {
        setIsRefreshing(true);
        setPullDistance(60);
        try { await Promise.resolve(onRefresh()); }
        catch (err) { log('[PTR] onRefresh failed:', err); }
        finally { setIsRefreshing(false); setPullDistance(0); }
      } else {
        setPullDistance(0);
      }
      return;
    }

    const vh = window.innerHeight || 1;
    const draggedFraction = Math.abs(deltaPx) / vh;
    const wantsNext = deltaPx < 0 || (Math.abs(deltaPx) < 4 && signedVelocity < 0);
    const wantsPrev = deltaPx > 0 || (Math.abs(deltaPx) < 4 && signedVelocity > 0);

    const atStart = activeIndex === 0;
    const atEnd = activeIndex >= polls.length - 1;
    const cannotMove = (atStart && wantsPrev) || (atEnd && wantsNext);

    // Commit por distancia O por velocidad (flick)
    const isFlick = instantVelocity >= VELOCITY_COMMIT_PX_MS;
    const shouldCommit = (draggedFraction >= SWIPE_FRACTION_THRESHOLD || isFlick) && !cannotMove;

    // 🚀 FIX BOTTLENECK #3 — Fast-scroll detection
    // ───────────────────────────────────────────
    // Si el commit es un FLICK (velocidad alta) o si el usuario ya estaba en
    // fast-scrolling (cascada de swipes), marcamos el flag global. Los slots
    // PREV/NEXT van a SUSPENDER la carga de HLS (manifest + primer segmento)
    // mientras el flag esté activo. Esto evita malgastar bandwidth en
    // contenido que el usuario va a saltar.
    // El tracker tiene auto-release a los 250ms de inactividad → cuando el
    // scroll se estabiliza, los slots vuelven a cargar HLS automáticamente.
    if (shouldCommit && isFlick) {
      setFastScrolling(true);
    } else if (shouldCommit) {
      // Commit lento (drag completo, sin flick) → es scroll deliberado.
      // No marcamos fast; los slots cargan HLS normalmente.
      setFastScrolling(false);
    }

    if (shouldCommit) {
      // remainingDvh: cuánto falta visualmente para llegar al snap
      const currentOffsetDvh = (Math.abs(deltaPx) / vh) * 100;
      const remainingDvh = Math.max(8, 100 - currentOffsetDvh);
      const direction = wantsNext ? +1 : -1;
      // Swipe completa: el viejo activo pasa a PREV — debe quedarse parado
      // (no reanudamos su HLS). Solo limpiamos la ref para no reanudarlo
      // accidentalmente en un cambio futuro de activeIndex.
      pausedHlsElRef.current = null;
      goToIndex(activeIndex + direction, { velocity: instantVelocity, remainingDvh });
    } else {
      // Spring back: el usuario soltó sin completar el swipe → vuelve al
      // mismo video. Reanudamos la descarga de segmentos HLS que paramos
      // en `handleTapePointerDown`.
      resumeActiveVideoHlsLoading();

      // Spring back: duración basada en cuánto hay que volver
      const currentOffsetDvh = (Math.abs(deltaPx) / vh) * 100;
      const remainingPx = (currentOffsetDvh / 100) * vh;
      let transMs = TRANSITION_MS_SLOW;
      if (instantVelocity > 0) {
        const natural = (remainingPx / instantVelocity) * 1.1;
        transMs = Math.max(TRANSITION_MS_MIN, Math.min(TRANSITION_MS_SLOW, natural));
      } else {
        transMs = Math.max(TRANSITION_MS_MIN, Math.min(TRANSITION_MS_SLOW, currentOffsetDvh * 2));
      }
      setTransitionDuration(transMs);
      setIsTransitioning(true);
      setTranslateOffset(0);
      setTimeout(() => setIsTransitioning(false), transMs);
    }
  }, [pullDistance, isRefreshing, onRefresh, goToIndex, activeIndex, polls.length, resumeActiveVideoHlsLoading]);

  // Teclado
  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.key === 'ArrowDown') goToIndex(activeIndex + 1);
      else if (event.key === 'ArrowUp') goToIndex(activeIndex - 1);
      else if (event.key === 'Escape') {
        audioManager.stop().then(() => onExitTikTok?.());
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [activeIndex, goToIndex, onExitTikTok]);

  // Mouse wheel
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let lastWheel = 0;
    const handleWheel = (e) => {
      e.preventDefault();
      const now = Date.now();
      if (now - lastWheel < 600) return;
      lastWheel = now;
      if (e.deltaY > 0) {
        // 🚀 Eager prefetch del +1 antes de iniciar la transición
        if (activeIndex + 1 < polls.length) eagerPrefetchNextPost(activeIndex + 1);
        goToIndex(activeIndex + 1);
      } else {
        goToIndex(activeIndex - 1);
      }
    };
    el.addEventListener('wheel', handleWheel, { passive: false });
    return () => el.removeEventListener('wheel', handleWheel);
  }, [activeIndex, goToIndex, polls.length, eagerPrefetchNextPost]);

  // Index change para SearchPage
  useEffect(() => {
    if (onIndexChange && activeIndex !== lastActiveIndex) {
      const direction = activeIndex > lastActiveIndex ? 'next' : 'previous';
      if (direction === 'next' && activeIndex >= polls.length - 2) onIndexChange('next', activeIndex);
      else if (direction === 'previous' && activeIndex <= 1) onIndexChange('previous', activeIndex);
    }
  }, [activeIndex, lastActiveIndex, onIndexChange, polls.length]);

  useEffect(() => {
    setActiveIndex(initialIndex);
    setLastActiveIndex(initialIndex);
  }, [initialIndex]);

  // ─── LIFECYCLE ────────────────────────────────────────────────────────────
  useEffect(() => {
    return () => { audioManager.stop().catch(() => {}); };
  }, []);

  useEffect(() => {
    const handleUnload = async () => { await audioManager.stop(); };
    window.addEventListener('beforeunload', handleUnload);
    window.addEventListener('pagehide', handleUnload);
    return () => {
      window.removeEventListener('beforeunload', handleUnload);
      window.removeEventListener('pagehide', handleUnload);
    };
  }, []);

  useEffect(() => {
    if (!closeOnBack || typeof onExitTikTok !== 'function') return;
    const handleAppBack = async (event) => {
      try { event.preventDefault?.(); } catch (_) {}
      try { await audioManager.stop(); } catch (_) {}
      onExitTikTok();
    };
    window.addEventListener('app:backbutton', handleAppBack);
    return () => window.removeEventListener('app:backbutton', handleAppBack);
  }, [closeOnBack, onExitTikTok]);

  // ─── PREDICTIVE PREFETCH (ventana deslizante TikTok-style) ────────────────
  //
  // Cuando el usuario está parado en el post `activeIndex`, mantenemos
  // una "ventana" de hasta 5 posts en distintos estados de preparación,
  // imitando el modelo del spec TikTok:
  //
  //   [-2] → Destruido (gestionado por <PollOptionMedia>: distance>3 = no <video>)
  //         + cache LRU del filesystem evicta sus bytes si hace falta espacio.
  //   [-1] → Suspendido — solo thumbnail/poster en cache (back-swipe instantáneo).
  //   [ 0] → Activo, reproduciendo a calidad completa.
  //   [+1] → Pre-cargado — video casi completo (10MB max) + thumbnail + audio
  //          pre-connect. La mayor parte de esto lo dispara también
  //          `eagerPrefetchNextPost` en el touchStart.
  //   [+2] → Thumbnail + PRIMER SEGMENTO del video (~512KB via Range request)
  //          → "Frame 50ms HLS ya descargado" del spec.
  //   [+3] → SOLO metadata (HEAD warmup → calienta DNS/TLS/HTTP2 connection
  //          pool, sin descargar bytes de video).
  //   [+4] → Nada (descansa).
  //
  // Esto sustituye al loop uniforme "offset 0..3 todos igual" que había antes
  // y permite que la bandwidth se priorice por proximidad real.
  useEffect(() => {
    if (!polls || polls.length === 0) return;
    let cancelled = false;
    const localCancellers = []; // cancel() de cada job de priorityFetchQueue

    // 🚫 Cancelar prefetches de posts que ahora están lejos del activo
    // (TikTok-style: tras un flick rápido, los intermedios ya no importan
    // y compiten por bandwidth con el post que el usuario sí mira).
    import('../services/feedMediaPrefetcher').then(({ default: prefetcher }) => {
      if (cancelled) return;
      try { prefetcher.cancelDistantPolls(activeIndex, 4); } catch (_) {}
    }).catch(() => {});

    // 🚫 Cancelar TODA la cola LOW al cambiar de activeIndex.
    // Lo que está in-flight de lookahead (+2/+3) del activeIndex ANTERIOR
    // ya no nos sirve: el usuario ya cambió de post. Esto coincide
    // exactamente con el spec TikTok punto #7: "Se cancela si el usuario
    // hace swipe rápido".
    import('../services/priorityFetchQueue').then(({ default: queue }) => {
      if (cancelled) return;
      try { queue.cancelAll('low'); } catch (_) {}
    }).catch(() => {});

    Promise.all([
      import('../services/mediaCacheService'),
      import('../utils/mediaUrl'),
      import('../services/priorityFetchQueue'),
    ]).then(([cacheMod, mediaMod, queueMod]) => {
      if (cancelled) return;
      const cache = cacheMod.default;
      const queue = queueMod.default;
      const { pickPlayableVideoUrl, pickPlayableHlsUrl, pickVideoPosterUrl, pickImageUrl } = mediaMod;
      const isCellular =
        navigator?.connection?.type === 'cellular' ||
        ['2g', '3g'].includes(navigator?.connection?.effectiveType);
      const isSaveData = !!navigator?.connection?.saveData;
      // Si el usuario activa Data Saver, NO bajamos video pesado más allá del +1
      const allowVideoPrefetch = !isSaveData;

      const imageMaxBytes = 2 * 1024 * 1024;     // 2MB por thumbnail
      const fullVideoMaxBytes = isCellular ? 0 : 10 * 1024 * 1024; // 10MB +1
      const firstSegmentBytes = isCellular ? 256 * 1024 : 512 * 1024; // 256KB-512KB +2

      // Helper: pide los primeros N bytes (Range request) del video del post.
      // Estos bytes quedan en el HTTP cache del navegador, así que cuando
      // el <video>/HlsVideo realmente arranque, el browser continúa desde
      // donde está sin pedir el rango de nuevo → playback instantáneo.
      //
      // Va por la cola LOW: si el usuario hace swipe rápido (flick), se
      // cancela junto con el resto de LOW vía `cancelAll('low')`.
      const prefetchFirstSegment = (videoUrl, bytes) => {
        if (!videoUrl || cancelled) return;
        try {
          const { cancel } = queue.enqueue(videoUrl, {
            priority: 'low',
            init: {
              headers: { Range: `bytes=0-${bytes - 1}` },
              cache: 'force-cache',
            },
          });
          // Cancelar si el effect se desmonta
          localCancellers.push(cancel);
        } catch (_) {}
      };

      // Helper: HEAD-only para calentar DNS/TLS/keepalive sin gastar datos.
      // También en LOW — el +3 puede esperar.
      const prefetchMetadataOnly = (url) => {
        if (!url || cancelled) return;
        try {
          const { cancel } = queue.enqueue(url, {
            priority: 'low',
            init: { method: 'HEAD', cache: 'no-cache' },
          });
          localCancellers.push(cancel);
        } catch (_) {}
      };

      // ── PROCESAMIENTO DE LA VENTANA ────────────────────────────────────
      // Iteramos -1, 0, +1, +2, +3 con políticas distintas:
      const windowOffsets = [-1, 0, 1, 2, 3];
      for (const offset of windowOffsets) {
        const idx = activeIndex + offset;
        if (idx < 0 || idx >= polls.length) continue;
        const p = polls[idx];
        if (!p?.options) continue;

        for (const option of p.options) {
          // Thumbnail/poster siempre (lo más ligero, ya está en disco LRU)
          const thumbUrl = pickVideoPosterUrl(option) || pickImageUrl(option);
          if (thumbUrl) {
            try { cache.prefetch(thumbUrl, { maxBytes: imageMaxBytes }); } catch (_) {}
          }

          const isVideoOpt = option.media_type === 'video' || option.media?.type === 'video';
          if (!isVideoOpt) continue;
          if (!allowVideoPrefetch) continue; // Save-Data → no video

          const videoUrl = pickPlayableVideoUrl(option);
          const hlsUrl = pickPlayableHlsUrl(option);

          // Política por offset
          if (offset === 0) {
            // Activo: ya lo está descargando el reproductor, no duplicamos
            // (sería pelearse con el <video> por la misma URL). Solo
            // garantizamos el thumbnail (ya hecho arriba).
            break;
          } else if (offset === 1) {
            // +1: video casi completo (hasta 10MB) → cache filesystem LRU.
            // Si hay HLS, los primeros segmentos quedan en HTTP cache via
            // el pre-decode de eagerPrefetchNextPost. Aquí cubrimos el MP4.
            if (videoUrl && fullVideoMaxBytes > 0) {
              try { cache.prefetch(videoUrl, { maxBytes: fullVideoMaxBytes }); } catch (_) {}
            }
            break; // 1 video por post es suficiente para el +1
          } else if (offset === 2) {
            // +2: primer segmento via Range request (256-512KB).
            // Si hay HLS, calentamos el .m3u8 con HEAD para que el manifest
            // esté listo cuando llegue el usuario. Luego un segmento real.
            if (hlsUrl) {
              prefetchMetadataOnly(hlsUrl);
            }
            if (videoUrl) {
              prefetchFirstSegment(videoUrl, firstSegmentBytes);
            }
            break;
          } else if (offset === 3) {
            // +3: solo metadata. HEAD warmup → calienta conexión, no descarga.
            if (hlsUrl) prefetchMetadataOnly(hlsUrl);
            else if (videoUrl) prefetchMetadataOnly(videoUrl);
            break;
          } else if (offset === -1) {
            // -1: SOLO thumbnail (ya hecho arriba). No tocamos video.
            // El back-swipe es raro pero rápido cuando ocurre.
            break;
          }
        }
      }
    }).catch(() => {});

    return () => {
      cancelled = true;
      // Abortar Range/HEAD requests pendientes al cambiar de activeIndex.
      // Crítico: si el usuario hace flick rápido, no queremos seguir
      // descargando first-segments de posts que ya no son +2.
      for (const c of localCancellers) {
        try { c(); } catch (_) {}
      }
    };
  }, [activeIndex, polls]);

  // ─── SAVED/SHARED POLLS ───────────────────────────────────────────────────
  useEffect(() => {
    if (!polls?.length) return;
    setSavedPolls(prev => { const s = new Set(prev); polls.forEach(p => { if (p.isSaved) s.add(p.id); }); return s; });
    setCommentedPolls(prev => { const s = new Set(prev); polls.forEach(p => { if (p.userCommented) s.add(p.id); }); return s; });
    setSharedPolls(prev => { const s = new Set(prev); polls.forEach(p => { if (p.userShared) s.add(p.id); }); return s; });
  }, [polls]);

  useEffect(() => {
    const loadSaved = async () => {
      if (!currentUser?.id) return;
      try {
        const token = localStorage.getItem('token');
        if (!token) return;
        const payload = JSON.parse(atob(token.split('.')[1]));
        const userId = payload.sub;
        if (!userId) return;
        const res = await fetch(`${process.env.REACT_APP_BACKEND_URL}/api/users/${userId}/saved-polls`, { headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' } });
        if (res.ok) { const r = await res.json(); setSavedPolls(new Set(r.saved_polls?.map(p => p.id) || [])); }
      } catch (_) {}
    };
    loadSaved();
  }, [currentUser?.id]);

  useEffect(() => {
    const loadShared = async () => {
      if (!currentUser?.id) return;
      try {
        const token = localStorage.getItem('token');
        if (!token) return;
        const payload = JSON.parse(atob(token.split('.')[1]));
        const userId = payload.sub;
        if (!userId) return;
        const res = await fetch(`${process.env.REACT_APP_BACKEND_URL}/api/users/${userId}/shared-polls`, { headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' } });
        if (res.ok) { const r = await res.json(); setSharedPolls(new Set(r.shared_poll_ids || [])); }
      } catch (_) {}
    };
    loadShared();
  }, [currentUser?.id]);

  // ─── EMPTY STATES ─────────────────────────────────────────────────────────
  if (!polls.length) {
    if (isInitialLoading) {
      return (
        <div className="fixed inset-0 z-50 bg-black flex items-center justify-center" style={{ height: '100dvh' }}>
          <div className="text-center px-6">
            <div className="w-20 h-20 border-4 border-gray-700 border-t-blue-500 rounded-full animate-spin mx-auto mb-6" />
            <h3 className="text-xl font-semibold text-white mb-2">Cargando publicaciones...</h3>
            <p className="text-gray-400 text-sm">Por favor espera un momento</p>
          </div>
        </div>
      );
    }
    return (
      <div className="fixed inset-0 z-50 bg-black flex items-center justify-center" style={{ height: '100dvh' }}>
        {showActiveChallengesButton && (
          <div className="fixed z-50" style={{ top: 'max(1rem, var(--safe-area-inset-top))', right: 'max(1rem, var(--safe-area-inset-right))' }}>
            <Button onClick={() => navigate('/explore/active')} className="bg-red-500 text-white hover:bg-red-600 backdrop-blur-md border-none px-3 py-2 h-10 rounded-full flex items-center gap-1.5" size="sm">
              <Swords className="w-4 h-4" /><span className="text-sm font-medium">{t('explore.activeButton')}</span>
            </Button>
          </div>
        )}
        <div className="text-center px-6">
          <div className="w-20 h-20 bg-gray-800 rounded-full flex items-center justify-center mx-auto mb-6"><Trophy className="w-10 h-10 text-gray-500" /></div>
          <h3 className="text-xl font-semibold text-white mb-2">{emptyMessage}</h3>
          <p className="text-gray-400 text-sm">{emptySubMessage}</p>
        </div>
      </div>
    );
  }

  // ─── SHARED CARD PROPS ────────────────────────────────────────────────────
  const sharedCardProps = useMemo(() => ({
    onVote, onLike, onShare, onComment, onSave, onCreatePoll,
    showLogo, onUpdatePoll, onDeletePoll, isOwnProfile,
    currentUser, savedPolls, setSavedPolls,
    commentedPolls, setCommentedPolls, sharedPolls, setSharedPolls,
    isHighBandwidth, onModalStateChange: setIsModalOpen,
    fromAudioDetailPage, currentAudio,
  }), [
    onVote, onLike, onShare, onComment, onSave, onCreatePoll,
    showLogo, onUpdatePoll, onDeletePoll, isOwnProfile,
    currentUser, savedPolls, commentedPolls, sharedPolls,
    isHighBandwidth, fromAudioDetailPage, currentAudio,
  ]);

  const slots = useMemo(() => [
    { poll: slotPolls[SLOT_PREV], slotIndex: SLOT_PREV, pollIndex: activeIndex - 1 },
    { poll: slotPolls[SLOT_CUR],  slotIndex: SLOT_CUR,  pollIndex: activeIndex },
    { poll: slotPolls[SLOT_NEXT], slotIndex: SLOT_NEXT, pollIndex: activeIndex + 1 },
  ], [slotPolls, activeIndex]);

  return (
    <div
      ref={containerRef}
      className="fixed inset-0 z-50 bg-black overflow-hidden"
      style={{ height: '100dvh', touchAction: 'none' }}
    >
      {/* Pull-to-refresh spinner */}
      {(pullDistance > 0 || isRefreshing) && (
        <div className="absolute top-0 left-0 right-0 z-50 flex items-center justify-center transition-all duration-200"
          style={{ height: `${Math.max(pullDistance, isRefreshing ? 60 : 0)}px`, paddingTop: 'var(--safe-area-inset-top, 0px)' }}>
          <div className={`w-8 h-8 border-2 border-white/30 border-t-white rounded-full ${isRefreshing ? 'animate-spin' : ''}`}
            style={{ transform: `rotate(${(pullDistance / PTR_MAX) * 360}deg)`, transition: isRefreshing ? 'none' : 'transform 0.1s linear' }} />
        </div>
      )}

      {showCloseButton && (
        <button className="absolute top-0 right-0 z-50 p-4" style={{ top: 'max(1rem, var(--safe-area-inset-top))', right: 'max(1rem, var(--safe-area-inset-right))' }}
          onClick={() => { audioManager.stop(); onExitTikTok?.(); }}>
          <X className="w-6 h-6 text-white" />
        </button>
      )}
      {showActiveChallengesButton && (
        <div className="fixed z-50" style={{ top: 'max(1rem, var(--safe-area-inset-top))', right: 'max(1rem, var(--safe-area-inset-right))' }}>
          <Button onClick={() => navigate('/explore/active')} className="bg-red-500 text-white hover:bg-red-600 backdrop-blur-md border-none px-3 py-2 h-10 rounded-full flex items-center gap-1.5" size="sm">
            <Swords className="w-4 h-4" /><span className="text-sm font-medium">{t('explore.activeButton')}</span>
          </Button>
        </div>
      )}

      {/* ── 3-SLOT TAPE ───────────────────────────────────────────────────── */}
      <div
        ref={tapeRef}
        style={{
          position: 'absolute',
          top: 0, left: 0, right: 0,
          height: '300dvh',
          // 🚀 translate3d → force compositor layer desde el primer paint.
          // El browser crea una capa GPU permanente, así que las
          // actualizaciones de transform durante el drag son compositor-only
          // (no layout, no paint del contenido).
          transform: `translate3d(0, calc(-100dvh + ${translateOffset}dvh + ${pullDistance > 0 ? pullDistance : 0}px), 0)`,
          // Duración adaptativa según velocidad del swipe
          transition: isTransitioning
            ? `transform ${transitionDuration}ms cubic-bezier(0.16, 1, 0.3, 1)`
            : 'none',
          willChange: 'transform',
          // 🚀 touch-action: pan-y → declara al browser que solo procesamos
          // pan vertical. El browser puede iniciar el gesto sin esperar a
          // saber si vamos a hacer preventDefault. Crítico para 60fps.
          // Sin esto, hay un retardo de ~50-100ms en mobile WebView.
          touchAction: 'pan-y',
        }}
        onTouchStart={handleTapePointerDown}
        onTouchMove={handleTapePointerMove}
        onTouchEnd={handleTapePointerUp}
        onMouseDown={handleTapePointerDown}
        onMouseMove={handleTapePointerMove}
        onMouseUp={handleTapePointerUp}
      >
        {slots.map(({ poll: slotPoll, slotIndex, pollIndex }) => (
          <div
            key={`slot-${slotIndex}`}
            // data-slot-active: usado por el background pause handler para
            // reanudar solo el video del slot activo al volver de background.
            data-slot-active={pollIndex === activeIndex ? 'true' : 'false'}
            style={{
              position: 'absolute',
              top: `${slotIndex * 100}dvh`,
              left: 0, right: 0,
              height: '100dvh',
              overflow: 'hidden',
              // 🚀 CSS Containment — aisla layout + paint + size del slot.
              // El browser sabe que cambios dentro del slot NO afectan a
              // los hermanos → puede optimizar layout/paint dramáticamente.
              // Esto reduce trabajo en repaint durante el drag.
              contain: 'layout paint size',
              // willChange solo en el slot activo y vecinos cercanos: cada
              // capa GPU cuesta memoria. Promote permanentemente solo
              // lo que se beneficia (los 3 slots SÍ, pero usamos transform3d
              // arriba para forzar la capa sin willChange constante).
              transform: 'translateZ(0)',
              backfaceVisibility: 'hidden',
            }}
          >
            {slotPoll ? (
              <TikTokPollCard
                key={`slot-card-${slotIndex}`}
                poll={slotPoll}
                isActive={pollIndex === activeIndex}
                index={pollIndex}
                total={polls.length}
                shouldPreload={Math.abs(pollIndex - activeIndex) <= 1}
                isVisible={Math.abs(pollIndex - activeIndex) <= 1}
                optimizeVideo={slotPoll.options?.some(opt => opt.media_type === 'video')}
                renderPriority={pollIndex === activeIndex ? 'high' : 'medium'}
                shouldUnload={false}
                layout={slotPoll.layout}
                distanceFromActive={Math.abs(pollIndex - activeIndex)}
                showScrollHint={showScrollHint && pollIndex === 0}
                {...sharedCardProps}
              />
            ) : (
              <div style={{ width: '100%', height: '100%', background: 'black' }} />
            )}
          </div>
        ))}
      </div>

      {/* ── CARGA SILENCIOSA (sin spinner) ─────────────────────────────────
          TikTok nunca muestra un spinner de "cargando más" — el contenido
          aparece disponible cuando el usuario llega al final, sin interrupciones.
          isLoadingMore se usa solo internamente para evitar peticiones duplicadas. */}

      <style>{`
        @supports (height: 100dvh) {
          .fixed.inset-0 { height: 100dvh; }
        }
        body { overscroll-behavior: none; }
        video {
          -webkit-transform: translateZ(0);
          transform: translateZ(0);
          -webkit-backface-visibility: hidden;
          backface-visibility: hidden;
        }
        * {
          -webkit-tap-highlight-color: transparent;
          -webkit-touch-callout: none;
        }
        @keyframes followBounce {
          0%   { transform: scale(0); }
          70%  { transform: scale(1.3); }
          100% { transform: scale(1); }
        }
      `}</style>
    </div>
  );
};

export default TikTokScrollView;
