from pydantic import BaseModel, Field, EmailStr, validator
from typing import List, Dict, Optional, Any
from datetime import datetime, date, timedelta
from enum import Enum
import uuid
from constants import VALID_LAYOUTS, DEFAULT_LAYOUT

class NotificationType(str, Enum):
    NEW_POLL = "new_poll"
    TRENDING = "trending"
    COMPETITION = "competition"

# User Profile - Simplified without levels/achievements
class UserProfile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    occupation: Optional[str] = None  # Field for user's profession/job
    is_verified: bool = False
    total_votes: int = 0
    total_polls_created: int = 0
    followers_count: int = 0
    following_count: int = 0
    likes_count: int = 0  # Likes received on user's content
    votes_count: int = 0  # Votes made by this user
    likes_given: int = 0  # Likes given by this user
    last_activity: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)

# =============  AUTHENTICATION MODELS =============

class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: EmailStr
    username: str
    display_name: str
    hashed_password: Optional[str] = None  # Allow None for OAuth users
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    occupation: Optional[str] = None  # Field for user's profession/job
    is_verified: bool = False
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None
    
    # Privacy settings
    is_public: bool = True
    allow_messages: bool = True
    
    # Notification settings (with sensible defaults)
    notifications_enabled: bool = True
    email_notifications: bool = True
    push_notifications: bool = True
    notifications_likes: bool = True
    notifications_comments: bool = True
    notifications_follows: bool = True
    notifications_mentions: bool = True
    
    # Performance & Data settings (APK specific)
    video_quality: str = 'auto'  # 'auto', 'high', 'medium', 'low'
    wifi_only: bool = False
    battery_saver: bool = False
    auto_cache: bool = True
    background_sync: bool = True
    
    # Language & Accessibility settings
    app_language: str = 'es'
    dark_mode: bool = False
    large_text: bool = False
    
    # Account settings
    two_factor_enabled: bool = False
    
    # OAuth fields
    oauth_provider: Optional[str] = None  # "google", "facebook", etc.
    oauth_id: Optional[str] = None
    
class UserCreate(BaseModel):
    email: EmailStr
    username: str
    display_name: str
    password: str = Field(..., min_length=6, max_length=128, description="Password must be 6-128 characters")
    avatar_url: Optional[str] = None  # Permitir avatares durante el registro

class UserLogin(BaseModel):
    email: str  # Accepts email or username
    password: str

class UserResponse(BaseModel):
    id: str
    email: str
    username: str
    display_name: str
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    occupation: Optional[str] = None  # Field for user's profession/job
    is_verified: bool
    created_at: datetime
    last_login: Optional[datetime] = None
    
    # Privacy settings
    is_public: bool
    allow_messages: bool
    
    # Notification settings
    notifications_enabled: bool = True
    email_notifications: bool = True
    push_notifications: bool = True
    notifications_likes: bool = True
    notifications_comments: bool = True
    notifications_follows: bool = True
    notifications_mentions: bool = True
    
    # Performance & Data settings (APK specific)
    video_quality: str = 'auto'
    wifi_only: bool = False
    battery_saver: bool = False
    auto_cache: bool = True
    background_sync: bool = True
    
    # Language & Accessibility settings
    app_language: str = 'es'
    dark_mode: bool = False
    large_text: bool = False
    
    # Account settings
    two_factor_enabled: bool = False

class UserSocialLinks(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    website: Optional[str] = None
    behance: Optional[str] = None
    dribbble: Optional[str] = None
    tiktok: Optional[str] = None
    twitch: Optional[str] = None
    instagram: Optional[str] = None
    discord: Optional[str] = None
    youtube: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class SocialLinksUpdate(BaseModel):
    website: Optional[str] = None
    behance: Optional[str] = None
    dribbble: Optional[str] = None
    tiktok: Optional[str] = None
    twitch: Optional[str] = None
    instagram: Optional[str] = None
    discord: Optional[str] = None
    youtube: Optional[str] = None

class SocialLinksResponse(BaseModel):
    website: Optional[str] = None
    behance: Optional[str] = None
    dribbble: Optional[str] = None
    tiktok: Optional[str] = None
    twitch: Optional[str] = None
    instagram: Optional[str] = None
    discord: Optional[str] = None
    youtube: Optional[str] = None

# =============  CUSTOM SOCIAL LINKS MODELS =============

class SocialLink(BaseModel):
    name: str
    url: str
    color: Optional[str] = "#007bff"

class SocialLinks(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    links: List[SocialLink] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class SocialLinksCreate(BaseModel):
    links: List[SocialLink]

class Token(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    user: UserResponse

class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    occupation: Optional[str] = None
    avatar_url: Optional[str] = None

class PasswordChange(BaseModel):
    current_password: str
    new_password: str

class UserSettings(BaseModel):
    # Privacy settings
    is_public: Optional[bool] = None
    allow_messages: Optional[bool] = None
    
    # Notification settings
    notifications_enabled: Optional[bool] = None
    email_notifications: Optional[bool] = None
    push_notifications: Optional[bool] = None
    notifications_likes: Optional[bool] = None
    notifications_comments: Optional[bool] = None
    notifications_follows: Optional[bool] = None
    notifications_mentions: Optional[bool] = None
    
    # Performance & Data settings (APK specific)
    video_quality: Optional[str] = None
    wifi_only: Optional[bool] = None
    battery_saver: Optional[bool] = None
    auto_cache: Optional[bool] = None
    background_sync: Optional[bool] = None
    
    # Language & Accessibility settings
    app_language: Optional[str] = None
    dark_mode: Optional[bool] = None
    large_text: Optional[bool] = None
    
    # Account settings
    two_factor_enabled: Optional[bool] = None

# =============  SECURITY MODELS =============

class LoginAttempt(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: str
    ip_address: str
    user_agent: str
    success: bool
    failure_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

class UserDevice(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    device_name: str
    device_type: str  # "desktop", "mobile", "tablet"
    browser: str
    os: str
    ip_address: str
    user_agent: str
    last_used: datetime = Field(default_factory=datetime.utcnow)
    is_trusted: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)

class UserSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    session_token: str
    device_id: Optional[str] = None
    ip_address: str
    user_agent: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = True

class SecurityNotification(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    notification_type: str  # "new_login", "password_change", "new_device"
    title: str
    message: str
    metadata: Dict[str, Any] = {}
    is_read: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)

# =============  MESSAGING MODELS =============

class ChatRequestStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

class ChatRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender_id: str
    receiver_id: str
    status: ChatRequestStatus = ChatRequestStatus.PENDING
    message: Optional[str] = None  # Optional message with the request
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None  # Auto-expire after 30 days

class ChatRequestCreate(BaseModel):
    receiver_id: str
    message: Optional[str] = None

class ChatRequestResponse(BaseModel):
    id: str
    sender: UserResponse
    receiver: UserResponse
    status: ChatRequestStatus
    message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class ChatRequestAction(BaseModel):
    action: str  # "accept" or "reject"

class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    sender_id: str
    recipient_id: str
    content: str
    message_type: str = "text"  # text, image, poll_share, etc.
    metadata: Dict[str, Any] = {}  # for attachments, poll links, etc.
    is_read: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class MessageCreate(BaseModel):
    recipient_id: str
    content: str
    message_type: str = "text"
    metadata: Dict[str, Any] = {}

class Conversation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    participants: List[str]  # user_ids
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread_count: Dict[str, int] = {}  # user_id -> unread count
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class ConversationResponse(BaseModel):
    id: str
    participants: List[UserResponse]
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread_count: int = 0
    created_at: datetime

# =============  FOLLOW MODELS =============

class Follow(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    follower_id: str  # ID del usuario que sigue
    following_id: str  # ID del usuario seguido
    created_at: datetime = Field(default_factory=datetime.utcnow)

class FollowCreate(BaseModel):
    user_id: str  # ID del usuario a seguir

class FollowResponse(BaseModel):
    id: str
    follower: UserResponse
    following: UserResponse
    created_at: datetime

class FollowStatus(BaseModel):
    is_following: bool
    follow_id: Optional[str] = None

class FollowingList(BaseModel):
    following: List[UserResponse]
    total: int

class FollowersList(BaseModel):
    followers: List[UserResponse]
    total: int

# =============  STORY MODELS ============= (DISABLED - Feature removed)
# All story models have been removed as the stories feature is disabled

# =============  COMMENT MODELS =============

class Comment(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    poll_id: str  # ID de la encuesta a la que pertenece el comentario
    user_id: str  # ID del usuario que creó el comentario
    content: str  # Contenido del comentario
    parent_comment_id: Optional[str] = None  # ID del comentario padre (para anidamiento)
    likes: int = 0  # Número de likes en el comentario
    is_edited: bool = False  # Si el comentario ha sido editado
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Metadata adicional
    metadata: Dict[str, Any] = {}  # Para menciones, hashtags, etc.

class CommentCreate(BaseModel):
    poll_id: str
    content: str
    parent_comment_id: Optional[str] = None

class CommentUpdate(BaseModel):
    content: str

class CommentResponse(BaseModel):
    id: str
    poll_id: str
    user: UserResponse  # Información completa del usuario
    content: str
    parent_comment_id: Optional[str] = None
    likes: int = 0
    is_edited: bool = False
    created_at: datetime
    updated_at: datetime
    # Para anidamiento
    replies: List["CommentResponse"] = []  # Lista de comentarios hijos
    reply_count: int = 0  # Conteo total de respuestas anidadas
    user_liked: bool = False  # Si el usuario actual le dio like
    # Reacciones rápidas (emoji): {emoji: count}
    reactions: Dict[str, int] = Field(default_factory=dict)
    user_reaction: Optional[str] = None  # Emoji con el que el usuario actual reaccionó
    # 🆕 Badge "liked by creator" (estilo TikTok/Instagram)
    liked_by_author: bool = False  # Si el autor del poll le dio like a este comentario
    post_author_id: Optional[str] = None  # ID del autor del poll (para detectar si current_user es autor)
    post_author_avatar_url: Optional[str] = None  # Avatar del autor (para mostrar el badge)

class CommentLike(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    comment_id: str
    user_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class CommentReaction(BaseModel):
    """Reacción rápida con emoji a un comentario.
    Un usuario solo puede tener UNA reacción activa por comentario; cambiar de
    emoji reemplaza la anterior."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    comment_id: str
    user_id: str
    emoji: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ReactionRequest(BaseModel):
    emoji: str

# =============  POLL MODELS =============

class PollOption(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str  # Usuario que creó esta opción
    text: Optional[str] = ""  # Texto opcional para la opción
    text_position: Optional[str] = "bottom"  # Posición del texto: "top", "center", "bottom"
    votes: int = 0
    media_type: Optional[str] = None  # "image", "video", None
    media_url: Optional[str] = None
    # URL del video transcodificado a 720p H.264+AAC+faststart (mobile-friendly).
    # Lo genera asíncronamente el video_pipeline tras la subida. Si está presente,
    # el cliente debería preferirlo sobre `media_url` para mejor rendimiento.
    # Si el transcoding falla, queda como None y el cliente usa `media_url`.
    optimized_media_url: Optional[str] = None
    # URL del master HLS multi-rendition (360p/540p/720p). Generado por
    # transcode_video_hls() en el video_pipeline tras la subida. Si está
    # presente, el cliente debería preferirlo para ABR streaming.
    # Si la generación falla, queda como None y el cliente usa optimized/media URL.
    hls_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    media_transform: Optional[dict] = None  # ✅ Transform data for image cropping/positioning
    mentioned_users: List[str] = []  # List of user IDs mentioned in this option
    extracted_audio_id: Optional[str] = None  # 🎵 Audio extraído de video del carrusel
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Poll(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    author_id: str  # Usuario que creó el poll
    description: Optional[str] = None
    options: List[PollOption] = []
    total_votes: int = 0
    likes: int = 0
    shares: int = 0
    comments_count: int = 0
    saves_count: int = 0  # Contador de veces que se guardó
    music_id: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Metadata
    tags: List[str] = []
    category: Optional[str] = None
    is_featured: bool = False
    # New fields for enhanced functionality
    mentioned_users: List[str] = []  # List of user IDs mentioned in the poll
    video_playback_settings: Optional[dict] = None  # Video playback configuration
    layout: Optional[str] = None  # Layout configuration (e.g., 'grid-3x2', 'vertical', etc.)
    challenge_id: Optional[str] = None  # ID del challenge si este poll es parte de uno
    # Post settings
    comments_enabled: bool = True  # Allow comments on this post
    show_vote_count: bool = True  # Show vote count to users
    audience_target: Optional[str] = None  # Target audience
    source_authenticity: Optional[str] = None  # Content source authenticity
    voting_privacy: Optional[str] = None  # Voting privacy setting
    mature_content: Optional[str] = None  # Mature content rating
    allow_downloads: bool = True  # Allow users to download content
    # Estado de validación de medios y ciclo de vida del post:
    #   "ready"      → publicado y visible (estado por defecto)
    #   "processing" → subida en curso, transcoding / generación de thumb
    #   "failed"     → falló el pipeline, NO visible en ningún listado
    #   "broken"     → media inaccesible detectada en runtime, NO visible
    #   "hidden"     → soft-delete (usuario borró o cuenta desactivada)
    # El filtro global `POLL_STATUS_FILTER` excluye
    # {broken, hidden, failed, processing} de todos los listados.
    status: str = "ready"
    # Metadatos del pipeline de processing (sólo presentes cuando aplica).
    processing_started_at: Optional[datetime] = None
    processing_completed_at: Optional[datetime] = None
    processing_error: Optional[str] = None
    # Soft-delete metadata (sólo presente cuando status == "hidden")
    hidden_at: Optional[datetime] = None
    hidden_by: Optional[str] = None

class PollCreate(BaseModel):
    title: str
    description: Optional[str] = None
    options: List[dict]  # [{text: str, media_url?: str, media_type?: str}]
    music_id: Optional[str] = None
    tags: List[str] = []
    category: Optional[str] = None
    # New fields for enhanced functionality
    mentioned_users: List[str] = []  # List of user IDs mentioned in the poll
    video_playback_settings: Optional[dict] = None  # Video playback configuration
    layout: Optional[str] = None  # Layout configuration (e.g., 'grid-3x2', 'vertical', etc.)
    challenge_id: Optional[str] = None  # ID del challenge si este poll es parte de uno
    # Post settings
    comments_enabled: bool = True  # Allow comments on this post
    show_vote_count: bool = True  # Show vote count to users
    audience_target: Optional[str] = None  # Target audience
    source_authenticity: Optional[str] = None  # Content source authenticity
    voting_privacy: Optional[str] = None  # Voting privacy setting
    mature_content: Optional[str] = None  # Mature content rating
    allow_downloads: bool = True  # Allow users to download content
    
    @validator('layout')
    def validate_layout(cls, v):
        """Validate that layout is one of the allowed values"""
        if v is not None and v not in VALID_LAYOUTS:
            raise ValueError(f'Invalid layout. Must be one of: {", ".join(VALID_LAYOUTS)}')
        return v or DEFAULT_LAYOUT

# Simple user model for mentions
class MentionedUser(BaseModel):
    id: str
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None

class PollResponse(BaseModel):
    id: str
    title: str
    author: Optional[UserResponse] = None  # Hacer opcional para manejar casos sin autor
    description: Optional[str] = None
    options: List[dict]  # Incluirá información del usuario de cada opción
    total_votes: int
    likes: int
    shares: int
    comments_count: int
    saves_count: int = 0  # Contador de veces que se guardó la publicación
    music: Optional[dict] = None  # Información de la música
    user_vote: Optional[str] = None  # ID de la opción votada por el usuario actual
    user_liked: bool = False
    is_saved: bool = False
    user_commented: bool = False
    is_featured: bool
    tags: List[str]
    category: Optional[str]
    mentioned_users: List[MentionedUser] = []  # List of mentioned users with details
    layout: Optional[str] = None  # Layout configuration
    challenge_id: Optional[str] = None  # ID del challenge si este poll es parte de uno
    is_challenge: bool = False  # Si este poll es un challenge publicado
    challenge_status: Optional[str] = None  # Estado del challenge
    participants: List[dict] = []  # Participantes del challenge
    # VS Experience fields - for multi-question VS polls
    vs_id: Optional[str] = None  # Reference to VS experience
    vs_questions: List[dict] = []  # All questions for VS experience
    creator_country: Optional[str] = None  # Country where VS was created
    vs_orientation: Optional[str] = "horizontal"  # 'vertical'=lado a lado (izq-der), 'horizontal'=arriba-abajo
    # 🎬 VS Composed (estilo TikTok Duet) — MP4 con split-screen pre-incrustado
    # generado por FFmpeg en background al publicar el VS. El frontend lo
    # reproduce como un único <video> mientras el usuario no ha votado.
    composed_video_url: Optional[str] = None
    composed_hls_url: Optional[str] = None  # 🚀 HLS ABR del compuesto (arranque instantáneo + adaptación a la red)
    composed_status: Optional[str] = None  # 'pending'|'processing'|'ready'|'failed'|'not_applicable'
    composed_orientation: Optional[str] = None  # snapshot de vs_orientation al componer
    created_at: datetime
    time_ago: str  # Campo calculado como "hace 2 horas"
    # Post settings
    comments_enabled: bool = True  # Allow comments on this post
    show_vote_count: bool = True  # Show vote count to users

class Vote(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    poll_id: str
    option_id: str
    user_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class VoteCreate(BaseModel):
    option_id: str

class PollLike(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    poll_id: str
    user_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


# =============  CHALLENGE MODELS =============

class ChallengeStatus(str, Enum):
    PENDING = "pending"  # Creado, esperando aceptación de participantes
    ACTIVE = "active"  # Al menos 2 usuarios aceptaron, creando contenido
    COMPLETED = "completed"  # Todos crearon contenido, listo para publicar
    PUBLISHED = "published"  # Publicado en el feed
    CANCELLED = "cancelled"  # Cancelado por el creador

class ParticipantStatus(str, Enum):
    INVITED = "invited"  # Invitado, aún no respondió
    ACCEPTED = "accepted"  # Aceptó participar
    REJECTED = "rejected"  # Rechazó participar
    CONTENT_SUBMITTED = "content_submitted"  # Ya envió su contenido

class ChallengeParticipant(BaseModel):
    user_id: str
    username: str  # Para mostrar en UI
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    status: ParticipantStatus = ParticipantStatus.INVITED
    poll_id: Optional[str] = None  # ID del poll que creó para el challenge
    votes_received: int = 0  # Votos recibidos de usuarios
    joined_at: Optional[datetime] = None  # Cuándo aceptó
    submitted_at: Optional[datetime] = None  # Cuándo envió contenido

class Challenge(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str  # Título del challenge (ej: "Challenge de Baile 💃")
    description: Optional[str] = None  # Descripción del reto
    creator_id: str  # Usuario que creó el challenge
    creator_username: str
    creator_display_name: Optional[str] = None
    creator_avatar_url: Optional[str] = None
    participants: List[ChallengeParticipant] = []  # Lista de participantes
    max_participants: int = 6  # Máximo 6 participantes
    min_participants: int = 2  # Mínimo 2 para que se active
    status: ChallengeStatus = ChallengeStatus.PENDING
    challenge_type: Optional[str] = None  # dance, art, cooking, etc.
    required_layout: Optional[str] = None  # Layout obligatorio para todos los participantes
    deadline: Optional[datetime] = None  # Fecha límite (opcional)
    published_poll_id: Optional[str] = None  # ID del poll final publicado
    created_at: datetime = Field(default_factory=datetime.utcnow)
    published_at: Optional[datetime] = None
    total_votes: int = 0  # Total de votos en el challenge publicado
    total_views: int = 0  # Vistas del challenge publicado

class ChallengeCreate(BaseModel):
    title: str
    description: Optional[str] = None
    participant_ids: List[str]  # Lista de user IDs a invitar (máx 6)
    challenge_type: Optional[str] = None
    deadline: Optional[datetime] = None
    creator_poll_id: str  # ID del poll que el creador ya subió
    required_layout: Optional[str] = None  # Layout que todos los participantes deben usar
    
    @validator('participant_ids')
    def validate_participants(cls, v):
        if len(v) < 1:
            raise ValueError('Debe invitar al menos 1 participante')
        if len(v) > 6:
            raise ValueError('Máximo 6 participantes permitidos')
        if len(v) != len(set(v)):
            raise ValueError('No puede invitar al mismo usuario más de una vez')
        return v

class ChallengeResponse(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    creator_id: str
    creator_username: str
    creator_display_name: Optional[str] = None
    creator_avatar_url: Optional[str] = None
    participants: List[ChallengeParticipant]
    status: ChallengeStatus
    challenge_type: Optional[str] = None
    required_layout: Optional[str] = None  # Layout obligatorio para participantes
    deadline: Optional[datetime] = None
    published_poll_id: Optional[str] = None
    created_at: datetime
    published_at: Optional[datetime] = None
    total_votes: int = 0
    total_views: int = 0
    # Campos calculados
    accepted_count: int = 0  # Cuántos aceptaron
    submitted_count: int = 0  # Cuántos enviaron contenido
    is_ready_to_publish: bool = False  # Todos completaron?

class ChallengeVote(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    challenge_id: str
    voter_id: str  # Usuario que vota
    participant_id: str  # Usuario por quien vota
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ChallengeVoteCreate(BaseModel):
    participant_id: str  # ID del participante por quien votar

# =============  MUSIC MODELS =============

class Music(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    artist: str
    duration: int  # in seconds
    url: str
    cover_url: Optional[str] = None
    is_original: bool = False
    waveform: List[float] = []  # Visualization data
    created_at: datetime = Field(default_factory=datetime.utcnow)

# =============  FILE UPLOAD MODELS =============

class UploadType(str, Enum):
    AVATAR = "avatar"
    POLL_OPTION = "poll_option"
    POLL_BACKGROUND = "poll_background"
    GENERAL = "general"
    AUDIO = "audio"  # Nuevo tipo para archivos de audio

class FileType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"  # Nuevo tipo para archivos de audio

class UploadedFile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    filename: str
    original_filename: str
    file_type: FileType  # "image" or "video"
    file_format: str  # jpg, png, mp4, etc.
    file_size: int  # in bytes
    upload_type: UploadType
    uploader_id: str  # User who uploaded
    file_path: str  # Local file path
    public_url: str  # URL to access file
    thumbnail_url: Optional[str] = None  # Thumbnail URL for videos
    width: Optional[int] = None  # For images/videos
    height: Optional[int] = None  # For images/videos
    duration: Optional[float] = None  # For videos in seconds
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class UploadResponse(BaseModel):
    id: str
    filename: str
    original_filename: str
    file_type: FileType
    file_format: str
    file_size: int
    public_url: str
    thumbnail_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    created_at: datetime

# =============  USER AUDIO MODELS =============

class AudioPrivacy(str, Enum):
    PRIVATE = "private"  # Solo el usuario puede usar este audio
    PUBLIC = "public"    # Otros usuarios pueden descubrir y usar este audio

class UserAudio(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str  # Título del audio (editable por usuario)
    artist: str = ""  # Artista/Creador (defaults to username)
    original_filename: str  # Nombre del archivo original
    filename: str  # Nombre del archivo en el servidor
    file_format: str  # mp3, m4a, wav, aac
    file_size: int  # Tamaño en bytes
    duration: int  # Duración en segundos (máx 60)
    uploader_id: str  # ID del usuario que subió el audio
    file_path: str  # Ruta local del archivo
    public_url: str  # URL pública para acceder al audio
    waveform: List[float] = []  # Datos de visualización de onda
    cover_url: Optional[str] = None  # URL de la imagen de portada (opcional)
    privacy: AudioPrivacy = AudioPrivacy.PRIVATE  # Privacidad del audio
    uses_count: int = 0  # Número de veces que se ha usado en posts
    is_original: bool = True  # Siempre True para audio subido por usuarios
    category: str = "User Audio"  # Categoría fija
    # Metadatos del archivo
    bitrate: Optional[int] = None  # Bitrate del audio
    sample_rate: Optional[int] = None  # Frecuencia de muestreo
    # Control de estado
    is_active: bool = True  # Si el audio está activo
    is_processed: bool = False  # Si el audio ha sido procesado
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class UserAudioCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    artist: Optional[str] = Field(None, max_length=100)
    privacy: AudioPrivacy = AudioPrivacy.PRIVATE
    cover_url: Optional[str] = None

class UserAudioUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=100)
    artist: Optional[str] = Field(None, max_length=100)
    privacy: Optional[AudioPrivacy] = None
    cover_url: Optional[str] = None

class UserAudioResponse(BaseModel):
    id: str
    title: str
    artist: str
    duration: int
    public_url: str
    waveform: List[float]
    cover_url: Optional[str]
    privacy: AudioPrivacy
    uses_count: int
    is_original: bool
    category: str
    file_format: str
    file_size: int
    uploader: UserResponse  # Información del usuario que subió
    created_at: datetime
    # Para compatibilidad con sistema de música existente
    url: str = ""  # Será igual a public_url
    preview_url: str = ""  # Será igual a public_url
    isTrending: bool = False
    uses: int = 0  # Será igual a uses_count

class UserAudioUse(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    audio_id: str  # ID del audio usado
    user_id: str  # ID del usuario que usó el audio
    poll_id: Optional[str] = None  # ID del poll donde se usó (si aplica)
    created_at: datetime = Field(default_factory=datetime.utcnow)

# =============  AUDIO FAVORITES MODELS =============

class AudioFavorite(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str  # Usuario que marcó como favorito
    audio_id: str  # ID del audio favorito
    audio_type: str = "system"  # "system" para música del sistema, "user" para audio de usuario
    audio_title: Optional[str] = None  # Cached title for display
    audio_artist: Optional[str] = None  # Cached artist for display  
    audio_cover_url: Optional[str] = None  # Cached cover for display
    created_at: datetime = Field(default_factory=datetime.utcnow)

class AudioFavoriteCreate(BaseModel):
    audio_id: str
    audio_type: str = "system"  # "system" o "user"

class AudioFavoriteResponse(BaseModel):
    id: str
    audio_id: str
    audio_type: str
    audio_title: Optional[str] = None
    audio_artist: Optional[str] = None
    audio_cover_url: Optional[str] = None
    created_at: datetime
    # Información completa del audio cuando esté disponible
    audio_details: Optional[Dict[str, Any]] = None

# =============  FEED MENU MODELS =============

class UserPreference(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str  # User who set the preference
    poll_id: Optional[str] = None  # Specific poll hidden (for "no me interesa")
    author_id: Optional[str] = None  # Author blocked (for "ocultar usuario")
    preference_type: str  # "not_interested", "hidden_user", "notifications_enabled"
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ContentReport(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    poll_id: str  # Poll being reported
    reported_by: str  # User who reported
    category: str  # Report category (spam, harassment, etc.)
    comment: Optional[str] = None  # Additional comment
    status: str = "pending"  # pending, reviewed, resolved, dismissed
    reviewed_by: Optional[str] = None  # Admin who reviewed
    reviewed_at: Optional[datetime] = None
    resolution: Optional[str] = None  # Resolution details
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class UserNotificationPreference(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str  # User receiving notifications
    author_id: str  # Author to get notifications from
    is_enabled: bool = True  # Whether notifications are enabled for this author
    notification_types: List[str] = ["new_polls"]  # Types of notifications
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Request/Response models
class FeedMenuActionCreate(BaseModel):
    poll_id: str
    action_type: str  # "not_interested", "hide_user", "toggle_notifications", "report"
    data: Optional[Dict[str, Any]] = None  # Additional data (e.g., report category/comment)

class FeedMenuResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None



# =============  STORY MODELS =============

class Story(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    media_type: str  # "image" or "video"
    media_url: str
    thumbnail_url: Optional[str] = None
    duration: int = 86400  # Duration in seconds (default 24 hours)
    text_overlays: List[Dict[str, Any]] = []  # Text overlays with position and style
    stickers: List[Dict[str, Any]] = []  # Stickers with position
    music_id: Optional[str] = None
    views_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime = Field(default_factory=lambda: datetime.utcnow() + timedelta(hours=24))
    is_active: bool = True

class StoryCreate(BaseModel):
    media_type: str
    media_url: str
    thumbnail_url: Optional[str] = None
    text_overlays: List[Dict[str, Any]] = []
    stickers: List[Dict[str, Any]] = []
    music_id: Optional[str] = None
    duration: int = 86400

class StoryView(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    story_id: str
    user_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class StoryResponse(BaseModel):
    id: str
    user: UserResponse
    media_type: str
    media_url: str
    thumbnail_url: Optional[str] = None
    text_overlays: List[Dict[str, Any]] = []
    stickers: List[Dict[str, Any]] = []
    music_id: Optional[str] = None
    music: Optional[Dict[str, Any]] = None  # Complete music object with title, artist, preview_url
    views_count: int
    created_at: datetime
    expires_at: datetime
    is_active: bool
    viewed_by_me: bool = False

class StoriesGroupResponse(BaseModel):
    user: UserResponse
    stories: List[StoryResponse]
    total_stories: int
    has_unviewed: bool

# Necesario para resolver referencia circular
CommentResponse.model_rebuild()

# ============= PUSH NOTIFICATION MODELS =============

class FCMToken(BaseModel):
    """Firebase Cloud Messaging device token"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    token: str
    device_type: str = "android"  # android, ios, web
    device_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = True

class FCMTokenCreate(BaseModel):
    """Request model for registering FCM token"""
    token: str
    device_type: str = "android"
    device_name: Optional[str] = None

class PushNotification(BaseModel):
    """Push notification record"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    title: str
    body: str
    data: Optional[Dict[str, str]] = None
    notification_type: str  # "message", "comment", "like", "follow", "challenge"
    related_id: Optional[str] = None  # ID of related entity (message, comment, etc)
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    delivered: bool = False
    read: bool = False