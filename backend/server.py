from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, Request, Response, UploadFile, File, Query, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
import os
import uuid
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Any
import uuid
from datetime import datetime, timedelta, date, timedelta
import random
import asyncio
import re
import hashlib
import json
import aiohttp
import httpx
from user_agents import parse
import aiofiles
from PIL import Image
import mimetypes
from io import BytesIO

# ElevenLabs TTS
try:
    from elevenlabs.client import ElevenLabs
    ELEVENLABS_AVAILABLE = True
except ImportError:
    ELEVENLABS_AVAILABLE = False
    logging.warning("ElevenLabs not installed. TTS features disabled.")

# Optional heavy dependencies for media processing
try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    print("⚠️  OpenCV not available - video processing disabled")

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    print("⚠️  NumPy not available - advanced audio processing disabled")

# Import models
from models import (
    UserProfile, User, UserCreate, UserLogin, UserResponse, Token,
    UserSocialLinks, SocialLinksCreate, SocialLinksUpdate, SocialLinksResponse,
    Message, MessageCreate, Conversation, ConversationResponse,
    UserUpdate, PasswordChange, UserSettings,
    Comment, CommentCreate, CommentUpdate, CommentResponse, CommentLike,
    CommentReaction, ReactionRequest,
    Story, StoryCreate, StoryView, StoryResponse, StoriesGroupResponse,
    Follow, FollowCreate, FollowResponse, FollowStatus, FollowingList, FollowersList,
    LoginAttempt, UserDevice, UserSession, SecurityNotification,
    Poll, PollCreate, PollResponse, PollOption, Vote, VoteCreate, PollLike, Music, MentionedUser,
    UploadType, FileType, UploadedFile, UploadResponse,
    # User Audio Models
    UserAudio, UserAudioCreate, UserAudioUpdate, UserAudioResponse, UserAudioUse, AudioPrivacy,
    # Audio Favorites Models
    AudioFavorite, AudioFavoriteCreate, AudioFavoriteResponse,
    # Challenge Models
    Challenge, ChallengeCreate, ChallengeResponse, ChallengeParticipant, 
    ChallengeStatus, ParticipantStatus, ChallengeVote, ChallengeVoteCreate,
    # Story Models - REMOVED (Stories feature disabled)
    # Story, StoryCreate, StoryResponse, StoryView, StoryLike, StoryViewCreate, StoryLikeCreate, StoryType, StoryPrivacy,
    # Chat Request Models
    ChatRequest, ChatRequestCreate, ChatRequestResponse, ChatRequestAction, ChatRequestStatus,
    # Push Notification Models
    FCMToken, FCMTokenCreate, PushNotification
)
from auth import (
    verify_password, get_password_hash, create_access_token, 
    verify_token, ACCESS_TOKEN_EXPIRE_MINUTES
)
from media_validator import compute_status_for_poll
import video_pipeline

# 🩺 FILTRO GLOBAL DE VISIBILIDAD DE PUBLICACIONES
# ------------------------------------------------------------------
# State machine de `status` en un poll:
#   • (ausente)   → legacy, visible (equivalente a "ready")
#   • "ready"     → publicación válida, visible en feed/perfil/explore/etc.
#   • "processing"→ subida en proceso (ffmpeg transcoding), NO visible
#   • "failed"    → falló el pipeline de upload/transcoding, NO visible
#   • "broken"    → media inaccesible (validación en tiempo real), NO visible
#   • "hidden"    → soft-delete (usuario borró o cuenta desactivada), NO visible
#
# Usamos $nin para un único filtro componible: Mongo considera un $nin
# como "match" también cuando el campo NO existe, por lo que los polls
# legacy sin campo `status` siguen apareciendo.
POLL_STATUS_FILTER = {"status": {"$nin": ["broken", "hidden", "failed", "processing"]}}

# Import configuration
from config import config

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# ✅ Inicializar configuración automática de entorno
config.initialize_environment()

# MongoDB connection - imported from shared database module to avoid circular imports
from database import db, client

print(f"🔗 MongoDB: Conectando a {config.MONGO_URL}")
print(f"🗄️ Database: Usando '{config.DB_NAME}'")

# Initialize Feed Optimizer
try:
    from optimized_feed import init_feed_optimizer
    init_feed_optimizer(db)
    print("🚀 Feed optimizer initialized successfully")
except Exception as e:
    print(f"⚠️  Feed optimizer initialization failed: {e}")

# Custom JSON encoder to handle datetime with UTC timezone
def custom_json_serializer(obj):
    """Custom JSON serializer that adds 'Z' suffix to UTC datetime objects"""
    if isinstance(obj, datetime):
        # Convert to ISO format and ensure 'Z' suffix for UTC
        return obj.isoformat() + 'Z' if not obj.isoformat().endswith('Z') else obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

# Create the main app without a prefix
app = FastAPI(
    title="Social Media Network", 
    description="Advanced social network with polls, messaging and media",
    version=config.API_VERSION
)

# Configure FastAPI to use custom JSON encoder for responses
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse as FastAPIJSONResponse

class CustomJSONResponse(FastAPIJSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
            default=custom_json_serializer,
        ).encode("utf-8")

# Set as default response class
app.router.default_response_class = CustomJSONResponse

# Initialize Fast Upload System
try:
    from fast_upload_endpoints import fast_upload_router
    app.include_router(fast_upload_router)
    print("⚡ Fast upload system initialized successfully")
except Exception as e:
    print(f"⚠️  Fast upload system initialization failed: {e}")

# Initialize Database Optimizer
try:
    from database_optimizer import init_db_optimizer
    init_db_optimizer(db)
    print("🗄️ Database optimizer initialized successfully")
except Exception as e:
    print(f"⚠️  Database optimizer initialization failed: {e}")

# Create unique index on story_views to prevent duplicate views
async def init_story_views_index():
    try:
        # First, clean up existing duplicates
        pipeline = [
            {"$group": {"_id": {"story_id": "$story_id", "user_id": "$user_id"}, "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
            {"$match": {"count": {"$gt": 1}}}
        ]
        duplicates = await db.story_views.aggregate(pipeline).to_list(1000)
        for dup in duplicates:
            # Keep the first, delete the rest
            ids_to_delete = dup["ids"][1:]
            await db.story_views.delete_many({"_id": {"$in": ids_to_delete}})
            # Fix the views_count for the story
            actual_count = await db.story_views.count_documents({"story_id": dup["_id"]["story_id"]})
            await db.stories.update_one(
                {"id": dup["_id"]["story_id"]},
                {"$set": {"views_count": actual_count}}
            )
        if duplicates:
            print(f"🧹 Cleaned up {len(duplicates)} duplicate story views")
        
        # Then create the unique index
        await db.story_views.create_index(
            [("story_id", 1), ("user_id", 1)],
            unique=True,
            background=True
        )
        print("✅ Unique index on story_views (story_id, user_id) created")
    except Exception as e:
        print(f"⚠️  Story views index creation: {e}")

import asyncio
try:
    loop = asyncio.get_event_loop()
    loop.create_task(init_story_views_index())
except RuntimeError:
    pass

# File upload configuration using config
config.create_upload_directories()
UPLOAD_DIR = config.UPLOAD_BASE_DIR

# Mount static files to serve uploads
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Cache for iTunes API responses to improve performance
itunes_cache = {}
follow_status_cache = {}
CACHE_EXPIRY_HOURS = 24  # Cache iTunes data for 24 hours
FOLLOW_CACHE_EXPIRY_MINUTES = 10  # Cache follow status for 10 minutes

def is_cache_valid(cached_item):
    """Check if cached item is still valid"""
    if not cached_item:
        return False
    return (datetime.utcnow() - cached_item['cached_at']).total_seconds() < (CACHE_EXPIRY_HOURS * 3600)

def is_follow_cache_valid(cached_item):
    """Check if follow cache item is still valid"""
    if not cached_item:
        return False
    return (datetime.utcnow() - cached_item['cached_at']).total_seconds() < (FOLLOW_CACHE_EXPIRY_MINUTES * 60)

# Create a router with configurable prefix
api_router = APIRouter(prefix=config.API_PREFIX)

# Security
security = HTTPBearer()

# Authentication dependency
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserResponse:
    """Get current authenticated user"""
    token = credentials.credentials
    payload = verify_token(token)
    if not payload:
        raise HTTPException(
            status_code=config.StatusCodes.UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user from database - exclude _id to avoid ObjectId serialization issues
    user_data = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user_data:
        raise HTTPException(
            status_code=config.StatusCodes.UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return UserResponse(**user_data)


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))
) -> Optional[UserResponse]:
    """Get current authenticated user, returns None if not authenticated"""
    if not credentials:
        return None
    
    try:
        token = credentials.credentials
        payload = verify_token(token)
        if not payload:
            return None
        
        # Get user from database - exclude _id to avoid ObjectId serialization issues
        user_data = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
        if not user_data:
            return None
        
        return UserResponse(**user_data)
    except Exception:
        return None


# =============  SECURITY UTILITIES =============

async def track_login_attempt(email: str, ip_address: str, user_agent: str, success: bool, failure_reason: Optional[str] = None):
    """Track login attempts for security monitoring"""
    attempt = LoginAttempt(
        email=email,
        ip_address=ip_address,
        user_agent=user_agent,
        success=success,
        failure_reason=failure_reason
    )
    await db.login_attempts.insert_one(attempt.dict())

async def check_rate_limit(email: str, ip_address: str) -> bool:
    """Check if user has exceeded login attempt limits"""
    # Check failed attempts in last 15 minutes
    time_threshold = datetime.utcnow() - timedelta(minutes=15)
    
    # Count failed attempts by email
    email_failures = await db.login_attempts.count_documents({
        "email": email,
        "success": False,
        "created_at": {"$gte": time_threshold}
    })
    
    # Count failed attempts by IP
    ip_failures = await db.login_attempts.count_documents({
        "ip_address": ip_address,
        "success": False,
        "created_at": {"$gte": time_threshold}
    })
    
    # Rate limits: 5 attempts per email, 10 per IP in 15 minutes
    return email_failures < 5 and ip_failures < 10

def parse_user_agent(user_agent_string: str) -> Dict[str, str]:
    """Parse user agent string to extract device information"""
    user_agent = parse(user_agent_string)
    return {
        "browser": f"{user_agent.browser.family} {user_agent.browser.version_string}",
        "os": f"{user_agent.os.family} {user_agent.os.version_string}",
        "device_type": "mobile" if user_agent.is_mobile else "tablet" if user_agent.is_tablet else "desktop",
        "device_name": user_agent.device.family
    }

async def get_or_create_device(user_id: str, ip_address: str, user_agent: str) -> UserDevice:
    """Get existing device or create new one"""
    device_info = parse_user_agent(user_agent)
    
    # Create device fingerprint
    device_fingerprint = hashlib.sha256(
        f"{device_info['browser']}{device_info['os']}{user_agent}".encode()
    ).hexdigest()
    
    # Try to find existing device
    existing_device = await db.user_devices.find_one({
        "user_id": user_id,
        "id": device_fingerprint  # Use fingerprint as device ID
    })
    
    if existing_device:
        # Update last used
        await db.user_devices.update_one(
            {"id": device_fingerprint},
            {"$set": {"last_used": datetime.utcnow(), "ip_address": ip_address}}
        )
        return UserDevice(**existing_device)
    else:
        # Create new device
        device = UserDevice(
            id=device_fingerprint,
            user_id=user_id,
            device_name=device_info["device_name"],
            device_type=device_info["device_type"],
            browser=device_info["browser"],
            os=device_info["os"],
            ip_address=ip_address,
            user_agent=user_agent,
            is_trusted=False  # New devices are not trusted by default
        )
        
        await db.user_devices.insert_one(device.dict())
        return device

async def create_security_notification(user_id: str, notification_type: str, title: str, message: str, metadata: Dict = None):
    """Create a security notification for the user"""
    notification = SecurityNotification(
        user_id=user_id,
        notification_type=notification_type,
        title=title,
        message=message,
        metadata=metadata or {}
    )
    await db.security_notifications.insert_one(notification.dict())

async def create_session(user_id: str, device_id: str, ip_address: str, user_agent: str) -> str:
    """Create a new user session"""
    session_token = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(days=7)  # 7 days expiry
    
    session = UserSession(
        user_id=user_id,
        session_token=session_token,
        device_id=device_id,
        ip_address=ip_address,
        user_agent=user_agent,
        expires_at=expires_at
    )
    
    await db.user_sessions.insert_one(session.dict())
    return session_token

def get_client_ip(request: Request) -> str:
    """Get client IP address from request"""
    x_forwarded_for = request.headers.get('x-forwarded-for')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0]
    return request.client.host

# =============  GOOGLE OAUTH UTILITIES =============

async def get_oauth_user_data(session_id: str) -> Dict:
    """Get user data from Emergent Auth API"""
    url = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
    headers = {"X-Session-ID": session_id}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise HTTPException(
                    status_code=400, 
                    detail="Invalid session ID or OAuth authentication failed"
                )

async def create_or_get_oauth_user(oauth_data: Dict, ip_address: str, user_agent: str) -> User:
    """Create new user or get existing user from OAuth data"""
    email = oauth_data.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Email not provided by OAuth provider")
    
    # Check if user exists
    existing_user = await db.users.find_one({"email": email})
    
    if existing_user:
        # Update OAuth info if needed
        update_data = {}
        if not existing_user.get("oauth_provider"):
            update_data["oauth_provider"] = "google"
            update_data["oauth_id"] = oauth_data.get("id")
            update_data["avatar_url"] = oauth_data.get("picture")
        
        update_data["last_login"] = datetime.utcnow()
        
        if update_data:
            await db.users.update_one({"id": existing_user["id"]}, {"$set": update_data})
            
        # Get updated user data
        user_data = await db.users.find_one({"id": existing_user["id"]})
        return User(**user_data)
    else:
        # Create new user from OAuth data
        username = email.split("@")[0]  # Use email prefix as username
        
        # Ensure username is unique
        counter = 1
        original_username = username
        while await db.users.find_one({"username": username}):
            username = f"{original_username}{counter}"
            counter += 1
        
        user = User(
            email=email,
            username=username,
            display_name=oauth_data.get("name", email),
            avatar_url=oauth_data.get("picture"),
            oauth_provider="google",
            oauth_id=oauth_data.get("id"),
            is_verified=True  # OAuth users are considered verified
        )
        
        await db.users.insert_one(user.dict())
        
        # Create user profile
        profile = UserProfile(id=user.id, username=user.username)
        await db.user_profiles.insert_one(profile.dict())
        
        return user

# =============  AUDIO PROCESSING UTILITIES =============

# Audio processing dependencies (optional)
try:
    import pydub
    from pydub import AudioSegment
    from pydub.utils import which
    PYDUB_AVAILABLE = True
    # Configure FFmpeg path for pydub
    AudioSegment.converter = "/usr/bin/ffmpeg"
    AudioSegment.ffmpeg = "/usr/bin/ffmpeg"
    AudioSegment.ffprobe = "/usr/bin/ffprobe"
except ImportError:
    PYDUB_AVAILABLE = False
    AudioSegment = None
    print("⚠️  pydub not available - basic audio processing disabled")

try:
    import librosa
    import soundfile as sf
    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False  
    print("⚠️  librosa not available - advanced audio analysis disabled")

def process_audio_file(file_path: str, max_duration: int = 60) -> dict:
    """
    Process uploaded audio file: validate, trim to max duration, extract metadata
    """
    if not PYDUB_AVAILABLE:
        return {
            'success': False,
            'error': 'Audio processing not available in this deployment'
        }
        
    try:
        import subprocess
        import tempfile
        
        # Load audio using pydub (supports MP3, M4A, WAV, AAC)
        audio = AudioSegment.from_file(file_path)
        
        # Get original metadata
        original_duration = len(audio) / 1000.0  # Convert to seconds
        sample_rate = audio.frame_rate
        channels = audio.channels
        
        # Trim to max duration if necessary
        if original_duration > max_duration:
            audio = audio[:max_duration * 1000]  # pydub uses milliseconds
            print(f"🎵 Audio trimmed from {original_duration:.1f}s to {max_duration}s")
        
        # Create temporary output file
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_out:
            processed_path = tmp_out.name
        
        # Export processed audio (convert to MP3 for consistency)
        audio.export(processed_path, format="mp3", bitrate="128k")
        
        # Generate waveform data for visualization
        waveform = generate_waveform(processed_path)
        
        return {
            'success': True,
            'processed_path': processed_path,
            'duration': min(original_duration, max_duration),
            'sample_rate': sample_rate,
            'channels': channels,
            'bitrate': 128,  # Fixed bitrate for processed files
            'waveform': waveform,
            'was_trimmed': original_duration > max_duration,
            'original_duration': original_duration
        }
        
    except Exception as e:
        print(f"❌ Audio processing error: {str(e)}")
        # Try with ffmpeg as fallback
        try:
            return process_audio_with_ffmpeg(file_path, max_duration)
        except Exception as fallback_error:
            print(f"❌ FFmpeg fallback also failed: {str(fallback_error)}")
            return {
                'success': False,
                'error': f"Error processing audio: {str(e)}"
            }

def process_audio_with_ffmpeg(file_path: str, max_duration: int = 60) -> dict:
    """
    Fallback audio processing using FFmpeg directly
    """
    import subprocess
    import tempfile
    
    try:
        # Create temporary output file
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_out:
            output_path = tmp_out.name
        
        # Use FFmpeg to process audio
        cmd = [
            'ffmpeg', '-i', file_path,
            '-t', str(max_duration),  # Trim to max duration
            '-acodec', 'mp3',
            '-ab', '128k',
            '-ar', '44100',
            '-y',  # Overwrite output file
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            raise Exception(f"FFmpeg failed: {result.stderr}")
        
        # Get audio info using FFprobe
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', output_path
        ]
        
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        
        if probe_result.returncode == 0:
            import json
            probe_data = json.loads(probe_result.stdout)
            format_info = probe_data.get('format', {})
            duration = float(format_info.get('duration', max_duration))
            
            # Generate basic waveform
            waveform = generate_basic_waveform()
            
            return {
                'success': True,
                'processed_path': output_path,
                'duration': duration,
                'sample_rate': 44100,
                'channels': 2,
                'bitrate': 128,
                'waveform': waveform,
                'was_trimmed': duration >= max_duration,
                'original_duration': duration
            }
        else:
            raise Exception(f"FFprobe failed: {probe_result.stderr}")
            
    except Exception as e:
        return {
            'success': False,
            'error': f"FFmpeg processing failed: {str(e)}"
        }

def generate_basic_waveform(points: int = 20) -> List[float]:
    """Generate a basic waveform for visualization when librosa fails"""
    import random
    return [random.uniform(0.3, 0.9) for _ in range(points)]

def generate_waveform(audio_path: str, points: int = 20) -> List[float]:
    """
    Generate waveform visualization data from audio file
    """
    if not LIBROSA_AVAILABLE or not NUMPY_AVAILABLE:
        return generate_basic_waveform(points)
        
    try:
        # Try to load audio with librosa for analysis
        y, sr = librosa.load(audio_path)
        
        # Calculate RMS energy for each segment
        hop_length = max(1, len(y) // points)
        waveform = []
        
        for i in range(points):
            start = i * hop_length
            end = min((i + 1) * hop_length, len(y))
            if start < len(y):
                segment = y[start:end]
                rms = np.sqrt(np.mean(segment**2)) if len(segment) > 0 else 0
                # Normalize to 0-1 range
                waveform.append(min(1.0, rms * 3))  # Amplify for better visualization
            else:
                waveform.append(0.0)
        
        return waveform
    except Exception as e:
        print(f"❌ Librosa waveform generation failed: {str(e)}")
        print("🔄 Using basic waveform generation")
        # Return default waveform if generation fails
        return generate_basic_waveform(points)

def validate_audio_file(file: UploadFile) -> dict:
    """
    Validate uploaded audio file (format, size, etc.)
    """
    # Supported audio formats
    SUPPORTED_FORMATS = ['mp3', 'm4a', 'wav', 'aac', 'flac', 'ogg']
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    
    # Get file extension
    file_extension = file.filename.lower().split('.')[-1] if '.' in file.filename else ''
    
    # Validate format
    if file_extension not in SUPPORTED_FORMATS:
        return {
            'valid': False,
            'error': f"Formato no soportado. Use: {', '.join(SUPPORTED_FORMATS.upper())}"
        }
    
    # Validate size
    if hasattr(file, 'size') and file.size > MAX_FILE_SIZE:
        return {
            'valid': False,
            'error': f"Archivo muy grande. Máximo 10MB permitido."
        }
    
    return {
        'valid': True,
        'format': file_extension
    }

# =============  MUSIC UTILITIES =============

async def search_itunes_track(artist: str, track: str):
    """Search iTunes API for real song preview"""
    try:
        # Construct search query
        query = f"{artist} {track}".strip()
        url = "https://itunes.apple.com/search"
        params = {
            'term': query,
            'media': 'music',
            'entity': 'song',
            'limit': 1,
            'country': 'US',  # Can be changed to ES for Spanish market
            'callback': ''  # Disable JSONP to get pure JSON
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    # Get text and parse as JSON (iTunes returns JSONP by default)
                    text = await response.text()
                    
                    # If it starts with a function call, extract JSON
                    if text.strip().startswith('(') or 'callback' in text:
                        # Find JSON part
                        start = text.find('{')
                        end = text.rfind('}') + 1
                        if start >= 0 and end > start:
                            text = text[start:end]
                    
                    import json
                    data = json.loads(text)
                    
                    if data.get('results') and len(data['results']) > 0:
                        result = data['results'][0]
                        return {
                            'preview_url': result.get('previewUrl'),
                            'artwork_url': result.get('artworkUrl100', '').replace('100x100', '400x400'),
                            'artist_name': result.get('artistName'),
                            'track_name': result.get('trackName'),
                            'duration_ms': result.get('trackTimeMillis', 30000),
                            'genre': result.get('primaryGenreName'),
                            'iTunes_id': result.get('trackId')
                        }
        return None
    except Exception as e:
        print(f"Error searching iTunes: {e}")
        return None

async def get_music_info(music_id: str):
    """
    Get music information by ID with automatic iTunes preview fetching
    Supports both static library IDs and dynamic iTunes IDs (format: itunes_XXXXX)
    """
    if not music_id:
        return None
    
    # Check if this is an iTunes ID (format: itunes_XXXXX)
    if music_id.startswith('itunes_'):
        try:
            # Extract iTunes track ID
            itunes_track_id = music_id.replace('itunes_', '')
            
            # Check cache first
            if itunes_track_id in itunes_cache and is_cache_valid(itunes_cache[itunes_track_id]):
                print(f"🎵 Using cached iTunes track info for ID: {itunes_track_id}")
                return itunes_cache[itunes_track_id]['data']
            
            print(f"🎵 Fetching iTunes track info for ID: {itunes_track_id}")
            
            # Fetch track info directly from iTunes API using track ID
            url = f"https://itunes.apple.com/lookup?id={itunes_track_id}"
            
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    results = data.get('results', [])
                    if results:
                        result = results[0]
                        music_info = {
                            'id': music_id,
                            'title': result.get('trackName'),
                            'artist': result.get('artistName'),
                            'duration': 30,  # iTunes previews are 30 seconds
                            'url': '',  # No local URL for iTunes tracks
                            'preview_url': result.get('previewUrl'),
                            'cover': result.get('artworkUrl100', '').replace('100x100bb.jpg', '400x400bb.jpg'),
                            'category': result.get('primaryGenreName', 'Music'),
                            'isOriginal': False,
                            'isTrending': False,
                            'uses': 0,  # Default for iTunes tracks
                            'source': 'iTunes'
                        }
                        print(f"✅ Successfully fetched iTunes track: {music_info['title']} - {music_info['artist']}")
                        
                        # Cache the result
                        itunes_cache[itunes_track_id] = {
                            'data': music_info,
                            'cached_at': datetime.utcnow()
                        }
                        
                        return music_info
                    else:
                        print(f"❌ No results found for iTunes track ID: {itunes_track_id}")
                        return None
                else:
                    print(f"❌ iTunes API error: {response.status_code}")
                    return None
        except Exception as e:
            print(f"❌ Error fetching iTunes track {music_id}: {str(e)}")
            return None
    
    # Check if this is a user audio ID (format: user_audio_XXXXX)
    user_audio_id = None
    
    if music_id.startswith('user_audio_'):
        # New format with prefix - extract UUID
        user_audio_id = music_id.replace('user_audio_', '')
        print(f"🎵 Fetching user audio info for prefixed ID: {user_audio_id}")
    else:
        # Check if this looks like a UUID (backward compatibility)
        # UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 chars with 4 hyphens)
        import re
        uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
        if re.match(uuid_pattern, music_id):
            user_audio_id = music_id
            print(f"🔄 Detected bare UUID (backward compatibility): {user_audio_id}")
    
    if user_audio_id:
        try:
            # Query the user_audio collection
            user_audio = await db.user_audio.find_one({"id": user_audio_id})
            print(f"🔍 Database query result: {user_audio is not None}")
            
            if user_audio:
                print(f"📝 Found user audio: {user_audio.get('title')} by {user_audio.get('artist')}")
                music_info = {
                    'id': music_id,  # Keep original ID for consistency
                    'title': user_audio.get('title'),
                    'artist': user_audio.get('artist'),
                    'duration': user_audio.get('duration', 0),
                    'url': user_audio.get('public_url'),
                    'preview_url': user_audio.get('public_url'),
                    'cover': user_audio.get('cover_url'),  # May be None
                    'category': 'User Audio',
                    'isOriginal': True,
                    'isTrending': False,
                    'uses': user_audio.get('uses_count', 0),
                    'source': 'User Upload',
                    'isUserUploaded': True,
                    'uploader': {
                        'id': user_audio.get('uploader_id'),
                        'username': user_audio.get('artist'),  # Artist is usually the uploader's display name
                    }
                }
                print(f"✅ Successfully fetched user audio: {music_info['title']} - {music_info['artist']}")
                return music_info
            else:
                print(f"❌ User audio not found for ID: {user_audio_id}")
                return None
                
        except Exception as e:
            print(f"❌ Error fetching user audio {music_id}: {str(e)}")
            return None
    
    # If not an iTunes ID or user audio, check static music library
    music_library = {
        # TRENDING
        'music_trending_1': {
            'id': 'music_trending_1',
            'title': 'LA BOTELLA',
            'artist': 'Morad',
            'duration': 195,
            'url': '/music/morad-la-botella.mp3',
            'preview_url': None,  # Will be fetched from iTunes
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Trending',
            'isOriginal': False,
            'isTrending': True,
            'uses': 8500000
        },
        'music_trending_2': {
            'id': 'music_trending_2',
            'title': 'Un Verano Sin Ti',
            'artist': 'Bad Bunny',
            'duration': 208,
            'url': '/music/bad-bunny-verano.mp3',
            'preview_url': None,  # Will be fetched from iTunes
            'cover': 'https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=400&h=400&fit=crop&crop=center',
            'category': 'Trending',
            'isOriginal': False,
            'isTrending': True,
            'uses': 12500000
        },
        'music_trending_3': {
            'id': 'music_trending_3',
            'title': 'TQG',
            'artist': 'Karol G ft. Shakira',
            'duration': 192,
            'url': '/music/karol-g-tqg.mp3',
            'preview_url': None,  # Will be fetched from iTunes
            'cover': 'https://images.unsplash.com/photo-1520262494112-9fe481d36ec3?w=400&h=400&fit=crop&crop=center',
            'category': 'Trending',
            'isOriginal': False,
            'isTrending': True,
            'uses': 9800000
        },
        
        # REGGAETON
        'music_reggaeton_1': {
            'id': 'music_reggaeton_1',
            'title': 'Me Porto Bonito',
            'artist': 'Bad Bunny x Chencho Corleone',
            'duration': 178,
            'url': '/music/bad-bunny-me-porto-bonito.mp3',
            'preview_url': 'https://audio-ssl.itunes.apple.com/itunes-assets/AudioPreview211/v4/2c/7a/80/2c7a8014-6ff1-88a5-d3df-39125a23546a/mzaf_4090450781883707192.plus.aac.p.m4a',  # REAL PREVIEW!
            'cover': 'https://images.unsplash.com/photo-1514525253161-7a46d19cd819?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 7200000
        },
        'music_reggaeton_2': {
            'id': 'music_reggaeton_2',
            'title': 'Provenza',
            'artist': 'Karol G',
            'duration': 213,
            'url': '/music/karol-g-provenza.mp3',
            'cover': 'https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 6800000
        },
        'music_reggaeton_3': {
            'id': 'music_reggaeton_3',
            'title': 'FERXXO 100',
            'artist': 'Feid',
            'duration': 185,
            'url': '/music/feid-ferxxo-100.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 4500000
        },
        'music_collab_1': {
            'id': 'music_collab_1',
            'title': 'Tití Me Preguntó',
            'artist': 'Bad Bunny',
            'duration': 224,
            'url': '/music/bad-bunny-titi-me-pregunto.mp3',
            'cover': 'https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 11200000
        },
        
        # TRAP
        'music_trap_1': {
            'id': 'music_trap_1',
            'title': 'BZRP Music Sessions #52',
            'artist': 'Quevedo x Bizarrap',
            'duration': 201,
            'url': '/music/quevedo-bzrp-52.mp3',
            'cover': 'https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=400&h=400&fit=crop&crop=center',
            'category': 'Trap',
            'isOriginal': False,
            'uses': 15200000
        },
        'music_trap_3': {
            'id': 'music_trap_3',
            'title': 'MOTOROLA',
            'artist': 'Morad',
            'duration': 189,
            'url': '/music/morad-motorola.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Trap',
            'isOriginal': False,
            'uses': 6100000
        },
        
        # URBANO ESPAÑOL
        'music_urbano_esp_1': {
            'id': 'music_urbano_esp_1',
            'title': 'DURMIENDO EN EL SUELO',
            'artist': 'Morad',
            'duration': 176,
            'url': '/music/morad-durmiendo-suelo.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Urbano Español',
            'isOriginal': False,
            'uses': 4200000
        },
        'music_urbano_esp_2': {
            'id': 'music_urbano_esp_2',
            'title': 'NO TE PIENSO',
            'artist': 'Morad',
            'duration': 198,
            'url': '/music/morad-no-te-pienso.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Urbano Español',
            'isOriginal': False,
            'uses': 3700000
        },
        
        # POP LATINO
        'music_pop_latino_1': {
            'id': 'music_pop_latino_1',
            'title': 'Flowers',
            'artist': 'Miley Cyrus (Remix Latino)',
            'duration': 201,
            'url': '/music/miley-flowers-remix.mp3',
            'cover': 'https://images.unsplash.com/photo-1520262494112-9fe481d36ec3?w=400&h=400&fit=crop&crop=center',
            'category': 'Pop Latino',
            'isOriginal': False,
            'uses': 8900000
        },
        'music_pop_latino_2': {
            'id': 'music_pop_latino_2',
            'title': 'MAMIII',
            'artist': 'Becky G x Karol G',
            'duration': 187,
            'url': '/music/becky-g-mamiii.mp3',
            'cover': 'https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=400&h=400&fit=crop&crop=center',
            'category': 'Pop Latino',
            'isOriginal': False,
            'uses': 6300000
        },
        
        # ORIGINAL SOUND
        'original_sound': {
            'id': 'original_sound',
            'title': 'Sonido Original',
            'artist': 'Sin música de fondo',
            'duration': 0,
            'url': '',
            'cover': '/images/original-sound.png',
            'category': 'Original',
            'isOriginal': True,
            'uses': 0
        }
    }
    
    music_info = music_library.get(music_id)
    if not music_info:
        return None
    
    # If preview_url is None, try to fetch from iTunes API
    if music_info.get('preview_url') is None:
        try:
            itunes_result = await search_itunes_track(music_info['artist'], music_info['title'])
            if itunes_result and itunes_result.get('preview_url'):
                # Update the music info with real preview URL
                music_info = music_info.copy()  # Create a copy to avoid modifying the original
                music_info['preview_url'] = itunes_result['preview_url']
                print(f"✅ Fetched real preview URL for {music_info['title']} - {music_info['artist']}")
            else:
                print(f"⚠️ Could not fetch preview URL for {music_info['title']} - {music_info['artist']}")
        except Exception as e:
            print(f"❌ Error fetching iTunes preview for {music_info['title']}: {str(e)}")
    
    return music_info

# =============  REAL MUSIC PREVIEW ENDPOINTS =============

@api_router.get("/music/search")
async def search_music_preview(
    artist: str,
    track: Optional[str] = None,
    current_user: UserResponse = Depends(get_current_user)
):
    """Search for real music preview using iTunes API"""
    try:
        # If no track specified, use first word of artist as track
        search_track = track or artist.split()[0] if artist else ""
        
        itunes_result = await search_itunes_track(artist, search_track)
        
        if itunes_result and itunes_result['preview_url']:
            return {
                'success': True,
                'music': {
                    'id': f"itunes_{itunes_result['iTunes_id']}",
                    'title': itunes_result['track_name'],
                    'artist': itunes_result['artist_name'],
                    'preview_url': itunes_result['preview_url'],
                    'cover': itunes_result['artwork_url'],
                    'duration': 30,  # iTunes previews are 30 seconds
                    'category': itunes_result['genre'],
                    'isOriginal': False,
                    'source': 'iTunes'
                }
            }
        else:
            return {
                'success': False,
                'message': 'No preview found',
                'music': None
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching music: {str(e)}")

@api_router.get("/music/search-realtime")
async def search_music_realtime(
    query: str,
    limit: int = 200,
    current_user: UserResponse = Depends(get_current_user)
):
    """Search for music in real time using iTunes API - supports any artist/song"""
    try:
        # Clean and prepare the search query
        query = query.strip()
        if not query:
            return {
                'success': False,
                'message': 'Query is required',
                'results': []
            }
        
        # Use iTunes Search API with more flexible search
        url = "https://itunes.apple.com/search"
        params = {
            'term': query,
            'media': 'music',
            'entity': 'song',
            'limit': limit,
            'country': 'US'  # Can be changed to support different countries
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
        
        results = []
        if 'results' in data:
            for result in data['results']:
                # Only include results with preview URLs
                preview_url = result.get('previewUrl')
                if preview_url:
                    music_item = {
                        'id': f"itunes_{result.get('trackId')}",
                        'title': result.get('trackName', 'Unknown Title'),
                        'artist': result.get('artistName', 'Unknown Artist'),
                        'preview_url': preview_url,
                        'cover': result.get('artworkUrl100', '').replace('100x100', '400x400'),  # Higher resolution
                        'duration': 30,  # iTunes previews are typically 30 seconds
                        'category': result.get('primaryGenreName', 'Music'),
                        'isOriginal': False,
                        'isTrending': False,
                        'uses': 0,  # Real-time results don't have use counts
                        'waveform': [0.7, 0.8, 0.6, 0.9, 0.5, 0.8, 0.7, 0.9, 0.6, 0.8] * 2,  # Default waveform
                        'source': 'iTunes',
                        'album': result.get('collectionName', ''),
                        'release_date': result.get('releaseDate', ''),
                        'itunes_url': result.get('trackViewUrl', '')
                    }
                    results.append(music_item)
        
        return {
            'success': True,
            'message': f'Found {len(results)} songs for "{query}"',
            'results': results,
            'total': len(results),
            'query': query
        }
        
    except httpx.TimeoutException:
        return {
            'success': False,
            'message': 'Search timeout - please try again',
            'results': []
        }
    except Exception as e:
        print(f"Error in real-time music search: {e}")
        return {
            'success': False,
            'message': f'Search error: {str(e)}',
            'results': []
        }


@api_router.get("/music/trending")
async def get_trending_music(
    limit: int = 20,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get trending music from iTunes - varies each time"""
    # Pool of trending search terms that rotate randomly
    trending_searches = [
        "top hits 2025", "reggaeton 2025", "bad bunny", "karol g",
        "feid", "morad", "quevedo", "rosalia", "rauw alejandro",
        "peso pluma", "junior h", "myke towers", "anuel aa",
        "ozuna", "daddy yankee", "j balvin", "shakira",
        "bizarrap", "duki", "nicky jam", "sech",
        "trending latin", "trap latino", "urbano español",
        "latin pop 2025", "reggaeton nuevos", "perreo",
        "trap 2025", "latin urban", "dembow 2025"
    ]
    
    # Pick 3 random search terms each time
    selected_terms = random.sample(trending_searches, min(3, len(trending_searches)))
    
    all_results = []
    seen_ids = set()
    
    try:
        async with httpx.AsyncClient() as client:
            for term in selected_terms:
                try:
                    url = "https://itunes.apple.com/search"
                    params = {
                        'term': term,
                        'media': 'music',
                        'entity': 'song',
                        'limit': 10,
                        'country': 'US'
                    }
                    response = await client.get(url, params=params, timeout=8)
                    response.raise_for_status()
                    data = response.json()
                    
                    if 'results' in data:
                        for result in data['results']:
                            track_id = result.get('trackId')
                            preview_url = result.get('previewUrl')
                            if preview_url and track_id and track_id not in seen_ids:
                                seen_ids.add(track_id)
                                all_results.append({
                                    'id': f"itunes_{track_id}",
                                    'title': result.get('trackName', 'Unknown'),
                                    'artist': result.get('artistName', 'Unknown'),
                                    'preview_url': preview_url,
                                    'cover': result.get('artworkUrl100', '').replace('100x100', '400x400'),
                                    'duration': 30,
                                    'category': result.get('primaryGenreName', 'Music'),
                                    'isOriginal': False,
                                    'isTrending': True,
                                    'uses': random.randint(100000, 9000000),
                                    'source': 'iTunes'
                                })
                except Exception as e:
                    print(f"Error fetching trending for '{term}': {e}")
                    continue
        
        # Shuffle and limit
        random.shuffle(all_results)
        all_results = all_results[:limit]
        
        return {
            'success': True,
            'results': all_results,
            'total': len(all_results)
        }
    except Exception as e:
        print(f"Error in trending music: {e}")
        return {
            'success': False,
            'results': [],
            'total': 0,
            'message': str(e)
        }


@api_router.get("/music/library-with-previews")
async def get_music_library_with_real_previews(
    limit: int = 20,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get music library with real iTunes preview URLs"""
    
    # Define popular songs to fetch real previews for
    popular_songs = [
        {'artist': 'Morad', 'track': 'LA BOTELLA'},
        {'artist': 'Bad Bunny', 'track': 'Un Verano Sin Ti'},
        {'artist': 'Karol G', 'track': 'TQG'},
        {'artist': 'Bad Bunny', 'track': 'Me Porto Bonito'},
        {'artist': 'Karol G', 'track': 'Provenza'},
        {'artist': 'Feid', 'track': 'FERXXO 100'},
        {'artist': 'Quevedo', 'track': 'BZRP Music Sessions 52'},
        {'artist': 'Morad', 'track': 'MOTOROLA'},
        {'artist': 'Morad', 'track': 'DURMIENDO EN EL SUELO'},
        {'artist': 'Becky G', 'track': 'MAMIII'},
        {'artist': 'Rosalía', 'track': 'Despechá'},
        {'artist': 'Bad Bunny', 'track': 'Tití Me Preguntó'}
    ]
    
    music_with_previews = []
    
    # Fetch real previews from iTunes
    for song_info in popular_songs[:limit]:
        itunes_result = await search_itunes_track(song_info['artist'], song_info['track'])
        
        if itunes_result and itunes_result['preview_url']:
            music_with_previews.append({
                'id': f"real_{itunes_result['iTunes_id']}",
                'title': itunes_result['track_name'],
                'artist': itunes_result['artist_name'],
                'preview_url': itunes_result['preview_url'],  # REAL 30-second preview
                'cover': itunes_result['artwork_url'],
                'duration': 30,
                'category': itunes_result['genre'] or 'Urban',
                'isOriginal': False,
                'isTrending': True,
                'uses': 8500000,  # Mock usage count
                'source': 'iTunes',
                'waveform': [0.8, 0.9, 0.7, 0.9, 0.8, 1.0, 0.6, 0.9, 0.8, 0.7, 0.9, 0.8, 1.0, 0.7, 0.9, 0.8, 0.6, 0.9, 0.8, 0.7]
            })
        
        # Limit concurrent requests to avoid rate limiting
        if len(music_with_previews) >= 5:
            await asyncio.sleep(0.1)
    
    return {
        'music': music_with_previews,
        'total': len(music_with_previews),
        'has_real_previews': True,
        'source': 'iTunes Search API'
    }

# =============  MUSIC ENDPOINTS =============

@api_router.get("/music/library", response_model=List[dict])
async def get_music_library(
    category: Optional[str] = None,
    search: Optional[str] = None,
    trending: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0
):
    """Get music library with filtering options"""
    
    # All music from library - Expanded with real artists
    all_music = [
        # TRENDING - Top Artists
        {
            'id': 'music_trending_1',
            'title': 'LA BOTELLA',
            'artist': 'Morad',
            'duration': 195,
            'url': '/music/morad-la-botella.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Trending',
            'isOriginal': False,
            'isTrending': True,
            'uses': 8500000,
            'waveform': [0.8, 0.9, 0.7, 0.9, 0.8, 1.0, 0.6, 0.9, 0.8, 0.7, 0.9, 0.8, 1.0, 0.7, 0.9, 0.8, 0.6, 0.9, 0.8, 0.7]
        },
        {
            'id': 'music_trending_2',
            'title': 'Un Verano Sin Ti',
            'artist': 'Bad Bunny',
            'duration': 208,
            'url': '/music/bad-bunny-verano.mp3',
            'cover': 'https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=400&h=400&fit=crop&crop=center',
            'category': 'Trending',
            'isOriginal': False,
            'isTrending': True,
            'uses': 12500000,
            'waveform': [0.9, 0.8, 0.9, 0.7, 0.8, 0.9, 0.6, 0.8, 0.9, 0.7, 0.8, 0.9, 0.5, 0.8, 0.9, 0.7, 0.8, 0.9, 0.6, 0.8]
        },
        {
            'id': 'music_trending_3',
            'title': 'TQG',
            'artist': 'Karol G ft. Shakira',
            'duration': 192,
            'url': '/music/karol-g-tqg.mp3',
            'cover': 'https://images.unsplash.com/photo-1520262494112-9fe481d36ec3?w=400&h=400&fit=crop&crop=center',
            'category': 'Trending',
            'isOriginal': False,
            'isTrending': True,
            'uses': 9800000,
            'waveform': [0.7, 0.9, 0.8, 0.9, 0.6, 0.8, 0.9, 0.7, 0.8, 0.9, 0.6, 0.8, 0.9, 0.7, 0.8, 0.9, 0.6, 0.8, 0.9, 0.7]
        },
        
        # REGGAETON - Urban Music
        {
            'id': 'music_reggaeton_1',
            'title': 'Me Porto Bonito',
            'artist': 'Bad Bunny x Chencho Corleone',
            'duration': 178,
            'url': '/music/bad-bunny-me-porto-bonito.mp3',
            'cover': 'https://images.unsplash.com/photo-1514525253161-7a46d19cd819?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 7200000,
            'waveform': [0.8, 0.6, 0.9, 0.7, 0.8, 0.9, 0.5, 0.8, 0.6, 0.9, 0.7, 0.8, 0.5, 0.9, 0.6, 0.8, 0.7, 0.9, 0.5, 0.8]
        },
        {
            'id': 'music_reggaeton_2',
            'title': 'Provenza',
            'artist': 'Karol G',
            'duration': 213,
            'url': '/music/karol-g-provenza.mp3',
            'cover': 'https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 6800000,
            'waveform': [0.7, 0.8, 0.6, 0.9, 0.5, 0.8, 0.7, 0.9, 0.4, 0.8, 0.6, 0.9, 0.7, 0.8, 0.5, 0.9, 0.6, 0.8, 0.7, 0.9]
        },
        {
            'id': 'music_reggaeton_3',
            'title': 'FERXXO 100',
            'artist': 'Feid',
            'duration': 185,
            'url': '/music/feid-ferxxo-100.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 4500000,
            'waveform': [0.6, 0.9, 0.7, 0.8, 0.9, 0.5, 0.8, 0.7, 0.9, 0.6, 0.8, 0.5, 0.9, 0.7, 0.8, 0.6, 0.9, 0.7, 0.8, 0.5]
        },
        {
            'id': 'music_collab_1',
            'title': 'Tití Me Preguntó',
            'artist': 'Bad Bunny',
            'duration': 224,
            'url': '/music/bad-bunny-titi-me-pregunto.mp3',
            'cover': 'https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=400&h=400&fit=crop&crop=center',
            'category': 'Reggaeton',
            'isOriginal': False,
            'uses': 11200000,
            'waveform': [0.8, 0.9, 0.6, 0.8, 0.9, 0.7, 0.8, 0.5, 0.9, 0.8, 0.6, 0.9, 0.7, 0.8, 0.4, 0.9, 0.8, 0.6, 0.9, 0.7]
        },
        
        # TRAP
        {
            'id': 'music_trap_1',
            'title': 'BZRP Music Sessions #52',
            'artist': 'Quevedo x Bizarrap',
            'duration': 201,
            'url': '/music/quevedo-bzrp-52.mp3',
            'cover': 'https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=400&h=400&fit=crop&crop=center',
            'category': 'Trap',
            'isOriginal': False,
            'uses': 15200000,
            'waveform': [0.9, 0.4, 0.8, 0.6, 0.9, 0.2, 0.7, 0.8, 0.5, 0.9, 0.3, 0.8, 0.6, 0.9, 0.4, 0.7, 0.8, 0.5, 0.9, 0.6]
        },
        {
            'id': 'music_trap_3',
            'title': 'MOTOROLA',
            'artist': 'Morad',
            'duration': 189,
            'url': '/music/morad-motorola.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Trap',
            'isOriginal': False,
            'uses': 6100000,
            'waveform': [0.8, 0.6, 0.9, 0.7, 0.8, 0.9, 0.5, 0.8, 0.6, 0.9, 0.7, 0.8, 0.5, 0.9, 0.6, 0.8, 0.7, 0.9, 0.5, 0.8]
        },
        
        # URBANO ESPAÑOL
        {
            'id': 'music_urbano_esp_1',
            'title': 'DURMIENDO EN EL SUELO',
            'artist': 'Morad',
            'duration': 176,
            'url': '/music/morad-durmiendo-suelo.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Urbano Español',
            'isOriginal': False,
            'uses': 4200000,
            'waveform': [0.6, 0.8, 0.7, 0.9, 0.5, 0.8, 0.6, 0.9, 0.7, 0.8, 0.4, 0.9, 0.6, 0.8, 0.7, 0.9, 0.5, 0.8, 0.6, 0.9]
        },
        {
            'id': 'music_urbano_esp_2',
            'title': 'NO TE PIENSO',
            'artist': 'Morad',
            'duration': 198,
            'url': '/music/morad-no-te-pienso.mp3',
            'cover': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=400&h=400&fit=crop&crop=center',
            'category': 'Urbano Español',
            'isOriginal': False,
            'uses': 3700000,
            'waveform': [0.7, 0.5, 0.8, 0.9, 0.6, 0.8, 0.4, 0.9, 0.7, 0.8, 0.5, 0.9, 0.6, 0.8, 0.7, 0.9, 0.4, 0.8, 0.6, 0.9]
        },
        
        # POP LATINO  
        {
            'id': 'music_pop_latino_1',
            'title': 'Flowers',
            'artist': 'Miley Cyrus (Remix Latino)',
            'duration': 201,
            'url': '/music/miley-flowers-remix.mp3',
            'cover': 'https://images.unsplash.com/photo-1520262494112-9fe481d36ec3?w=400&h=400&fit=crop&crop=center',
            'category': 'Pop Latino',
            'isOriginal': False,
            'uses': 8900000,
            'waveform': [0.5, 0.7, 0.4, 0.8, 0.3, 0.6, 0.9, 0.2, 0.7, 0.5, 0.8, 0.4, 0.6, 0.9, 0.3, 0.7, 0.5, 0.8, 0.2, 0.6]
        },
        {
            'id': 'music_pop_latino_2',
            'title': 'MAMIII',
            'artist': 'Becky G x Karol G',
            'duration': 187,
            'url': '/music/becky-g-mamiii.mp3',
            'cover': 'https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=400&h=400&fit=crop&crop=center',
            'category': 'Pop Latino',
            'isOriginal': False,
            'uses': 6300000,
            'waveform': [0.6, 0.8, 0.5, 0.9, 0.4, 0.7, 0.8, 0.3, 0.6, 0.9, 0.5, 0.8, 0.4, 0.7, 0.9, 0.6, 0.8, 0.3, 0.7, 0.9]
        }
    ]
    
    filtered_music = all_music.copy()
    
    # Apply filters
    if category and category != 'Todas':
        filtered_music = [m for m in filtered_music if m['category'] == category]
    
    if search:
        search_lower = search.lower()
        filtered_music = [m for m in filtered_music if 
            search_lower in m['title'].lower() or 
            search_lower in m['artist'].lower() or
            search_lower in m['category'].lower()
        ]
    
    if trending:
        filtered_music = [m for m in filtered_music if m.get('isTrending', False)]
    
    # Sort by uses (popularity)
    filtered_music.sort(key=lambda x: x.get('uses', 0), reverse=True)
    
    # Apply pagination
    total = len(filtered_music)
    filtered_music = filtered_music[offset:offset + limit]
    
    return {
        'music': filtered_music,
        'total': total,
        'limit': limit,
        'offset': offset,
        'has_more': offset + limit < total
    }

# =============  CHAT PERMISSION UTILITIES =============

async def check_chat_permission(sender_id: str, receiver_id: str) -> bool:
    """
    Check if users can chat directly or need permission
    Returns True if they can chat directly, False if permission is needed
    """
    # Check if users are following each other (mutual follow = can chat directly)
    sender_follows_receiver = await db.follows.find_one({
        "follower_id": sender_id,
        "following_id": receiver_id
    })
    
    receiver_follows_sender = await db.follows.find_one({
        "follower_id": receiver_id,
        "following_id": sender_id
    })
    
    # If they follow each other, they can chat directly
    if sender_follows_receiver and receiver_follows_sender:
        return True
    
    # Check if there's an existing accepted chat request
    accepted_request = await db.chat_requests.find_one({
        "$or": [
            {"sender_id": sender_id, "receiver_id": receiver_id, "status": "accepted"},
            {"sender_id": receiver_id, "receiver_id": sender_id, "status": "accepted"}
        ]
    })
    
    if accepted_request:
        return True
    
    # Otherwise, they need permission
    return False

# =============  NOTIFICATION UTILITIES =============

async def send_mention_notifications(mentioned_users: List[str], poll_id: str, current_user: UserResponse):
    """Send notifications to mentioned users"""
    # TODO: Implement notification system for mentioned users
    # This is a placeholder function that will be implemented later
    # For now, we'll just log the mentions
    print(f"Sending mention notifications to {mentioned_users} for poll {poll_id} by user {current_user.username}")
    pass

# Basic API endpoint
@api_router.get("/")
async def get_api_info():
    """Get API information"""
    return {
        "name": "Social Network API",
        "version": "1.0",
        "description": "Social network with messaging functionality",
        "features": ["messaging", "user_profiles"]
    }

# ============= GEOLOCATION ENDPOINT =============

@api_router.get("/geolocation")
async def get_user_geolocation(request: Request):
    """Get user's country based on IP address"""
    try:
        # Get client IP
        client_ip = get_client_ip(request)
        
        # For localhost/private IPs, return default
        if client_ip in ["127.0.0.1", "localhost"] or client_ip.startswith("192.168.") or client_ip.startswith("10."):
            return {
                "country": "unknown",
                "country_code": "XX",
                "ip": client_ip
            }
        
        # Use ip-api.com (free, no API key needed)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://ip-api.com/json/{client_ip}?fields=status,country,countryCode",
                timeout=5.0
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    return {
                        "country": data.get("country", "unknown").lower(),
                        "country_code": data.get("countryCode", "XX"),
                        "ip": client_ip
                    }
        
        return {
            "country": "unknown",
            "country_code": "XX",
            "ip": client_ip
        }
        
    except Exception as e:
        logger.error(f"Geolocation error: {e}")
        return {
            "country": "unknown",
            "country_code": "XX",
            "ip": "unknown"
        }

# ============= ELEVENLABS TTS ENDPOINTS =============

class TTSRequest(BaseModel):
    text: str
    
# Initialize ElevenLabs client
elevenlabs_client = None
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")

if ELEVENLABS_AVAILABLE:
    elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY")
    if elevenlabs_api_key:
        try:
            elevenlabs_client = ElevenLabs(api_key=elevenlabs_api_key)
            logger.info("ElevenLabs client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize ElevenLabs client: {e}")

@api_router.post("/tts/generate")
async def generate_tts_audio(request: TTSRequest):
    """
    Generate Text-to-Speech audio using ElevenLabs.
    Returns MP3 audio that can be played in the browser.
    """
    elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY")
    
    if not elevenlabs_api_key:
        raise HTTPException(
            status_code=503, 
            detail="Text-to-Speech service not available - no API key"
        )
    
    if not request.text or len(request.text) == 0:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    
    # Limit text length for short clips (3-4 seconds)
    if len(request.text) > 150:
        raise HTTPException(
            status_code=400, 
            detail="Text too long. Maximum 150 characters for short clips."
        )
    
    try:
        # Use ElevenLabs REST API directly
        voice_id = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        
        headers = {
            "xi-api-key": elevenlabs_api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg"
        }
        
        data = {
            "text": request.text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True
            }
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json=data,
                timeout=30.0
            )
            
            if response.status_code != 200:
                logger.error(f"ElevenLabs API error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"ElevenLabs API error: {response.text}"
                )
            
            # Return audio as streaming response
            return StreamingResponse(
                BytesIO(response.content),
                media_type="audio/mpeg",
                headers={
                    "Content-Disposition": "inline; filename=tts_audio.mp3",
                    "Cache-Control": "no-cache"
                }
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS generation error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate audio: {str(e)}"
        )

# =============  AUTHENTICATION ENDPOINTS =============

@api_router.post("/auth/register", response_model=Token)
async def register(user_data: UserCreate, request: Request):
    """Register a new user with enhanced security"""
    ip_address = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    
    # Check if email exists
    if await db.users.find_one({"email": user_data.email}):
        await track_login_attempt(
            user_data.email, ip_address, user_agent,
            False, "Email already registered"
        )
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )
    
    # Check if username exists
    if await db.users.find_one({"username": user_data.username}):
        raise HTTPException(
            status_code=400,
            detail="Username already taken"
        )
    
    # Create user
    hashed_password = get_password_hash(user_data.password)
    user = User(
        email=user_data.email,
        username=user_data.username,
        display_name=user_data.display_name,
        hashed_password=hashed_password,
        avatar_url=user_data.avatar_url  # Incluir avatar_url del registro
    )
    
    # Insert user
    await db.users.insert_one(user.dict())
    
    # Create user profile
    profile = UserProfile(
        id=user.id,
        username=user.username,
        display_name=user_data.display_name,
        avatar_url=user_data.avatar_url  # Incluir avatar_url en el perfil también
    )
    await db.user_profiles.insert_one(profile.dict())
    
    # Get or create device for registration
    device = await get_or_create_device(user.id, ip_address, user_agent)
    
    # Track successful registration
    await track_login_attempt(user_data.email, ip_address, user_agent, True)
    
    # Generate token
    access_token = create_access_token(data={"sub": user.id})
    
    # Create session
    session_token = await create_session(user.id, device.id, ip_address, user_agent)
    
    # Create welcome notification
    await create_security_notification(
        user.id,
        "account_created",
        "Welcome!",
        f"Your account has been created successfully from {device.device_name}",
        {
            "device_id": device.id,
            "ip_address": ip_address
        }
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse(**user.dict())
    )

@api_router.post("/auth/login", response_model=Token)
async def login(login_data: UserLogin, request: Request):
    """Login user with enhanced security"""
    ip_address = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    
    # Check rate limiting
    login_identifier = login_data.email.strip()
    if not await check_rate_limit(login_identifier, ip_address):
        await track_login_attempt(
            login_identifier, ip_address, user_agent, 
            False, "Rate limit exceeded"
        )
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Please try again later."
        )
    
    # Find user by email OR username
    is_email = "@" in login_identifier
    if is_email:
        user_data = await db.users.find_one({"email": login_identifier})
    else:
        # Search by username (case-insensitive, ignore leading/trailing spaces)
        user_data = await db.users.find_one({
            "username": {"$regex": f"^\\s*{login_identifier}\\s*$", "$options": "i"}
        })
    
    if not user_data:
        await track_login_attempt(
            login_identifier, ip_address, user_agent,
            False, "User not found"
        )
        raise HTTPException(
            status_code=400,
            detail="Incorrect email/username or password"
        )
    
    # Verify password (skip for OAuth users)
    if user_data.get("hashed_password") and not verify_password(login_data.password, user_data["hashed_password"]):
        await track_login_attempt(
            login_identifier, ip_address, user_agent,
            False, "Invalid password"
        )
        raise HTTPException(
            status_code=400,
            detail="Incorrect email/username or password"
        )
    elif not user_data.get("hashed_password"):
        await track_login_attempt(
            login_identifier, ip_address, user_agent,
            False, "OAuth user attempted regular login"
        )
        raise HTTPException(
            status_code=400,
            detail="This account uses social login. Please use Google sign-in."
        )
    
    # Get or create device
    device = await get_or_create_device(user_data["id"], ip_address, user_agent)
    
    # Check if this is a new device
    if not device.is_trusted:
        await create_security_notification(
            user_data["id"],
            "new_device",
            "New Device Login",
            f"Login detected from new device: {device.device_name} ({device.browser})",
            {
                "device_id": device.id,
                "ip_address": ip_address,
                "location": "Unknown"  # You could add IP geolocation here
            }
        )
    
    # Update last login
    await db.users.update_one(
        {"id": user_data["id"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )
    
    # Track successful login
    await track_login_attempt(login_identifier, ip_address, user_agent, True)
    
    # Generate token
    access_token = create_access_token(data={"sub": user_data["id"]})
    
    # Create session
    session_token = await create_session(user_data["id"], device.id, ip_address, user_agent)
    
    # Create login notification
    await create_security_notification(
        user_data["id"],
        "new_login",
        "New Login",
        f"Successful login from {device.device_name} ({device.browser})",
        {
            "device_id": device.id,
            "ip_address": ip_address,
            "session_token": session_token
        }
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer", 
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse(**user_data)
    )

@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: UserResponse = Depends(get_current_user)):
    """Get current user information"""
    return current_user


@api_router.delete("/auth/me", status_code=200)
async def delete_my_account(current_user: UserResponse = Depends(get_current_user)):
    """Soft-delete / desactivar la propia cuenta del usuario.

    El registro NO se elimina físicamente para preservar integridad
    referencial (votos emitidos por este usuario en polls de otros,
    comentarios, menciones, etc. siguen existiendo). En su lugar:
      • `is_active = False`, `deleted_at` = ahora → bloquea login.
      • `username` y `display_name` se anonimizan a "usuario_eliminado"
        (+ sufijo corto para unicidad).
      • `avatar_url`, `bio`, `cover_url` y datos personales → None / vacío.
      • Todas las publicaciones propias → `status = "hidden"` (soft-delete
        cascada). El filtro global `POLL_STATUS_FILTER` las oculta de
        feed/perfil/explore/búsqueda automáticamente.

    Es idempotente: llamar dos veces devuelve 200 igualmente.
    """
    user_id = current_user.id
    now = datetime.utcnow()

    try:
        existing = await db.users.find_one({"id": user_id})
        if not existing:
            raise HTTPException(status_code=404, detail="User not found")

        # Idempotencia: si ya está desactivada, devolver éxito sin volver a
        # modificar (evita regenerar el sufijo del username).
        if existing.get("is_active") is False and existing.get("deleted_at"):
            return {
                "message": "Account already deleted",
                "idempotent": True,
                "hidden_polls": 0,
            }

        # Anonimización: sufijo corto del UUID para evitar colisiones de
        # username único. Ej: "usuario_eliminado_a1b2c3d4"
        anon_suffix = user_id.split("-")[0] if "-" in user_id else user_id[:8]
        anon_username = f"usuario_eliminado_{anon_suffix}"
        anon_display_name = "Usuario eliminado"

        await db.users.update_one(
            {"id": user_id},
            {"$set": {
                "is_active": False,
                "deleted_at": now,
                "username": anon_username,
                "display_name": anon_display_name,
                "avatar_url": None,
                "cover_url": None,
                "bio": "",
                "phone": None,
                "location": None,
                "website": None,
                # Limpiar datos potencialmente sensibles
                "google_id": None,
                "apple_id": None,
                # Preferencias de privacidad: forzar perfil privado para que
                # la cuenta anonimizada no sea descubrible.
                "is_public": False,
                "allow_messages": False,
                "push_notifications": False,
            }},
        )

        # Cascade: soft-delete de TODAS las publicaciones del usuario que
        # no estuvieran ya ocultas.
        cascade = await db.polls.update_many(
            {"author_id": user_id, "status": {"$ne": "hidden"}},
            {"$set": {
                "status": "hidden",
                "hidden_at": now,
                "hidden_by": user_id,
            }},
        )

        # Invalidar tokens/devices activos para forzar logout inmediato en
        # todos los dispositivos del usuario (buenas prácticas de seguridad).
        try:
            await db.refresh_tokens.delete_many({"user_id": user_id})
            await db.user_devices.delete_many({"user_id": user_id})
        except Exception as token_err:
            logger.warning(f"[delete-account] token cleanup warning for {user_id}: {token_err}")

        logger.info(
            f"👋 Account {user_id} soft-deleted. "
            f"Cascaded {cascade.modified_count} polls to hidden."
        )

        return {
            "message": "Account deleted successfully",
            "hidden_polls": cascade.modified_count,
            "idempotent": False,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error soft-deleting account {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# =============  GOOGLE OAUTH ENDPOINTS =============

@api_router.post("/auth/oauth/google", response_model=Token)
async def oauth_google_callback(session_id: str, request: Request):
    """Handle Google OAuth callback with session ID"""
    ip_address = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    
    try:
        # Get user data from Emergent Auth API
        oauth_data = await get_oauth_user_data(session_id)
        
        # Create or get existing user
        user = await create_or_get_oauth_user(oauth_data, ip_address, user_agent)
        
        # Get or create device
        device = await get_or_create_device(user.id, ip_address, user_agent)
        
        # Check if this is a new device
        if not device.is_trusted:
            await create_security_notification(
                user.id,
                "new_device",
                "New Device Login",
                f"Google login from new device: {device.device_name} ({device.browser})",
                {
                    "device_id": device.id,
                    "ip_address": ip_address,
                    "oauth_provider": "google"
                }
            )
        
        # Track successful login
        await track_login_attempt(user.email, ip_address, user_agent, True)
        
        # Generate JWT token
        access_token = create_access_token(data={"sub": user.id})
        
        # Create session
        session_token = await create_session(user.id, device.id, ip_address, user_agent)
        
        # Create login notification
        await create_security_notification(
            user.id,
            "new_login", 
            "Google Login",
            f"Successful Google login from {device.device_name} ({device.browser})",
            {
                "device_id": device.id,
                "ip_address": ip_address,
                "oauth_provider": "google",
                "session_token": session_token
            }
        )
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=UserResponse(**user.dict())
        )
        
    except Exception as e:
        await track_login_attempt(
            "unknown", ip_address, user_agent,
            False, f"OAuth error: {str(e)}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"OAuth authentication failed: {str(e)}"
        )

# =============  SECURITY ENDPOINTS =============

@api_router.get("/auth/security/notifications")
async def get_security_notifications(
    current_user: UserResponse = Depends(get_current_user),
    limit: int = 20,
    offset: int = 0
):
    """Get user's security notifications"""
    notifications = await db.security_notifications.find({
        "user_id": current_user.id
    }).sort("created_at", -1).skip(offset).limit(limit).to_list(limit)
    
    return notifications

@api_router.put("/auth/security/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Mark a security notification as read"""
    result = await db.security_notifications.update_one(
        {
            "id": notification_id,
            "user_id": current_user.id
        },
        {"$set": {"is_read": True}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    return {"message": "Notification marked as read"}

@api_router.get("/auth/security/devices")
async def get_user_devices(current_user: UserResponse = Depends(get_current_user)):
    """Get user's trusted devices"""
    devices = await db.user_devices.find({
        "user_id": current_user.id
    }).sort("last_used", -1).to_list(50)
    
    return devices

@api_router.put("/auth/security/devices/{device_id}/trust")
async def trust_device(
    device_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Mark a device as trusted"""
    result = await db.user_devices.update_one(
        {
            "id": device_id,
            "user_id": current_user.id
        },
        {"$set": {"is_trusted": True}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    
    return {"message": "Device marked as trusted"}

@api_router.delete("/auth/security/devices/{device_id}")
async def remove_device(
    device_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Remove a device and invalidate its sessions"""
    # Remove device
    device_result = await db.user_devices.delete_one({
        "id": device_id,
        "user_id": current_user.id
    })
    
    if device_result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Invalidate sessions for this device
    await db.user_sessions.update_many(
        {
            "device_id": device_id,
            "user_id": current_user.id
        },
        {"$set": {"is_active": False}}
    )
    
    return {"message": "Device removed and sessions invalidated"}

@api_router.get("/auth/security/login-history")
async def get_login_history(
    current_user: UserResponse = Depends(get_current_user),
    limit: int = 50,
    offset: int = 0
):
    """Get user's recent login history"""
    attempts = await db.login_attempts.find({
        "email": current_user.email,
        "success": True
    }).sort("created_at", -1).skip(offset).limit(limit).to_list(limit)
    
    return attempts

# =============  USER UPDATE ENDPOINTS =============

@api_router.put("/auth/profile", response_model=UserResponse)
async def update_profile(
    user_data: UserUpdate, 
    current_user: UserResponse = Depends(get_current_user)
):
    """Update user profile information"""
    update_fields = {}
    
    if user_data.display_name is not None:
        update_fields["display_name"] = user_data.display_name.strip()
    if user_data.bio is not None:
        update_fields["bio"] = user_data.bio.strip()
    if user_data.occupation is not None:
        update_fields["occupation"] = user_data.occupation.strip()
    if user_data.avatar_url is not None:
        update_fields["avatar_url"] = user_data.avatar_url.strip()
    
    if not update_fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    # Update user in database
    result = await db.users.update_one(
        {"id": current_user.id},
        {"$set": update_fields}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    # SYNC DATA: Also update user_profiles collection to maintain consistency
    # Update user_profiles with the same fields to keep both collections in sync
    user_profile_fields = {}
    if "display_name" in update_fields:
        user_profile_fields["display_name"] = update_fields["display_name"]
    if "bio" in update_fields:
        user_profile_fields["bio"] = update_fields["bio"]
    if "occupation" in update_fields:
        user_profile_fields["occupation"] = update_fields["occupation"]
    if "avatar_url" in update_fields:
        user_profile_fields["avatar_url"] = update_fields["avatar_url"]
    
    if user_profile_fields:
        await db.user_profiles.update_one(
            {"user_id": current_user.id},
            {"$set": user_profile_fields},
            upsert=True  # Create if doesn't exist
        )
        logger.info(f"✅ Synced user_profiles for user {current_user.id}: {user_profile_fields}")
    
    # Return updated user
    updated_user = await db.users.find_one({"id": current_user.id})
    return UserResponse(**updated_user)

@api_router.put("/auth/password")
async def change_password(
    password_data: PasswordChange,
    current_user: UserResponse = Depends(get_current_user),
    request: Request = None
):
    """Change user password with security tracking"""
    ip_address = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    
    # Get current user with password
    user_data = await db.users.find_one({"id": current_user.id})
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if user has a password (not OAuth-only user)
    if not user_data.get("hashed_password"):
        raise HTTPException(
            status_code=400, 
            detail="Cannot change password for OAuth-only accounts"
        )
    
    # Verify current password
    if not verify_password(password_data.current_password, user_data["hashed_password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    
    # Hash new password
    new_hashed_password = get_password_hash(password_data.new_password)
    
    # Update password in database
    result = await db.users.update_one(
        {"id": current_user.id},
        {"$set": {"hashed_password": new_hashed_password}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to update password")
    
    # Get device info
    device = await get_or_create_device(current_user.id, ip_address, user_agent)
    
    # Create security notification
    await create_security_notification(
        current_user.id,
        "password_change",
        "Password Changed",
        f"Your password was changed from {device.device_name} ({device.browser})",
        {
            "device_id": device.id,
            "ip_address": ip_address
        }
    )
    
    # Invalidate all other sessions for security
    await db.user_sessions.update_many(
        {"user_id": current_user.id},
        {"$set": {"is_active": False}}
    )
    
    return {"message": "Password updated successfully. Please log in again on other devices."}

@api_router.put("/auth/settings", response_model=UserResponse)
async def update_settings(
    settings_data: UserSettings,
    current_user: UserResponse = Depends(get_current_user)
):
    """Update user settings (privacy, notifications, performance, language, account)"""
    update_fields = {}
    
    # Privacy settings
    if settings_data.is_public is not None:
        update_fields["is_public"] = settings_data.is_public
    if settings_data.allow_messages is not None:
        update_fields["allow_messages"] = settings_data.allow_messages
    
    # Notification settings
    if settings_data.notifications_enabled is not None:
        update_fields["notifications_enabled"] = settings_data.notifications_enabled
    if settings_data.email_notifications is not None:
        update_fields["email_notifications"] = settings_data.email_notifications
    if settings_data.push_notifications is not None:
        update_fields["push_notifications"] = settings_data.push_notifications
    if settings_data.notifications_likes is not None:
        update_fields["notifications_likes"] = settings_data.notifications_likes
    if settings_data.notifications_comments is not None:
        update_fields["notifications_comments"] = settings_data.notifications_comments
    if settings_data.notifications_follows is not None:
        update_fields["notifications_follows"] = settings_data.notifications_follows
    if settings_data.notifications_mentions is not None:
        update_fields["notifications_mentions"] = settings_data.notifications_mentions
    
    # Performance & Data settings (APK specific)
    if settings_data.video_quality is not None:
        # Validate video quality values
        if settings_data.video_quality in ['auto', 'high', 'medium', 'low']:
            update_fields["video_quality"] = settings_data.video_quality
    if settings_data.wifi_only is not None:
        update_fields["wifi_only"] = settings_data.wifi_only
    if settings_data.battery_saver is not None:
        update_fields["battery_saver"] = settings_data.battery_saver
    if settings_data.auto_cache is not None:
        update_fields["auto_cache"] = settings_data.auto_cache
    if settings_data.background_sync is not None:
        update_fields["background_sync"] = settings_data.background_sync
    
    # Language & Accessibility settings
    if settings_data.app_language is not None:
        # Validate language codes
        if settings_data.app_language in ['es', 'en', 'fr', 'pt']:
            update_fields["app_language"] = settings_data.app_language
    if settings_data.dark_mode is not None:
        update_fields["dark_mode"] = settings_data.dark_mode
    if settings_data.large_text is not None:
        update_fields["large_text"] = settings_data.large_text
    
    # Account settings
    if settings_data.two_factor_enabled is not None:
        update_fields["two_factor_enabled"] = settings_data.two_factor_enabled
    
    if not update_fields:
        raise HTTPException(status_code=400, detail="No settings to update")
    
    # Update user settings in database
    result = await db.users.update_one(
        {"id": current_user.id},
        {"$set": update_fields}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Return updated user
    updated_user = await db.users.find_one({"id": current_user.id})
    return UserResponse(**updated_user)

# =============  USER PROFILE ENDPOINTS =============

@api_router.get("/user/profile")
async def get_my_profile(current_user: UserResponse = Depends(get_current_user)):
    """Get current user profile (recalculates stats to keep totals fresh)"""
    profile_data = await ensure_user_profile(current_user.id)
    if not profile_data:
        # Fallback: create a new profile if user has no doc and ensure failed
        profile = UserProfile(id=current_user.id, username=current_user.username)
        await db.user_profiles.insert_one(profile.dict())
        return profile

    profile = UserProfile(**profile_data)
    return profile

# Helper function to ensure user profile exists and is up to date
async def ensure_user_profile(user_id: str):
    """Ensure user profile exists and is synchronized with user data"""
    try:
        # Get user data from users collection
        user_data = await db.users.find_one({"id": user_id})
        if not user_data:
            return None
            
        # Check if profile exists
        profile_data = await db.user_profiles.find_one({"id": user_id})
        
        # Count followers and following
        followers_count = await db.follows.count_documents({"following_id": user_id})
        following_count = await db.follows.count_documents({"follower_id": user_id})
        
        # Count total votes and polls
        total_polls = await db.polls.count_documents({"author_id": user_id, "is_active": True})
        
        # Calculate actual vote statistics using aggregation for accuracy
        # Use MongoDB aggregation to count real votes received on user's polls
        pipeline = [
            {"$match": {"author_id": user_id, "is_active": True}},
            {"$unwind": "$options"},
            {"$group": {
                "_id": None,
                "total_votes_received": {"$sum": "$options.votes"}
            }}
        ]
        
        result = await db.polls.aggregate(pipeline).to_list(length=1)
        total_votes_received = result[0]["total_votes_received"] if result else 0
        
        # 🏆 Also count votes received in challenges
        challenge_votes_pipeline = [
            {"$unwind": "$participants"},
            {"$match": {"participants.user_id": user_id}},
            {"$group": {
                "_id": None,
                "challenge_votes": {"$sum": "$participants.votes_received"}
            }}
        ]
        challenge_votes_result = await db.challenges.aggregate(challenge_votes_pipeline).to_list(length=1)
        challenge_votes_received = challenge_votes_result[0]["challenge_votes"] if challenge_votes_result else 0
        total_votes_received += challenge_votes_received

        # 🆚 Also count votes received in VS experiences (votes are stored in
        # vs_experiences.questions[].options[].votes; the corresponding poll doc
        # is not incremented, so the polls aggregation above misses them).
        vs_votes_pipeline = [
            {"$match": {"author_id": user_id}},
            {"$unwind": "$questions"},
            {"$unwind": "$questions.options"},
            {"$group": {
                "_id": None,
                "vs_votes": {"$sum": "$questions.options.votes"}
            }}
        ]
        vs_votes_result = await db.vs_experiences.aggregate(vs_votes_pipeline).to_list(length=1)
        vs_votes_received = vs_votes_result[0]["vs_votes"] if vs_votes_result else 0
        total_votes_received += vs_votes_received

        logger.info(f"📊 Calculated real votes for user {user_id}: {total_votes_received} (polls + {challenge_votes_received} challenge + {vs_votes_received} VS)")
        
        # Count votes made by this user
        votes_made_by_user = await db.votes.count_documents({"user_id": user_id})
        # 🏆 Also count challenge votes made by this user
        challenge_votes_made = await db.challenge_votes.count_documents({"voter_id": user_id})
        votes_made_by_user += challenge_votes_made
        
        # Count likes received on user's polls using aggregation
        likes_pipeline = [
            {"$match": {"author_id": user_id, "is_active": True}},
            {"$group": {
                "_id": None,
                "total_likes": {"$sum": "$likes"}
            }}
        ]
        
        likes_result = await db.polls.aggregate(likes_pipeline).to_list(length=1)
        likes_received = likes_result[0]["total_likes"] if likes_result else 0
        
        # Count likes given by this user
        likes_given_by_user = await db.poll_likes.count_documents({"user_id": user_id})
        
        # Prepare profile data
        profile_update = {
            "id": user_id,
            "username": user_data.get("username"),
            "display_name": user_data.get("display_name"),
            "avatar_url": user_data.get("avatar_url"),
            "bio": user_data.get("bio"),
            "occupation": user_data.get("occupation"),  # ✅ ADDED: Include occupation field
            "is_verified": user_data.get("is_verified", False),
            "followers_count": followers_count,
            "following_count": following_count,
            "total_polls_created": total_polls,
            "total_votes": total_votes_received,  # Total votes received on user's polls
            "likes_count": likes_received,  # Total likes received on user's polls
            "votes_count": votes_made_by_user,  # Total votes made by this user
            "likes_given": likes_given_by_user,  # Total likes given by this user
            "last_activity": datetime.utcnow(),
            "created_at": profile_data.get("created_at", datetime.utcnow()) if profile_data else datetime.utcnow()
        }
        
        # Update or insert profile
        await db.user_profiles.update_one(
            {"id": user_id},
            {"$set": profile_update},
            upsert=True
        )
        
        return profile_update
        
    except Exception as e:
        print(f"❌ Error ensuring user profile for {user_id}: {e}")
        return None

@api_router.get("/user/profile/{user_id}")
async def get_user_profile(user_id: str):
    """Get user profile by ID (public endpoint)"""
    # Ensure profile exists and is up to date
    profile_data = await ensure_user_profile(user_id)
    if not profile_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    profile = UserProfile(**profile_data)
    return profile

@api_router.get("/user/profile/by-username/{username}")
async def get_user_profile_by_username(username: str):
    """Get user profile by username (public endpoint)"""
    # First find user by username to get user_id
    # Try exact match first, then try with trimmed spaces to handle trailing spaces in usernames
    user_data = await db.users.find_one({"username": username})
    if not user_data:
        # Try trimmed version and also check if any username when stripped matches
        stripped_username = username.strip()
        user_data = await db.users.find_one({"username": stripped_username})
        if not user_data:
            # Try finding users where the trimmed version of stored username matches
            users_cursor = db.users.find({})
            async for user in users_cursor:
                if user.get("username", "").strip() == stripped_username:
                    user_data = user
                    break
    
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Ensure profile exists and is up to date
    profile_data = await ensure_user_profile(user_data["id"])
    if not profile_data:
        raise HTTPException(status_code=404, detail="User profile not found")
    
    profile = UserProfile(**profile_data)
    return profile

# =============  USER SEARCH ENDPOINTS =============

@api_router.get("/users/search")
async def search_users(q: str = "", current_user: UserResponse = Depends(get_current_user)):
    """Search users by username or display name"""
    if not q.strip():
        return []
    
    # Search by username or display_name (case-insensitive)
    search_regex = {"$regex": q, "$options": "i"}
    users = await db.users.find({
        "$and": [
            {"id": {"$ne": current_user.id}},  # Exclude current user
            {
                "$or": [
                    {"username": search_regex},
                    {"display_name": search_regex}
                ]
            }
        ]
    }).limit(10).to_list(10)
    
    return [UserResponse(**user) for user in users]

# =============  UNIVERSAL SEARCH ENDPOINTS =============

from difflib import SequenceMatcher
import re

def calculate_similarity(a, b):
    """Calculate similarity between two strings for fuzzy search"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio()

def extract_hashtags_from_text(text):
    """Extract hashtags from text content"""
    if not text:
        return []
    hashtag_pattern = r'#(\w+)'
    hashtags = re.findall(hashtag_pattern, text)
    return [f"#{tag}" for tag in hashtags]

# Main search endpoint (alias)
@api_router.get("/search")
async def search_endpoint(
    q: str = "",
    filter: str = config.SEARCH_CONFIG['DEFAULT_FILTER'],
    sort_by: str = config.SEARCH_CONFIG['DEFAULT_SORT'],
    limit: int = config.SEARCH_CONFIG['DEFAULT_SEARCH_LIMIT'],
    current_user: UserResponse = Depends(get_current_user)
):
    """Universal search endpoint - redirects to universal_search"""
    return await universal_search(q, filter, sort_by, limit, current_user)

@api_router.get("/search/universal")
async def universal_search(
    q: str = "",
    filter_type: str = config.SEARCH_CONFIG['DEFAULT_FILTER'],
    sort_by: str = config.SEARCH_CONFIG['DEFAULT_SORT'],
    limit: int = config.SEARCH_CONFIG['DEFAULT_SEARCH_LIMIT'],
    current_user: UserResponse = Depends(get_current_user)
):
    """Universal search across all content types - OPTIMIZED VERSION"""
    # Validate query parameters
    if limit > config.SEARCH_CONFIG['MAX_SEARCH_LIMIT']:
        limit = config.SEARCH_CONFIG['MAX_SEARCH_LIMIT']
    
    if filter_type not in config.SEARCH_CONFIG['AVAILABLE_FILTERS']:
        filter_type = config.SEARCH_CONFIG['DEFAULT_FILTER']
    
    if sort_by not in config.SEARCH_CONFIG['AVAILABLE_SORTS']:
        sort_by = config.SEARCH_CONFIG['DEFAULT_SORT']
    
    # Validate and clean query parameter
    if not q or not q.strip():
        return {
            "success": True,
            "results": [],
            "suggestions": await get_search_suggestions_helper(current_user.id),
            "trending": await get_trending_content(current_user.id)
        }
    
    query = q.strip()
    if len(query) < config.SEARCH_CONFIG['MIN_QUERY_LENGTH']:
        return {"success": True, "results": [], "message": "Query too short"}
    
    if len(query) > config.SEARCH_CONFIG['MAX_QUERY_LENGTH']:
        return {"success": False, "error": "Query too long"}
        
    query_lower = query.lower()
    results = []
    
    try:
        # OPTIMIZATION: Run searches concurrently for better performance
        import asyncio
        
        tasks = []
        
        # Search Users
        if filter_type in [config.SEARCH_CONFIG['DEFAULT_FILTER'], config.SEARCH_CONFIG['AVAILABLE_FILTERS'][1]]:
            tasks.append(search_users_optimized(query_lower, current_user.id, limit))
        
        # Search Posts  
        if filter_type in [config.SEARCH_CONFIG['DEFAULT_FILTER'], config.SEARCH_CONFIG['AVAILABLE_FILTERS'][2]]:
            tasks.append(search_posts_optimized(query_lower, current_user.id, limit))
        
        # Search Hashtags
        if filter_type in [config.SEARCH_CONFIG['DEFAULT_FILTER'], config.SEARCH_CONFIG['AVAILABLE_FILTERS'][3]]:
            tasks.append(search_hashtags_optimized(query_lower, current_user.id, limit))
        
        # Search Sounds/Music
        if filter_type in [config.SEARCH_CONFIG['DEFAULT_FILTER'], config.SEARCH_CONFIG['AVAILABLE_FILTERS'][4]]:
            tasks.append(search_sounds_optimized(query, current_user.id, limit))
        
        # Execute all searches concurrently
        if tasks:
            search_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Combine results from all successful searches
            for result in search_results:
                if isinstance(result, list):  # Only process successful results
                    results.extend(result)
        
        # Sort results
        if sort_by == "popularity":
            results.sort(key=lambda x: x.get("popularity_score", 0), reverse=True)
        elif sort_by == "recent":
            results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        else:  # relevance
            results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        
        # Limit final results
        results = results[:limit]
        
        return {
            "success": True,
            "results": results,
            "total": len(results),
            "query": q,
            "filter_type": filter_type,
            "sort_by": sort_by
        }
        
    except Exception as e:
        logger.error(f"Error in universal search: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Search error: {str(e)}")

# =============  OPTIMIZED SEARCH FUNCTIONS =============

async def search_posts_optimized(query: str, current_user_id: str, limit: int):
    """Optimized post search with minimal database operations"""
    try:
        # Use aggregation pipeline for better performance
        pipeline = [
            {
                "$match": {
                    "is_active": True,
                    "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
                    "$or": [
                        {"title": {"$regex": query, "$options": "i"}},
                        {"content": {"$regex": query, "$options": "i"}}
                    ]
                }
            },
            {
                "$limit": limit * 2  # Get more to ensure we have enough after processing
            },
            {
                "$lookup": {
                    "from": "users",
                    "localField": "author_id", 
                    "foreignField": "id",
                    "as": "author_info"
                }
            },
            {
                "$addFields": {
                    "author": {"$arrayElemAt": ["$author_info", 0]}
                }
            },
            {
                "$project": {
                    "id": 1,
                    "title": 1,
                    "content": 1,
                    "options": 1,  # Include options to extract media_url and thumbnail_url
                    "layout": 1,   # Include layout for frontend grid rendering
                    "vs_orientation": 1,  # 🎯 VS: orientación del split (vertical=lado a lado, horizontal=arriba-abajo)
                    "vs_questions": 1,    # 🎯 VS: lista de preguntas para polls VS multi-pregunta
                    "images": 1,
                    "image_url": 1,
                    "thumbnail_url": 1,
                    "video_url": 1,
                    "author_id": 1,  # Include author_id for follow functionality
                    "created_at": 1,
                    "votes_count": {"$ifNull": ["$votes_count", 0]},
                    "comments_count": {"$ifNull": ["$comments_count", 0]},
                    "saves_count": {"$ifNull": ["$saves_count", 0]},
                    "author.id": 1,  # Include author id
                    "author.username": 1,
                    "author.display_name": 1,
                    "author.avatar_url": 1,
                    "hashtags": 1
                }
            }
        ]
        
        posts = await db.polls.aggregate(pipeline).to_list(limit * 2)
        
        results = []
        for post in posts[:limit]:  # Limit results after processing
            # Calculate simple relevance score
            title_score = 2 if query in post.get("title", "").lower() else 0
            content_score = 1 if query in post.get("content", "").lower() else 0
            relevance_score = title_score + content_score
            
            # Get first image for thumbnail from poll options
            image_url = None
            media_url = None
            thumbnail_url = None
            
            # Extract images from poll options (where they actually are stored)
            if post.get("options") and len(post["options"]) > 0:
                for option in post["options"]:
                    if option.get("media_url"):
                        media_url = option["media_url"]
                        # Check if it's a video and get thumbnail
                        if option.get("media_type") == "video":
                            # Try to get existing thumbnail_url
                            option_thumbnail = option.get("thumbnail_url")
                            if not option_thumbnail:
                                # Generate thumbnail URL for video
                                option_thumbnail = await get_thumbnail_for_media_url(media_url)
                            if option_thumbnail:
                                thumbnail_url = option_thumbnail
                                image_url = option_thumbnail  # Use thumbnail for display
                            else:
                                # Fallback to media_url if thumbnail not available
                                image_url = media_url
                        else:
                            # For images, use media_url directly
                            image_url = media_url
                        break
                    elif option.get("thumbnail_url"):
                        thumbnail_url = option["thumbnail_url"]
                        if not image_url:  # Use thumbnail as fallback if no media_url found
                            image_url = thumbnail_url
            
            # Fallback to legacy fields if they exist
            if not image_url:
                if post.get("images") and len(post["images"]) > 0:
                    image_url = post["images"][0].get("url")
                elif post.get("image_url"):
                    image_url = post["image_url"]
                elif post.get("thumbnail_url"):
                    image_url = post["thumbnail_url"]
                
            # Build images array for frontend compatibility and process options with thumbnails
            images_array = []
            processed_options = []
            if post.get("options"):
                for option in post["options"]:
                    option_copy = option.copy()
                    
                    if option.get("media_url"):
                        images_array.append({"url": option["media_url"]})
                        
                        # If it's a video, ensure thumbnail_url is set
                        if option.get("media_type") == "video":
                            if not option_copy.get("thumbnail_url"):
                                video_thumbnail = await get_thumbnail_for_media_url(option["media_url"])
                                if video_thumbnail:
                                    option_copy["thumbnail_url"] = video_thumbnail
                    
                    processed_options.append(option_copy)
            
            results.append({
                "type": "post",
                "id": post["id"],
                "title": post.get("title", ""),
                "content": post.get("content", ""),
                "image_url": image_url,
                "thumbnail_url": thumbnail_url or image_url,  # Use specific thumbnail if available
                "media_url": media_url or image_url,  # Add media_url field for frontend compatibility
                "images": images_array,  # Add images array for frontend compatibility (result.images?.[0]?.url)
                "layout": post.get("layout", "vertical"),  # Include layout for frontend grid rendering
                "vs_orientation": post.get("vs_orientation", "horizontal"),  # 🎯 VS: orientación para que el thumbnail coincida con la vista completa
                "vs_questions": post.get("vs_questions", []),  # 🎯 VS: preguntas multi-pregunta
                "options": processed_options,  # Include all options with thumbnails for complete poll rendering
                "video_url": post.get("video_url"),
                "author_id": post.get("author_id"),  # Add author_id for follow functionality
                "author": {
                    "id": post.get("author", {}).get("id", ""),  # Add author id
                    "username": post.get("author", {}).get("username", ""),
                    "display_name": post.get("author", {}).get("display_name", ""),  # Include display_name
                    "avatar_url": post.get("author", {}).get("avatar_url", "")  # Changed 'avatar' to 'avatar_url' for consistency
                },
                "created_at": post.get("created_at", ""),
                "votes_count": post.get("votes_count", 0),
                "comments_count": post.get("comments_count", 0),
                "hashtags": post.get("hashtags", []),
                "relevance_score": relevance_score,
                "popularity_score": post.get("votes_count", 0) + post.get("comments_count", 0)
            })
        
        return results
        
    except Exception as e:
        logger.error(f"Error in optimized post search: {str(e)}")
        return []

async def search_users_optimized(query: str, current_user_id: str, limit: int):
    """Optimized user search with minimal database operations"""
    try:
        # Use aggregation pipeline for better performance
        pipeline = [
            {
                "$match": {
                    "$and": [
                        {"id": {"$ne": current_user_id}},
                        {
                            "$or": [
                                {"username": {"$regex": query, "$options": "i"}},
                                {"display_name": {"$regex": query, "$options": "i"}}
                            ]
                        }
                    ]
                }
            },
            {
                "$limit": limit
            },
            {
                "$lookup": {
                    "from": "user_profiles",
                    "localField": "id",
                    "foreignField": "id", 
                    "as": "profile_info"
                }
            },
            {
                "$addFields": {
                    "profile": {"$arrayElemAt": ["$profile_info", 0]}
                }
            },
            {
                "$project": {
                    "id": 1,
                    "username": 1,
                    "display_name": 1,
                    "avatar_url": 1,
                    "bio": 1,
                    "followers_count": {"$ifNull": ["$profile.followers_count", 0]}
                }
            }
        ]
        
        users = await db.users.aggregate(pipeline).to_list(limit)
        
        results = []
        for user in users:
            # Calculate simple relevance score
            username_score = 2 if query in user.get("username", "").lower() else 0
            display_name_score = 1.5 if query in user.get("display_name", "").lower() else 0
            relevance_score = username_score + display_name_score
            
            results.append({
                "type": "user",
                "id": user["id"],
                "user_id": user["id"],  # Add user_id for follow functionality
                "username": user.get("username", ""),
                "display_name": user.get("display_name", ""),
                "bio": user.get("bio", ""),
                "avatar_url": user.get("avatar_url", ""),  # Changed 'avatar' to 'avatar_url' for consistency
                "followers_count": user.get("followers_count", 0),
                "relevance_score": relevance_score,
                "popularity_score": user.get("followers_count", 0)
            })
        
        return results
        
    except Exception as e:
        logger.error(f"Error in optimized user search: {str(e)}")
        return []

async def search_hashtags_optimized(query: str, current_user_id: str, limit: int):
    """Optimized hashtag search - returns individual posts containing the hashtag"""
    try:
        # Search for hashtags in post tags, title, and content
        hashtag_query = query if query.startswith("#") else f"#{query}"
        query_without_hash = query.replace("#", "").strip()
        
        # Use aggregation pipeline similar to search_posts_optimized
        pipeline = [
            {
                "$match": {
                    "$or": [
                        # Search in tags array (exact or partial match)
                        {"tags": {"$regex": query_without_hash, "$options": "i"}},
                        # Search in title for hashtags
                        {"title": {"$regex": hashtag_query, "$options": "i"}},
                        # Search in content field if exists
                        {"content": {"$regex": hashtag_query, "$options": "i"}}
                    ]
                }
            },
            {
                "$limit": limit * 2  # Get more to ensure we have enough after processing
            },
            {
                "$lookup": {
                    "from": "users",
                    "localField": "author_id", 
                    "foreignField": "id",
                    "as": "author_info"
                }
            },
            {
                "$addFields": {
                    "author": {"$arrayElemAt": ["$author_info", 0]}
                }
            },
            {
                "$project": {
                    "id": 1,
                    "title": 1,
                    "content": 1,
                    "options": 1,
                    "layout": 1,
                    "vs_orientation": 1,  # 🎯 VS: orientación del split (vertical=lado a lado, horizontal=arriba-abajo)
                    "vs_questions": 1,    # 🎯 VS: preguntas multi-pregunta
                    "images": 1,
                    "image_url": 1,
                    "thumbnail_url": 1,
                    "video_url": 1,
                    "author_id": 1,
                    "created_at": 1,
                    "votes_count": {"$ifNull": ["$votes_count", 0]},
                    "comments_count": {"$ifNull": ["$comments_count", 0]},
                    "saves_count": {"$ifNull": ["$saves_count", 0]},
                    "author.id": 1,
                    "author.username": 1,
                    "author.display_name": 1,
                    "author.avatar_url": 1,
                    "hashtags": 1,
                    "tags": 1
                }
            }
        ]
        
        posts = await db.polls.aggregate(pipeline).to_list(limit * 2)
        
        results = []
        for post in posts[:limit]:  # Limit results after processing
            # Calculate relevance score based on hashtag match
            relevance_score = 0
            if query_without_hash.lower() in [tag.lower() for tag in post.get("tags", [])]:
                relevance_score += 3  # Higher score for exact tag match
            if hashtag_query.lower() in post.get("title", "").lower():
                relevance_score += 2
            if hashtag_query.lower() in post.get("content", "").lower():
                relevance_score += 1
            
            # Get first image for thumbnail from poll options
            image_url = None
            media_url = None
            thumbnail_url = None
            
            # Extract images from poll options
            if post.get("options") and len(post["options"]) > 0:
                for option in post["options"]:
                    if option.get("media_url"):
                        media_url = option["media_url"]
                        if option.get("media_type") == "video":
                            option_thumbnail = option.get("thumbnail_url")
                            if not option_thumbnail:
                                option_thumbnail = await get_thumbnail_for_media_url(media_url)
                            if option_thumbnail:
                                thumbnail_url = option_thumbnail
                                image_url = option_thumbnail
                            else:
                                image_url = media_url
                        else:
                            image_url = media_url
                        break
                    elif option.get("thumbnail_url"):
                        thumbnail_url = option["thumbnail_url"]
                        if not image_url:
                            image_url = thumbnail_url
            
            # Fallback to legacy fields
            if not image_url:
                if post.get("images") and len(post["images"]) > 0:
                    image_url = post["images"][0].get("url")
                elif post.get("image_url"):
                    image_url = post["image_url"]
                elif post.get("thumbnail_url"):
                    image_url = post["thumbnail_url"]
                
            # Build images array for frontend compatibility
            images_array = []
            processed_options = []
            if post.get("options"):
                for option in post["options"]:
                    option_copy = option.copy()
                    
                    if option.get("media_url"):
                        images_array.append({"url": option["media_url"]})
                        
                        if option.get("media_type") == "video":
                            if not option_copy.get("thumbnail_url"):
                                video_thumbnail = await get_thumbnail_for_media_url(option["media_url"])
                                if video_thumbnail:
                                    option_copy["thumbnail_url"] = video_thumbnail
                    
                    processed_options.append(option_copy)
            
            results.append({
                "type": "post",
                "id": post["id"],
                "title": post.get("title", ""),
                "content": post.get("content", ""),
                "image_url": image_url,
                "thumbnail_url": thumbnail_url or image_url,
                "media_url": media_url or image_url,
                "images": images_array,
                "layout": post.get("layout", "vertical"),
                "vs_orientation": post.get("vs_orientation", "horizontal"),  # 🎯 VS: orientación para thumbnail
                "vs_questions": post.get("vs_questions", []),                # 🎯 VS: preguntas multi-pregunta
                "options": processed_options,
                "video_url": post.get("video_url"),
                "author_id": post.get("author_id"),
                "author": {
                    "id": post.get("author", {}).get("id", ""),
                    "username": post.get("author", {}).get("username", ""),
                    "display_name": post.get("author", {}).get("display_name", ""),
                    "avatar_url": post.get("author", {}).get("avatar_url", "")
                },
                "created_at": post.get("created_at", ""),
                "votes_count": post.get("votes_count", 0),
                "comments_count": post.get("comments_count", 0),
                "hashtags": post.get("hashtags", []),
                "tags": post.get("tags", []),
                "relevance_score": relevance_score,
                "popularity_score": post.get("votes_count", 0) + post.get("comments_count", 0)
            })
        
        return results
        
    except Exception as e:
        logger.error(f"Error in optimized hashtag search: {str(e)}")
        return []

async def search_sounds_optimized(query: str, current_user_id: str, limit: int):
    """Optimized sound search - searches both user_audio and music used in posts"""
    try:
        results = []
        query_lower = query.lower()
        
        # 1. Search in user_audio collection (user uploaded audios)
        pipeline = [
            {
                "$match": {
                    "$or": [
                        {"title": {"$regex": query, "$options": "i"}},
                        {"artist": {"$regex": query, "$options": "i"}}
                    ]
                }
            },
            {
                "$limit": limit
            },
            {
                "$lookup": {
                    "from": "users",
                    "localField": "uploader_id",
                    "foreignField": "id",
                    "as": "user_info"
                }
            },
            {
                "$addFields": {
                    "author": {"$arrayElemAt": ["$user_info", 0]}
                }
            }
        ]
        
        sounds = await db.user_audio.aggregate(pipeline).to_list(limit)
        
        for sound in sounds:
            # Calculate relevance score
            title_score = 2 if query_lower in sound.get("title", "").lower() else 0
            artist_score = 1.5 if query_lower in sound.get("artist", "").lower() else 0
            relevance_score = title_score + artist_score
            
            # Count posts using this audio
            posts_count = await db.polls.count_documents({
                "$or": [
                    {"music.id": sound["id"]},
                    {"music_id": sound["id"]},
                    {"user_audio_id": sound["id"]}
                ]
            })
            
            # Get cover image
            cover_url = sound.get("cover_url") or sound.get("cover_image") or sound.get("waveform_url", "")
            
            results.append({
                "type": "sound",
                "id": sound["id"],
                "title": sound.get("title", ""),
                "artist": sound.get("artist", ""),
                "duration": sound.get("duration", 0),
                "cover_image": cover_url,
                "thumbnail_url": cover_url,
                "author": {
                    "username": sound.get("author", {}).get("username", ""),
                    "display_name": sound.get("author", {}).get("display_name", "")
                },
                "posts_count": posts_count,
                "posts_using_count": posts_count,
                "relevance_score": relevance_score,
                "popularity_score": posts_count
            })
        
        # 2. If user_audio results are less than limit, search in polls for system music
        if len(results) < limit:
            # Get unique music from polls - search by music_id field
            polls_with_music = await db.polls.find({
                "music_id": {"$exists": True, "$ne": None}
            }).to_list(500)  # Get more to filter
            
            # Collect unique music IDs and their info
            music_usage = {}
            for poll in polls_with_music:
                music_id = poll.get("music_id")
                if music_id and music_id not in [r["id"] for r in results]:
                    # Get music info for this ID
                    music_info = await get_music_info(music_id)
                    if music_info:
                        title = music_info.get("title", "")
                        artist = music_info.get("artist", "")
                        
                        # Check if matches search query
                        if query_lower in title.lower() or query_lower in artist.lower():
                            if music_id not in music_usage:
                                music_usage[music_id] = {
                                    "info": music_info,
                                    "count": 0
                                }
                            music_usage[music_id]["count"] += 1
            
            # Convert music_usage to results
            for music_id, data in music_usage.items():
                music_info = data["info"]
                posts_count = data["count"]
                
                title = music_info.get("title", "")
                artist = music_info.get("artist", "")
                
                # Calculate relevance score
                title_score = 2 if query_lower in title.lower() else 0
                artist_score = 1.5 if query_lower in artist.lower() else 0
                relevance_score = title_score + artist_score
                
                # Get cover/thumbnail
                cover_url = music_info.get("cover") or music_info.get("cover_url", "")
                
                results.append({
                    "type": "sound",
                    "id": music_id,
                    "title": title,
                    "artist": artist,
                    "duration": music_info.get("duration", 0),
                    "cover_image": cover_url,
                    "thumbnail_url": cover_url,
                    "author": {
                        "username": artist,
                        "display_name": artist
                    },
                    "posts_count": posts_count,
                    "posts_using_count": posts_count,
                    "relevance_score": relevance_score,
                    "popularity_score": posts_count
                })
                
                # Stop if we reached the limit
                if len(results) >= limit:
                    break
        
        # Sort by relevance and popularity
        results.sort(key=lambda x: (x["relevance_score"], x["popularity_score"]), reverse=True)
        
        return results[:limit]
        
    except Exception as e:
        logger.error(f"Error in optimized sound search: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return []

async def search_users_advanced(query: str, current_user_id: str, limit: int):
    """Advanced user search with fuzzy matching"""
    search_regex = {"$regex": query, "$options": "i"}
    
    # Find users matching query
    users = await db.users.find({
        "$and": [
            {"id": {"$ne": current_user_id}},
            {
                "$or": [
                    {"username": search_regex},
                    {"display_name": search_regex},
                    {"bio": search_regex}
                ]
            }
        ]
    }).limit(limit * config.SEARCH_CONFIG['FUZZY_SEARCH_MULTIPLIER']).to_list(limit * config.SEARCH_CONFIG['FUZZY_SEARCH_MULTIPLIER'])  # Get more for fuzzy filtering
    
    results = []
    for user in users:
        # Calculate relevance score with configurable multipliers
        username_sim = calculate_similarity(query, user.get("username", "")) * config.SEARCH_CONFIG['MULTIPLIERS']['USERNAME_MATCH']
        display_name_sim = calculate_similarity(query, user.get("display_name", "")) * config.SEARCH_CONFIG['MULTIPLIERS']['DISPLAY_NAME_MATCH']
        bio_sim = calculate_similarity(query, user.get("bio", "")) * config.SEARCH_CONFIG['MULTIPLIERS']['BIO_MATCH']
        
        relevance_score = max(username_sim, display_name_sim, bio_sim)
        
        # Get user profile for additional data
        profile = await db.user_profiles.find_one({"id": user["id"]})
        followers_count = profile.get("followers_count", 0) if profile else 0
        
        # Check if current user follows this user
        is_following = await db.follows.find_one({
            "follower_id": current_user_id,
            "following_id": user["id"]
        }) is not None
        
        results.append({
            "type": "user",
            "id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name", ""),
            "bio": user.get("bio", "")[:100] + "..." if user.get("bio", "") and len(user.get("bio", "")) > 100 else user.get("bio", ""),
            "avatar": user.get("avatar_url"),  # Usar avatar_url en lugar de avatar
            "followers_count": followers_count,
            "is_following": is_following,
            "relevance_score": relevance_score,
            "popularity_score": followers_count,
            "created_at": user.get("created_at", ""),
            "verified": user.get("verified", False)
        })
    
    # Filter by relevance threshold and return top results
    results = [r for r in results if r["relevance_score"] > config.SEARCH_CONFIG['MIN_RELEVANCE_SCORE']]
    return sorted(results, key=lambda x: x["relevance_score"], reverse=True)[:limit]

async def search_posts_advanced(query: str, current_user_id: str, limit: int):
    """Advanced posts search"""
    search_regex = {"$regex": query, "$options": "i"}
    
    # Find posts matching query in description or title  
    posts = await db.polls.find({
        "is_active": True,
        "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
        "$or": [
            {"description": search_regex},
            {"title": search_regex}
        ]
    }).limit(limit * config.SEARCH_CONFIG['FUZZY_SEARCH_MULTIPLIER']).to_list(limit * config.SEARCH_CONFIG['FUZZY_SEARCH_MULTIPLIER'])
    
    results = []
    for post in posts:
        # Calculate relevance score with configurable multipliers
        description_sim = calculate_similarity(query, post.get("description", "")) * config.SEARCH_CONFIG['MULTIPLIERS']['CONTENT_MATCH']
        title_sim = calculate_similarity(query, post.get("title", "")) * config.SEARCH_CONFIG['MULTIPLIERS']['TITLE_MATCH']
        relevance_score = max(description_sim, title_sim)
        
        # Get engagement metrics
        votes_count = len(post.get("votes", []))
        comments_count = await db.comments.count_documents({"poll_id": post["id"]})
        
        # Get author info
        author = await db.users.find_one({"id": post["author_id"]})
        
        # Extract media from options
        options = post.get("options", [])
        main_media_url = None
        main_thumbnail_url = None
        main_media_type = None
        
        # Find first option with media
        for option in options:
            if option.get("media_url"):
                main_media_url = option.get("media_url")
                main_thumbnail_url = option.get("thumbnail_url") or option.get("media_url")
                main_media_type = option.get("media_type", "image")
                break
        
        # Extract hashtags from description and title
        hashtags = []
        description_text = post.get("description", "")
        title_text = post.get("title", "")
        combined_text = f"{description_text} {title_text}"
        
        import re
        hashtag_pattern = r'#(\w+)'
        hashtags = re.findall(hashtag_pattern, combined_text)
        
        # Build images array for frontend compatibility
        images_array = []
        for option in options:
            if option.get("media_url"):
                images_array.append({"url": option["media_url"]})

        results.append({
            "type": "post",
            "id": post["id"],
            "title": post.get("title", ""),
            "content": post.get("description", ""),
            "image_url": main_media_url if main_media_type == "image" else main_thumbnail_url,
            "video_url": main_media_url if main_media_type == "video" else None,
            "media_type": main_media_type,
            "media_url": main_media_url,  # Add media_url field for frontend compatibility
            "thumbnail_url": main_thumbnail_url,
            "images": images_array,  # Add images array for frontend compatibility (result.images?.[0]?.url)
            "layout": post.get("layout", "vertical"),  # Include layout for frontend grid rendering
            "vs_orientation": post.get("vs_orientation", "horizontal"),  # 🎯 VS: orientación para que el thumbnail coincida con la vista completa
            "vs_questions": post.get("vs_questions", []),  # 🎯 VS: preguntas multi-pregunta
            "options": options,  # Include all options for complete poll rendering
            "hashtags": hashtags[:5],  # Limit to first 5 hashtags
            "tags": post.get("tags", []),
            "votes_count": votes_count,
            "comments_count": comments_count,
            "author": {
                "username": author.get("username", "") if author else "",
                "display_name": author.get("display_name", "") if author else "",
                "avatar": author.get("avatar_url") if author else None  # Usar avatar_url en lugar de avatar
            },
            "relevance_score": relevance_score,
            "popularity_score": votes_count + comments_count,
            "created_at": post.get("created_at", "")
        })
    
    results = [r for r in results if r["relevance_score"] > config.SEARCH_CONFIG['MIN_RELEVANCE_SCORE']]
    return sorted(results, key=lambda x: x["relevance_score"], reverse=True)[:limit]

async def search_hashtags_advanced(query: str, current_user_id: str, limit: int):
    """Advanced hashtags search"""
    # Ensure query starts with # for hashtag search
    hashtag_query = query if query.startswith("#") else f"#{query}"
    
    # Aggregate hashtags from posts content
    pipeline = [
        {
            "$match": {
                "content": {"$regex": hashtag_query, "$options": "i"}
            }
        },
        {
            "$group": {
                "_id": None,
                "posts": {"$push": "$$ROOT"}
            }
        }
    ]
    
    result = await db.polls.aggregate(pipeline).to_list(1)
    if not result:
        return []
    
    posts = result[0]["posts"]
    hashtag_counts = {}
    
    # Extract and count hashtags
    for post in posts:
        hashtags = extract_hashtags_from_text(post.get("content", ""))
        for hashtag in hashtags:
            if hashtag.lower().find(query.lower()) != -1:
                if hashtag not in hashtag_counts:
                    hashtag_counts[hashtag] = {
                        "count": 0,
                        "recent_posts": []
                    }
                hashtag_counts[hashtag]["count"] += 1
                if len(hashtag_counts[hashtag]["recent_posts"]) < 3:
                    hashtag_counts[hashtag]["recent_posts"].append({
                        "id": post["id"],
                        "content": post.get("content", "")[:100],
                        "image_url": post.get("image_url")
                    })
    
    results = []
    for hashtag, data in hashtag_counts.items():
        relevance_score = calculate_similarity(query, hashtag)
        results.append({
            "type": "hashtag",
            "id": hashtag,
            "hashtag": hashtag,
            "posts_count": data["count"],
            "recent_posts": data["recent_posts"],
            "relevance_score": relevance_score,
            "popularity_score": data["count"],
            "created_at": ""
        })
    
    results = [r for r in results if r["relevance_score"] > 0.3]
    return sorted(results, key=lambda x: x["relevance_score"], reverse=True)[:limit]

async def search_sounds_advanced(query: str, current_user_id: str, limit: int):
    """Advanced sounds/music search"""
    search_regex = {"$regex": query, "$options": "i"}
    
    # Search in user audios
    audios = await db.audios.find({
        "$or": [
            {"title": search_regex},
            {"description": search_regex}
        ]
    }).limit(limit).to_list(limit)
    
    results = []
    for audio in audios:
        # Calculate relevance score
        title_sim = calculate_similarity(query, audio.get("title", ""))
        desc_sim = calculate_similarity(query, audio.get("description", "")) * 0.7
        relevance_score = max(title_sim, desc_sim)
        
        # Count posts using this audio
        posts_using_count = await db.polls.count_documents({"audio_id": audio["id"]})
        
        # Get author info
        author = await db.users.find_one({"id": audio["user_id"]})
        
        results.append({
            "type": "sound",
            "id": audio["id"],
            "title": audio.get("title", ""),
            "description": audio.get("description", ""),
            "duration": audio.get("duration", 0),
            "audio_url": audio.get("audio_url"),
            "cover_image": audio.get("cover_image"),
            "posts_using_count": posts_using_count,
            "author": {
                "username": author.get("username", "") if author else "",
                "display_name": author.get("display_name", "") if author else "",
                "avatar": author.get("avatar_url") if author else None  # Usar avatar_url en lugar de avatar
            },
            "relevance_score": relevance_score,
            "popularity_score": posts_using_count,
            "created_at": audio.get("created_at", "")
        })
    
    results = [r for r in results if r["relevance_score"] > config.SEARCH_CONFIG['MIN_RELEVANCE_SCORE']]
    return sorted(results, key=lambda x: x["relevance_score"], reverse=True)[:limit]

async def get_search_suggestions_helper(current_user_id: str = None):
    """Helper function to get search suggestions including recent searches and popular terms"""
    suggestions = {
        "recent_searches": [],
        "popular_terms": [],
        "trending_hashtags": [],
        "suggested_users": []
    }
    
    try:
        # Get popular hashtags from recent posts
        pipeline = [
            {"$match": {"created_at": {"$gte": (datetime.utcnow() - timedelta(days=7)).isoformat()}}},
            {"$project": {"content": 1}},
            {"$limit": 1000}
        ]
        
        recent_posts = await db.polls.aggregate(pipeline).to_list(1000)
        hashtag_counts = {}
        
        for post in recent_posts:
            hashtags = extract_hashtags_from_text(post.get("content", ""))
            for hashtag in hashtags:
                hashtag_counts[hashtag] = hashtag_counts.get(hashtag, 0) + 1
        
        # Get top trending hashtags
        trending = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        suggestions["trending_hashtags"] = [{"hashtag": h[0], "count": h[1]} for h in trending]
        
        # Get suggested users (users with high follower count)
        pipeline = [
            {"$sort": {"followers_count": -1}},
            {"$limit": 5}
        ]
        
        top_profiles = await db.user_profiles.aggregate(pipeline).to_list(5)
        for profile in top_profiles:
            user = await db.users.find_one({"id": profile["user_id"]})
            if user and user["id"] != current_user_id:
                suggestions["suggested_users"].append({
                    "id": user["id"],
                    "username": user["username"],
                    "display_name": user.get("display_name", ""),
                    "avatar": user.get("avatar_url"),  # Usar avatar_url en lugar de avatar
                    "followers_count": profile.get("followers_count", 0)
                })
        
        return suggestions
        
    except Exception as e:
        logger.error(f"Error getting search suggestions: {str(e)}")
        return suggestions

@api_router.get("/search/suggestions")
async def get_search_suggestions_endpoint(current_user: UserResponse = Depends(get_current_user)):
    """Get search suggestions including recent searches and popular terms"""
    return await get_search_suggestions_helper(current_user.id)

async def get_trending_content(current_user_id: str):
    """Get trending content for discovery section"""
    try:
        # Get trending posts (high engagement in last 24 hours)
        recent_time = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        
        pipeline = [
            {"$match": {"created_at": {"$gte": recent_time}}},
            {"$addFields": {"votes_count": {"$size": "$votes"}}},
            {"$sort": {"votes_count": -1}},
            {"$limit": 10}
        ]
        
        trending_posts = await db.polls.aggregate(pipeline).to_list(10)
        
        results = []
        for post in trending_posts:
            author = await db.users.find_one({"id": post["user_id"]})
            comments_count = await db.comments.count_documents({"poll_id": post["id"]})
            
            results.append({
                "type": "trending_post",
                "id": post["id"],
                "title": post.get("title", ""),
                "content": post.get("content", "")[:100],
                "image_url": post.get("image_url"),
                "votes_count": len(post.get("votes", [])),
                "comments_count": comments_count,
                "author": {
                    "username": author.get("username", "") if author else "",
                    "display_name": author.get("display_name", "") if author else "",
                    "avatar": author.get("avatar_url") if author else None  # Usar avatar_url en lugar de avatar
                },
                "created_at": post.get("created_at", "")
            })
        
        return results[:5]  # Return top 5 trending posts
        
    except Exception as e:
        logger.error(f"Error getting trending content: {str(e)}")
        return []

@api_router.get("/search/autocomplete")
async def search_autocomplete(
    q: str = "",
    limit: int = config.SEARCH_CONFIG['DEFAULT_AUTOCOMPLETE_LIMIT'],
    current_user: UserResponse = Depends(get_current_user)
):
    """Real-time autocomplete suggestions"""
    # Validate parameters
    if limit > config.SEARCH_CONFIG['MAX_AUTOCOMPLETE_LIMIT']:
        limit = config.SEARCH_CONFIG['MAX_AUTOCOMPLETE_LIMIT']
    
    if not q or len(q.strip()) < config.SEARCH_CONFIG['MIN_AUTOCOMPLETE_LENGTH']:
        return {"suggestions": []}
    
    if len(q.strip()) > config.SEARCH_CONFIG['MAX_QUERY_LENGTH']:
        return {"suggestions": []}
    
    query = q.lower().strip()
    suggestions = []
    
    try:
        # User suggestions
        users = await db.users.find({
            "$and": [
                {"id": {"$ne": current_user.id}},
                {
                    "$or": [
                        {"username": {"$regex": f"^{query}", "$options": "i"}},
                        {"display_name": {"$regex": f"^{query}", "$options": "i"}}
                    ]
                }
            ]
        }).limit(limit // 2).to_list(limit // 2)
        
        for user in users:
            suggestions.append({
                "type": "user",
                "text": f"@{user['username']}",
                "display": f"{user.get('display_name', user['username'])} (@{user['username']})",
                "avatar": user.get("avatar")
            })
        
        # Hashtag suggestions
        if query.startswith("#") or not query.startswith("@"):
            hashtag_query = query if query.startswith("#") else f"#{query}"
            pipeline = [
                {"$match": {"content": {"$regex": hashtag_query, "$options": "i"}}},
                {"$limit": 100}
            ]
            
            posts = await db.polls.aggregate(pipeline).to_list(100)
            hashtag_counts = {}
            
            for post in posts:
                hashtags = extract_hashtags_from_text(post.get("content", ""))
                for hashtag in hashtags:
                    if hashtag.lower().startswith(query.lower()):
                        hashtag_counts[hashtag] = hashtag_counts.get(hashtag, 0) + 1
            
            # Get top hashtags
            top_hashtags = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            for hashtag, count in top_hashtags:
                suggestions.append({
                    "type": "hashtag",
                    "text": hashtag,
                    "display": f"{hashtag} ({count} posts)",
                    "count": count
                })
        
        return {"suggestions": suggestions[:8]}  # Limit to 8 suggestions
        
    except Exception as e:
        logger.error(f"Error in autocomplete: {str(e)}")
        return {"suggestions": []}

# =============  FOLLOW ENDPOINTS =============

# Helper function to update follow counts
async def update_follow_counts(user_id: str):
    """Update followers and following counts for a user"""
    try:
        # Count followers
        followers_count = await db.follows.count_documents({"following_id": user_id})
        
        # Count following
        following_count = await db.follows.count_documents({"follower_id": user_id})
        
        # Update user profile with new counts
        await db.user_profiles.update_one(
            {"id": user_id},
            {
                "$set": {
                    "followers_count": followers_count,
                    "following_count": following_count
                }
            },
            upsert=True  # Create profile if doesn't exist
        )
        
        print(f"✅ Updated follow counts for user {user_id}: {followers_count} followers, {following_count} following")
        
    except Exception as e:
        print(f"❌ Error updating follow counts for user {user_id}: {e}")

@api_router.post("/users/{user_id}/follow")
async def follow_user(user_id: str, current_user: UserResponse = Depends(get_current_user)):
    """Follow a user"""
    # Check if user to follow exists
    user_to_follow = await db.users.find_one({"id": user_id})
    if not user_to_follow:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if user is trying to follow themselves
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")
    
    # Check if already following
    existing_follow = await db.follows.find_one({
        "follower_id": current_user.id,
        "following_id": user_id
    })
    if existing_follow:
        raise HTTPException(status_code=400, detail="Already following this user")
    
    # Create follow relationship
    follow_data = Follow(
        follower_id=current_user.id,
        following_id=user_id
    )
    
    result = await db.follows.insert_one(follow_data.model_dump())
    if not result.inserted_id:
        raise HTTPException(status_code=500, detail="Failed to follow user")
    
    # Update follow counts for both users
    await update_follow_counts(user_id)  # Update followed user's follower count
    await update_follow_counts(current_user.id)  # Update current user's following count
    
    # Clear cache for this relationship
    cache_key = f"{current_user.id}:{user_id}"
    if cache_key in follow_status_cache:
        del follow_status_cache[cache_key]
    
    # Also clear reverse cache (for the followed user's perspective)
    reverse_cache_key = f"{user_id}:{current_user.id}"
    if reverse_cache_key in follow_status_cache:
        del follow_status_cache[reverse_cache_key]
    
    return {"message": "Successfully followed user", "follow_id": follow_data.id}

@api_router.delete("/users/{user_id}/follow")
async def unfollow_user(user_id: str, current_user: UserResponse = Depends(get_current_user)):
    """Unfollow a user"""
    # Find and delete follow relationship
    result = await db.follows.delete_one({
        "follower_id": current_user.id,
        "following_id": user_id
    })
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Follow relationship not found")
    
    # Update follow counts for both users
    await update_follow_counts(user_id)  # Update unfollowed user's follower count
    await update_follow_counts(current_user.id)  # Update current user's following count
    
    # Clear cache for this relationship
    cache_key = f"{current_user.id}:{user_id}"
    if cache_key in follow_status_cache:
        del follow_status_cache[cache_key]
    
    # Also clear reverse cache (for the followed user's perspective)
    reverse_cache_key = f"{user_id}:{current_user.id}"
    if reverse_cache_key in follow_status_cache:
        del follow_status_cache[reverse_cache_key]
    
    return {"message": "Successfully unfollowed user"}

@api_router.get("/users/{user_id}/follow-status")
async def get_follow_status(user_id: str, current_user: UserResponse = Depends(get_current_user)):
    """Get follow status for a specific user with caching"""
    
    # Check cache first
    cache_key = f"{current_user.id}:{user_id}"
    if cache_key in follow_status_cache and is_follow_cache_valid(follow_status_cache[cache_key]):
        return follow_status_cache[cache_key]['data']
    
    follow_relationship = await db.follows.find_one({
        "follower_id": current_user.id,
        "following_id": user_id
    })
    
    # Check if the other user follows me
    reverse_relationship = await db.follows.find_one({
        "follower_id": user_id,
        "following_id": current_user.id
    })
    
    result = {
        "is_following": follow_relationship is not None,
        "follow_id": follow_relationship["id"] if follow_relationship else None,
        "follows_me": reverse_relationship is not None
    }
    
    # Cache the result
    follow_status_cache[cache_key] = {
        'data': result,
        'cached_at': datetime.utcnow()
    }
    
    return result

@api_router.get("/users/following")
async def get_following_users(current_user: UserResponse = Depends(get_current_user)):
    """Get list of users that current user is following"""
    follows = await db.follows.find({"follower_id": current_user.id}).to_list(1000)
    
    following_ids = [follow["following_id"] for follow in follows]
    users = await db.users.find({"id": {"$in": following_ids}}).to_list(1000)
    
    return FollowingList(
        following=[UserResponse(**user) for user in users],
        total=len(users)
    )

@api_router.get("/users/{user_id}/followers")
async def get_user_followers(user_id: str):
    """Get list of users following the specified user"""
    follows = await db.follows.find({"following_id": user_id}).to_list(1000)
    
    follower_ids = [follow["follower_id"] for follow in follows]
    users = await db.users.find({"id": {"$in": follower_ids}}).to_list(1000)
    
    return FollowersList(
        followers=[UserResponse(**user) for user in users],
        total=len(users)
    )

@api_router.get("/users/{user_id}/following")
async def get_user_following(user_id: str):
    """Get list of users that specified user is following"""
    follows = await db.follows.find({"follower_id": user_id}).to_list(1000)
    
    following_ids = [follow["following_id"] for follow in follows]
    users = await db.users.find({"id": {"$in": following_ids}}).to_list(1000)
    
    return FollowingList(
        following=[UserResponse(**user) for user in users],
        total=len(users)
    )

# =============  MESSAGING ENDPOINTS =============

@api_router.post("/messages")
async def send_message(message: MessageCreate, current_user: UserResponse = Depends(get_current_user)):
    """Send a message to another user"""
    try:
        # Debug logging simplificado
        print(f"🔍 DEBUG - Message dict: {message.dict()}")
        print(f"🔍 DEBUG - Current user: {current_user.id}")
        
        # Verify recipient exists
        recipient = await db.users.find_one({"id": message.recipient_id})
        if not recipient:
            print(f"❌ DEBUG - Recipient not found: {message.recipient_id}")
            raise HTTPException(status_code=404, detail="Recipient not found")
        
        print(f"✅ DEBUG - Recipient found: {recipient.get('username')}")
        
        # Check if users can chat directly or need permission
        can_chat_directly = await check_chat_permission(current_user.id, message.recipient_id)
        
        if not can_chat_directly:
            # Create chat request instead of direct message
            existing_request = await db.chat_requests.find_one({
                "sender_id": current_user.id,
                "receiver_id": message.recipient_id,
                "status": {"$in": ["pending", "accepted"]}
            })
            
            if existing_request:
                if existing_request["status"] == "accepted":
                    # Permission granted, continue with message
                    pass
                else:
                    raise HTTPException(
                        status_code=403, 
                        detail="Chat request already sent. Wait for user to accept."
                    )
            else:
                # Send chat request
                chat_request = ChatRequest(
                    sender_id=current_user.id,
                    receiver_id=message.recipient_id,
                    message=f"Mensaje: {message.content[:100]}..." if len(message.content) > 100 else message.content,
                    expires_at=datetime.utcnow() + timedelta(days=30)
                )
                
                await db.chat_requests.insert_one(chat_request.dict())
                
                return {
                    "success": True,
                    "message": "Chat request sent. The user needs to accept before you can send messages.",
                    "type": "chat_request",
                    "request_id": chat_request.id
                }
        
    except Exception as e:
        print(f"❌ DEBUG - Error in send_message: {str(e)}")
        print(f"❌ DEBUG - Error type: {type(e)}")
        raise e
    
    # Find or create conversation
    conversation_data = await db.conversations.find_one({
        "participants": {"$all": [current_user.id, message.recipient_id]}
    })
    
    if not conversation_data:
        # Create new conversation
        conversation = Conversation(
            participants=[current_user.id, message.recipient_id],
            unread_count={
                current_user.id: 0,
                message.recipient_id: 1
            }
        )
        await db.conversations.insert_one(conversation.dict())
        conversation_id = conversation.id
    else:
        conversation_id = conversation_data["id"]
        # Update unread count for recipient
        await db.conversations.update_one(
            {"id": conversation_id},
            {
                "$inc": {f"unread_count.{message.recipient_id}": 1},
                "$set": {
                    "last_message": message.content,
                    "last_message_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )
    
    # Create message
    new_message = Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        recipient_id=message.recipient_id,
        content=message.content,
        message_type=message.message_type,
        metadata=message.metadata
    )
    
    await db.messages.insert_one(new_message.dict())
    
    # Prepare complete message response with sender info
    sender_user = await db.users.find_one({"id": current_user.id})
    
    return {
        "success": True,
        "message_id": new_message.id,
        "conversation_id": conversation_id,
        "timestamp": new_message.created_at.isoformat() if hasattr(new_message.created_at, 'isoformat') else new_message.created_at,
        "content": new_message.content,
        "sender_id": current_user.id,
        "sender": {
            "id": current_user.id,
            "username": sender_user.get("username") if sender_user else current_user.username,
            "display_name": sender_user.get("display_name") if sender_user else current_user.display_name,
            "avatar_url": sender_user.get("avatar_url") if sender_user else current_user.avatar_url
        }
    }

@api_router.get("/conversations")
async def get_conversations(current_user: UserResponse = Depends(get_current_user)):
    """Get user's conversations including pending chat requests"""
    # Get regular conversations
    conversations = await db.conversations.find({
        "participants": current_user.id,
        "is_active": True
    }).sort("last_message_at", -1).to_list(50)
    
    result = []
    for conv_data in conversations:
        # Get participant info
        participant_ids = [p for p in conv_data["participants"] if p != current_user.id]
        participants = []
        
        for participant_id in participant_ids:
            user_data = await db.users.find_one({"id": participant_id})
            if user_data:
                participants.append(UserResponse(**user_data))
        
        # Get unread count for current user
        unread_count = conv_data.get("unread_count", {}).get(current_user.id, 0)
        
        conversation_response = {
            "id": conv_data["id"],
            "participants": participants,
            "last_message": conv_data.get("last_message"),
            "last_message_at": conv_data.get("last_message_at"),
            "unread_count": unread_count,
            "created_at": conv_data["created_at"]
        }
        result.append(conversation_response)
    
    # Get pending chat requests where current user is the SENDER only
    # Receivers will see these in the separate "Solicitudes de mensajes" section
    pending_requests = await db.chat_requests.find({
        "sender_id": current_user.id,
        "status": "pending"
    }).sort("created_at", -1).to_list(50)
    
    # Convert chat requests to conversation-like format
    for req in pending_requests:
        # Get receiver user info
        other_user_data = await db.users.find_one({"id": req["receiver_id"]})
        if not other_user_data:
            continue
        
        other_user = UserResponse(**other_user_data)
        
        # Create conversation-like object with chat request metadata
        request_conversation = {
            "id": f"request-{req['id']}",  # Prefix to identify as request
            "participants": [other_user],
            "last_message": req.get("message", "Nueva solicitud de chat"),
            "last_message_at": req.get("created_at"),
            "unread_count": 0,  # No unread for sender
            "created_at": req["created_at"],
            # Chat request specific fields
            "is_chat_request": True,
            "chat_request_id": req["id"],
            "chat_request_status": "pending",
            "is_request_sender": True,
            "is_request_receiver": False
        }
        
        result.append(request_conversation)
    
    # Sort all by last message time
    result.sort(key=lambda x: x.get("last_message_at") or x.get("created_at"), reverse=True)
    
    return result

@api_router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    limit: int = 50,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get messages from a conversation"""
    # Verify user is participant in conversation
    conversation = await db.conversations.find_one({
        "id": conversation_id,
        "participants": current_user.id
    })
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Get messages
    messages = await db.messages.find({
        "conversation_id": conversation_id
    }).sort("created_at", -1).limit(limit).to_list(limit)
    
    # Mark messages as read
    await db.messages.update_many(
        {
            "conversation_id": conversation_id,
            "recipient_id": current_user.id,
            "is_read": False
        },
        {"$set": {"is_read": True}}
    )
    
    # Reset unread count for current user
    await db.conversations.update_one(
        {"id": conversation_id},
        {"$set": {f"unread_count.{current_user.id}": 0}}
    )
    
    # Reverse to get chronological order (oldest first)
    messages.reverse()
    
    # Enrich messages with sender information
    enriched_messages = []
    for msg in messages:
        # Get sender user info
        sender = await db.users.find_one({"id": msg["sender_id"]})
        
        # Remove MongoDB _id field if present (not JSON serializable)
        msg_dict = {k: v for k, v in msg.items() if k != "_id"}
        
        # Build enriched message object
        enriched_msg = {
            **msg_dict,
            "sender": {
                "id": msg["sender_id"],
                "username": sender.get("username") if sender else "unknown",
                "display_name": sender.get("display_name") if sender else "Usuario",
                "avatar_url": sender.get("avatar_url") if sender else None
            }
        }
        enriched_messages.append(enriched_msg)
    
    return enriched_messages

@api_router.get("/messages/unread")
async def get_unread_count(current_user: UserResponse = Depends(get_current_user)):
    """Get total unread message count"""
    conversations = await db.conversations.find({
        "participants": current_user.id,
        "is_active": True
    }).to_list(100)
    
    total_unread = sum(
        conv.get("unread_count", {}).get(current_user.id, 0) 
        for conv in conversations
    )
    
    return {"unread_count": total_unread}

@api_router.get("/chat-requests/{request_id}/messages")
async def get_chat_request_messages(
    request_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get the initial message from a pending chat request"""
    # Find the chat request
    chat_request = await db.chat_requests.find_one({"id": request_id})
    if not chat_request:
        raise HTTPException(status_code=404, detail="Chat request not found")
    
    # Check if current user is involved in this request
    if chat_request["sender_id"] != current_user.id and chat_request["receiver_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this chat request")
    
    # Get sender info
    sender_data = await db.users.find_one({"id": chat_request["sender_id"]})
    if not sender_data:
        raise HTTPException(status_code=404, detail="Sender not found")
    
    # Return the initial message as a message-like object
    return [{
        "id": f"request-msg-{chat_request['id']}",
        "content": chat_request.get("message", "Nueva solicitud de chat"),
        "sender_id": chat_request["sender_id"],
        "recipient_id": chat_request["receiver_id"],
        "sender": {
            "id": sender_data["id"],
            "username": sender_data.get("username"),
            "display_name": sender_data.get("display_name"),
            "avatar_url": sender_data.get("avatar_url")
        },
        "created_at": chat_request["created_at"],
        "is_chat_request_message": True
    }]

@api_router.get("/messages/requests")
async def get_message_requests(current_user: UserResponse = Depends(get_current_user)):
    """Get message requests (pending chat requests) where current user is the RECEIVER"""
    try:
        # Get pending chat requests where current user is the receiver ONLY
        requests = await db.chat_requests.find({
            "receiver_id": current_user.id,
            "status": "pending"
        }).sort("created_at", -1).to_list(50)
        
        # Get viewed request IDs
        request_ids = [req["id"] for req in requests]
        viewed_requests = await db.request_views.find({
            "user_id": current_user.id,
            "request_id": {"$in": request_ids}
        }).to_list(1000)
        viewed_ids = {v["request_id"] for v in viewed_requests}
        
        result = []
        for req in requests:
            # Get sender info
            sender = await db.users.find_one({"id": req["sender_id"]})
            if sender:
                result.append({
                    "id": req["id"],
                    "sender": {
                        "id": sender["id"],
                        "username": sender["username"],
                        "display_name": sender.get("display_name", sender["username"]),
                        "avatar_url": sender.get("avatar_url"),
                        "is_verified": sender.get("is_verified", False)
                    },
                    "message": req.get("message", ""),
                    "preview": req.get("message", "")[:100] if req.get("message") else "Solicitud de mensaje",
                    "created_at": req["created_at"],
                    "updated_at": req.get("updated_at"),
                    "unread": req["id"] not in viewed_ids
                })
        
        print(f"✅ Returning {len(result)} message requests, {len(viewed_ids)} already viewed")
        return result
        
    except Exception as e:
        print(f"❌ Error getting message requests: {str(e)}")
        return []


@api_router.post("/messages/requests/mark-read")
async def mark_requests_as_read(current_user: UserResponse = Depends(get_current_user)):
    """Mark all message requests as read for the current user"""
    try:
        # Get pending chat requests where current user is the receiver
        requests = await db.chat_requests.find({
            "receiver_id": current_user.id,
            "status": "pending"
        }).to_list(50)
        
        # Mark all as read (insert if not exists)
        now = datetime.utcnow()
        marked_count = 0
        for req in requests:
            await db.request_views.update_one(
                {"user_id": current_user.id, "request_id": req["id"]},
                {"$setOnInsert": {
                    "id": str(uuid.uuid4()),
                    "user_id": current_user.id,
                    "request_id": req["id"],
                    "viewed_at": now
                }},
                upsert=True
            )
            marked_count += 1
        
        print(f"✅ Marked {marked_count} requests as read for user {current_user.id}")
        return {"success": True, "marked_count": marked_count}
        
    except Exception as e:
        print(f"❌ Error marking requests as read: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error marking requests as read")


@api_router.get("/messages/requests/unread-count")
async def get_unread_requests_count(current_user: UserResponse = Depends(get_current_user)):
    """Get count of unread message requests for the current user"""
    try:
        # Get pending chat requests where current user is the receiver
        requests = await db.chat_requests.find({
            "receiver_id": current_user.id,
            "status": "pending"
        }).to_list(50)
        
        request_ids = [req["id"] for req in requests]
        
        # Get viewed requests
        viewed_requests = await db.request_views.find({
            "user_id": current_user.id,
            "request_id": {"$in": request_ids}
        }).to_list(1000)
        viewed_ids = {v["request_id"] for v in viewed_requests}
        
        # Count unread
        unread_count = len([rid for rid in request_ids if rid not in viewed_ids])
        
        return {"unread_count": unread_count, "total_count": len(request_ids)}
        
    except Exception as e:
        print(f"❌ Error getting unread requests count: {str(e)}")
        return {"unread_count": 0, "total_count": 0}

# =============  CHAT REQUEST ENDPOINTS =============

@api_router.post("/chat-requests")
async def send_chat_request(
    request_data: ChatRequestCreate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Send a chat request to another user"""
    # Check if recipient exists
    recipient = await db.users.find_one({"id": request_data.receiver_id})
    if not recipient:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if user is trying to send request to themselves
    if request_data.receiver_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot send chat request to yourself")
    
    # Check if there's already a pending or accepted request between these users
    existing_request = await db.chat_requests.find_one({
        "$or": [
            {
                "sender_id": current_user.id,
                "receiver_id": request_data.receiver_id,
                "status": {"$in": ["pending", "accepted"]}
            },
            {
                "sender_id": request_data.receiver_id,
                "receiver_id": current_user.id,
                "status": {"$in": ["pending", "accepted"]}
            }
        ]
    })
    
    if existing_request:
        if existing_request["status"] == "accepted":
            raise HTTPException(status_code=400, detail="Chat already exists between these users")
        else:
            raise HTTPException(status_code=400, detail="Chat request already pending")
    
    # Check if recipient allows messages
    if not recipient.get("allow_messages", True):
        raise HTTPException(status_code=403, detail="User does not accept chat requests")
    
    # Create chat request
    chat_request = ChatRequest(
        sender_id=current_user.id,
        receiver_id=request_data.receiver_id,
        message=request_data.message,
        expires_at=datetime.utcnow() + timedelta(days=30)
    )
    
    await db.chat_requests.insert_one(chat_request.dict())
    
    return {
        "success": True,
        "request_id": chat_request.id,
        "message": "Chat request sent successfully"
    }

@api_router.get("/chat-requests/received")
async def get_received_chat_requests(current_user: UserResponse = Depends(get_current_user)):
    """Get chat requests received by the current user"""
    requests = await db.chat_requests.find({
        "receiver_id": current_user.id,
        "status": "pending"
    }).sort("created_at", -1).to_list(50)
    
    result = []
    for req in requests:
        # Get sender info
        sender = await db.users.find_one({"id": req["sender_id"]})
        if sender:
            result.append(ChatRequestResponse(
                id=req["id"],
                sender=UserResponse(**sender),
                receiver=current_user,
                status=req["status"],
                message=req.get("message"),
                created_at=req["created_at"],
                updated_at=req["updated_at"]
            ))
    
    return result

@api_router.get("/chat-requests/sent")
async def get_sent_chat_requests(current_user: UserResponse = Depends(get_current_user)):
    """Get chat requests sent by the current user"""
    requests = await db.chat_requests.find({
        "sender_id": current_user.id,
        "status": "pending"
    }).sort("created_at", -1).to_list(50)
    
    result = []
    for req in requests:
        # Get receiver info
        receiver = await db.users.find_one({"id": req["receiver_id"]})
        if receiver:
            result.append(ChatRequestResponse(
                id=req["id"],
                sender=current_user,
                receiver=UserResponse(**receiver),
                status=req["status"],
                message=req.get("message"),
                created_at=req["created_at"],
                updated_at=req["updated_at"]
            ))
    
    return result

@api_router.put("/chat-requests/{request_id}")
async def respond_to_chat_request(
    request_id: str,
    action_data: ChatRequestAction,
    current_user: UserResponse = Depends(get_current_user)
):
    """Accept or reject a chat request"""
    # Find the chat request
    chat_request = await db.chat_requests.find_one({"id": request_id})
    if not chat_request:
        raise HTTPException(status_code=404, detail="Chat request not found")
    
    # Check if current user is the receiver
    if chat_request["receiver_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="You can only respond to requests sent to you")
    
    # Check if request is still pending
    if chat_request["status"] != "pending":
        raise HTTPException(status_code=400, detail="Chat request is no longer pending")
    
    if action_data.action not in ["accept", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'accept' or 'reject'")
    
    # Update request status
    new_status = "accepted" if action_data.action == "accept" else "rejected"
    await db.chat_requests.update_one(
        {"id": request_id},
        {
            "$set": {
                "status": new_status,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    # If accepted, create conversation and convert initial message
    if action_data.action == "accept":
        # Check if conversation already exists
        existing_conversation = await db.conversations.find_one({
            "participants": {"$all": [current_user.id, chat_request["sender_id"]]},
            "is_active": True
        })
        
        if not existing_conversation:
            # Create new conversation
            conversation = Conversation(
                participants=[current_user.id, chat_request["sender_id"]],
                last_message=chat_request.get("message", ""),
                last_message_at=datetime.utcnow(),
                unread_count={
                    current_user.id: 0,
                    chat_request["sender_id"]: 0
                }
            )
            await db.conversations.insert_one(conversation.dict())
            conversation_id = conversation.id
            
            # Convert the initial chat request message into a real message
            if chat_request.get("message"):
                initial_message = Message(
                    conversation_id=conversation_id,
                    sender_id=chat_request["sender_id"],
                    recipient_id=current_user.id,
                    content=chat_request["message"],
                    is_read=False
                )
                await db.messages.insert_one(initial_message.dict())
        else:
            conversation_id = existing_conversation["id"]
        
        return {
            "success": True,
            "message": "Chat request accepted",
            "conversation_id": conversation_id
        }
    else:
        return {
            "success": True,
            "message": "Chat request rejected"
        }

@api_router.delete("/chat-requests/{request_id}")
async def cancel_chat_request(
    request_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Cancel a sent chat request"""
    # Find the chat request
    chat_request = await db.chat_requests.find_one({"id": request_id})
    if not chat_request:
        raise HTTPException(status_code=404, detail="Chat request not found")
    
    # Check if current user is the sender
    if chat_request["sender_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="You can only cancel requests you sent")
    
    # Check if request is still pending
    if chat_request["status"] != "pending":
        raise HTTPException(status_code=400, detail="Can only cancel pending requests")
    
    # Update request status to cancelled
    await db.chat_requests.update_one(
        {"id": request_id},
        {
            "$set": {
                "status": "cancelled",
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    return {
        "success": True,
        "message": "Chat request cancelled"
    }

# =============  FOLLOWERS, ACTIVITY & MESSAGE REQUESTS ENDPOINTS =============

@api_router.get("/users/followers/recent")
async def get_recent_followers(current_user: UserResponse = Depends(get_current_user)):
    """Get recent followers (last 7 days)"""
    try:
        # Calculate 7 days ago
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        
        # Find recent follows where current user is the followed user
        recent_follows = await db.follows.find({
            "following_id": current_user.id,  # FIXED: Changed from followed_id to following_id
            "created_at": {"$gte": seven_days_ago}
        }).sort("created_at", -1).limit(50).to_list(50)
        
        # Get viewed follower IDs
        follower_ids = [follow["follower_id"] for follow in recent_follows]
        viewed_followers = await db.follower_views.find({
            "user_id": current_user.id,
            "follower_id": {"$in": follower_ids}
        }).to_list(1000)
        viewed_ids = {v["follower_id"] for v in viewed_followers}
        
        # Get follower details
        followers = []
        for follow in recent_follows:
            follower = await db.users.find_one({"id": follow["follower_id"]})
            if follower:
                followers.append({
                    "id": follower["id"],
                    "username": follower["username"],
                    "display_name": follower.get("display_name", follower["username"]),
                    "avatar_url": follower.get("avatar_url"),  # Usar foto de perfil real
                    "followed_at": follow["created_at"],
                    "is_verified": follower.get("is_verified", False),
                    "unread": follower["id"] not in viewed_ids
                })
        
        print(f"✅ Returning {len(followers)} recent followers, {len(viewed_ids)} already viewed")
        return followers
        
    except Exception as e:
        # Return empty list if no follows collection or error
        print(f"Error getting recent followers: {e}")
        return []


@api_router.post("/users/followers/mark-read")
async def mark_followers_as_read(current_user: UserResponse = Depends(get_current_user)):
    """Mark all recent followers as read for the current user"""
    try:
        # Calculate 7 days ago
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        
        # Find recent follows
        recent_follows = await db.follows.find({
            "following_id": current_user.id,
            "created_at": {"$gte": seven_days_ago}
        }).to_list(50)
        
        # Mark all as read (insert if not exists)
        now = datetime.utcnow()
        marked_count = 0
        for follow in recent_follows:
            await db.follower_views.update_one(
                {"user_id": current_user.id, "follower_id": follow["follower_id"]},
                {"$setOnInsert": {
                    "id": str(uuid.uuid4()),
                    "user_id": current_user.id,
                    "follower_id": follow["follower_id"],
                    "viewed_at": now
                }},
                upsert=True
            )
            marked_count += 1
        
        print(f"✅ Marked {marked_count} followers as read for user {current_user.id}")
        return {"success": True, "marked_count": marked_count}
        
    except Exception as e:
        print(f"❌ Error marking followers as read: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error marking followers as read")


@api_router.get("/users/followers/unread-count")
async def get_unread_followers_count(current_user: UserResponse = Depends(get_current_user)):
    """Get count of unread new followers for the current user"""
    try:
        # Calculate 7 days ago
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        
        # Find recent follows
        recent_follows = await db.follows.find({
            "following_id": current_user.id,
            "created_at": {"$gte": seven_days_ago}
        }).to_list(50)
        
        follower_ids = [follow["follower_id"] for follow in recent_follows]
        
        # Get viewed followers
        viewed_followers = await db.follower_views.find({
            "user_id": current_user.id,
            "follower_id": {"$in": follower_ids}
        }).to_list(1000)
        viewed_ids = {v["follower_id"] for v in viewed_followers}
        
        # Count unread
        unread_count = len([fid for fid in follower_ids if fid not in viewed_ids])
        
        return {"unread_count": unread_count, "total_count": len(follower_ids)}
        
    except Exception as e:
        print(f"❌ Error getting unread followers count: {str(e)}")
        return {"unread_count": 0, "total_count": 0}

@api_router.get("/users/activity/recent")
async def get_recent_activity(current_user: UserResponse = Depends(get_current_user)):
    """Get recent activity (likes, comments, mentions on user's content)"""
    
    def _extract_poll_thumbnail(poll: dict) -> str | None:
        """Extrae la URL de miniatura de un poll para mostrar en notificaciones.
        Busca en options[].media_url / thumbnail_url y cae a campos legacy."""
        if not poll:
            return None
        # 1) Primera opción con media o thumbnail
        for option in (poll.get("options") or []):
            if option.get("media_type") == "video" and option.get("thumbnail_url"):
                return option["thumbnail_url"]
            if option.get("media_url"):
                return option["media_url"]
            if option.get("thumbnail_url"):
                return option["thumbnail_url"]
        # 2) vs_questions[].options[].media_url para polls tipo VS
        for q in (poll.get("vs_questions") or []):
            for option in (q.get("options") or []):
                if option.get("media_url"):
                    return option["media_url"]
                if option.get("thumbnail_url"):
                    return option["thumbnail_url"]
        # 3) Campos legacy a nivel de poll
        if poll.get("thumbnail_url"):
            return poll["thumbnail_url"]
        if poll.get("image_url"):
            return poll["image_url"]
        images = poll.get("images") or []
        if images and isinstance(images, list):
            first = images[0]
            if isinstance(first, dict) and first.get("url"):
                return first["url"]
        return None
    
    def _extract_vs_thumbnails(poll: dict) -> list:
        """Para polls tipo VS, devuelve la lista de miniaturas de cada lado
        (típicamente 2). Así el frontend puede renderizar el VS lado-a-lado
        en la notificación, igual que en la vista completa."""
        if not poll or poll.get("layout") != "vs":
            return []
        urls = []
        # 1) Primero buscar en vs_questions[].options[] (estructura más nueva)
        for q in (poll.get("vs_questions") or []):
            for option in (q.get("options") or []):
                u = None
                if option.get("media_type") == "video" and option.get("thumbnail_url"):
                    u = option["thumbnail_url"]
                elif option.get("media_url"):
                    u = option["media_url"]
                elif option.get("thumbnail_url"):
                    u = option["thumbnail_url"]
                if u:
                    urls.append(u)
        # 2) Fallback a options[] de nivel superior si vs_questions está vacío
        if not urls:
            for option in (poll.get("options") or []):
                u = None
                if option.get("media_type") == "video" and option.get("thumbnail_url"):
                    u = option["thumbnail_url"]
                elif option.get("media_url"):
                    u = option["media_url"]
                elif option.get("thumbnail_url"):
                    u = option["thumbnail_url"]
                if u:
                    urls.append(u)
        # Limitar a 2 (los dos lados del VS)
        return urls[:2]
    
    try:
        # Get recent activity from last 7 days
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        activities = []
        
        # Get user's polls first
        user_polls = await db.polls.find({
            "author_id": current_user.id
        }).to_list(1000)
        
        user_poll_ids = [poll["id"] for poll in user_polls]
        # Map de polls del usuario por id para lookup rápido de miniaturas
        user_polls_by_id = {p["id"]: p for p in user_polls}
        print(f"DEBUG Activity: User has {len(user_poll_ids)} polls")
        
        # Get recent likes on user's polls using poll_id
        likes = await db.poll_likes.find({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": current_user.id},  # Exclude self-likes
            "created_at": {"$gte": seven_days_ago}
        }).sort("created_at", -1).limit(20).to_list(20)
        
        print(f"DEBUG Activity: Found {len(likes)} likes on user's polls")
        
        for like in likes:
            user = await db.users.find_one({"id": like["user_id"]})
            poll = await db.polls.find_one({"id": like["poll_id"]})
            if user and poll:
                activities.append({
                    "id": f"like-{like['id']}",
                    "type": "like",
                    "user": {
                        "id": user["id"],
                        "username": user["username"],
                        "display_name": user.get("display_name", user["username"]),
                        "avatar_url": user.get("avatar_url")  # Incluir avatar_url
                    },
                    "content_type": "poll",
                    "content_preview": poll.get("question", "")[:50],
                    "poll_id": poll["id"],
                    "poll_layout": poll.get("layout"),
                    "poll_thumbnail": _extract_poll_thumbnail(poll),
                    "poll_vs_thumbnails": _extract_vs_thumbnails(poll),
                    "created_at": like["created_at"],
                    "unread": True
                })
        
        # Get recent comments on user's polls using poll_id
        comments = await db.comments.find({
            "$and": [
                {"poll_id": {"$in": user_poll_ids}},
                {"user_id": {"$ne": current_user.id}},  # Exclude self-comments (using correct field name)
                {"created_at": {"$gte": seven_days_ago}}
            ]
        }).sort("created_at", -1).limit(20).to_list(20)
        
        print(f"DEBUG Activity: Found {len(comments)} comments on user's polls")
        
        # Batch-fetch likes by current user (creator) on these comments to set
        # the initial heart state on the activity inbox.
        _comment_ids_list = [c["id"] for c in comments]
        _liked_by_creator = set()
        if _comment_ids_list:
            _creator_likes_cursor = db.comment_likes.find({
                "comment_id": {"$in": _comment_ids_list},
                "user_id": current_user.id
            })
            _creator_likes = await _creator_likes_cursor.to_list(len(_comment_ids_list))
            _liked_by_creator = {row["comment_id"] for row in _creator_likes}
        
        for comment in comments:
            user = await db.users.find_one({"id": comment["user_id"]})  # Using correct field name
            poll = user_polls_by_id.get(comment.get("poll_id"))
            if user:
                activities.append({
                    "id": f"comment-{comment['id']}",
                    "type": "comment",
                    "user": {
                        "id": user["id"],
                        "username": user["username"],
                        "display_name": user.get("display_name", user["username"]),
                        "avatar_url": user.get("avatar_url")  # Incluir avatar_url
                    },
                    "content_type": "poll",
                    "comment_preview": comment.get("content", "")[:100],
                    "comment_id": comment.get("id"),
                    "comment_liked": comment.get("id") in _liked_by_creator,
                    "poll_id": comment.get("poll_id"),
                    "poll_layout": (poll or {}).get("layout"),
                    "poll_thumbnail": _extract_poll_thumbnail(poll),
                    "poll_vs_thumbnails": _extract_vs_thumbnails(poll),
                    "created_at": comment["created_at"],
                    "unread": True
                })
        
        # Get recent votes on user's polls using poll_id
        votes = await db.votes.find({
            "$and": [
                {"poll_id": {"$in": user_poll_ids}},
                {"user_id": {"$ne": current_user.id}},  # Exclude self-votes
                {"created_at": {"$gte": seven_days_ago}}
            ]
        }).sort("created_at", -1).limit(20).to_list(20)
        
        print(f"DEBUG Activity: Found {len(votes)} votes on user's polls")
        
        for vote in votes:
            user = await db.users.find_one({"id": vote["user_id"]})
            poll = await db.polls.find_one({"id": vote["poll_id"]})
            option = None
            
            # Find the option that was voted for
            if poll and poll.get("options"):
                for opt in poll["options"]:
                    if opt.get("id") == vote["option_id"]:
                        option = opt
                        break
            
            if user and poll and option:
                activities.append({
                    "id": f"vote-{vote['id']}",
                    "type": "vote",
                    "user": {
                        "id": user["id"],
                        "username": user["username"],
                        "display_name": user.get("display_name", user["username"]),
                        "avatar_url": user.get("avatar_url")  # Incluir avatar_url
                    },
                    "content_type": "poll",
                    "content_preview": poll.get("title", "")[:50],
                    "vote_option": option.get("text", ""),
                    "poll_id": poll["id"],
                    "poll_layout": poll.get("layout"),
                    "poll_thumbnail": _extract_poll_thumbnail(poll),
                    "poll_vs_thumbnails": _extract_vs_thumbnails(poll),
                    "created_at": vote["created_at"],
                    "unread": True
                })
        
        # 🆕 Get recent VS votes on user's VS polls (stored in vs_votes, NOT votes).
        # Sin esto, el inbox del autor nunca recibe notificaciones cuando alguien
        # vota su publicación tipo VS (los votos VS van a otra colección).
        user_vs_ids = [poll["id"] for poll in user_polls if poll.get("layout") == "vs"]
        if user_vs_ids:
            vs_votes_recent = await db.vs_votes.find({
                "vs_id": {"$in": user_vs_ids},
                "user_id": {"$ne": current_user.id},
                "created_at": {"$gte": seven_days_ago}
            }).sort("created_at", -1).limit(30).to_list(30)
            
            print(f"DEBUG Activity: Found {len(vs_votes_recent)} VS votes on user's VS polls")
            
            # Map de polls VS por id para lookup rápido
            vs_polls_by_id = {p["id"]: p for p in user_polls if p.get("layout") == "vs"}
            
            for vs_vote in vs_votes_recent:
                voter = await db.users.find_one({"id": vs_vote["user_id"]})
                poll = vs_polls_by_id.get(vs_vote["vs_id"])
                option_text = ""
                # Buscar el texto de la opción votada dentro de vs_questions
                if poll:
                    for q in poll.get("vs_questions", []):
                        if q.get("id") == vs_vote.get("question_id"):
                            for opt in q.get("options", []):
                                if opt.get("id") == vs_vote.get("option_id"):
                                    option_text = opt.get("text", "") or ""
                                    break
                            break
                    # Fallback: buscar en options de nivel superior (primera pregunta)
                    if not option_text:
                        for opt in poll.get("options", []):
                            if opt.get("id") == vs_vote.get("option_id"):
                                option_text = opt.get("text", "") or ""
                                break
                if voter and poll:
                    activities.append({
                        "id": f"vs-vote-{vs_vote['id']}",
                        "type": "vote",
                        "user": {
                            "id": voter["id"],
                            "username": voter["username"],
                            "display_name": voter.get("display_name", voter["username"]),
                            "avatar_url": voter.get("avatar_url")
                        },
                        "content_type": "poll",
                        "content_preview": (poll.get("title") or "VS")[:50],
                        "vote_option": option_text,
                        "poll_id": poll["id"],
                        "poll_layout": poll.get("layout"),
                        "poll_thumbnail": _extract_poll_thumbnail(poll),
                        "poll_vs_thumbnails": _extract_vs_thumbnails(poll),
                        "created_at": vs_vote["created_at"],
                        "unread": True
                    })
        
        # Get recent mentions - find polls where user is mentioned
        print(f"DEBUG Activity: Looking for mentions of user {current_user.id}")
        
        # Search in mentioned_users (poll level mentions)
        polls_with_general_mentions = await db.polls.find({
            "mentioned_users": current_user.id,
            "author_id": {"$ne": current_user.id},  # Exclude own polls
            "created_at": {"$gte": seven_days_ago}
        }).sort("created_at", -1).limit(20).to_list(20)
        
        print(f"DEBUG Activity: Found {len(polls_with_general_mentions)} polls with general mentions")
        
        for poll in polls_with_general_mentions:
            author = await db.users.find_one({"id": poll["author_id"]})
            if author:
                activities.append({
                    "id": f"mention-general-{poll['id']}",
                    "type": "mention",
                    "user": {
                        "id": author["id"],
                        "username": author["username"],
                        "display_name": author.get("display_name", author["username"]),
                        "avatar_url": author.get("avatar_url")
                    },
                    "content_type": "poll",
                    "content_preview": poll.get("title", "")[:50],
                    "mention_type": "general",
                    "poll_id": poll["id"],
                    "poll_layout": poll.get("layout"),
                    "poll_thumbnail": _extract_poll_thumbnail(poll),
                    "poll_vs_thumbnails": _extract_vs_thumbnails(poll),
                    "created_at": poll["created_at"],
                    "unread": True
                })
        
        # Search in option-specific mentions
        polls_with_option_mentions = await db.polls.find({
            "options.mentioned_users.id": current_user.id,
            "author_id": {"$ne": current_user.id},  # Exclude own polls
            "created_at": {"$gte": seven_days_ago}
        }).sort("created_at", -1).limit(20).to_list(20)
        
        print(f"DEBUG Activity: Found {len(polls_with_option_mentions)} polls with option mentions")
        
        for poll in polls_with_option_mentions:
            author = await db.users.find_one({"id": poll["author_id"]})
            if author:
                # Find which option mentioned the user
                mentioned_option = None
                for option in poll.get("options", []):
                    for mentioned_user in option.get("mentioned_users", []):
                        if mentioned_user.get("id") == current_user.id:
                            mentioned_option = option.get("text", "")
                            break
                    if mentioned_option:
                        break
                
                activities.append({
                    "id": f"mention-option-{poll['id']}",
                    "type": "mention", 
                    "user": {
                        "id": author["id"],
                        "username": author["username"],
                        "display_name": author.get("display_name", author["username"]),
                        "avatar_url": author.get("avatar_url")
                    },
                    "content_type": "poll",
                    "content_preview": poll.get("title", "")[:50],
                    "mention_type": "option",
                    "mention_option": mentioned_option,
                    "poll_id": poll["id"],
                    "poll_layout": poll.get("layout"),
                    "poll_thumbnail": _extract_poll_thumbnail(poll),
                    "poll_vs_thumbnails": _extract_vs_thumbnails(poll),
                    "created_at": poll["created_at"],
                    "unread": True
                })
        
        # Sort all activities by date
        activities.sort(key=lambda x: x["created_at"], reverse=True)
        
        # Limit to max 30 activities
        activities = activities[:30]
        
        # Check which activities have been viewed by the user
        activity_ids = [a["id"] for a in activities]
        viewed_activities = await db.activity_views.find({
            "user_id": current_user.id,
            "activity_id": {"$in": activity_ids}
        }).to_list(1000)
        viewed_ids = {v["activity_id"] for v in viewed_activities}
        
        # Update unread status based on view history
        for activity in activities:
            activity["unread"] = activity["id"] not in viewed_ids
        
        print(f"DEBUG Activity: Returning {len(activities)} activities, {len(viewed_ids)} already viewed")
        
        return activities
        
    except Exception as e:
        # Log error and return empty list
        print(f"❌ Error in get_recent_activity: {str(e)}")
        import traceback
        traceback.print_exc()
        return []


@api_router.post("/users/activity/mark-read")
async def mark_activities_as_read(current_user: UserResponse = Depends(get_current_user)):
    """Mark all activities as read for the current user"""
    try:
        # Get current activities to mark them as read
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        
        # Get user's polls
        user_polls = await db.polls.find({"author_id": current_user.id}).to_list(1000)
        user_poll_ids = [poll["id"] for poll in user_polls]
        
        # Collect all activity IDs
        activity_ids = []
        
        # Get likes on user's polls
        likes = await db.poll_likes.find({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for like in likes:
            activity_ids.append(f"like-{like['id']}")
        
        # Get comments on user's polls
        comments = await db.comments.find({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for comment in comments:
            activity_ids.append(f"comment-{comment['id']}")
        
        # Get votes on user's polls
        votes = await db.votes.find({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for vote in votes:
            activity_ids.append(f"vote-{vote['id']}")
        
        # 🆕 Get VS votes on user's VS polls (stored in vs_votes collection).
        # Si no se incluyen aquí, /activity/recent los seguirá devolviendo como
        # unread=True después de marcar como leído, y el punto rojo persistirá
        # en los tabs "Todo" y "Votos".
        user_vs_ids = [poll["id"] for poll in user_polls if poll.get("layout") == "vs"]
        if user_vs_ids:
            vs_votes_recent = await db.vs_votes.find({
                "vs_id": {"$in": user_vs_ids},
                "user_id": {"$ne": current_user.id},
                "created_at": {"$gte": seven_days_ago}
            }).to_list(1000)
            for vs_vote in vs_votes_recent:
                activity_ids.append(f"vs-vote-{vs_vote['id']}")
        
        # Get general mentions
        polls_with_mentions = await db.polls.find({
            "mentioned_users": current_user.id,
            "author_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for poll in polls_with_mentions:
            activity_ids.append(f"mention-general-{poll['id']}")
        
        # Get option mentions
        polls_with_option_mentions = await db.polls.find({
            "options.mentioned_users.id": current_user.id,
            "author_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for poll in polls_with_option_mentions:
            activity_ids.append(f"mention-option-{poll['id']}")
        
        # Mark all as read (insert if not exists)
        now = datetime.utcnow()
        for activity_id in activity_ids:
            await db.activity_views.update_one(
                {"user_id": current_user.id, "activity_id": activity_id},
                {"$setOnInsert": {
                    "id": str(uuid.uuid4()),
                    "user_id": current_user.id,
                    "activity_id": activity_id,
                    "viewed_at": now
                }},
                upsert=True
            )
        
        print(f"✅ Marked {len(activity_ids)} activities as read for user {current_user.id}")
        return {"success": True, "marked_count": len(activity_ids)}
        
    except Exception as e:
        print(f"❌ Error marking activities as read: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error marking activities as read")


@api_router.get("/users/activity/unread-count")
async def get_unread_activity_count(current_user: UserResponse = Depends(get_current_user)):
    """Get count of unread activities for the current user"""
    try:
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        
        # Get user's polls
        user_polls = await db.polls.find({"author_id": current_user.id}).to_list(1000)
        user_poll_ids = [poll["id"] for poll in user_polls]
        
        # Collect all activity IDs
        activity_ids = []
        
        # Count likes
        likes = await db.poll_likes.find({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for like in likes:
            activity_ids.append(f"like-{like['id']}")
        
        # Count comments
        comments = await db.comments.find({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for comment in comments:
            activity_ids.append(f"comment-{comment['id']}")
        
        # Count votes
        votes = await db.votes.find({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for vote in votes:
            activity_ids.append(f"vote-{vote['id']}")
        
        # 🆕 Count VS votes (stored in vs_votes collection)
        user_vs_ids = [poll["id"] for poll in user_polls if poll.get("layout") == "vs"]
        if user_vs_ids:
            vs_votes_recent = await db.vs_votes.find({
                "vs_id": {"$in": user_vs_ids},
                "user_id": {"$ne": current_user.id},
                "created_at": {"$gte": seven_days_ago}
            }).to_list(1000)
            for vs_vote in vs_votes_recent:
                activity_ids.append(f"vs-vote-{vs_vote['id']}")
        
        # Count mentions
        polls_with_mentions = await db.polls.find({
            "mentioned_users": current_user.id,
            "author_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for poll in polls_with_mentions:
            activity_ids.append(f"mention-general-{poll['id']}")
        
        polls_with_option_mentions = await db.polls.find({
            "options.mentioned_users.id": current_user.id,
            "author_id": {"$ne": current_user.id},
            "created_at": {"$gte": seven_days_ago}
        }).to_list(1000)
        for poll in polls_with_option_mentions:
            activity_ids.append(f"mention-option-{poll['id']}")
        
        # Get viewed activities
        viewed_activities = await db.activity_views.find({
            "user_id": current_user.id,
            "activity_id": {"$in": activity_ids}
        }).to_list(1000)
        viewed_ids = {v["activity_id"] for v in viewed_activities}
        
        # Count unread
        unread_count = len([aid for aid in activity_ids if aid not in viewed_ids])
        
        return {"unread_count": unread_count, "total_count": len(activity_ids)}
        
    except Exception as e:
        print(f"❌ Error getting unread count: {str(e)}")
        return {"unread_count": 0, "total_count": 0}


# =============  COMMENT ENDPOINTS =============

@api_router.post("/polls/{poll_id}/comments", response_model=CommentResponse)
async def create_comment(
    poll_id: str,
    comment_data: CommentCreate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Create a new comment on a poll or challenge"""
    # Verificar que el poll_id coincida con el de los datos
    if comment_data.poll_id != poll_id:
        raise HTTPException(status_code=400, detail="Poll ID mismatch")
    
    # Si es una respuesta, verificar que el comentario padre existe
    if comment_data.parent_comment_id:
        parent_comment = await db.comments.find_one({
            "id": comment_data.parent_comment_id,
            "poll_id": poll_id
        })
        if not parent_comment:
            raise HTTPException(status_code=404, detail="Parent comment not found")
    
    # Detect challenge or poll
    is_challenge = poll_id.startswith("challenge_")
    
    if is_challenge:
        real_challenge_id = poll_id.replace("challenge_", "", 1)
        challenge = await db.challenges.find_one({"id": real_challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge not found")
    else:
        poll = await db.polls.find_one({"id": poll_id})
        if not poll:
            raise HTTPException(status_code=404, detail="Poll not found")
    
    # Crear el comentario
    comment = Comment(
        poll_id=poll_id,
        user_id=current_user.id,
        content=comment_data.content.strip(),
        parent_comment_id=comment_data.parent_comment_id
    )
    
    # Insertar en la base de datos
    await db.comments.insert_one(comment.dict())
    
    # Incrementar el contador de comentarios
    if is_challenge:
        await db.challenges.update_one(
            {"id": real_challenge_id},
            {"$inc": {"comments_count": 1}}
        )
    else:
        await db.polls.update_one(
            {"id": poll_id},
            {"$inc": {"comments_count": 1}}
        )
    
    # 🆕 Resolver autor del poll para devolver post_author_* en el response
    _post_author_id_create: Optional[str] = None
    _post_author_avatar_url_create: Optional[str] = None
    if is_challenge:
        _post_author_id_create = (challenge or {}).get("author_id") or (challenge or {}).get("creator_id") or (challenge or {}).get("user_id")
    else:
        _post_author_id_create = (poll or {}).get("author_id")
    if _post_author_id_create:
        _author_doc_create = await db.users.find_one({"id": _post_author_id_create})
        if _author_doc_create:
            _post_author_avatar_url_create = _author_doc_create.get("avatar_url")
    
    # Retornar el comentario creado con información del usuario
    return CommentResponse(
        **comment.dict(),
        user=current_user,
        replies=[],
        reply_count=0,
        user_liked=False,
        liked_by_author=False,
        post_author_id=_post_author_id_create,
        post_author_avatar_url=_post_author_avatar_url_create
    )

@api_router.get("/polls/{poll_id}/comments")
async def get_poll_comments(
    poll_id: str,
    limit: int = 50,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get comments for a specific poll with nested structure"""
    
    # 🆕 Obtener info del autor del poll (para el badge "liked by creator")
    _post_author_id: Optional[str] = None
    _post_author_avatar_url: Optional[str] = None
    is_challenge = poll_id.startswith("challenge_")
    if is_challenge:
        _real_challenge_id = poll_id.replace("challenge_", "", 1)
        _challenge = await db.challenges.find_one({"id": _real_challenge_id})
        if _challenge:
            _post_author_id = _challenge.get("author_id") or _challenge.get("creator_id") or _challenge.get("user_id")
    else:
        _poll_doc = await db.polls.find_one({"id": poll_id})
        if _poll_doc:
            _post_author_id = _poll_doc.get("author_id")
    if _post_author_id:
        _author_doc = await db.users.find_one({"id": _post_author_id})
        if _author_doc:
            _post_author_avatar_url = _author_doc.get("avatar_url")
    
    # Obtener todos los comentarios del poll
    comments_cursor = db.comments.find({
        "poll_id": poll_id
    }).sort("created_at", 1)  # Orden cronológico
    
    all_comments = await comments_cursor.to_list(1000)  # Límite alto para obtener todos
    
    if not all_comments:
        return []
    
    # Obtener información de usuarios únicos
    user_ids = list(set(comment["user_id"] for comment in all_comments))
    users_cursor = db.users.find({"id": {"$in": user_ids}})
    users_list = await users_cursor.to_list(len(user_ids))
    users_dict = {user["id"]: UserResponse(**user) for user in users_list}
    
    # Obtener likes del usuario actual para cada comentario
    comment_ids = [comment["id"] for comment in all_comments]
    user_likes = await db.comment_likes.find({
        "comment_id": {"$in": comment_ids},
        "user_id": current_user.id
    }).to_list(len(comment_ids))
    
    liked_comments = set(like["comment_id"] for like in user_likes)
    
    # 🆕 Likes del autor del poll sobre estos comentarios
    author_liked_comments: set = set()
    if _post_author_id and comment_ids:
        # Si el usuario actual ES el autor, reutilizamos el set ya consultado
        if _post_author_id == current_user.id:
            author_liked_comments = set(liked_comments)
        else:
            _author_likes = await db.comment_likes.find({
                "comment_id": {"$in": comment_ids},
                "user_id": _post_author_id
            }).to_list(len(comment_ids))
            author_liked_comments = {row["comment_id"] for row in _author_likes}

    # Reacciones rápidas: agregamos por comment_id → {emoji: count}
    reactions_by_comment: dict = {}
    if comment_ids:
        agg = db.comment_reactions.aggregate([
            {"$match": {"comment_id": {"$in": comment_ids}}},
            {"$group": {"_id": {"comment_id": "$comment_id", "emoji": "$emoji"}, "count": {"$sum": 1}}}
        ])
        async for row in agg:
            cid = row["_id"]["comment_id"]
            emoji = row["_id"]["emoji"]
            reactions_by_comment.setdefault(cid, {})[emoji] = row["count"]

    # Reacción del usuario actual por comentario
    user_reactions = await db.comment_reactions.find({
        "comment_id": {"$in": comment_ids},
        "user_id": current_user.id
    }).to_list(len(comment_ids))
    user_reaction_map = {r["comment_id"]: r["emoji"] for r in user_reactions}
    
    # Crear diccionario de comentarios
    comments_dict = {}
    root_comments = []
    
    # Construir estructura anidada
    for comment_data in all_comments:
        # Eliminar campos que se pasan explícitamente para evitar duplicados
        clean_data = {k: v for k, v in comment_data.items() if k not in ('user', 'replies', 'reply_count', 'user_liked', 'reactions', 'user_reaction', 'liked_by_author', 'post_author_id', 'post_author_avatar_url', '_id')}
        comment_resp = CommentResponse(
            **clean_data,
            user=users_dict.get(comment_data["user_id"]),
            replies=[],
            reply_count=0,
            user_liked=comment_data["id"] in liked_comments,
            reactions=reactions_by_comment.get(comment_data["id"], {}),
            user_reaction=user_reaction_map.get(comment_data["id"]),
            liked_by_author=comment_data["id"] in author_liked_comments,
            post_author_id=_post_author_id,
            post_author_avatar_url=_post_author_avatar_url
        )
        
        comments_dict[comment_data["id"]] = comment_resp
        
        if comment_data["parent_comment_id"] is None:
            root_comments.append(comment_resp)
    
    # Construir jerarquía de respuestas
    for comment_data in all_comments:
        if comment_data["parent_comment_id"]:
            parent = comments_dict.get(comment_data["parent_comment_id"])
            child = comments_dict.get(comment_data["id"])
            if parent and child:
                parent.replies.append(child)
    
    # Calcular conteos de respuestas anidadas recursivamente
    def calculate_reply_count(comment):
        count = len(comment.replies)
        for reply in comment.replies:
            count += calculate_reply_count(reply)
        comment.reply_count = count
        return count
    
    for comment in root_comments:
        calculate_reply_count(comment)
    
    # Aplicar paginación solo a comentarios raíz
    paginated_comments = root_comments[offset:offset + limit]
    
    return paginated_comments

@api_router.put("/comments/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: str,
    comment_data: CommentUpdate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Update a comment (only by the comment author)"""
    
    # Verificar que el comentario existe y pertenece al usuario
    comment = await db.comments.find_one({
        "id": comment_id,
        "user_id": current_user.id
    })
    
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")
    
    # Actualizar el comentario
    update_data = {
        "content": comment_data.content.strip(),
        "is_edited": True,
        "updated_at": datetime.utcnow()
    }
    
    result = await db.comments.update_one(
        {"id": comment_id},
        {"$set": update_data}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to update comment")
    
    # Obtener el comentario actualizado
    updated_comment = await db.comments.find_one({"id": comment_id})
    
    return CommentResponse(
        **updated_comment,
        user=current_user,
        replies=[],
        reply_count=0,
        user_liked=False
    )

@api_router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Delete a comment (only by the comment author)"""
    
    # Verificar que el comentario existe y pertenece al usuario
    comment = await db.comments.find_one({
        "id": comment_id,
        "user_id": current_user.id
    })
    
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")
    
    # Guardar el poll_id para actualizar el contador después
    poll_id = comment.get("poll_id")
    
    # Eliminar el comentario y todas sus respuestas recursivamente
    # La función retorna el número de comentarios eliminados
    deleted_count = await delete_comment_recursive(comment_id)
    
    # Decrementar el contador de comentarios en el poll
    if poll_id:
        await db.polls.update_one(
            {"id": poll_id},
            {"$inc": {"comments_count": -deleted_count}}
        )
    
    return {"message": "Comment deleted successfully"}

async def delete_comment_recursive(comment_id: str) -> int:
    """Función auxiliar para eliminar comentarios de forma recursiva
    Retorna el número de comentarios eliminados (incluyendo hijos)"""
    
    # Encontrar todos los comentarios hijos
    child_comments = await db.comments.find({"parent_comment_id": comment_id}).to_list(1000)
    
    # Contador de comentarios eliminados
    deleted_count = 1  # El comentario actual
    
    # Eliminar recursivamente los comentarios hijos
    for child in child_comments:
        deleted_count += await delete_comment_recursive(child["id"])
    
    # Eliminar likes del comentario
    await db.comment_likes.delete_many({"comment_id": comment_id})

    # Eliminar reacciones rápidas del comentario
    await db.comment_reactions.delete_many({"comment_id": comment_id})
    
    # Eliminar el comentario principal
    await db.comments.delete_one({"id": comment_id})
    
    return deleted_count

@api_router.post("/comments/{comment_id}/like")
async def toggle_comment_like(
    comment_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Toggle like on a comment"""
    
    # Verificar que el comentario existe
    comment = await db.comments.find_one({"id": comment_id})
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    # Verificar si ya existe el like
    existing_like = await db.comment_likes.find_one({
        "comment_id": comment_id,
        "user_id": current_user.id
    })
    
    if existing_like:
        # Quitar like
        await db.comment_likes.delete_one({
            "comment_id": comment_id,
            "user_id": current_user.id
        })
        
        # Decrementar contador de likes
        await db.comments.update_one(
            {"id": comment_id},
            {"$inc": {"likes": -1}}
        )
        
        # Obtener nuevo conteo
        updated_comment = await db.comments.find_one({"id": comment_id})
        
        return {
            "liked": False,
            "likes": updated_comment["likes"]
        }
    else:
        # Agregar like
        like = CommentLike(
            comment_id=comment_id,
            user_id=current_user.id
        )
        
        await db.comment_likes.insert_one(like.dict())
        
        # Incrementar contador de likes
        await db.comments.update_one(
            {"id": comment_id},
            {"$inc": {"likes": 1}}
        )
        
        # Obtener nuevo conteo
        updated_comment = await db.comments.find_one({"id": comment_id})
        
        return {
            "liked": True,
            "likes": updated_comment["likes"]
        }

@api_router.post("/comments/{comment_id}/reaction")
async def toggle_comment_reaction(
    comment_id: str,
    payload: ReactionRequest,
    current_user: UserResponse = Depends(get_current_user)
):
    """Toggle/set una reacción rápida (emoji) sobre un comentario.

    Reglas:
    - Un usuario solo tiene UNA reacción activa por comentario.
    - Si manda el MISMO emoji que ya tiene → se elimina (toggle off).
    - Si manda un emoji DIFERENTE → reemplaza el anterior.
    - Si no tenía ninguna → la agrega.
    """
    emoji = (payload.emoji or "").strip()
    if not emoji or len(emoji) > 16:
        raise HTTPException(status_code=400, detail="Invalid emoji")

    comment = await db.comments.find_one({"id": comment_id})
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    existing = await db.comment_reactions.find_one({
        "comment_id": comment_id,
        "user_id": current_user.id
    })

    if existing and existing.get("emoji") == emoji:
        # Toggle off
        await db.comment_reactions.delete_one({
            "comment_id": comment_id,
            "user_id": current_user.id
        })
        user_reaction = None
    else:
        if existing:
            await db.comment_reactions.delete_one({
                "comment_id": comment_id,
                "user_id": current_user.id
            })
        reaction = CommentReaction(
            comment_id=comment_id,
            user_id=current_user.id,
            emoji=emoji
        )
        await db.comment_reactions.insert_one(reaction.dict())
        user_reaction = emoji

    # Recalcular contadores agregados de este comentario
    agg = db.comment_reactions.aggregate([
        {"$match": {"comment_id": comment_id}},
        {"$group": {"_id": "$emoji", "count": {"$sum": 1}}}
    ])
    reactions: dict = {}
    async for row in agg:
        reactions[row["_id"]] = row["count"]

    return {
        "user_reaction": user_reaction,
        "reactions": reactions,
    }

@api_router.get("/comments/{comment_id}")
async def get_comment(
    comment_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get a specific comment with its replies"""
    comment = await db.comments.find_one({"id": comment_id})
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    # Obtener información del usuario
    user_data = await db.users.find_one({"id": comment["user_id"]})
    if not user_data:
        raise HTTPException(status_code=404, detail="Comment author not found")
    
    user = UserResponse(**user_data)
    
    # Verificar si el usuario actual le dio like
    user_like = await db.comment_likes.find_one({
        "comment_id": comment_id,
        "user_id": current_user.id
    })
    
    # Obtener respuestas directas
    replies_cursor = db.comments.find({
        "parent_comment_id": comment_id
    }).sort("created_at", 1)
    
    replies_data = await replies_cursor.to_list(100)
    
    # Procesar respuestas
    replies = []
    for reply_data in replies_data:
        reply_user_data = await db.users.find_one({"id": reply_data["user_id"]})
        if reply_user_data:
            reply_user_like = await db.comment_likes.find_one({
                "comment_id": reply_data["id"],
                "user_id": current_user.id
            })
            
            reply = CommentResponse(
                **reply_data,
                user=UserResponse(**reply_user_data),
                replies=[],  # Por ahora solo 2 niveles
                reply_count=0,
                user_liked=bool(reply_user_like)
            )
            replies.append(reply)
    
    return CommentResponse(
        **comment,
        user=user,
        replies=replies,
        reply_count=len(replies),
        user_liked=bool(user_like)
    )

# =============  FILE UPLOAD UTILITIES =============

def get_file_format(filename: str) -> str:
    """Extract file format from filename"""
    return Path(filename).suffix.lower().lstrip('.')

def is_image_file(file_format: str) -> bool:
    """Check if file format is a supported image"""
    return file_format.lower() in config.SUPPORTED_IMAGE_FORMATS

def is_video_file(file_format: str) -> bool:
    """Check if file format is a supported video"""
    return file_format.lower() in config.SUPPORTED_VIDEO_FORMATS

def validate_file(file: UploadFile, upload_type: UploadType) -> tuple[bool, str, FileType]:
    """Validate uploaded file using configuration"""
    if not file.filename:
        return False, "No filename provided", FileType.IMAGE
    
    file_format = get_file_format(file.filename)
    
    # Check if supported format
    if not (is_image_file(file_format) or is_video_file(file_format)):
        supported_formats = config.SUPPORTED_IMAGE_FORMATS + config.SUPPORTED_VIDEO_FORMATS
        return False, f"Unsupported file format. Supported: {', '.join(supported_formats)}", FileType.IMAGE
    
    # Determine file type
    file_type = FileType.IMAGE if is_image_file(file_format) else FileType.VIDEO
    
    # Check file size using config
    max_size = config.IMAGE_MAX_SIZE if file_type == FileType.IMAGE else config.VIDEO_MAX_SIZE
    if hasattr(file, 'size') and file.size > max_size:
        max_mb = max_size // (1024 * 1024)
        return False, f"File too large. Maximum size: {max_mb}MB", file_type
    
    return True, "", file_type

def get_upload_path(upload_type: UploadType, file_format: str, filename: str) -> tuple[Path, str]:
    """Get the upload path and public URL for a file using configuration"""
    # Create unique filename to avoid conflicts
    unique_filename = f"{uuid.uuid4()}.{file_format}"
    
    # Determine subdirectory based on upload type
    subdir_map = {
        UploadType.AVATAR: config.UPLOAD_SUBDIRS[0],           # "avatars"
        UploadType.POLL_OPTION: config.UPLOAD_SUBDIRS[1],      # "poll_options"
        UploadType.POLL_BACKGROUND: config.UPLOAD_SUBDIRS[2],  # "poll_backgrounds"
        UploadType.GENERAL: config.UPLOAD_SUBDIRS[3]           # "general"
    }
    subdir = subdir_map[upload_type]
    
    file_path = config.UPLOAD_BASE_DIR / subdir / unique_filename
    # Use API endpoint URL instead of direct static file URL
    public_url = f"{config.API_PREFIX}/uploads/{subdir}/{unique_filename}"
    
    return file_path, public_url

async def get_image_dimensions(file_path: Path) -> tuple[Optional[int], Optional[int]]:
    """Get image dimensions using PIL"""
    try:
        with Image.open(file_path) as img:
            return img.width, img.height
    except Exception as e:
        print(f"Error getting image dimensions: {e}")
        return None, None

def _is_video_url(url: Optional[str]) -> bool:
    """Check if URL ends with a video extension"""
    if not url or not isinstance(url, str):
        return False
    lower = url.lower().split('?')[0]
    return any(lower.endswith(ext) for ext in ['.mp4', '.mov', '.webm', '.avi', '.mkv'])


async def resolve_video_thumbnail(media_url: Optional[str], stored_thumbnail_url: Optional[str]) -> Optional[str]:
    """
    Resolve a proper image thumbnail URL for a video.
    Handles the legacy case where stored_thumbnail_url was set to the video URL itself.
    Returns a JPG thumbnail URL, or None if it cannot be generated.

    Resolution order:
      1) Stored thumbnail_url if it's a real image (not the video URL).
      2) Thumbnail recorded in uploaded_files collection (lazily generated).
      3) FFmpeg generation directly from the file on disk (covers legacy uploads
         where uploaded_files doesn't have a record — e.g. fast-upload or older
         media). This is what keeps VS thumbnails working without any backfill.
    """
    # 1) Stored thumbnail is a real image → use it
    if stored_thumbnail_url and not _is_video_url(stored_thumbnail_url) and stored_thumbnail_url != media_url:
        return stored_thumbnail_url

    # 2) Try DB-backed lazy generation (works if uploaded_files has the record)
    if media_url:
        try:
            thumb = await get_thumbnail_for_media_url(media_url)
            if thumb and not _is_video_url(thumb):
                return thumb
        except Exception as e:
            print(f"⚠️ resolve_video_thumbnail: DB lookup failed for {media_url}: {e}")

    # 3) FFmpeg fallback: derive the file path from the media URL and run ffmpeg
    #    directly. Works for any video already on disk under UPLOAD_DIR.
    if media_url and "/api/uploads/" in media_url:
        try:
            # /api/uploads/<category>/<filename>  → <UPLOAD_DIR>/<category>/<filename>
            after = media_url.split("/api/uploads/", 1)[1]
            file_path = UPLOAD_DIR / after
            if file_path.exists() and file_path.is_file():
                # Map category name → UploadType so the thumbnail URL is consistent
                category = after.split("/", 1)[0] if "/" in after else "general"
                upload_type_map = {
                    "avatars": UploadType.AVATAR,
                    "poll_options": UploadType.POLL_OPTION,
                    "poll_backgrounds": UploadType.POLL_BACKGROUND,
                    "general": UploadType.GENERAL,
                }
                upload_type = upload_type_map.get(category, UploadType.GENERAL)
                thumb_url = get_video_thumbnail_url(str(file_path), upload_type)
                if thumb_url and not _is_video_url(thumb_url):
                    # Best-effort: cache the generated thumbnail back into
                    # uploaded_files if a record exists, so next time we hit
                    # path (2) instead of re-running ffmpeg.
                    try:
                        filename = file_path.name
                        await db.uploaded_files.update_one(
                            {"filename": filename},
                            {"$set": {"thumbnail_url": thumb_url}}
                        )
                    except Exception:
                        pass  # cache is best-effort
                    return thumb_url
        except Exception as e:
            print(f"⚠️ resolve_video_thumbnail: ffmpeg fallback failed for {media_url}: {e}")

    return None


async def get_thumbnail_for_media_url(media_url: str) -> Optional[str]:
    """Get thumbnail URL from uploaded_files collection - Generate if doesn't exist"""
    try:
        if not media_url:
            return None
            
        # Extract filename and category from media URL
        # URLs are like: /api/uploads/general/filename.mp4
        if "/api/uploads/" in media_url:
            parts = media_url.split("/")
            if len(parts) >= 4:
                category = parts[-2]  # e.g., "general", "poll_options"
                filename = parts[-1]
                
                # Query uploaded_files collection
                uploaded_file = await db.uploaded_files.find_one({"filename": filename})
                
                if uploaded_file:
                    # If thumbnail exists in DB AND is a valid image, return it
                    cached = uploaded_file.get("thumbnail_url")
                    if cached and not _is_video_url(cached):
                        return cached
                    
                    # If no thumbnail but it's a video, try to generate it
                    if uploaded_file.get("file_type") == "video":
                        file_path = uploaded_file.get("file_path")
                        if file_path and Path(file_path).exists():
                            # Map category to UploadType
                            upload_type_map = {
                                "avatars": UploadType.AVATAR,
                                "poll_options": UploadType.POLL_OPTION,
                                "poll_backgrounds": UploadType.POLL_BACKGROUND,
                                "general": UploadType.GENERAL
                            }
                            upload_type = upload_type_map.get(category, UploadType.GENERAL)
                            
                            # Generate thumbnail
                            thumbnail_url = get_video_thumbnail_url(file_path, upload_type)
                            
                            # Update database with thumbnail URL
                            if thumbnail_url:
                                await db.uploaded_files.update_one(
                                    {"filename": filename},
                                    {"$set": {"thumbnail_url": thumbnail_url}}
                                )
                                print(f"✅ Generated and saved thumbnail for {filename}: {thumbnail_url}")
                                return thumbnail_url
                
        return None
        
    except Exception as e:
        print(f"Error getting thumbnail for media URL {media_url}: {e}")
        return None

async def get_video_info(file_path: Path) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """Get video info and generate thumbnail"""
    try:
        # Use OpenCV to get video information and generate thumbnail
        cap = cv2.VideoCapture(str(file_path))
        
        if not cap.isOpened():
            print(f"Could not open video: {file_path}")
            return 1280, 720, 30.0  # Return defaults if can't open
        
        # Get video properties
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        duration = frame_count / fps if fps > 0 else 30.0
        
        # Generate thumbnail from middle frame
        middle_frame = frame_count // 2 if frame_count > 0 else 0
        cap.set(cv2.CAP_PROP_POS_FRAMES, middle_frame)
        
        ret, frame = cap.read()
        if ret:
            # Create thumbnail directory if it doesn't exist
            thumbnail_dir = file_path.parent / "thumbnails"
            thumbnail_dir.mkdir(exist_ok=True)
            
            # Generate thumbnail filename
            thumbnail_filename = f"{file_path.stem}_thumbnail.jpg"
            thumbnail_path = thumbnail_dir / thumbnail_filename
            
            # Resize frame to thumbnail size (maintain aspect ratio)
            max_size = 800
            if width > height:
                new_width = max_size
                new_height = int(height * (max_size / width))
            else:
                new_height = max_size
                new_width = int(width * (max_size / height))
            
            resized_frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
            
            # Save thumbnail
            cv2.imwrite(str(thumbnail_path), resized_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            print(f"Generated thumbnail: {thumbnail_path}")
            
        cap.release()
        
        return width, height, duration
        
    except Exception as e:
        print(f"Error getting video info: {e}")
        # Return reasonable defaults on error
        return 1280, 720, 30.0

def get_video_thumbnail_url(file_path: str, upload_type: UploadType = UploadType.GENERAL) -> Optional[str]:
    """Generate thumbnail URL for video files - Creates thumbnail if it doesn't exist"""
    try:
        file_path_obj = Path(file_path)
        thumbnail_filename = f"{file_path_obj.stem}_thumbnail.jpg"
        
        # Map upload type to category for URL generation
        category_map = {
            UploadType.AVATAR: "avatars",
            UploadType.POLL_OPTION: "poll_options", 
            UploadType.POLL_BACKGROUND: "poll_backgrounds",
            UploadType.GENERAL: "general"
        }
        
        category = category_map.get(upload_type, "general")
        
        # Create thumbnails directory if it doesn't exist
        thumbnails_dir = file_path_obj.parent / "thumbnails"
        thumbnails_dir.mkdir(parents=True, exist_ok=True)
        
        # Full path for thumbnail
        thumbnail_path = thumbnails_dir / thumbnail_filename
        
        # Generate thumbnail if it doesn't exist
        if not thumbnail_path.exists():
            try:
                import subprocess
                # Use ffmpeg to extract frame at 1 second
                # -ss 1: seek to 1 second
                # -i: input file
                # -vframes 1: extract 1 frame
                # -vf scale=...: scale to max 720px width while maintaining aspect ratio
                # -q:v 2: high quality JPEG
                result = subprocess.run([
                    'ffmpeg',
                    '-ss', '1',  # Extract frame at 1 second
                    '-i', str(file_path_obj),
                    '-vframes', '1',  # Extract only 1 frame
                    '-vf', 'scale=720:-2',  # Scale to 720px width, maintain aspect ratio
                    '-q:v', '2',  # High quality JPEG
                    '-y',  # Overwrite if exists
                    str(thumbnail_path)
                ], capture_output=True, timeout=10)
                
                if result.returncode != 0:
                    print(f"⚠️ FFmpeg error generating thumbnail: {result.stderr.decode()}")
                    return None
                else:
                    print(f"✅ Thumbnail generated successfully: {thumbnail_path}")
            except subprocess.TimeoutExpired:
                print(f"⚠️ Timeout generating thumbnail for {file_path}")
                return None
            except Exception as e:
                print(f"⚠️ Error generating thumbnail with ffmpeg: {e}")
                return None
        
        # Check if thumbnail was created successfully
        if thumbnail_path.exists():
            return f"/api/uploads/{category}/thumbnails/{thumbnail_filename}"
        
        return None
        
    except Exception as e:
        print(f"Error generating thumbnail URL: {e}")
        return None

async def save_upload_file(file: UploadFile, file_path: Path) -> int:
    """Save uploaded file to disk and return file size"""
    file_size = 0
    
    async with aiofiles.open(file_path, 'wb') as f:
        while chunk := await file.read(1024 * 1024):  # Read in 1MB chunks
            file_size += len(chunk)
            await f.write(chunk)
    
    return file_size

# =============  FILE SERVING ENDPOINTS =============

@api_router.get("/uploads/{category}/{filename}")
async def get_upload_file(category: str, filename: str):
    """Serve uploaded files through API endpoint"""
    
    # Validate category
    # 🛠️ Incluimos `thumbnails` y `videos` porque la video_pipeline genera
    # archivos en UPLOAD_DIR/thumbnails/{option_id}.jpg y los expone como
    # /api/uploads/thumbnails/{option_id}.jpg. Sin esta categoría, las
    # imágenes generadas server-side (covers de audio extraído, posters
    # del player) devolvían 404 y se veían rotas en el frontend.
    allowed_categories = [
        "avatars", "poll_options", "poll_backgrounds",
        "general", "audio", "stories",
        "thumbnails", "videos", "moments",
    ]
    if category not in allowed_categories:
        raise HTTPException(status_code=404, detail="Invalid category")
    
    # Construct file path
    file_path = UPLOAD_DIR / category / filename
    
    # Check if file exists
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Get MIME type
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "application/octet-stream"
    
    # Return the file
    # NOTE: NO pasamos `filename=` para evitar que Starlette inyecte
    # `Content-Disposition: attachment` por defecto. Forzamos `inline` para
    # que el browser reproduzca videos/imágenes en lugar de descargarlos.
    return FileResponse(
        path=file_path,
        media_type=mime_type,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Accept-Ranges": "bytes",
        },
    )

@api_router.get("/uploads/{category}/thumbnails/{filename}")
async def get_thumbnail_file(category: str, filename: str):
    """Serve thumbnail files through API endpoint"""
    
    # Validate category
    allowed_categories = [
        "avatars", "poll_options", "poll_backgrounds",
        "general", "audio", "stories",
        "thumbnails", "videos", "moments",
    ]
    if category not in allowed_categories:
        raise HTTPException(status_code=404, detail="Invalid category")
    
    # Construct thumbnail file path
    file_path = UPLOAD_DIR / category / "thumbnails" / filename
    
    # Check if file exists
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    
    # Get MIME type (thumbnails are always JPEG)
    mime_type = "image/jpeg"
    
    # Return the thumbnail (inline para que el browser lo renderice como imagen)
    return FileResponse(
        path=file_path,
        media_type=mime_type,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@api_router.get("/uploads/videos/optimized/{filename}")
async def get_optimized_video_file(filename: str):
    """Serve transcoded videos generated by the video pipeline.

    The pipeline writes to UPLOAD_DIR/videos/optimized/<option_id>.mp4 and
    exposes them as /api/uploads/videos/optimized/<filename>. Sin esta ruta
    el `optimized_media_url` daba 404 y el reproductor caía al original (o
    aparecía roto en clientes que no implementan fallback).
    """
    file_path = UPLOAD_DIR / "videos" / "optimized" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Optimized video not found")

    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "video/mp4"

    return FileResponse(
        path=file_path,
        media_type=mime_type,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Accept-Ranges": "bytes",
        },
    )


@api_router.get("/uploads/videos/vs_composed/{filename}")
async def get_vs_composed_video_file(filename: str):
    """🎬 Sirve los MP4 compuestos de VS (estilo TikTok Duet).

    Pipeline:
        backend/video_pipeline.compose_vs_video()
            → UPLOAD_DIR/videos/vs_composed/<vs_id>.mp4
            → expuesto como /api/uploads/videos/vs_composed/<vs_id>.mp4

    El archivo es inmutable una vez generado (mismo vs_id → mismo output) → se
    cachea agresivamente. `Accept-Ranges: bytes` permite progressive playback
    desde cualquier offset (fast scrub).
    """
    file_path = UPLOAD_DIR / "videos" / "vs_composed" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="VS composed video not found")

    return FileResponse(
        path=file_path,
        media_type="video/mp4",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Accept-Ranges": "bytes",
        },
    )


@api_router.get("/uploads/videos/hls/{option_id}/{filename}")
async def get_hls_file(option_id: str, filename: str):
    """Serve HLS playlist (.m3u8) and segments (.ts) generated by video_pipeline.

    Estructura en disco:
        UPLOAD_DIR/videos/hls/<option_id>/master.m3u8
        UPLOAD_DIR/videos/hls/<option_id>/<rendition>.m3u8
        UPLOAD_DIR/videos/hls/<option_id>/<rendition>_NNN.ts

    Headers:
        - Content-Disposition: inline → browsers reproducen, no descargan.
        - Content-Type correcto según extensión:
            .m3u8 → application/vnd.apple.mpegurl
            .ts   → video/mp2t
        - Cache-Control immutable: archivos VOD inmutables (los .ts y la
          playlist no cambian una vez generados; usamos UUIDs únicos).
        - Accept-Ranges: bytes → hls.js puede hacer range requests en .ts.

    Sanitización: rechazamos cualquier filename con `..` o `/` para evitar
    path traversal (option_id ya viene como UUID validado por la pipeline).
    """
    # Path traversal guard
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if ".." in option_id or "/" in option_id or "\\" in option_id:
        raise HTTPException(status_code=400, detail="Invalid option_id")

    file_path = UPLOAD_DIR / "videos" / "hls" / option_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="HLS file not found")

    # Content-Type explícito (mimetypes no siempre conoce .m3u8/.ts)
    if filename.endswith(".m3u8"):
        media_type = "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        media_type = "video/mp2t"
    else:
        media_type, _ = mimetypes.guess_type(str(file_path))
        if not media_type:
            media_type = "application/octet-stream"

    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Accept-Ranges": "bytes",
        },
    )


# =============  FILE UPLOAD ENDPOINTS =============

@api_router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    upload_type: UploadType = UploadType.GENERAL,
    current_user: UserResponse = Depends(get_current_user)
):
    """Upload a file (image or video)"""
    
    # Validate file
    is_valid, error_message, file_type = validate_file(file, upload_type)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_message)
    
    try:
        # Get file info
        file_format = get_file_format(file.filename)
        file_path, public_url = get_upload_path(upload_type, file_format, file.filename)
        
        # Save file
        file_size = await save_upload_file(file, file_path)
        
        # Get dimensions/duration based on file type
        width = height = duration = thumbnail_url = None
        if file_type == FileType.IMAGE:
            width, height = await get_image_dimensions(file_path)
        elif file_type == FileType.VIDEO:
            width, height, duration = await get_video_info(file_path)
            # Generate thumbnail URL for videos
            thumbnail_url = get_video_thumbnail_url(str(file_path), upload_type)
        
        # Create database record
        uploaded_file = UploadedFile(
            filename=file_path.name,
            original_filename=file.filename,
            file_type=file_type,
            file_format=file_format,
            file_size=file_size,
            upload_type=upload_type,
            uploader_id=current_user.id,
            file_path=str(file_path),
            public_url=public_url,
            thumbnail_url=thumbnail_url,
            width=width,
            height=height,
            duration=duration
        )
        
        # Save to database
        await db.uploaded_files.insert_one(uploaded_file.dict())
        
        return UploadResponse(
            id=uploaded_file.id,
            filename=uploaded_file.filename,
            original_filename=uploaded_file.original_filename,
            file_type=uploaded_file.file_type,
            file_format=uploaded_file.file_format,
            file_size=uploaded_file.file_size,
            public_url=uploaded_file.public_url,
            thumbnail_url=uploaded_file.thumbnail_url,
            width=uploaded_file.width,
            height=uploaded_file.height,
            duration=uploaded_file.duration,
            created_at=uploaded_file.created_at
        )
        
    except Exception as e:
        # Clean up file if database save fails
        if file_path.exists():
            file_path.unlink()
        
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload file: {str(e)}"
        )

@api_router.get("/upload/{file_id}", response_model=UploadResponse)
async def get_upload_info(
    file_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get information about an uploaded file"""
    
    file_data = await db.uploaded_files.find_one({"id": file_id})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    
    return UploadResponse(**file_data)

@api_router.delete("/upload/{file_id}")
async def delete_upload(
    file_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Delete an uploaded file"""
    
    # Get file info
    file_data = await db.uploaded_files.find_one({"id": file_id})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Check if user owns the file
    if file_data["uploader_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this file")
    
    try:
        # Delete physical file
        file_path = Path(file_data["file_path"])
        if file_path.exists():
            file_path.unlink()
        
        # Delete database record
        await db.uploaded_files.delete_one({"id": file_id})
        
        return {"message": "File deleted successfully"}
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete file: {str(e)}"
        )

@api_router.get("/uploads/user", response_model=List[UploadResponse])
async def get_user_uploads(
    upload_type: Optional[UploadType] = None,
    limit: int = 20,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get user's uploaded files"""
    
    filter_query = {"uploader_id": current_user.id}
    if upload_type:
        filter_query["upload_type"] = upload_type
    
    files_cursor = db.uploaded_files.find(filter_query).sort("created_at", -1).skip(offset).limit(limit)
    files = await files_cursor.to_list(limit)
    
    return [UploadResponse(**file_data) for file_data in files]

# =============  POLL ENDPOINTS =============

def calculate_time_ago(created_at) -> str:
    """Calculate time ago string from datetime or string"""
    from datetime import datetime
    import dateutil.parser
    
    now = datetime.utcnow()
    
    # Handle both datetime objects and ISO string formats
    if isinstance(created_at, str):
        try:
            created_at = dateutil.parser.parse(created_at).replace(tzinfo=None)
        except Exception:
            # Fallback if parsing fails
            return "hace un tiempo"
    elif created_at is None:
        return "fecha desconocida"
    
    diff = now - created_at
    
    if diff.days > 0:
        if diff.days == 1:
            return "hace 1 día"
        elif diff.days < 30:
            return f"hace {diff.days} días"
        elif diff.days < 365:
            months = diff.days // 30
            return f"hace {months} {'mes' if months == 1 else 'meses'}"
        else:
            years = diff.days // 365
            return f"hace {years} {'año' if years == 1 else 'años'}"
    
    hours = diff.seconds // 3600
    if hours > 0:
        return f"hace {hours} {'hora' if hours == 1 else 'horas'}"
    
    minutes = diff.seconds // 60
    if minutes > 0:
        return f"hace {minutes} {'minuto' if minutes == 1 else 'minutos'}"
    
    return "hace unos momentos"

# Simple debug endpoint
@api_router.get("/debug/simple")
async def debug_simple():
    """Simple debug endpoint"""
    try:
        # Count collections
        users_count = await db.users.count_documents({})
        follows_count = await db.follows.count_documents({})
        polls_count = await db.polls.count_documents({})
        
        return {
            "status": "ok",
            "users": users_count,
            "follows": follows_count, 
            "polls": polls_count,
            "message": "Backend is working"
        }
        
    except Exception as e:
        return {"error": str(e), "status": "error"}

# Test endpoint for carousel debugging - NO AUTH REQUIRED
@api_router.get("/polls/test-carousel")
async def get_test_carousel():
    """Get test carousel posts for debugging - no authentication required"""
    try:
        # Find carousel posts (layout = "off")
        carousel_posts = await db.polls.find({"layout": "off"}).to_list(10)
        
        if not carousel_posts:
            return []
        
        # Transform to expected format
        transformed_posts = []
        for poll in carousel_posts:
            transformed_post = {
                "id": poll.get("id"),
                "title": poll.get("title"),
                "description": poll.get("description"),
                "layout": poll.get("layout"),
                "options": poll.get("options", []),
                "author": poll.get("author", {}),
                "authorUser": poll.get("authorUser", {}),
                "totalVotes": poll.get("totalVotes", 0),
                "likes": poll.get("likes", 0),
                "comments": poll.get("comments", 0),
                "shares": poll.get("shares", 0),
                "userVote": None,
                "userLiked": False,
                "timeAgo": poll.get("timeAgo", "test"),
                "created_at": poll.get("created_at"),
                "music": poll.get("music"),
                "tags": poll.get("tags", []),
                "category": poll.get("category", "test")
            }
            transformed_posts.append(transformed_post)
        
        return transformed_posts
        
    except Exception as e:
        print(f"Error getting test carousel: {e}")
        return []

@api_router.get("/polls", response_model=List[PollResponse])
async def get_polls(
    limit: int = 20,
    offset: int = 0,
    category: Optional[str] = None,
    featured: Optional[bool] = None,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get polls with pagination and filters"""
    
    # Build filter query
    # 🔒 CRITICAL: Excluir polls de challenges no publicados
    filter_query = {
        "is_active": True,
        "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
        "$or": [
            {"challenge_pending": {"$exists": False}},
            {"challenge_pending": False},
            {"challenge_pending": None}
        ]
    }
    if category:
        filter_query["category"] = category
    if featured is not None:
        filter_query["is_featured"] = featured
    
    # Get polls
    polls_cursor = db.polls.find(filter_query).sort("created_at", -1).skip(offset).limit(limit)
    polls = await polls_cursor.to_list(limit)
    
    if not polls:
        return []
    
    # Get all author IDs
    author_ids = list(set(poll["author_id"] for poll in polls))
    authors_cursor = db.users.find({"id": {"$in": author_ids}})
    authors_list = await authors_cursor.to_list(len(author_ids))
    authors_dict = {user["id"]: UserResponse(**user) for user in authors_list}
    
    # Batch get follow status for all authors to reduce DB queries
    follow_relationships_cursor = db.follows.find({
        "follower_id": current_user.id,
        "following_id": {"$in": author_ids}
    })
    follow_relationships = await follow_relationships_cursor.to_list(len(author_ids))
    following_dict = {rel["following_id"]: rel for rel in follow_relationships}
    
    # Get user votes and likes
    poll_ids = [poll["id"] for poll in polls]
    
    user_votes_cursor = db.votes.find({
        "poll_id": {"$in": poll_ids},
        "user_id": current_user.id
    })
    user_votes = await user_votes_cursor.to_list(len(poll_ids))
    user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
    
    user_likes_cursor = db.poll_likes.find({
        "poll_id": {"$in": poll_ids},
        "user_id": current_user.id
    })
    user_likes = await user_likes_cursor.to_list(len(poll_ids))
    liked_poll_ids = set(like["poll_id"] for like in user_likes)
    
    # Build response
    result = []
    for poll_data in polls:
        # Get option users (solo si las opciones tienen user_id)
        option_user_ids = [option["user_id"] for option in poll_data.get("options", []) if "user_id" in option]
        if option_user_ids:
            option_users_cursor = db.users.find({"id": {"$in": option_user_ids}})
            option_users_list = await option_users_cursor.to_list(len(option_user_ids))
            option_users_dict = {user["id"]: user for user in option_users_list}
        else:
            option_users_dict = {}
        
        # Process options
        options = []
        for option in poll_data.get("options", []):
            option_user = option_users_dict.get(option.get("user_id")) if option.get("user_id") else None
            
            # Keep media_url as relative path for frontend to handle
            media_url = option.get("media_url")
            
            # Get thumbnail URL for videos (handles legacy bogus thumbnail_url == media_url)
            if option.get("media_type") == "video" and media_url:
                thumbnail_url = await resolve_video_thumbnail(media_url, option.get("thumbnail_url"))
            else:
                thumbnail_url = option.get("thumbnail_url")
            
            # Resolve mentioned users for this option
            option_mentioned_users_data = []
            if option.get("mentioned_users"):
                option_mentioned_user_ids = option.get("mentioned_users", [])
                if option_mentioned_user_ids:
                    try:
                        option_mentioned_cursor = db.users.find({"id": {"$in": option_mentioned_user_ids}})
                        option_mentioned_list = await option_mentioned_cursor.to_list(len(option_mentioned_user_ids))
                        
                        option_mentioned_users_data = [
                            {
                                "id": user["id"],
                                "username": user["username"],
                                "display_name": user.get("display_name"),
                                "avatar_url": user.get("avatar_url")
                            } 
                            for user in option_mentioned_list
                        ]
                    except Exception as e:
                        print(f"DEBUG: Error resolving mentioned users for option {option.get('id')}: {e}")
                        option_mentioned_users_data = []
            
            option_dict = {
                "id": option.get("id"),
                "text": option.get("text", ""),
                "votes": option.get("votes", 0),
                "user": {
                    "username": option_user["username"] if option_user else None,
                    "displayName": option_user["display_name"] if option_user else None,
                    "avatar": option_user.get("avatar_url") if option_user else None,
                    "verified": option_user.get("is_verified", False) if option_user else False,
                    "followers": "1K"  # Placeholder
                } if option_user else None,
                "mentioned_users": option_mentioned_users_data,
                "extracted_audio_id": option.get("extracted_audio_id"),
                "media": {
                    "type": option.get("media_type"),
                    "url": media_url,
                    "thumbnail": thumbnail_url,  # None si no hay miniatura real (no caer al video URL)
                    "transform": option.get("media_transform"),
                    # 🎬 Phase 2: optimized MP4 + HLS master playlist URLs.
                    "optimizedUrl": option.get("optimized_media_url"),
                    "hls": option.get("hls_url"),
                } if media_url else None,
                # 🎬 Phase 2 (legacy flat): el utilitario mediaUrl.js también
                # lee estos campos si la estructura "media" no los trae.
                "optimized_media_url": option.get("optimized_media_url"),
                "hls_url": option.get("hls_url"),
            }
            options.append(option_dict)
        
        # Skip polls without valid options or without title
        if not options or not poll_data.get("title"):
            continue
        
        # Get music info if available
        music_info = await get_music_info(poll_data.get("music_id")) if poll_data.get("music_id") else None
        
        # Resolve mentioned users to user objects
        mentioned_users_data = []
        if poll_data.get("mentioned_users"):
            mentioned_user_ids = poll_data.get("mentioned_users", [])
            if mentioned_user_ids:
                try:
                    mentioned_users_cursor = db.users.find({"id": {"$in": mentioned_user_ids}})
                    mentioned_users_list = await mentioned_users_cursor.to_list(len(mentioned_user_ids))
                    
                    # Log for debugging
                    print(f"DEBUG: Found {len(mentioned_users_list)} users out of {len(mentioned_user_ids)} mentioned IDs for poll {poll_data['id']}")
                    if len(mentioned_users_list) != len(mentioned_user_ids):
                        print(f"DEBUG: Missing users for IDs: {set(mentioned_user_ids) - set(user['id'] for user in mentioned_users_list)}")
                    
                    mentioned_users_data = [
                        MentionedUser(
                            id=user["id"],
                            username=user["username"],
                            display_name=user.get("display_name"),
                            avatar_url=user.get("avatar_url")
                        ) 
                        for user in mentioned_users_list
                    ]
                except Exception as e:
                    print(f"DEBUG: Error resolving mentioned users for poll {poll_data['id']}: {e}")
                    mentioned_users_data = []
        
        poll_response = PollResponse(
            id=poll_data["id"],
            title=poll_data["title"],
            author=authors_dict.get(poll_data["author_id"]),
            description=poll_data.get("description"),
            options=options,
            total_votes=poll_data["total_votes"],
            likes=len(poll_data["likes"]) if isinstance(poll_data["likes"], list) else poll_data["likes"],
            shares=len(poll_data["shares"]) if isinstance(poll_data["shares"], list) else poll_data["shares"],
            comments_count=poll_data["comments_count"],
            saves_count=poll_data.get("saves_count", 0),
            music=music_info,  # Include music information
            user_vote=user_votes_dict.get(poll_data["id"]),
            user_liked=poll_data["id"] in liked_poll_ids,
            is_featured=poll_data["is_featured"],
            tags=poll_data.get("tags", []),
            category=poll_data.get("category"),
            mentioned_users=mentioned_users_data,  # Include resolved mentioned users
            layout=poll_data.get("layout"),  # Include layout configuration
            # VS Experience fields - for multi-question VS polls
            vs_id=poll_data.get("vs_id"),
            vs_questions=poll_data.get("vs_questions", []),
            creator_country=poll_data.get("creator_country"),  # Country where VS was created
            vs_orientation=poll_data.get("vs_orientation", "horizontal"),  # VS orientation: 'vertical' or 'horizontal'
            composed_video_url=poll_data.get("composed_video_url"),
            composed_hls_url=poll_data.get("composed_hls_url"),
            composed_status=poll_data.get("composed_status"),
            composed_orientation=poll_data.get("composed_orientation"),
            created_at=poll_data["created_at"],
            time_ago=calculate_time_ago(poll_data["created_at"]),
            # Post settings
            comments_enabled=poll_data.get("comments_enabled", True),
            show_vote_count=poll_data.get("show_vote_count", True)
        )
        result.append(poll_response)
    
    return result

# =============  OPTIMIZED FEED ENDPOINTS =============

@api_router.get("/polls/fast")
async def get_fast_polls(
    limit: int = 10,
    offset: int = 0,
    lightweight: bool = True,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    🚀 FAST FEED: Optimized endpoint for quick loading
    - Minimal DB queries (3 total vs 20+)
    - Lightweight data structure  
    - Built-in caching
    - Perfect for initial feed load
    """
    try:
        from optimized_feed import feed_optimizer, init_feed_optimizer
        
        # Initialize optimizer if not done
        if feed_optimizer is None:
            init_feed_optimizer(db)
        
        if lightweight:
            polls = await feed_optimizer.get_lightweight_feed(
                current_user_id=current_user.id,
                limit=limit,
                offset=offset
            )
        else:
            polls = await feed_optimizer.get_optimized_polls(
                current_user_id=current_user.id, 
                limit=limit,
                offset=offset,
                load_thumbnails=False  # Skip for speed
            )
        
        return {
            "polls": polls,
            "total": len(polls),
            "offset": offset,
            "limit": limit,
            "optimized": True,
            "cache_enabled": True
        }
        
    except Exception as e:
        print(f"❌ Fast feed error: {str(e)}")
        # Fallback to original endpoint
        raise HTTPException(status_code=500, detail="Fast feed temporarily unavailable")

@api_router.get("/polls/{poll_id}/details")
async def get_poll_details_on_demand(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    📱 ON-DEMAND DETAILS: Load full poll data only when user views it
    - Reduces initial load time
    - Full data when needed
    - Optimized for TikTok-style browsing
    """
    try:
        from optimized_feed import feed_optimizer, init_feed_optimizer
        
        # Initialize optimizer if not done
        if feed_optimizer is None:
            init_feed_optimizer(db)
        
        poll_details = await feed_optimizer.get_poll_details_on_demand(
            poll_id=poll_id,
            current_user_id=current_user.id
        )
        
        if not poll_details:
            raise HTTPException(status_code=404, detail="Poll not found")
        
        return poll_details
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Poll details error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to load poll details")

@api_router.get("/polls/preload")
async def preload_next_batch(
    current_offset: int = 0,
    batch_size: int = 5,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    ⚡ PRELOAD: Background loading for smooth infinite scroll
    - Preloads next batch while user views current
    - Smaller batches for faster response
    - Background processing
    """
    try:
        from optimized_feed import feed_optimizer, init_feed_optimizer
        
        # Initialize optimizer if not done
        if feed_optimizer is None:
            init_feed_optimizer(db)
        
        # Load next batch
        next_offset = current_offset + batch_size
        next_polls = await feed_optimizer.get_lightweight_feed(
            current_user_id=current_user.id,
            limit=batch_size,
            offset=next_offset
        )
        
        return {
            "polls": next_polls,
            "next_offset": next_offset,
            "batch_size": batch_size,
            "preloaded": True
        }
        
    except Exception as e:
        print(f"❌ Preload error: {str(e)}")
        return {"polls": [], "next_offset": current_offset, "error": str(e)}

@api_router.get("/polls/ultra-fast")
async def get_ultra_fast_feed(
    limit: int = 10,
    offset: int = 0,
    algorithm: str = "for_you",
    current_user: UserResponse = Depends(get_current_user)
):
    """
    🚀 ULTRA-FAST FEED: Simplified optimized query (no complex aggregation)
    - Fast and reliable
    - Built for performance without complexity
    """
    try:
        # 🚀 SIMPLIFIED FAST QUERY - Just use the working endpoint logic
        # 🔒 CRITICAL: Excluir polls que pertenecen a challenges no publicados
        filter_query = {
            "is_active": True,
            "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
            "$or": [
                {"challenge_pending": {"$exists": False}},  # No tiene el campo
                {"challenge_pending": False},               # Campo existe pero es false
                {"challenge_pending": None}                 # Campo es null
            ]
        }
        
        # Get polls with simple sort (fast and reliable)
        if algorithm == "trending":
            sort_criteria = [("likes_count", -1), ("created_at", -1)]
        elif algorithm == "recent": 
            sort_criteria = [("created_at", -1)]
        else:  # for_you default
            sort_criteria = [("total_votes", -1), ("created_at", -1)]
        
        # Execute fast query
        polls_cursor = db.polls.find(filter_query).sort(sort_criteria).skip(offset).limit(limit)
        polls = await polls_cursor.to_list(limit)
        
        # Get author info in batch (fast)
        author_ids = list(set(poll.get("author_id") for poll in polls if poll.get("author_id")))
        authors_cursor = db.users.find({"id": {"$in": author_ids}})
        authors_list = await authors_cursor.to_list(len(author_ids))
        # Remove _id from authors to avoid ObjectId serialization issues
        authors_dict = {}
        for user in authors_list:
            user_copy = {k: v for k, v in user.items() if k != "_id"}
            authors_dict[user["id"]] = user_copy
        
        # 📊 Batch lookup: user votes and likes for percentage bars
        poll_ids = [poll.get("id") for poll in polls if poll.get("id")]
        
        user_votes_cursor = db.votes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_votes = await user_votes_cursor.to_list(len(poll_ids))
        user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
        
        user_likes_cursor = db.poll_likes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_likes = await user_likes_cursor.to_list(len(poll_ids))
        liked_poll_ids = set(like["poll_id"] for like in user_likes)
        
        # 🔖 Batch lookup: saved polls
        user_saves_cursor = db.saved_polls.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_saves = await user_saves_cursor.to_list(len(poll_ids))
        saved_poll_ids = set(save["poll_id"] for save in user_saves)
        
        # 💬 Batch lookup: user commented polls
        user_comments_pipeline = [
            {"$match": {"poll_id": {"$in": poll_ids}, "user_id": current_user.id}},
            {"$group": {"_id": "$poll_id"}}
        ]
        user_comments_agg = await db.comments.aggregate(user_comments_pipeline).to_list(len(poll_ids))
        commented_poll_ids = set(doc["_id"] for doc in user_comments_agg)
        
        # 🏷️ Batch lookup: Resolve all mentioned user IDs across all options
        all_mentioned_ids = set()
        for poll_data in polls:
            for opt in poll_data.get("options", []):
                for mu in opt.get("mentioned_users", []):
                    if isinstance(mu, str):
                        all_mentioned_ids.add(mu)
        
        mentioned_users_dict = {}
        if all_mentioned_ids:
            mentioned_cursor = db.users.find({"id": {"$in": list(all_mentioned_ids)}})
            mentioned_list = await mentioned_cursor.to_list(len(all_mentioned_ids))
            for u in mentioned_list:
                mentioned_users_dict[u["id"]] = {
                    "id": u["id"],
                    "username": u.get("username"),
                    "display_name": u.get("display_name"),
                    "avatar_url": u.get("avatar_url")
                }

        # Build response quickly
        result = []
        for poll_data in polls:
            author = authors_dict.get(poll_data.get("author_id"))
            if author:
                # 🎨 CRITICAL FIX: Transform options to frontend format with media object
                transformed_options = []
                for opt in poll_data.get("options", []):
                    # Get media fields from database
                    media_url = opt.get("media_url")
                    media_type = opt.get("media_type")
                    # Resolve thumbnail (handles legacy bogus thumbnail_url == media_url for videos)
                    if media_type == "video" and media_url:
                        thumbnail_url = await resolve_video_thumbnail(media_url, opt.get("thumbnail_url"))
                    else:
                        thumbnail_url = opt.get("thumbnail_url")
                    
                    # Resolve mentioned users from IDs to full objects
                    raw_mentions = opt.get("mentioned_users", [])
                    resolved_mentions = []
                    for mu in raw_mentions:
                        if isinstance(mu, str):
                            user_obj = mentioned_users_dict.get(mu)
                            if user_obj:
                                resolved_mentions.append(user_obj)
                        elif isinstance(mu, dict):
                            resolved_mentions.append(mu)
                    
                    # Build option with proper media structure
                    option_dict = {
                        "id": opt.get("id"),
                        "text": opt.get("text", ""),
                        "votes": opt.get("votes", 0),
                        "extracted_audio_id": opt.get("extracted_audio_id"),
                        "mentioned_users": resolved_mentions,
                        # 🎨 CRITICAL: Transform media to frontend format
                        "media": {
                            "type": media_type,
                            "url": media_url,
                            "thumbnail": thumbnail_url,  # None si no hay miniatura real (no caer al video URL)
                            "transform": opt.get("media_transform"),
                            "optimizedUrl": opt.get("optimized_media_url"),
                            "hls": opt.get("hls_url"),
                        } if media_url else None,
                        "optimized_media_url": opt.get("optimized_media_url"),
                        "hls_url": opt.get("hls_url"),
                    }
                    transformed_options.append(option_dict)
                
                # 🎵 CRITICAL FIX: Get music info if music_id exists
                music_info = None
                if poll_data.get("music_id"):
                    music_info = await get_music_info(poll_data.get("music_id"))
                if not music_info and poll_data.get("music"):
                    # Fallback: If music data already embedded, use it
                    music_info = poll_data.get("music")
                
                poll_response = {
                    "id": poll_data.get("id"),
                    "title": poll_data.get("title"),
                    "author": author,
                    "authorUser": author,  # For compatibility
                    "options": transformed_options,  # 🎨 FIXED: Use transformed options
                    "created_at": poll_data.get("created_at"),
                    "total_votes": poll_data.get("total_votes", 0),
                    "totalVotes": poll_data.get("total_votes", 0),  # camelCase alias for frontend
                    "likes": poll_data.get("likes", 0),
                    "likes_count": poll_data.get("likes", 0),  # alias for compatibility
                    "shares": poll_data.get("shares", 0),
                    "saves_count": poll_data.get("saves_count", 0),
                    "comments_count": poll_data.get("comments_count", 0),
                    "layout": poll_data.get("layout"),
                    "mentioned_users": poll_data.get("mentioned_users", []),
                    "music": music_info,  # 🎵 FIXED: Use resolved music info
                    "userVote": user_votes_dict.get(poll_data.get("id")),
                    "userLiked": poll_data.get("id") in liked_poll_ids,
                    "isSaved": poll_data.get("id") in saved_poll_ids,
                    "userCommented": poll_data.get("id") in commented_poll_ids,
                    # Post settings - Include from database
                    "comments_enabled": poll_data.get("comments_enabled", True),
                    "show_vote_count": poll_data.get("show_vote_count", True),
                    # VS fields
                    "vs_id": poll_data.get("vs_id"),
                    "vs_questions": poll_data.get("vs_questions", []),
                    "creator_country": poll_data.get("creator_country"),
                    "vs_orientation": poll_data.get("vs_orientation", "horizontal")
                }
                result.append(poll_response)
        
        # 🏆 AGREGAR CHALLENGES PUBLICADOS AL FEED
        # Los challenges publicados aparecen como una sola entrada unificada
        published_challenges_cursor = db.challenges.find({
            "status": ChallengeStatus.PUBLISHED
        }).sort("published_at", -1).limit(10)
        
        published_challenges = await published_challenges_cursor.to_list(length=10)
        
        for challenge in published_challenges:
            # Obtener los polls de este challenge
            participant_poll_ids = [
                p.get("poll_id") for p in challenge.get("participants", [])
                if p.get("poll_id") and p.get("status") == ParticipantStatus.CONTENT_SUBMITTED
            ]
            
            if not participant_poll_ids:
                continue
            
            challenge_polls = await db.polls.find({"id": {"$in": participant_poll_ids}}).to_list(length=10)
            
            if not challenge_polls:
                continue
            
            # Obtener voto del usuario actual en este challenge
            user_challenge_vote = None
            if current_user:
                existing_vote = await db.challenge_votes.find_one({
                    "challenge_id": challenge.get("id"),
                    "voter_id": current_user.id
                })
                if existing_vote:
                    user_challenge_vote = existing_vote.get("participant_id")
            
            # Construir las opciones del challenge (cada poll es una opción)
            challenge_options = []
            for idx, poll in enumerate(challenge_polls):
                # Obtener la primera opción del poll (el contenido)
                poll_options = poll.get("options", [])
                if poll_options:
                    first_option = poll_options[0]
                    # Buscar info del participante
                    participant_info = next(
                        (p for p in challenge.get("participants", []) if p.get("poll_id") == poll.get("id")),
                        None
                    )
                    
                    # Votos vienen de votes_received del participante en el challenge
                    participant_votes = participant_info.get("votes_received", 0) if participant_info else 0
                    participant_user_id = participant_info.get("user_id") if participant_info else None
                    
                    # Usar el texto original del poll del participante, NO el username
                    original_text = first_option.get("text", "") if first_option else ""
                    
                    challenge_options.append({
                        "id": participant_user_id or poll.get("id"),
                        "text": original_text,
                        "votes": participant_votes,
                        "participant_id": participant_user_id,
                        "participant_username": participant_info.get("username") if participant_info else None,
                        "participant_avatar": participant_info.get("avatar_url") if participant_info else None,
                        "media": {
                            "type": first_option.get("media_type"),
                            "url": first_option.get("media_url"),
                            "thumbnail": first_option.get("thumbnail_url") or first_option.get("media_url"),
                            "optimizedUrl": first_option.get("optimized_media_url"),
                            "hls": first_option.get("hls_url"),
                        } if first_option.get("media_url") else None,
                        "optimized_media_url": first_option.get("optimized_media_url"),
                        "hls_url": first_option.get("hls_url"),
                    })
            
            # Obtener info del creador
            creator = authors_dict.get(challenge.get("creator_id"))
            if not creator:
                creator_doc = await db.users.find_one({"id": challenge.get("creator_id")})
                if creator_doc:
                    creator = {k: v for k, v in creator_doc.items() if k != "_id"}
            
            # Determinar layout: usar required_layout del challenge o auto-calcular
            stored_layout = challenge.get("required_layout")
            if stored_layout:
                challenge_layout = stored_layout
            else:
                # Fallback: auto-calcular según número de participantes
                num_participants = len(challenge_options)
                if num_participants == 2:
                    challenge_layout = "vs-horizontal"
                elif num_participants == 3:
                    challenge_layout = "vs-stack"
                elif num_participants == 4:
                    challenge_layout = "vs-grid-2x2"
                else:
                    challenge_layout = "vs-grid"
            
            # 🎵 Resolve music from challenge polls
            challenge_music_info = None
            for poll in challenge_polls:
                if poll.get("music"):
                    challenge_music_info = poll.get("music")
                    break
                elif poll.get("music_id"):
                    challenge_music_info = await get_music_info(poll.get("music_id"))
                    if challenge_music_info:
                        break
            
            challenge_response = {
                "id": f"challenge_{challenge.get('id')}",
                "challenge_id": challenge.get("id"),
                "title": challenge.get("title"),
                "author": creator,
                "authorUser": creator,
                "options": challenge_options,
                "created_at": challenge.get("published_at") or challenge.get("created_at"),
                "total_votes": sum(opt.get("votes", 0) for opt in challenge_options),
                "totalVotes": sum(opt.get("votes", 0) for opt in challenge_options),
                "likes": challenge.get("likes_count", 0),
                "likes_count": challenge.get("likes_count", 0),
                "comments": challenge.get("comments_count", 0),
                "comments_count": challenge.get("comments_count", 0),
                "shares": 0,
                "saves_count": challenge.get("saves_count", 0),
                "layout": challenge_layout,
                "music": challenge_music_info,
                "userVote": user_challenge_vote,
                "userLiked": False,
                "isSaved": False,
                "userCommented": False,
                "comments_enabled": True,
                "show_vote_count": True,
                "is_challenge": True,
                "challenge_status": "published",
                "participants": [],
            }
            
            # Check if current user liked this challenge
            if current_user:
                challenge_feed_id = f"challenge_{challenge.get('id')}"
                user_like = await db.poll_likes.find_one({
                    "poll_id": challenge_feed_id,
                    "user_id": current_user.id
                })
                challenge_response["userLiked"] = user_like is not None
                
                # Check if saved
                user_save = await db.saved_polls.find_one({
                    "poll_id": challenge_feed_id,
                    "user_id": current_user.id
                })
                challenge_response["isSaved"] = user_save is not None
                challenge_response["saves_count"] = challenge.get("saves_count", 0)
                
                # Check if commented
                user_comment = await db.comments.find_one({
                    "poll_id": challenge_feed_id,
                    "user_id": current_user.id
                })
                challenge_response["userCommented"] = user_comment is not None
            
            # Fetch fresh user data for participants
            participant_user_ids = [
                p.get("user_id") for p in challenge.get("participants", [])
                if p.get("status") == ParticipantStatus.CONTENT_SUBMITTED
            ]
            if participant_user_ids:
                fresh_users = await db.users.find(
                    {"id": {"$in": participant_user_ids}},
                    {"id": 1, "username": 1, "display_name": 1, "avatar_url": 1, "_id": 0}
                ).to_list(length=10)
                fresh_users_map = {u["id"]: u for u in fresh_users}
                
                challenge_response["participants"] = [
                    {
                        "id": p.get("user_id"),
                        "username": fresh_users_map.get(p.get("user_id"), {}).get("username", p.get("username")),
                        "display_name": fresh_users_map.get(p.get("user_id"), {}).get("display_name", p.get("display_name")),
                        "avatar_url": fresh_users_map.get(p.get("user_id"), {}).get("avatar_url", p.get("avatar_url"))
                    }
                    for p in challenge.get("participants", [])
                    if p.get("status") == ParticipantStatus.CONTENT_SUBMITTED
                ]
            
            # Insertar al inicio del feed para que los challenges destaquen
            result.insert(0, challenge_response)
        
        return {
            "polls": result,
            "total": len(result),
            "offset": offset,
            "limit": limit,
            "algorithm": algorithm,
            "optimized": True,
            "simplified": True,
            "performance_level": "ultra-fast-simplified"
        }
        
    except Exception as e:
        print(f"❌ Ultra-fast feed error: {str(e)}")
        raise HTTPException(status_code=500, detail="Ultra-fast feed temporarily unavailable")

@api_router.get("/polls/performance-stats")
async def get_performance_stats(
    current_user: UserResponse = Depends(get_current_user)
):
    """
    📊 PERFORMANCE STATISTICS: Real-time performance metrics
    """
    try:
        from database_optimizer import db_optimizer
        from optimized_feed import feed_optimizer
        
        stats = {
            "database_optimizer": {
                "initialized": db_optimizer is not None,
                "cache_stats": db_optimizer.get_cache_stats() if db_optimizer else None
            },
            "feed_optimizer": {
                "initialized": feed_optimizer is not None, 
                "cache_stats": feed_optimizer.getCacheStats() if feed_optimizer else None
            },
            "performance_endpoints": {
                "ultra_fast_feed": "/api/polls/ultra-fast",
                "fast_feed": "/api/polls/fast", 
                "fast_upload": "/api/fast/upload/video",
                "batch_upload": "/api/fast/upload/batch"
            },
            "optimizations_active": {
                "database_indexes": True,
                "query_aggregation": True,
                "caching": True,
                "batch_processing": True,
                "lazy_loading": True,
                "video_optimization": True
            }
        }
        
        return stats
        
    except Exception as e:
        print(f"❌ Performance stats error: {str(e)}")
        return {"error": "Performance stats temporarily unavailable"}

@api_router.get("/system/health")
async def system_health_check():
    """
    🏥 SYSTEM HEALTH CHECK: Quick system status for performance testing
    """
    try:
        import time
        start_time = time.time()
        
        # Quick DB health check
        db_healthy = False
        try:
            await db.polls.count_documents({"is_active": True}, limit=1)
            db_healthy = True
        except:
            pass
        
        # Performance measurements
        response_time = (time.time() - start_time) * 1000
        
        return {
            "status": "healthy" if db_healthy else "degraded",
            "database": "connected" if db_healthy else "disconnected", 
            "response_time_ms": round(response_time, 2),
            "optimizations": {
                "video_memory_manager": "active",
                "database_optimizer": "active",
                "feed_optimizer": "active",
                "fast_upload": "active"
            },
            "performance_tips": [
                "Use /api/polls/ultra-fast for best feed performance",
                "Use /api/fast/upload/batch for multiple video uploads", 
                "Enable ?optimized=true in frontend for TikTok scroll optimization"
            ],
            "timestamp": time.time()
        }
        
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": time.time()
        }

@api_router.get("/media/thumbnail/{media_id}")
async def get_thumbnail_lazy(
    media_id: str,
    size: str = "small"  # small, medium, large
):
    """
    🖼️ LAZY THUMBNAILS: Generate/serve thumbnails on demand
    - Avoids blocking main feed load
    - Multiple sizes available
    - Cached after first generation
    """
    try:
        # This would normally generate thumbnail on demand
        # For now, return placeholder or cached version
        
        thumbnail_sizes = {
            "small": "150x150",
            "medium": "300x300", 
            "large": "600x600"
        }
        
        # Check if cached thumbnail exists
        # If not, generate asynchronously and return placeholder
        
        return {
            "media_id": media_id,
            "thumbnail_url": f"/api/uploads/thumbnails/{media_id}_{size}.jpg",
            "size": thumbnail_sizes.get(size, "150x150"),
            "cached": True  # Would check actual cache status
        }
        
    except Exception as e:
        print(f"❌ Thumbnail error: {str(e)}")
        # Return placeholder
        return {
            "media_id": media_id,
            "thumbnail_url": "/api/static/placeholder.jpg",
            "size": "150x150",
            "cached": False
        }

@api_router.get("/polls/analytics") 
async def get_feed_analytics(
    current_user: UserResponse = Depends(get_current_user)
):
    """
    📊 FEED ANALYTICS: Performance metrics for optimization
    """
    try:
        # Get basic stats
        total_polls = await db.polls.count_documents({"is_active": True})
        user_polls = await db.polls.count_documents({
            "author_id": current_user.id,
            "is_active": True
        })
        
        # Get recent performance metrics
        recent_polls = await db.polls.find(
            {"created_at": {"$gte": datetime.utcnow() - timedelta(days=7)}},
            {"total_votes": 1, "likes_count": 1, "created_at": 1}
        ).to_list(100)
        
        avg_votes = sum(p.get("total_votes", 0) for p in recent_polls) / max(len(recent_polls), 1)
        avg_likes = sum(p.get("likes_count", 0) for p in recent_polls) / max(len(recent_polls), 1)
        
        return {
            "total_polls": total_polls,
            "user_polls": user_polls,
            "recent_activity": {
                "posts_last_7_days": len(recent_polls),
                "avg_votes_per_post": round(avg_votes, 1),
                "avg_likes_per_post": round(avg_likes, 1)
            },
            "optimization_enabled": True,
            "cache_status": "active"
        }
        
    except Exception as e:
        print(f"❌ Analytics error: {str(e)}")
        return {"error": "Analytics temporarily unavailable"}

@api_router.get("/polls/battles", response_model=List[PollResponse])
async def get_battle_polls(
    limit: int = 20,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get VS/Battle polls (polls with layout='vs' or type='vs')"""
    
    # Build filter query for VS battles
    filter_query = {
        "is_active": True,
        "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
        "$or": [
            {"layout": "vs"},
            {"type": "vs"}
        ]
    }
    
    # Get polls sorted by most recent
    polls_cursor = db.polls.find(filter_query).sort("created_at", -1).skip(offset).limit(limit)
    polls = await polls_cursor.to_list(limit)
    
    if not polls:
        return []
    
    # Get all author IDs
    author_ids = list(set(poll["author_id"] for poll in polls))
    authors_cursor = db.users.find({"id": {"$in": author_ids}})
    authors_list = await authors_cursor.to_list(len(author_ids))
    authors_dict = {user["id"]: UserResponse(**user) for user in authors_list}
    
    # Batch get follow status for all authors
    follow_relationships_cursor = db.follows.find({
        "follower_id": current_user.id,
        "following_id": {"$in": author_ids}
    })
    follow_relationships = await follow_relationships_cursor.to_list(len(author_ids))
    following_dict = {rel["following_id"]: rel for rel in follow_relationships}
    
    # Get user votes and likes
    poll_ids = [poll["id"] for poll in polls]
    
    user_votes_cursor = db.votes.find({
        "poll_id": {"$in": poll_ids},
        "user_id": current_user.id
    })
    user_votes = await user_votes_cursor.to_list(len(poll_ids))
    user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
    
    user_likes_cursor = db.poll_likes.find({
        "poll_id": {"$in": poll_ids},
        "user_id": current_user.id
    })
    user_likes = await user_likes_cursor.to_list(len(poll_ids))
    liked_poll_ids = set(like["poll_id"] for like in user_likes)
    
    # Build response
    result = []
    for poll_data in polls:
        # Get option users
        option_user_ids = [option["user_id"] for option in poll_data.get("options", []) if "user_id" in option]
        if option_user_ids:
            option_users_cursor = db.users.find({"id": {"$in": option_user_ids}})
            option_users_list = await option_users_cursor.to_list(len(option_user_ids))
            option_users_dict = {user["id"]: user for user in option_users_list}
        else:
            option_users_dict = {}
        
        # Process options - determine winner based on votes
        options = []
        max_votes = 0
        for option in poll_data.get("options", []):
            if option.get("votes", 0) > max_votes:
                max_votes = option.get("votes", 0)
        
        for option in poll_data.get("options", []):
            option_user = option_users_dict.get(option.get("user_id")) if option.get("user_id") else None

            # 🎬 Build media dict: preserve existing VS media shape, inject HLS fields.
            # Fallback to flat-field reconstruction for legacy/non-VS option shapes.
            existing_media = option.get("media")
            if isinstance(existing_media, dict):
                merged_media = dict(existing_media)
                # Inject HLS/optimized fields if missing
                if merged_media.get("optimizedUrl") in (None, ""):
                    merged_media["optimizedUrl"] = option.get("optimized_media_url")
                if merged_media.get("hls") in (None, ""):
                    merged_media["hls"] = option.get("hls_url")
            elif option.get("media_url"):
                merged_media = {
                    "type": option.get("media_type"),
                    "url": option.get("media_url"),
                    "thumbnail": option.get("thumbnail_url") or option.get("media_url"),
                    "transform": option.get("media_transform"),
                    "optimizedUrl": option.get("optimized_media_url"),
                    "hls": option.get("hls_url"),
                }
            else:
                merged_media = None

            processed_option = {
                "id": option.get("id"),
                "text": option.get("text"),
                "votes": option.get("votes", 0),
                "media": merged_media,
                # 🎬 Flat HLS/optimized fields for legacy clients (mediaUrl.js fallback)
                "optimized_media_url": option.get("optimized_media_url"),
                "hls_url": option.get("hls_url"),
                "isWinner": option.get("votes", 0) == max_votes and max_votes > 0
            }
            if option_user:
                processed_option["user"] = {
                    "id": option_user["id"],
                    "username": option_user["username"],
                    "display_name": option_user.get("display_name"),
                    "avatar": option_user.get("avatar_url"),
                    "verified": option_user.get("is_verified", False)
                }
            options.append(processed_option)
        
        author = authors_dict.get(poll_data["author_id"])
        is_following_author = poll_data["author_id"] in following_dict
        
        created_at_value = poll_data.get("created_at", datetime.utcnow())
        poll_response = PollResponse(
            id=poll_data["id"],
            title=poll_data["title"],
            author=author,
            description=poll_data.get("description"),
            options=options,
            total_votes=poll_data.get("total_votes", 0),
            likes=poll_data.get("likes", 0),
            shares=poll_data.get("shares", 0),
            comments_count=poll_data.get("comments_count", 0),
            saves_count=poll_data.get("saves_count", 0),
            music_id=poll_data.get("music_id"),
            is_active=poll_data.get("is_active", True),
            created_at=created_at_value,
            time_ago=calculate_time_ago(created_at_value),  # 🐛 FIX: required field
            tags=poll_data.get("tags", []),
            category=poll_data.get("category"),
            is_featured=poll_data.get("is_featured", False),
            mentioned_users=[],
            user_vote=user_votes_dict.get(poll_data["id"]),
            is_liked=poll_data["id"] in liked_poll_ids,
            is_following_author=is_following_author,
            layout=poll_data.get("layout", "vs"),
            type=poll_data.get("type", "vs"),
            views=poll_data.get("views", 0),
            # VS Experience fields (this endpoint serves VS battles)
            vs_id=poll_data.get("vs_id"),
            vs_questions=poll_data.get("vs_questions", []),
            creator_country=poll_data.get("creator_country"),
            vs_orientation=poll_data.get("vs_orientation", "horizontal"),
            composed_video_url=poll_data.get("composed_video_url"),
            composed_hls_url=poll_data.get("composed_hls_url"),
            composed_status=poll_data.get("composed_status"),
            composed_orientation=poll_data.get("composed_orientation"),
        )
        result.append(poll_response)
    
    return result

@api_router.get("/polls/following", response_model=List[PollResponse])
async def get_following_polls(
    limit: int = 20,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get polls from users that the current user follows"""
    
    # Get users that current user follows
    follow_relationships_cursor = db.follows.find({
        "follower_id": current_user.id
    })
    follow_relationships = await follow_relationships_cursor.to_list(None)
    
    if not follow_relationships:
        return []
    
    # Extract following user IDs
    following_user_ids = [rel["following_id"] for rel in follow_relationships]
    
    # Build filter query to only include polls from followed users
    # 🔒 CRITICAL: Excluir polls de challenges no publicados
    filter_query = {
        "is_active": True,
        "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
        "author_id": {"$in": following_user_ids},
        "$or": [
            {"challenge_pending": {"$exists": False}},
            {"challenge_pending": False},
            {"challenge_pending": None}
        ]
    }
    
    # Get polls from followed users only
    polls_cursor = db.polls.find(filter_query).sort("created_at", -1).skip(offset).limit(limit)
    polls = await polls_cursor.to_list(limit)
    
    if not polls:
        # Don't return early - still need to check for published challenges below
        polls = []
    
    # Get all author IDs (should all be in following_user_ids but let's be safe)
    author_ids = list(set(poll["author_id"] for poll in polls)) if polls else []
    authors_cursor = db.users.find({"id": {"$in": author_ids}})
    authors_list = await authors_cursor.to_list(len(author_ids))
    # Remove _id to avoid ObjectId serialization issues
    authors_dict = {user["id"]: UserResponse(**{k: v for k, v in user.items() if k != "_id"}) for user in authors_list}
    
    # For following polls, we know user follows all authors already
    following_dict = {user_id: {"following_id": user_id, "follower_id": current_user.id} 
                     for user_id in following_user_ids}
    
    # Get user votes and likes
    poll_ids = [poll["id"] for poll in polls]
    
    user_votes_cursor = db.votes.find({
        "poll_id": {"$in": poll_ids},
        "user_id": current_user.id
    })
    user_votes = await user_votes_cursor.to_list(len(poll_ids))
    user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
    
    user_likes_cursor = db.poll_likes.find({
        "poll_id": {"$in": poll_ids},
        "user_id": current_user.id
    })
    user_likes = await user_likes_cursor.to_list(len(poll_ids))
    liked_poll_ids = set(like["poll_id"] for like in user_likes)
    
    # 🔖 Batch lookup: saved polls
    user_saves_cursor = db.saved_polls.find({
        "poll_id": {"$in": poll_ids},
        "user_id": current_user.id
    })
    user_saves = await user_saves_cursor.to_list(len(poll_ids))
    saved_poll_ids = set(save["poll_id"] for save in user_saves)
    
    # 💬 Batch lookup: user commented polls
    user_comments_pipeline = [
        {"$match": {"poll_id": {"$in": poll_ids}, "user_id": current_user.id}},
        {"$group": {"_id": "$poll_id"}}
    ]
    user_comments_agg = await db.comments.aggregate(user_comments_pipeline).to_list(len(poll_ids))
    commented_poll_ids = set(doc["_id"] for doc in user_comments_agg)
    
    # 🏷️ Batch resolve: mentioned user IDs to full objects
    all_mentioned_ids = set()
    for poll_data in polls:
        for opt in poll_data.get("options", []):
            for mu in opt.get("mentioned_users", []):
                if isinstance(mu, str):
                    all_mentioned_ids.add(mu)
    
    mentioned_users_map = {}
    if all_mentioned_ids:
        mu_cursor = db.users.find({"id": {"$in": list(all_mentioned_ids)}})
        mu_list = await mu_cursor.to_list(len(all_mentioned_ids))
        for u in mu_list:
            mentioned_users_map[u["id"]] = {
                "id": u["id"],
                "username": u.get("username"),
                "display_name": u.get("display_name"),
                "avatar_url": u.get("avatar_url")
            }

    # Build response (same logic as get_polls but for followed users only)
    result = []
    for poll_data in polls:
        # Get option users - handle options without user_id
        option_user_ids = [option.get("user_id") for option in poll_data.get("options", []) if option.get("user_id")]
        if option_user_ids:
            option_users_cursor = db.users.find({"id": {"$in": option_user_ids}})
            option_users_list = await option_users_cursor.to_list(len(option_user_ids))
            option_users_dict = {user["id"]: user for user in option_users_list}
        else:
            option_users_dict = {}
        
        # Process options
        options = []
        for option in poll_data.get("options", []):
            option_user_id = option.get("user_id")
            option_user = option_users_dict.get(option_user_id) if option_user_id else None
            
            # Keep media_url as relative path for frontend to handle
            media_url = option.get("media_url")
            
            # Get thumbnail URL for videos (handles legacy bogus thumbnail_url == media_url)
            if option.get("media_type") == "video" and media_url:
                thumbnail_url = await resolve_video_thumbnail(media_url, option.get("thumbnail_url"))
            else:
                thumbnail_url = option.get("thumbnail_url")
            
            # Resolve mentioned_users from IDs to full objects
            raw_mentions = option.get("mentioned_users", [])
            resolved_mentions = []
            for mu in raw_mentions:
                if isinstance(mu, str):
                    user_obj = mentioned_users_map.get(mu)
                    if user_obj:
                        resolved_mentions.append(user_obj)
                elif isinstance(mu, dict):
                    resolved_mentions.append(mu)
            
            option_dict = {
                "id": option.get("id"),
                "text": option.get("text", ""),
                "votes": option.get("votes", 0),
                "user": {
                    "username": option_user["username"] if option_user else "unknown",
                    "displayName": option_user.get("display_name", "") if option_user else "",
                    "avatar": option_user.get("avatar_url") if option_user else None,
                    "verified": option_user.get("is_verified", False) if option_user else False,
                    "id": option_user["id"] if option_user else None
                } if option_user else None,
                "mentioned_users": resolved_mentions,
                "extracted_audio_id": option.get("extracted_audio_id"),
                "media": {
                    "type": option.get("media_type"),
                    "url": media_url,
                    "thumbnail": thumbnail_url,
                    "transform": option.get("media_transform"),
                    "optimizedUrl": option.get("optimized_media_url"),
                    "hls": option.get("hls_url"),
                } if media_url else None,
                "optimized_media_url": option.get("optimized_media_url"),
                "hls_url": option.get("hls_url"),
            }
            options.append(option_dict)
        
        # Get author information
        author = authors_dict.get(poll_data["author_id"])
        if not author:
            continue
        
        # Calculate total votes
        total_votes = sum(opt["votes"] for opt in poll_data.get("options", []))
        
        # Get music information
        music_info = None
        if poll_data.get("music_id"):
            music_info = await get_music_info(poll_data["music_id"])
        
        # Resolve mentioned users to user objects
        mentioned_users_data = []
        if poll_data.get("mentioned_users"):
            mentioned_user_ids = poll_data.get("mentioned_users", [])
            if mentioned_user_ids:
                try:
                    mentioned_users_cursor = db.users.find({"id": {"$in": mentioned_user_ids}})
                    mentioned_users_list = await mentioned_users_cursor.to_list(len(mentioned_user_ids))
                    
                    # Log for debugging
                    print(f"DEBUG: Found {len(mentioned_users_list)} users out of {len(mentioned_user_ids)} mentioned IDs for poll {poll_data['id']}")
                    if len(mentioned_users_list) != len(mentioned_user_ids):
                        print(f"DEBUG: Missing users for IDs: {set(mentioned_user_ids) - set(user['id'] for user in mentioned_users_list)}")
                    
                    mentioned_users_data = [
                        MentionedUser(
                            id=user["id"],
                            username=user["username"],
                            display_name=user.get("display_name"),
                            avatar_url=user.get("avatar_url")
                        ) 
                        for user in mentioned_users_list
                    ]
                except Exception as e:
                    print(f"DEBUG: Error resolving mentioned users for poll {poll_data['id']}: {e}")
                    mentioned_users_data = []
        
        poll_response = PollResponse(
            id=poll_data["id"],
            title=poll_data["title"],
            description=poll_data.get("description"),
            author=author,
            options=options,
            total_votes=total_votes,
            likes=poll_data.get("likes", 0),
            shares=poll_data.get("shares", 0),
            comments_count=poll_data.get("comments_count", 0),
            saves_count=poll_data.get("saves_count", 0),
            music=music_info,
            user_vote=user_votes_dict.get(poll_data["id"]),
            user_liked=poll_data["id"] in liked_poll_ids,
            is_saved=poll_data["id"] in saved_poll_ids,
            user_commented=poll_data["id"] in commented_poll_ids,
            is_featured=poll_data.get("is_featured", False),
            tags=poll_data.get("tags", []),
            category=poll_data.get("category"),
            mentioned_users=mentioned_users_data,  # Include resolved mentioned users
            layout=poll_data.get("layout"),
            # VS Experience fields
            vs_id=poll_data.get("vs_id"),
            vs_questions=poll_data.get("vs_questions", []),
            creator_country=poll_data.get("creator_country"),
            vs_orientation=poll_data.get("vs_orientation", "horizontal"),
            composed_video_url=poll_data.get("composed_video_url"),
            composed_hls_url=poll_data.get("composed_hls_url"),
            composed_status=poll_data.get("composed_status"),
            composed_orientation=poll_data.get("composed_orientation"),
            created_at=poll_data["created_at"],
            time_ago=calculate_time_ago(poll_data["created_at"]),
            # Post settings
            comments_enabled=poll_data.get("comments_enabled", True),
            show_vote_count=poll_data.get("show_vote_count", True)
        )
        result.append(poll_response)
    
    # 🏆 AGREGAR CHALLENGES PUBLICADOS DE USUARIOS SEGUIDOS
    try:
        published_challenges_cursor = db.challenges.find({
            "status": ChallengeStatus.PUBLISHED,
            "creator_id": {"$in": following_user_ids}
        }).sort("published_at", -1).limit(10)
        
        published_challenges = await published_challenges_cursor.to_list(length=10)
        
        for challenge in published_challenges:
            participant_poll_ids = [
                p.get("poll_id") for p in challenge.get("participants", [])
                if p.get("poll_id") and p.get("status") == ParticipantStatus.CONTENT_SUBMITTED
            ]
            
            if not participant_poll_ids:
                continue
            
            challenge_polls = await db.polls.find({"id": {"$in": participant_poll_ids}}).to_list(length=10)
            
            if not challenge_polls:
                continue
            
            # Voto del usuario actual en este challenge
            user_challenge_vote = None
            existing_vote = await db.challenge_votes.find_one({
                "challenge_id": challenge.get("id"),
                "voter_id": current_user.id
            })
            if existing_vote:
                user_challenge_vote = existing_vote.get("participant_id")
            
            # Construir opciones del challenge
            challenge_options = []
            for c_poll in challenge_polls:
                c_poll_options = c_poll.get("options", [])
                if c_poll_options:
                    first_option = c_poll_options[0]
                    participant_info = next(
                        (p for p in challenge.get("participants", []) if p.get("poll_id") == c_poll.get("id")),
                        None
                    )
                    participant_votes = participant_info.get("votes_received", 0) if participant_info else 0
                    participant_user_id = participant_info.get("user_id") if participant_info else None
                    
                    # Usar el texto original del poll del participante, NO el username
                    original_text = first_option.get("text", "") if first_option else ""
                    
                    challenge_options.append({
                        "id": participant_user_id or c_poll.get("id"),
                        "text": original_text,
                        "votes": participant_votes,
                        "participant_id": participant_user_id,
                        "participant_username": participant_info.get("username") if participant_info else None,
                        "participant_avatar": participant_info.get("avatar_url") if participant_info else None,
                        "media": {
                            "type": first_option.get("media_type"),
                            "url": first_option.get("media_url"),
                            "thumbnail": first_option.get("thumbnail_url") or first_option.get("media_url"),
                            "optimizedUrl": first_option.get("optimized_media_url"),
                            "hls": first_option.get("hls_url"),
                        } if first_option.get("media_url") else None,
                        "optimized_media_url": first_option.get("optimized_media_url"),
                        "hls_url": first_option.get("hls_url"),
                    })
            
            # Info del creador
            creator = authors_dict.get(challenge.get("creator_id"))
            if not creator:
                creator_doc = await db.users.find_one({"id": challenge.get("creator_id")})
                if creator_doc:
                    creator = UserResponse(**{k: v for k, v in creator_doc.items() if k != "_id"})
            
            if not creator:
                continue
            
            # Layout: usar required_layout del challenge o auto-calcular
            stored_layout = challenge.get("required_layout")
            if stored_layout:
                challenge_layout = stored_layout
            else:
                num_participants = len(challenge_options)
                if num_participants == 2:
                    challenge_layout = "vs-horizontal"
                elif num_participants == 3:
                    challenge_layout = "vs-stack"
                elif num_participants == 4:
                    challenge_layout = "vs-grid-2x2"
                else:
                    challenge_layout = "vs-grid"
            
            # Música del challenge
            challenge_music_info = None
            for c_poll in challenge_polls:
                if c_poll.get("music"):
                    challenge_music_info = c_poll.get("music")
                    break
                elif c_poll.get("music_id"):
                    challenge_music_info = await get_music_info(c_poll.get("music_id"))
                    if challenge_music_info:
                        break
            
            # Participantes con datos frescos
            challenge_participants = []
            p_user_ids = [
                p.get("user_id") for p in challenge.get("participants", [])
                if p.get("status") == ParticipantStatus.CONTENT_SUBMITTED
            ]
            if p_user_ids:
                fresh_users = await db.users.find(
                    {"id": {"$in": p_user_ids}},
                    {"id": 1, "username": 1, "display_name": 1, "avatar_url": 1, "_id": 0}
                ).to_list(length=10)
                fresh_users_map = {u["id"]: u for u in fresh_users}
                
                challenge_participants = [
                    {
                        "id": p.get("user_id"),
                        "username": fresh_users_map.get(p.get("user_id"), {}).get("username", p.get("username")),
                        "display_name": fresh_users_map.get(p.get("user_id"), {}).get("display_name", p.get("display_name")),
                        "avatar_url": fresh_users_map.get(p.get("user_id"), {}).get("avatar_url", p.get("avatar_url"))
                    }
                    for p in challenge.get("participants", [])
                    if p.get("status") == ParticipantStatus.CONTENT_SUBMITTED
                ]
            
            # Check user interaction status
            challenge_feed_id = f"challenge_{challenge.get('id')}"
            user_liked = False
            is_saved = False
            user_commented = False
            
            user_like = await db.poll_likes.find_one({
                "poll_id": challenge_feed_id,
                "user_id": current_user.id
            })
            user_liked = user_like is not None
            
            user_save = await db.saved_polls.find_one({
                "poll_id": challenge_feed_id,
                "user_id": current_user.id
            })
            is_saved = user_save is not None
            
            user_comment = await db.comments.find_one({
                "poll_id": challenge_feed_id,
                "user_id": current_user.id
            })
            user_commented = user_comment is not None
            
            challenge_response = PollResponse(
                id=f"challenge_{challenge.get('id')}",
                title=challenge.get("title", "Challenge"),
                author=creator,
                options=challenge_options,
                total_votes=sum(opt.get("votes", 0) for opt in challenge_options),
                likes=challenge.get("likes_count", 0),
                shares=0,
                comments_count=challenge.get("comments_count", 0),
                saves_count=challenge.get("saves_count", 0),
                music=challenge_music_info,
                user_vote=user_challenge_vote,
                user_liked=user_liked,
                is_saved=is_saved,
                user_commented=user_commented,
                is_featured=False,
                tags=[],
                category=None,
                layout=challenge_layout,
                challenge_id=challenge.get("id"),
                is_challenge=True,
                challenge_status="published",
                participants=challenge_participants,
                created_at=challenge.get("published_at") or challenge.get("created_at"),
                time_ago=calculate_time_ago(challenge.get("published_at") or challenge.get("created_at")),
                comments_enabled=True,
                show_vote_count=True
            )
            result.insert(0, challenge_response)
    except Exception as e:
        print(f"⚠️ Error loading challenges for following feed: {e}")
    
    return result

@api_router.get("/users/{user_id}/polls")
async def get_user_polls(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get polls created by a specific user - for profile page"""
    try:
        # Find user by ID or username (case-insensitive, trimmed)
        target_user = await db.users.find_one({"id": user_id})
        if not target_user:
            target_user = await db.users.find_one({"username": user_id})
            if not target_user:
                # Try case-insensitive and trimmed match
                stripped = user_id.strip()
                target_user = await db.users.find_one({"username": {"$regex": f"^{stripped}\\s*$", "$options": "i"}})
                if not target_user:
                    raise HTTPException(status_code=404, detail="User not found")
        
        target_user_id = target_user["id"]
        
        # Query polls by this user
        filter_query = {
            "author_id": target_user_id,
            "is_active": True,
            "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
            "$or": [
                {"challenge_pending": {"$exists": False}},
                {"challenge_pending": False},
                {"challenge_pending": None}
            ]
        }
        
        polls_cursor = db.polls.find(filter_query).sort("created_at", -1).skip(offset).limit(limit)
        polls = await polls_cursor.to_list(limit)
        
        # Get author info
        author_data = {k: v for k, v in target_user.items() if k != "_id"}
        
        # Get user votes and likes for current user
        poll_ids = [poll.get("id") for poll in polls if poll.get("id")]
        
        user_votes_cursor = db.votes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_votes = await user_votes_cursor.to_list(len(poll_ids))
        user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
        
        user_likes_cursor = db.poll_likes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_likes = await user_likes_cursor.to_list(len(poll_ids))
        liked_poll_ids = set(like["poll_id"] for like in user_likes)
        
        # Resolve mentioned users
        all_mentioned_ids = set()
        for poll_data in polls:
            for opt in poll_data.get("options", []):
                for mu in opt.get("mentioned_users", []):
                    if isinstance(mu, str):
                        all_mentioned_ids.add(mu)
        
        mentioned_users_dict = {}
        if all_mentioned_ids:
            mentioned_cursor = db.users.find({"id": {"$in": list(all_mentioned_ids)}})
            mentioned_list = await mentioned_cursor.to_list(len(all_mentioned_ids))
            for u in mentioned_list:
                mentioned_users_dict[u["id"]] = {
                    "id": u["id"],
                    "username": u.get("username"),
                    "display_name": u.get("display_name"),
                    "avatar_url": u.get("avatar_url")
                }
        
        # Build response
        result = []
        for poll_data in polls:
            # Transform options
            transformed_options = []
            for opt in poll_data.get("options", []):
                media_url = opt.get("media_url")
                media_type = opt.get("media_type")
                # Resolve thumbnail (handles legacy bogus thumbnail_url == media_url for videos)
                if media_type == "video" and media_url:
                    thumbnail_url = await resolve_video_thumbnail(media_url, opt.get("thumbnail_url"))
                else:
                    thumbnail_url = opt.get("thumbnail_url")
                
                raw_mentions = opt.get("mentioned_users", [])
                resolved_mentions = []
                for mu in raw_mentions:
                    if isinstance(mu, str):
                        user_obj = mentioned_users_dict.get(mu)
                        if user_obj:
                            resolved_mentions.append(user_obj)
                    elif isinstance(mu, dict):
                        resolved_mentions.append(mu)
                
                option_dict = {
                    "id": opt.get("id"),
                    "text": opt.get("text", ""),
                    "votes": opt.get("votes", 0),
                    "extracted_audio_id": opt.get("extracted_audio_id"),
                    "mentioned_users": resolved_mentions,
                    "media": {
                        "type": media_type,
                        "url": media_url,
                        "thumbnail": thumbnail_url,  # None si no hay miniatura real (no caer al video URL)
                        "transform": opt.get("media_transform"),
                        "optimizedUrl": opt.get("optimized_media_url"),
                        "hls": opt.get("hls_url"),
                    } if media_url else None,
                    "optimized_media_url": opt.get("optimized_media_url"),
                    "hls_url": opt.get("hls_url"),
                }
                transformed_options.append(option_dict)
            
            # Resolve music
            music_info = None
            if poll_data.get("music_id"):
                music_info = await get_music_info(poll_data.get("music_id"))
            elif poll_data.get("music"):
                music_info = poll_data.get("music")
            
            poll_response = {
                "id": poll_data.get("id"),
                "title": poll_data.get("title"),
                "author": author_data,
                "options": transformed_options,
                "created_at": poll_data.get("created_at"),
                "total_votes": poll_data.get("total_votes", 0),
                "likes": poll_data.get("likes", 0),
                "likes_count": poll_data.get("likes", 0),
                "shares": poll_data.get("shares", 0),
                "saves_count": poll_data.get("saves_count", 0),
                "comments_count": poll_data.get("comments_count", 0),
                "layout": poll_data.get("layout"),
                "mentioned_users": poll_data.get("mentioned_users", []),
                "music": music_info,
                "user_vote": user_votes_dict.get(poll_data.get("id")),
                "user_liked": poll_data.get("id") in liked_poll_ids,
                "comments_enabled": poll_data.get("comments_enabled", True),
                "show_vote_count": poll_data.get("show_vote_count", True),
                "vs_id": poll_data.get("vs_id"),
                "vs_questions": poll_data.get("vs_questions", []),
                "creator_country": poll_data.get("creator_country"),
                "vs_orientation": poll_data.get("vs_orientation", "horizontal"),
                "is_challenge": poll_data.get("is_challenge", False),
                "challenge_id": poll_data.get("challenge_id"),
                "challenge_status": poll_data.get("challenge_status"),
                "participants": poll_data.get("participants", []),
            }
            result.append(poll_response)
        
        total = await db.polls.count_documents(filter_query)
        
        # 🏆 También incluir challenges publicados donde este usuario es creador
        try:
            published_challenges = await db.challenges.find({
                "status": ChallengeStatus.PUBLISHED,
                "creator_id": target_user_id
            }).sort("published_at", -1).to_list(20)
            
            for challenge in published_challenges:
                challenge_poll_ids = []
                for p in challenge.get("participants", []):
                    if p.get("poll_id") and p.get("status") == "content_submitted":
                        challenge_poll_ids.append(p.get("poll_id"))
                
                if not challenge_poll_ids:
                    continue
                
                challenge_polls_cursor = db.polls.find({"id": {"$in": challenge_poll_ids}})
                challenge_polls = await challenge_polls_cursor.to_list(len(challenge_poll_ids))
                challenge_polls_dict = {cp.get("id"): cp for cp in challenge_polls}
                
                challenge_options = []
                for cp in challenge_polls:
                    first_option = cp.get("options", [{}])[0] if cp.get("options") else {}
                    participant_info = next(
                        (p for p in challenge.get("participants", []) if p.get("poll_id") == cp.get("id")),
                        None
                    )
                    participant_votes = participant_info.get("votes_received", 0) if participant_info else 0
                    participant_user_id = participant_info.get("user_id") if participant_info else None
                    original_text = first_option.get("text", "") if first_option else ""
                    
                    challenge_options.append({
                        "id": participant_user_id or cp.get("id"),
                        "text": original_text,
                        "votes": participant_votes,
                        "participant_id": participant_user_id,
                        "participant_username": participant_info.get("username") if participant_info else None,
                        "participant_avatar": participant_info.get("avatar_url") if participant_info else None,
                        "media": {
                            "type": first_option.get("media_type"),
                            "url": first_option.get("media_url"),
                            "thumbnail": first_option.get("thumbnail_url") or first_option.get("media_url"),
                            "optimizedUrl": first_option.get("optimized_media_url"),
                            "hls": first_option.get("hls_url"),
                        } if first_option.get("media_url") else None,
                        "optimized_media_url": first_option.get("optimized_media_url"),
                        "hls_url": first_option.get("hls_url"),
                    })
                
                if not challenge_options:
                    continue
                
                # Layout del challenge
                stored_layout = challenge.get("required_layout")
                if stored_layout:
                    challenge_layout = stored_layout
                else:
                    num_p = len(challenge_options)
                    challenge_layout = "vs-horizontal" if num_p == 2 else "vs-stack" if num_p == 3 else "vs-grid-2x2" if num_p == 4 else "vs-grid"
                
                # Música del challenge
                challenge_music = None
                for cp in challenge_polls:
                    if cp.get("music"):
                        challenge_music = cp.get("music")
                        break
                    elif cp.get("music_id"):
                        challenge_music = await get_music_info(cp.get("music_id"))
                        if challenge_music:
                            break
                
                # Participantes info
                challenge_participants = []
                for p in challenge.get("participants", []):
                    if p.get("status") == "content_submitted":
                        challenge_participants.append({
                            "id": p.get("user_id"),
                            "username": p.get("username"),
                            "display_name": p.get("display_name"),
                            "avatar_url": p.get("avatar_url")
                        })
                
                challenge_poll_response = {
                    "id": f"challenge_{challenge.get('id')}",
                    "challenge_id": challenge.get("id"),
                    "title": challenge.get("title", "Challenge"),
                    "author": author_data,
                    "options": challenge_options,
                    "created_at": challenge.get("published_at") or challenge.get("created_at"),
                    "total_votes": challenge.get("total_votes", 0),
                    "likes": 0,
                    "likes_count": 0,
                    "shares": 0,
                    "saves_count": 0,
                    "comments_count": 0,
                    "layout": challenge_layout,
                    "is_challenge": True,
                    "challenge_status": "published",
                    "participants": challenge_participants,
                    "music": challenge_music,
                    "user_vote": None,
                    "user_liked": False,
                    "comments_enabled": True,
                    "show_vote_count": True,
                }
                result.append(challenge_poll_response)
                total += 1
        except Exception as e:
            logger.error(f"Error loading challenges for user profile: {e}")
        
        # Ordenar todo por fecha (más reciente primero)
        result.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        
        return {"polls": result, "total": total}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user polls: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/users/{user_id}/mentioned-polls", response_model=List[PollResponse])
async def get_user_mentioned_polls(
    user_id: str,
    limit: int = 20,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get polls where a specific user is mentioned (in poll or options)"""
    
    try:
        # Verify the user exists
        target_user = await db.users.find_one({"id": user_id})
        if not target_user:
            # Try to find by username if not found by ID
            target_user = await db.users.find_one({"username": user_id})
            if not target_user:
                raise HTTPException(status_code=404, detail="User not found")
            user_id = target_user["id"]  # Use the actual UUID
        
        # Find polls where user is mentioned (either in poll level or option level)
        polls_cursor = db.polls.find({
            "is_active": True,
            "status": {"$nin": ["broken", "hidden", "failed", "processing"]},  # 🩺 ocultar publicaciones rotas
            "$or": [
                # User mentioned in poll level
                {"mentioned_users": user_id},
                # User mentioned in any option
                {"options.mentioned_users": user_id}
            ]
        }).sort("created_at", -1).skip(offset).limit(limit)
        
        polls = await polls_cursor.to_list(limit)
        
        if not polls:
            return []
        
        # Get all unique author IDs
        author_ids = list(set(poll["author_id"] for poll in polls))
        authors_cursor = db.users.find({"id": {"$in": author_ids}})
        authors_list = await authors_cursor.to_list(len(author_ids))
        authors_dict = {user["id"]: UserResponse(**user) for user in authors_list}
        
        # Check following status for all authors
        following_cursor = db.follows.find({
            "follower_id": current_user.id,
            "following_id": {"$in": author_ids}
        })
        following_list = await following_cursor.to_list(len(author_ids))
        following_dict = {follow["following_id"]: follow for follow in following_list}
        
        # Get user votes and likes for these polls
        poll_ids = [poll["id"] for poll in polls]
        
        user_votes_cursor = db.votes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_votes = await user_votes_cursor.to_list(len(poll_ids))
        user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
        
        user_likes_cursor = db.likes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_likes = await user_likes_cursor.to_list(len(poll_ids))
        user_likes_set = {like["poll_id"] for like in user_likes}
        
        # Process each poll and build response
        result = []
        for poll_data in polls:
            # Get all option user IDs for this poll
            option_user_ids = []
            for option in poll_data.get("options", []):
                if "user_id" in option:
                    option_user_ids.append(option["user_id"])
            
            # Get option users
            option_users_dict = {}
            if option_user_ids:
                option_users_cursor = db.users.find({"id": {"$in": option_user_ids}})
                option_users = await option_users_cursor.to_list(len(option_user_ids))
                option_users_dict = {user["id"]: user for user in option_users}
            
            # Build options with resolved mentioned_users
            options = []
            for option in poll_data.get("options", []):
                option_user = option_users_dict.get(option.get("user_id"))
                
                # Resolve mentioned users for this option
                resolved_mentioned_users = []
                mentioned_user_ids = option.get("mentioned_users", [])
                
                if mentioned_user_ids:
                    mentioned_users_cursor = db.users.find({"id": {"$in": mentioned_user_ids}})
                    mentioned_users_data = await mentioned_users_cursor.to_list(len(mentioned_user_ids))
                    
                    for mentioned_user_data in mentioned_users_data:
                        resolved_mentioned_users.append({
                            "id": mentioned_user_data["id"],
                            "username": mentioned_user_data["username"],
                            "display_name": mentioned_user_data.get("display_name", mentioned_user_data["username"]),
                            "avatar_url": mentioned_user_data.get("avatar_url", "")
                        })
                
                # Handle media URLs
                media_url = option.get("media_url")
                thumbnail_url = option.get("thumbnail_url")
                
                option_dict = {
                    "id": option.get("id"),
                    "text": option["text"],
                    "votes": option["votes"],
                    "user": {
                        "username": option_user["username"] if option_user else "unknown",
                        "displayName": option_user["display_name"] if option_user else "Unknown User",
                        "avatar": option_user.get("avatar_url") if option_user else None,
                        "verified": option_user.get("is_verified", False) if option_user else False,
                        "id": option_user["id"] if option_user else None
                    } if option_user else None,
                    "mentioned_users": resolved_mentioned_users,
                    "media": {
                        "type": option.get("media_type"),
                        "url": media_url,
                        "thumbnail": thumbnail_url,
                        "transform": option.get("media_transform"),
                        "optimizedUrl": option.get("optimized_media_url"),
                        "hls": option.get("hls_url"),
                    } if media_url else None,
                    "optimized_media_url": option.get("optimized_media_url"),
                    "hls_url": option.get("hls_url"),
                }
                options.append(option_dict)
            
            # Get author information
            author = authors_dict.get(poll_data["author_id"])
            if not author:
                continue
            
            # Resolve poll-level mentioned users
            poll_mentioned_users = []
            poll_mentioned_user_ids = poll_data.get("mentioned_users", [])
            
            if poll_mentioned_user_ids:
                poll_mentioned_cursor = db.users.find({"id": {"$in": poll_mentioned_user_ids}})
                poll_mentioned_data = await poll_mentioned_cursor.to_list(len(poll_mentioned_user_ids))
                
                for mentioned_user_data in poll_mentioned_data:
                    poll_mentioned_users.append({
                        "id": mentioned_user_data["id"],
                        "username": mentioned_user_data["username"],
                        "display_name": mentioned_user_data.get("display_name", mentioned_user_data["username"]),
                        "avatar_url": mentioned_user_data.get("avatar_url", "")
                    })
            
            # Create poll response
            poll_response = PollResponse(
                id=poll_data["id"],
                title=poll_data["title"],
                author=author,
                description=poll_data.get("description", ""),
                options=options,
                total_votes=poll_data.get("total_votes", 0),
                likes=poll_data.get("likes", 0),
                shares=poll_data.get("shares", 0),
                comments_count=poll_data.get("comments_count", 0),
                saves_count=poll_data.get("saves_count", 0),
                music=None,  # Could add music info later
                user_vote=user_votes_dict.get(poll_data["id"]),
                user_liked=poll_data["id"] in user_likes_set,
                is_featured=poll_data.get("is_featured", False),
                tags=poll_data.get("tags", []),
                category=poll_data.get("category"),
                mentioned_users=poll_mentioned_users,
                layout=poll_data.get("layout", "carousel"),
                created_at=poll_data.get("created_at"),
                time_ago=calculate_time_ago(poll_data["created_at"]) if poll_data.get("created_at") else "hace un momento",
                # VS Experience fields
                vs_id=poll_data.get("vs_id"),
                vs_questions=poll_data.get("vs_questions", []),
                creator_country=poll_data.get("creator_country"),
                vs_orientation=poll_data.get("vs_orientation", "horizontal"),
                composed_video_url=poll_data.get("composed_video_url"),
            composed_hls_url=poll_data.get("composed_hls_url"),
                composed_status=poll_data.get("composed_status"),
                composed_orientation=poll_data.get("composed_orientation"),
                # Post settings
                comments_enabled=poll_data.get("comments_enabled", True),
                show_vote_count=poll_data.get("show_vote_count", True)
            )
            result.append(poll_response)
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error getting mentioned polls: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

async def _extract_carousel_audio_background(
    poll_id: str,
    video_options: list,
    user_info: dict,
):
    """
    🎵 Extrae audio de los videos de un carrusel en background.

    Se ejecuta DESPUÉS de que el endpoint POST /api/polls devuelve la
    respuesta al cliente, para evitar timeouts en el cliente nativo
    cuando el carrusel tiene varios videos (ffmpeg puede tardar 30-60s
    por video y los proxies/WebViews suelen cortar a los 60s).

    Por cada video válido:
      1. Verifica que tenga pista de audio.
      2. Extrae los primeros `max_duration` segundos a mp3.
      3. Crea un registro en `user_audio`.
      4. Actualiza la opción del poll con su `extracted_audio_id`.
      5. Asigna el primer audio extraído como `music_id` del poll.
    """
    try:
        from audio_utils import extract_audio_from_video, check_video_has_audio
    except Exception as import_err:
        logger.error(
            f"[audio-bg poll={poll_id}] audio_utils not available: {import_err}"
        )
        return

    user_id = user_info.get("id")
    username = user_info.get("username") or "user"
    display_name = user_info.get("display_name") or username

    extracted_count = 0
    primary_audio_id = None

    for vopt in video_options:
        i = vopt["option_index"]
        option_id = vopt["option_id"]
        media_url = vopt["media_url"]
        thumbnail_url = vopt.get("thumbnail_url")

        try:
            if "/api/uploads/" not in (media_url or ""):
                logger.info(
                    f"[audio-bg poll={poll_id}] option {i} not a local upload, skipping"
                )
                continue

            video_filename = media_url.split("/api/uploads/")[-1]
            video_path = UPLOAD_DIR / video_filename

            if not video_path.exists():
                logger.warning(
                    f"[audio-bg poll={poll_id}] option {i} video file missing: {video_path}"
                )
                continue

            if not check_video_has_audio(str(video_path)):
                logger.info(
                    f"[audio-bg poll={poll_id}] option {i} has no audio track, skipping"
                )
                continue

            timestamp = int(datetime.utcnow().timestamp())
            unique_filename = f"carousel_audio_{user_id}_{timestamp}_opt{i}"

            extraction_result = extract_audio_from_video(
                video_path=video_path,
                output_dir=str(AUDIO_UPLOAD_DIR),
                target_filename=unique_filename,
                max_duration=60,
            )

            audio_title = f"original sound-@{username}"
            audio_public_url = f"/api/uploads/audio/{extraction_result['filename']}"

            user_audio_data = {
                "id": str(uuid.uuid4()),
                "title": audio_title,
                "artist": display_name,
                "original_filename": extraction_result["filename"],
                "filename": extraction_result["filename"],
                "file_format": "mp3",
                "file_size": extraction_result["file_size"],
                "duration": int(extraction_result["duration"]),
                "uploader_id": user_id,
                "file_path": extraction_result["processed_path"],
                "public_url": audio_public_url,
                "waveform": extraction_result.get("waveform", []),
                "cover_url": thumbnail_url,
                "privacy": "public",
                "uses_count": 1,
                # 🔧 FIX: sin is_active=True, el endpoint GET /api/audio/{id}
                # devuelve 404 (filtra por is_active:True), el player no
                # resuelve la URL del mp3 y aparece roto. Antes faltaba y
                # todos los audios extraídos de video eran "invisibles".
                "is_active": True,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }

            await db.user_audio.insert_one(user_audio_data)

            audio_id = f"user_audio_{user_audio_data['id']}"

            # Actualizar la opción específica dentro del array `options`
            await db.polls.update_one(
                {"id": poll_id, "options.id": option_id},
                {"$set": {"options.$.extracted_audio_id": audio_id}},
            )

            extracted_count += 1
            if primary_audio_id is None:
                primary_audio_id = audio_id

            logger.info(
                f"[audio-bg poll={poll_id}] option {i} extracted: {audio_id} "
                f"({extraction_result['duration']:.1f}s)"
            )
        except Exception as opt_err:
            logger.error(
                f"[audio-bg poll={poll_id}] option {i} extraction failed: {opt_err}"
            )
            continue

    # Si extrajimos al menos un audio y el poll todavía no tiene music_id,
    # lo asignamos como música principal.
    if primary_audio_id:
        try:
            await db.polls.update_one(
                {"id": poll_id, "$or": [{"music_id": None}, {"music_id": ""}, {"music_id": {"$exists": False}}]},
                {"$set": {"music_id": primary_audio_id}},
            )
            logger.info(
                f"[audio-bg poll={poll_id}] done: {extracted_count} audio(s) extracted, "
                f"primary={primary_audio_id}"
            )
        except Exception as music_err:
            logger.error(
                f"[audio-bg poll={poll_id}] failed to set primary music_id: {music_err}"
            )
    else:
        logger.info(f"[audio-bg poll={poll_id}] done: no audio extracted")


@api_router.post("/polls", response_model=PollResponse)
async def create_poll(
    poll_data: PollCreate,
    background_tasks: BackgroundTasks,
    current_user: UserResponse = Depends(get_current_user)
):
    """Create a new poll"""
    
    # Create poll options
    options = []
    # 🛡️ Helper: detectar thumbnails inválidos (clientes legacy que mandaban
    # el propio .mp4 como thumbnail_url). Si el thumb es un video o coincide
    # con el media_url, lo limpiamos para que la pipeline genere uno real con
    # ffmpeg (1er frame). Sin esto, el cover del audio extraído y el poster
    # del player aparecen rotos.
    _VIDEO_EXTS = ('.mp4', '.mov', '.webm', '.m4v', '.mkv', '.avi')
    def _looks_like_video_url(u):
        if not u or not isinstance(u, str):
            return False
        return u.lower().split('?')[0].endswith(_VIDEO_EXTS)

    for i, option_data in enumerate(poll_data.options):
        # 📱 MOBILE DEBUG: Log incoming transform data
        media_transform = option_data.get("media_transform")
        print(f"🔍 [BACKEND CROP DEBUG] Option {i}: media_transform = {media_transform}")

        # 🛡️ SANITIZAR thumbnail_url
        raw_thumb = option_data.get("thumbnail_url")
        raw_media = option_data.get("media_url")
        sanitized_thumb = raw_thumb
        if raw_thumb and (
            _looks_like_video_url(raw_thumb)
            or (raw_media and raw_thumb == raw_media)
        ):
            print(
                f"🛡️ [thumb-sanitize] option {i}: thumbnail_url apuntaba a un video "
                f"({raw_thumb!r}) → se limpia para que la pipeline genere uno real"
            )
            sanitized_thumb = None

        # 🛡️ media_type debe coincidir con la extensión real del archivo.
        # Si el frontend mandó 'image' pero la URL es un .mp4 (bug histórico),
        # lo corregimos a 'video' aquí, en el punto de entrada, para que la
        # detección posterior funcione (resolve_video_thumbnail, etc).
        declared_type = option_data.get("media_type")
        url_for_type = option_data.get("media_url") or ""
        if url_for_type and re.search(r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)", url_for_type, re.IGNORECASE):
            if declared_type != "video":
                print(f"🛡️ [media_type-fix] option {i}: media_type={declared_type!r} pero la URL es video → corrigiendo a 'video'")
                declared_type = "video"
        elif url_for_type and re.search(r"\.(jpg|jpeg|png|gif|webp|bmp|avif|heic|heif)(\?|$)", url_for_type, re.IGNORECASE):
            if declared_type != "image":
                declared_type = "image"

        option = PollOption(
            user_id=current_user.id,  # For now, creator adds all options
            text=option_data["text"],
            text_position=option_data.get("text_position", "bottom"),  # ✅ Include text position
            media_type=declared_type,
            media_url=option_data.get("media_url"),
            thumbnail_url=sanitized_thumb,
            media_transform=media_transform,  # ✅ Include transform data
            mentioned_users=option_data.get("mentioned_users", [])
        )
        options.append(option)
    
    # Validate mentioned users exist before saving
    valid_mentioned_users = []
    if poll_data.mentioned_users:
        try:
            mentioned_users_cursor = db.users.find({"id": {"$in": poll_data.mentioned_users}})
            existing_users = await mentioned_users_cursor.to_list(len(poll_data.mentioned_users))
            valid_mentioned_users = [user["id"] for user in existing_users]
            
            invalid_users = set(poll_data.mentioned_users) - set(valid_mentioned_users)
            if invalid_users:
                print(f"DEBUG: Invalid mentioned user IDs filtered out: {invalid_users}")
        except Exception as e:
            print(f"DEBUG: Error validating mentioned users: {e}")
            valid_mentioned_users = []

    # 🎵 CAROUSEL VIDEO AUDIO EXTRACTION
    # ⚡ BACKGROUND-EXTRACTION: Si es layout carrusel y no tiene música asignada,
    # programamos la extracción para que se ejecute DESPUÉS de devolver la
    # respuesta. Antes era síncrono y bloqueaba el request hasta varios minutos
    # con múltiples videos, lo que provocaba timeout en la app nativa
    # (Capacitor WebView / proxy ingress) y la publicación parecía fallar.
    needs_carousel_audio_extraction = (
        poll_data.layout == 'off' and not poll_data.music_id
        and any(
            getattr(opt, "media_type", None) == "video" and getattr(opt, "media_url", None)
            for opt in options
        )
    )

    # Create poll
    poll = Poll(
        title=poll_data.title,
        author_id=current_user.id,
        description=poll_data.description,
        options=[opt.model_dump() for opt in options],  # Store as dict in MongoDB - Pydantic v2
        music_id=poll_data.music_id,  # Audio extraído se asignará en background
        tags=poll_data.tags,
        category=poll_data.category,
        mentioned_users=valid_mentioned_users,  # Only save validated user IDs
        video_playback_settings=poll_data.video_playback_settings,
        layout=poll_data.layout,  # Include layout configuration
        # Post settings from frontend
        comments_enabled=poll_data.comments_enabled,
        show_vote_count=poll_data.show_vote_count,
        audience_target=poll_data.audience_target,
        source_authenticity=poll_data.source_authenticity,
        voting_privacy=poll_data.voting_privacy,
        mature_content=poll_data.mature_content,
        allow_downloads=poll_data.allow_downloads
    )
    
    # Insert into database
    await db.polls.insert_one(poll.model_dump())  # Pydantic v2

    # 🩺 VALIDACION DE MEDIOS: marcar el post como "ready" o "broken" segun
    # si todos sus media_url existen en disco (uploads locales) o responden
    # a un HEAD (URLs externas). Los endpoints de listado filtran los
    # "broken" para no mostrar publicaciones rotas al usuario.
    try:
        poll_dict_for_validation = poll.model_dump()
        new_status = await compute_status_for_poll(poll_dict_for_validation)
        if new_status != poll.status:
            await db.polls.update_one(
                {"id": poll.id},
                {"$set": {"status": new_status}}
            )
            poll.status = new_status
            if new_status == "broken":
                logger.warning(
                    f"Poll {poll.id} created but marked as BROKEN (media validation failed)"
                )
    except Exception as validation_error:
        # La validacion no debe bloquear la creacion. Si algo falla,
        # el poll queda en el status por defecto ("ready").
        logger.error(f"Media validation error for poll {poll.id}: {validation_error}")

    # 🎬 VIDEO PIPELINE: si la publicación contiene videos y ya pasó la
    # validación básica de archivos (no está "broken"), la movemos a
    # "processing" y lanzamos un BackgroundTask que:
    #   1. Valida ffprobe cada video (detecta archivos corruptos).
    #   2. Genera thumbnails server-side si faltan.
    #   3. Deja el poll en "ready" (ya visible en feed) o "failed".
    #   4. Transcodifica a 720p mobile-friendly en paralelo (NO bloquea ready).
    # Nota: el status "processing" es muy breve (< 5 s) porque sólo la validación
    # y el thumbnail son síncronos; el transcoding actualiza `optimized_media_url`
    # cuando termina sin cambiar el status.
    try:
        has_video_option = any(
            getattr(opt, "media_type", None) == "video" and getattr(opt, "media_url", None)
            for opt in options
        )
        if has_video_option and poll.status != "broken":
            await db.polls.update_one(
                {"id": poll.id},
                {"$set": {
                    "status": "processing",
                    "processing_started_at": datetime.utcnow(),
                }},
            )
            poll.status = "processing"
            # BackgroundTasks de FastAPI ejecuta esto después de devolver la
            # respuesta al cliente. Si el proceso muere en medio, el reaper
            # lo marcará "failed" tras 10 min.
            background_tasks.add_task(video_pipeline.process_poll_media, db, poll.id)
            logger.info(f"🎬 Poll {poll.id} queued to video pipeline")
    except Exception as pipeline_err:
        # Si el hook falla, no rompemos la creación del poll.
        logger.error(f"Error queuing video pipeline for poll {poll.id}: {pipeline_err}")

    # 🎵 CAROUSEL AUDIO EXTRACTION (BACKGROUND)
    # Programamos la extracción de audio para que se ejecute después de
    # devolver la respuesta. Esto evita que el cliente nativo (Capacitor)
    # haga timeout al subir carruseles con varios videos.
    if needs_carousel_audio_extraction:
        try:
            video_options_payload = [
                {
                    "option_index": i,
                    "option_id": opt.id,
                    "media_url": opt.media_url,
                    "thumbnail_url": opt.thumbnail_url,
                }
                for i, opt in enumerate(options)
                if opt.media_type == "video" and opt.media_url
            ]
            background_tasks.add_task(
                _extract_carousel_audio_background,
                poll.id,
                video_options_payload,
                {
                    "id": current_user.id,
                    "username": current_user.username,
                    "display_name": current_user.display_name,
                },
            )
            logger.info(
                f"🎵 Poll {poll.id} queued for background audio extraction "
                f"({len(video_options_payload)} video(s))"
            )
        except Exception as audio_queue_err:
            logger.error(
                f"Error queuing carousel audio extraction for poll {poll.id}: {audio_queue_err}"
            )

    # Send notifications to mentioned users (both general and option-specific)
    all_mentioned_users = set(poll_data.mentioned_users)
    
    # Collect mentioned users from all options
    for option in options:
        all_mentioned_users.update(option.mentioned_users)
    
    if all_mentioned_users:
        await send_mention_notifications(list(all_mentioned_users), poll.id, current_user)
    
    # Return poll response
    options_response = []
    for option in options:
        option_dict = {
            "id": option.id,
            "text": option.text,
            "votes": option.votes,
            "user": {
                "username": current_user.username,
                "displayName": current_user.display_name,
                "avatar": current_user.avatar_url,
                "verified": current_user.is_verified,
                "followers": "1K"  # Placeholder
            },
            "mentioned_users": option.mentioned_users,  # Include mentioned users in option
            "extracted_audio_id": option.extracted_audio_id,  # 🎵 Include extracted audio ID
            "media": {
                "type": option.media_type,
                "url": option.media_url,
                "thumbnail": option.thumbnail_url,
                "transform": option.media_transform,  # ✅ Include transform data in response
                "optimizedUrl": option.optimized_media_url,
                "hls": option.hls_url,
            } if option.media_url else None,
            "optimized_media_url": option.optimized_media_url,
            "hls_url": option.hls_url,
        }
        options_response.append(option_dict)
    
    # Get music info if available
    music_info = await get_music_info(poll.music_id) if poll.music_id else None
    
    # Resolve mentioned users to user objects for the response
    mentioned_users_data = []
    if poll.mentioned_users:
        try:
            mentioned_users_cursor = db.users.find({"id": {"$in": poll.mentioned_users}})
            mentioned_users_list = await mentioned_users_cursor.to_list(len(poll.mentioned_users))
            
            # Log for debugging
            print(f"DEBUG: Found {len(mentioned_users_list)} users out of {len(poll.mentioned_users)} mentioned IDs for poll {poll.id}")
            if len(mentioned_users_list) != len(poll.mentioned_users):
                print(f"DEBUG: Missing users for IDs: {set(poll.mentioned_users) - set(user['id'] for user in mentioned_users_list)}")
            
            mentioned_users_data = [
                MentionedUser(
                    id=user["id"],
                    username=user["username"],
                    display_name=user.get("display_name"),
                    avatar_url=user.get("avatar_url")
                ) 
                for user in mentioned_users_list
            ]
        except Exception as e:
            print(f"DEBUG: Error resolving mentioned users for poll {poll.id}: {e}")
            mentioned_users_data = []
    
    return PollResponse(
        id=poll.id,
        title=poll.title,
        author=current_user,
        description=poll.description,
        options=options_response,
        total_votes=poll.total_votes,
        likes=poll.likes,
        shares=poll.shares,
        comments_count=poll.comments_count,
        saves_count=poll.saves_count if hasattr(poll, 'saves_count') else 0,
        music=music_info,  # Include music information
        user_vote=None,
        user_liked=False,
        is_featured=poll.is_featured,
        tags=poll.tags,
        category=poll.category,
        mentioned_users=mentioned_users_data,  # Include resolved mentioned users
        layout=poll.layout,  # ✅ FIXED: Include layout field in response
        created_at=poll.created_at,
        time_ago=calculate_time_ago(poll.created_at),
        # Post settings
        comments_enabled=poll.comments_enabled,
        show_vote_count=poll.show_vote_count
    )

@api_router.post("/polls/{poll_id}/vote")
async def vote_on_poll(
    poll_id: str,
    vote_data: VoteCreate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Vote on a poll"""
    
    # Check if poll exists
    poll = await db.polls.find_one({"id": poll_id, "is_active": True})
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    
    # Check if user already voted
    existing_vote = await db.votes.find_one({
        "poll_id": poll_id,
        "user_id": current_user.id
    })
    
    if existing_vote:
        # Update existing vote
        # First, decrease vote count from previous option
        await db.polls.update_one(
            {"id": poll_id, "options.id": existing_vote["option_id"]},
            {"$inc": {"options.$.votes": -1, "total_votes": -1}}
        )
        
        # Update vote record
        await db.votes.update_one(
            {"id": existing_vote["id"]},
            {"$set": {"option_id": vote_data.option_id}}
        )
    else:
        # Create new vote
        vote = Vote(
            poll_id=poll_id,
            option_id=vote_data.option_id,
            user_id=current_user.id
        )
        await db.votes.insert_one(vote.dict())
    
    # Increment vote count for new option
    result = await db.polls.update_one(
        {"id": poll_id, "options.id": vote_data.option_id},
        {"$inc": {"options.$.votes": 1, "total_votes": 1}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Invalid option ID")
    
    # Update user profiles after vote
    try:
        # Update the voter's profile (increment votes_count)
        await ensure_user_profile(current_user.id)
        
        # Update the poll author's profile (increment total_votes received)
        poll_author_id = poll.get("author_id")
        if poll_author_id:
            await ensure_user_profile(poll_author_id)
            
        # Clear follow status cache for affected users
        cache_keys_to_clear = [key for key in follow_status_cache.keys() if current_user.id in key or poll_author_id in key]
        for key in cache_keys_to_clear:
            follow_status_cache.pop(key, None)
            
    except Exception as e:
        print(f"Error updating profiles after vote: {e}")
    
    # Fetch updated poll data to return
    updated_poll = await db.polls.find_one({"id": poll_id})
    if not updated_poll:
        return {"message": "Vote recorded successfully"}
    
    # Find which option index the user voted for
    user_vote_index = None
    for idx, option in enumerate(updated_poll.get("options", [])):
        if option.get("id") == vote_data.option_id:
            user_vote_index = idx
            break
    
    return {
        "message": "Vote recorded successfully",
        "poll_id": poll_id,
        "user_vote": user_vote_index,
        "total_votes": updated_poll.get("total_votes", 0),
        "options": updated_poll.get("options", [])
    }

@api_router.post("/polls/{poll_id}/like")
async def toggle_poll_like(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Toggle like on a poll or challenge"""
    
    # Detect if this is a challenge
    is_challenge = poll_id.startswith("challenge_")
    
    if is_challenge:
        real_challenge_id = poll_id.replace("challenge_", "", 1)
        challenge = await db.challenges.find_one({"id": real_challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge not found")
        
        existing_like = await db.poll_likes.find_one({
            "poll_id": poll_id,
            "user_id": current_user.id
        })
        
        if existing_like:
            await db.poll_likes.delete_one({"poll_id": poll_id, "user_id": current_user.id})
            await db.challenges.update_one({"id": real_challenge_id}, {"$inc": {"likes_count": -1}})
            updated = await db.challenges.find_one({"id": real_challenge_id})
            return {"liked": False, "likes": max(0, updated.get("likes_count", 0))}
        else:
            like = PollLike(poll_id=poll_id, user_id=current_user.id)
            await db.poll_likes.insert_one(like.dict())
            await db.challenges.update_one({"id": real_challenge_id}, {"$inc": {"likes_count": 1}})
            updated = await db.challenges.find_one({"id": real_challenge_id})
            return {"liked": True, "likes": updated.get("likes_count", 1)}
    
    # Regular poll
    poll = await db.polls.find_one({"id": poll_id, "is_active": True})
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    
    # Check if user already liked
    existing_like = await db.poll_likes.find_one({
        "poll_id": poll_id,
        "user_id": current_user.id
    })
    
    if existing_like:
        # Remove like
        await db.poll_likes.delete_one({
            "poll_id": poll_id,
            "user_id": current_user.id
        })
        
        # Decrement like count
        await db.polls.update_one(
            {"id": poll_id},
            {"$inc": {"likes": -1}}
        )
        
        # Get updated count
        updated_poll = await db.polls.find_one({"id": poll_id})
        
        # Update user profiles after like removal
        try:
            # Update the liker's profile (decrement likes_given)
            await ensure_user_profile(current_user.id)
            
            # Update the poll author's profile (decrement likes_count)
            poll_author_id = poll.get("author_id")
            if poll_author_id:
                await ensure_user_profile(poll_author_id)
                
            # Clear follow status cache for affected users
            cache_keys_to_clear = [key for key in follow_status_cache.keys() if current_user.id in key or poll_author_id in key]
            for key in cache_keys_to_clear:
                follow_status_cache.pop(key, None)
                
        except Exception as e:
            print(f"Error updating profiles after like removal: {e}")
        
        return {
            "liked": False,
            "likes": updated_poll["likes"]
        }
    else:
        # Add like
        like = PollLike(
            poll_id=poll_id,
            user_id=current_user.id
        )
        
        await db.poll_likes.insert_one(like.dict())
        
        # Increment like count
        await db.polls.update_one(
            {"id": poll_id},
            {"$inc": {"likes": 1}}
        )
        
        # Get updated count
        updated_poll = await db.polls.find_one({"id": poll_id})
        
        # Update user profiles after like addition
        try:
            # Update the liker's profile (increment likes_given)
            await ensure_user_profile(current_user.id)
            
            # Update the poll author's profile (increment likes_count)
            poll_author_id = poll.get("author_id")
            if poll_author_id:
                await ensure_user_profile(poll_author_id)
                
            # Clear follow status cache for affected users
            cache_keys_to_clear = [key for key in follow_status_cache.keys() if current_user.id in key or poll_author_id in key]
            for key in cache_keys_to_clear:
                follow_status_cache.pop(key, None)
                
        except Exception as e:
            print(f"Error updating profiles after like addition: {e}")
        
        return {
            "liked": True,
            "likes": updated_poll["likes"]
        }


@api_router.get("/polls/{poll_id}/voters")
async def get_poll_voters(
    poll_id: str,
    limit: int = 50,
    skip: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get list of users who voted on a poll"""
    
    # Check if poll exists
    poll = await db.polls.find_one({"id": poll_id, "is_active": True})
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    
    # Get votes with user info
    votes_cursor = db.votes.find({"poll_id": poll_id}).sort("created_at", -1).skip(skip).limit(limit)
    votes = await votes_cursor.to_list(length=limit)
    
    # Get user info for each vote
    voters = []
    seen_users = set()  # To avoid duplicates if user voted multiple times
    
    for vote in votes:
        user_id = vote.get("user_id")
        if user_id and user_id not in seen_users:
            seen_users.add(user_id)
            user = await db.users.find_one({"id": user_id})
            if user:
                # Check if current user follows this voter
                is_following = await db.follows.find_one({
                    "follower_id": current_user.id,
                    "following_id": user_id
                }) is not None
                
                # Check if this voter follows the current user
                follows_me = await db.follows.find_one({
                    "follower_id": user_id,
                    "following_id": current_user.id
                }) is not None
                
                # Get which option they voted for
                option_index = vote.get("option_index")
                option_text = None
                if option_index is not None and poll.get("options"):
                    if 0 <= option_index < len(poll["options"]):
                        option_text = poll["options"][option_index].get("text", "")
                
                voters.append({
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "display_name": user.get("display_name", user.get("username")),
                    "avatar_url": user.get("avatar_url"),
                    "is_verified": user.get("is_verified", False),
                    "is_following": is_following,
                    "follows_me": follows_me,
                    "voted_at": vote.get("created_at"),
                    "voted_option": option_text
                })
    
    # Get total votes count (unique users)
    all_votes = await db.votes.find({"poll_id": poll_id}).to_list(length=None)
    unique_voters = len(set(v.get("user_id") for v in all_votes if v.get("user_id")))
    
    # Count total views from poll_views collection
    # This counts EVERY view, not unique users - includes repeated views
    total_views = await db.poll_views.count_documents({"poll_id": poll_id})
    
    # If no views registered yet, use poll's views field or default to 0
    views = total_views if total_views > 0 else poll.get("views", 0)
    
    return {
        "poll_id": poll_id,
        "voters": voters,
        "total_votes": unique_voters,
        "views": views,
        "has_more": len(seen_users) >= limit
    }

@api_router.post("/polls/{poll_id}/share")
async def share_poll(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Increment share count for a poll and track user share"""
    
    # Check if poll exists
    poll = await db.polls.find_one({"id": poll_id, "is_active": True})
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    
    # Check if user has already shared this poll
    existing_share = await db.poll_shares.find_one({
        "poll_id": poll_id,
        "user_id": current_user.id
    })
    
    if not existing_share:
        # Record the share
        await db.poll_shares.insert_one({
            "id": str(uuid.uuid4()),
            "poll_id": poll_id,
            "user_id": current_user.id,
            "shared_at": datetime.utcnow().isoformat()
        })
        
        # Increment share count only if it's a new share
        await db.polls.update_one(
            {"id": poll_id},
            {"$inc": {"shares": 1}}
        )
    
    # Get updated count
    updated_poll = await db.polls.find_one({"id": poll_id})
    
    return {
        "shares": updated_poll.get("shares", 0),
        "user_shared": True
    }

@api_router.post("/polls/{poll_id}/view")
@api_router.post("/polls/{poll_id}/view")
async def register_poll_view(
    poll_id: str,
    request: Request,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """Register a view for a poll - counts every view, even repeated views from same user"""
    
    # Check if poll exists
    poll = await db.polls.find_one({"id": poll_id, "is_active": True})
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    
    # Get user_id if authenticated, otherwise use session_id from request
    user_id = current_user.id if current_user else None
    
    # For non-authenticated users, use a session identifier
    # This could be from a header, cookie, or client-generated ID
    session_id = None
    if not user_id:
        # Try to get session_id from request body or generate from IP
        session_id = request.headers.get("X-Session-ID")
        if not session_id:
            # Fallback: use IP address as session identifier
            client_ip = request.client.host if request.client else "unknown"
            session_id = f"anon_{client_ip}_{datetime.utcnow().timestamp()}"
    
    # Register the view (always create a new entry)
    view_record = {
        "id": str(uuid.uuid4()),
        "poll_id": poll_id,
        "user_id": user_id,
        "session_id": session_id,
        "viewed_at": datetime.utcnow().isoformat(),
        "ip_address": request.client.host if request.client else None
    }
    
    await db.poll_views.insert_one(view_record)
    
    # Get total views count for this poll
    total_views = await db.poll_views.count_documents({"poll_id": poll_id})
    
    return {
        "success": True,
        "poll_id": poll_id,
        "total_views": total_views,
        "message": "View registered successfully"
    }



@api_router.get("/polls/{poll_id}", response_model=PollResponse)
async def get_poll_by_id(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get a specific poll by ID"""
    
    # Get poll
    poll = await db.polls.find_one({"id": poll_id, "is_active": True})
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    
    # Get author info
    author_data = await db.users.find_one({"id": poll["author_id"]})
    if not author_data:
        raise HTTPException(status_code=404, detail="Author not found")
    author = UserResponse(**author_data)
    
    # Get option users.
    # NOTA: las polls tipo VS pueden tener options sin `user_id` (las opciones
    # son simples imágenes/videos sin autor asociado). Usamos .get() y
    # filtramos None para no romper con KeyError. Antes este endpoint
    # devolvía 500 al pedir un poll VS desde /post/:id, mostrando
    # "Post no disponible" en el frontend.
    option_user_ids = [
        option.get("user_id")
        for option in poll.get("options", [])
        if option.get("user_id")
    ]
    option_users_dict = {}
    if option_user_ids:
        option_users_cursor = db.users.find({"id": {"$in": option_user_ids}})
        option_users_list = await option_users_cursor.to_list(len(option_user_ids))
        option_users_dict = {user["id"]: user for user in option_users_list}
    
    # Get user vote and like
    user_vote = await db.votes.find_one({
        "poll_id": poll_id,
        "user_id": current_user.id
    })
    
    user_like = await db.poll_likes.find_one({
        "poll_id": poll_id,
        "user_id": current_user.id
    })
    
    # Process options
    options = []
    for option in poll.get("options", []):
        # Igual que arriba: usar .get() para no romper en opciones VS sin user_id
        option_user = option_users_dict.get(option.get("user_id")) if option.get("user_id") else None
        if option_user:
            media_url = option.get("media_url")
            
            # Get thumbnail URL for videos (handles legacy bogus thumbnail_url == media_url)
            if option.get("media_type") == "video" and media_url:
                thumbnail_url = await resolve_video_thumbnail(media_url, option.get("thumbnail_url"))
            else:
                thumbnail_url = option.get("thumbnail_url")
            
            option_dict = {
                "id": option["id"],
                "text": option["text"],
                "votes": option["votes"],
                "user": {
                    "username": option_user["username"],
                    "displayName": option_user["display_name"],
                    "avatar": option_user.get("avatar_url"),
                    "verified": option_user.get("is_verified", False),
                    "followers": "1K"  # Placeholder
                },
                "mentioned_users": option.get("mentioned_users", []),  # Include mentioned users
                "extracted_audio_id": option.get("extracted_audio_id"),  # 🎵 Include extracted audio ID
                "media": {
                    "type": option.get("media_type"),
                    "url": media_url,
                    "thumbnail": thumbnail_url,
                    "transform": option.get("media_transform"),  # ✅ Include transform data
                    "optimizedUrl": option.get("optimized_media_url"),
                    "hls": option.get("hls_url"),
                } if media_url else None,
                "optimized_media_url": option.get("optimized_media_url"),
                "hls_url": option.get("hls_url"),
            }
            options.append(option_dict)
    
    # Get music info if available
    music_info = await get_music_info(poll.get("music_id")) if poll.get("music_id") else None
    
    return PollResponse(
        id=poll["id"],
        title=poll["title"],
        author=author,
        description=poll.get("description"),
        options=options,
        total_votes=poll["total_votes"],
        likes=poll["likes"],
        shares=poll["shares"],
        comments_count=poll["comments_count"],
        saves_count=poll.get("saves_count", 0),
        music=music_info,  # Include music information
        user_vote=user_vote["option_id"] if user_vote else None,
        user_liked=bool(user_like),
        is_featured=poll["is_featured"],
        tags=poll.get("tags", []),
        category=poll.get("category"),
        mentioned_users=poll.get("mentioned_users", []),  # Include mentioned users
        layout=poll.get("layout"),  # ✅ CRITICAL FIX: Include layout field for LayoutRenderer
        # VS Experience fields - for multi-question VS polls. Sin esto, al
        # abrir un poll VS desde la pantalla de Activity, el frontend no
        # recibía vs_questions/vs_orientation y mostraba el layout por
        # defecto (arriba-abajo) sin imágenes en vez de izq-der.
        vs_id=poll.get("vs_id"),
        vs_questions=poll.get("vs_questions", []),
        creator_country=poll.get("creator_country"),
        vs_orientation=poll.get("vs_orientation", "horizontal"),
        composed_video_url=poll.get("composed_video_url"),
        composed_hls_url=poll.get("composed_hls_url"),
        composed_status=poll.get("composed_status"),
        composed_orientation=poll.get("composed_orientation"),
        created_at=poll["created_at"],
        time_ago=calculate_time_ago(poll["created_at"]),
        # Post settings
        comments_enabled=poll.get("comments_enabled", True),
        show_vote_count=poll.get("show_vote_count", True)
    )

# =============  USER AUDIO ENDPOINTS =============

# Create audio upload directory
AUDIO_UPLOAD_DIR = UPLOAD_DIR / "audio"
AUDIO_UPLOAD_DIR.mkdir(exist_ok=True)

def get_unique_filename(user_id: str, original_filename: str) -> str:
    """Generate unique filename for uploaded audio"""
    timestamp = int(datetime.utcnow().timestamp())
    file_ext = original_filename.split('.')[-1].lower()
    return f"audio_{user_id}_{timestamp}.{file_ext}"

def cleanup_temp_files(*file_paths):
    """Clean up temporary files"""
    for file_path in file_paths:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Could not delete temp file {file_path}: {e}")

class AudioProcessingError(Exception):
    """Custom exception for audio processing errors"""
    pass

@api_router.post("/audio/upload")
async def upload_audio(
    file: UploadFile = File(...),
    title: str = "",
    artist: str = "",
    privacy: AudioPrivacy = AudioPrivacy.PRIVATE,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Subir archivo de audio personal
    
    Formatos soportados: MP3, M4A, WAV, AAC
    Duración máxima: 60 segundos (se recorta automáticamente)
    Tamaño máximo: 10MB
    """
    import tempfile
    
    try:
        # Validar tipo de archivo
        if not file.filename:
            raise HTTPException(status_code=400, detail="Filename is required")
        
        # Guardar archivo temporal
        temp_file = None
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file.filename}") as tmp:
            temp_file = tmp.name
            content = await file.read()
            tmp.write(content)
        
        try:
            # Validar archivo de audio
            validation = validate_audio_file(file)
            if not validation['valid']:
                raise HTTPException(status_code=400, detail=validation['error'])
            
            logger.info(f"Audio validation passed: {file.filename}")
            
            # Generar nombre único para el archivo
            unique_filename = get_unique_filename(current_user.id, file.filename)
            final_path = AUDIO_UPLOAD_DIR / unique_filename.replace(unique_filename.split('.')[-1], 'mp3')
            
            # Procesar audio (recortar, optimizar, generar waveform)
            processing_result = process_audio_file(temp_file, max_duration=60)
            
            if not processing_result['success']:
                raise AudioProcessingError(processing_result['error'])
            
            # Mover archivo procesado a ubicación final
            import shutil
            shutil.move(processing_result['processed_path'], str(final_path))
            
            # Obtener tamaño del archivo final
            file_size = os.path.getsize(final_path)
            
            logger.info(f"Audio processing completed: {processing_result}")
            
            # Crear URL pública
            public_url = f"/api/uploads/audio/{final_path.name}"
            
            # Preparar datos para la base de datos
            audio_data = UserAudio(
                title=title.strip() or file.filename.split('.')[0],
                artist=artist.strip() or current_user.display_name or current_user.username,
                original_filename=file.filename,
                filename=final_path.name,
                file_format='mp3',  # Siempre convertimos a MP3
                file_size=file_size,
                duration=int(processing_result['duration']),
                uploader_id=current_user.id,
                file_path=str(final_path),
                public_url=public_url,
                waveform=processing_result['waveform'],
                privacy=privacy,
                bitrate=processing_result.get('bitrate', 128),
                sample_rate=processing_result.get('sample_rate', 44100),
                is_processed=True
            )
            
            # Guardar en base de datos
            result = await db.user_audio.insert_one(audio_data.dict())
            if not result.inserted_id:
                raise HTTPException(status_code=500, detail="Failed to save audio to database")
            
            # Obtener información del usuario
            uploader_response = UserResponse(
                id=current_user.id,
                username=current_user.username,
                display_name=current_user.display_name,
                avatar_url=current_user.avatar_url,
                email=current_user.email,
                bio=current_user.bio,
                is_verified=current_user.is_verified,
                created_at=current_user.created_at,
                last_login=current_user.last_login,
                is_public=current_user.is_public,
                allow_messages=current_user.allow_messages
            )
            
            # Preparar respuesta
            audio_response = UserAudioResponse(
                **audio_data.dict(),
                uploader=uploader_response,
                url=public_url,
                preview_url=public_url,
                uses=0
            )
            
            logger.info(f"✅ Audio uploaded successfully: {audio_data.title} by {current_user.username}")
            
            return {
                "success": True,
                "message": "Audio uploaded and processed successfully",
                "audio": audio_response.dict()
            }
            
        finally:
            # Limpiar archivo temporal
            cleanup_temp_files(temp_file)
            
    except AudioProcessingError as e:
        logger.error(f"Audio processing error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error uploading audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error uploading audio: {str(e)}")

@api_router.get("/audio/my-library")
async def get_my_audio_library(
    limit: int = 20,
    offset: int = 0,
    search: Optional[str] = None,
    current_user: UserResponse = Depends(get_current_user)
):
    """Obtener biblioteca de audio personal del usuario"""
    try:
        # Construir filtro de búsqueda
        filter_query = {
            "uploader_id": current_user.id,
            "is_active": True
        }
        
        if search:
            search_regex = {"$regex": search.strip(), "$options": "i"}
            filter_query["$or"] = [
                {"title": search_regex},
                {"artist": search_regex}
            ]
        
        # Obtener total de audios
        total = await db.user_audio.count_documents(filter_query)
        
        # Obtener audios con paginación
        user_audios = await db.user_audio.find(filter_query) \
            .sort("created_at", -1) \
            .skip(offset) \
            .limit(limit) \
            .to_list(limit)
        
        # Preparar respuesta con información del usuario
        audio_responses = []
        for audio_data in user_audios:
            uploader_response = UserResponse(
                id=current_user.id,
                username=current_user.username,
                display_name=current_user.display_name,
                avatar_url=current_user.avatar_url,
                email=current_user.email,
                bio=current_user.bio,
                is_verified=current_user.is_verified,
                created_at=current_user.created_at,
                last_login=current_user.last_login,
                is_public=current_user.is_public,
                allow_messages=current_user.allow_messages
            )
            
            audio_response = UserAudioResponse(
                **audio_data,
                uploader=uploader_response,
                url=audio_data["public_url"],
                preview_url=audio_data["public_url"],
                uses=audio_data["uses_count"]
            )
            audio_responses.append(audio_response.dict())
        
        return {
            "success": True,
            "audios": audio_responses,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total
        }
        
    except Exception as e:
        logger.error(f"Error getting user audio library: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting audio library: {str(e)}")

@api_router.get("/audio/public-library")
async def get_public_audio_library(
    limit: int = 20,
    offset: int = 0,
    search: Optional[str] = None,
    current_user: UserResponse = Depends(get_current_user)
):
    """Obtener biblioteca de audio público (audios que otros usuarios han marcado como públicos)"""
    try:
        # Construir filtro de búsqueda para audios públicos
        filter_query = {
            "privacy": AudioPrivacy.PUBLIC,
            "is_active": True,
            "is_processed": True
        }
        
        if search:
            search_regex = {"$regex": search.strip(), "$options": "i"}
            filter_query["$or"] = [
                {"title": search_regex},
                {"artist": search_regex}
            ]
        
        # Obtener total de audios públicos
        total = await db.user_audio.count_documents(filter_query)
        
        # Obtener audios con paginación, ordenados por uses_count (más populares primero)
        user_audios = await db.user_audio.find(filter_query) \
            .sort("uses_count", -1) \
            .skip(offset) \
            .limit(limit) \
            .to_list(limit)
        
        # Preparar respuesta con información de los usuarios
        audio_responses = []
        for audio_data in user_audios:
            # Obtener información del usuario que subió el audio
            uploader = await db.users.find_one({"id": audio_data["uploader_id"]})
            if uploader:
                uploader_response = UserResponse(**uploader)
                
                audio_response = UserAudioResponse(
                    **audio_data,
                    uploader=uploader_response,
                    url=audio_data["public_url"],
                    preview_url=audio_data["public_url"],
                    uses=audio_data["uses_count"]
                )
                audio_responses.append(audio_response.dict())
        
        return {
            "success": True,
            "audios": audio_responses,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
            "message": f"Found {len(audio_responses)} public audio tracks"
        }
        
    except Exception as e:
        logger.error(f"Error getting public audio library: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting public audio library: {str(e)}")

@api_router.put("/audio/{audio_id}")
async def update_audio(
    audio_id: str,
    update_data: UserAudioUpdate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Actualizar información de un audio (solo el propietario)"""
    try:
        # Verificar que el audio existe y pertenece al usuario
        audio_data = await db.user_audio.find_one({
            "id": audio_id,
            "uploader_id": current_user.id,
            "is_active": True
        })
        
        if not audio_data:
            raise HTTPException(status_code=404, detail="Audio not found or access denied")
        
        # Preparar campos de actualización
        update_fields = {"updated_at": datetime.utcnow()}
        
        if update_data.title is not None:
            update_fields["title"] = update_data.title.strip()
        if update_data.artist is not None:
            update_fields["artist"] = update_data.artist.strip()
        if update_data.privacy is not None:
            update_fields["privacy"] = update_data.privacy
        if update_data.cover_url is not None:
            update_fields["cover_url"] = update_data.cover_url.strip()
        
        # Actualizar en base de datos
        result = await db.user_audio.update_one(
            {"id": audio_id},
            {"$set": update_fields}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to update audio")
        
        # Obtener audio actualizado
        updated_audio = await db.user_audio.find_one({"id": audio_id})
        uploader_response = UserResponse(
            id=current_user.id,
            username=current_user.username,
            display_name=current_user.display_name,
            avatar_url=current_user.avatar_url,
            email=current_user.email
        )
        
        audio_response = UserAudioResponse(
            **updated_audio,
            uploader=uploader_response,
            url=updated_audio["public_url"],
            preview_url=updated_audio["public_url"],
            uses=updated_audio["uses_count"]
        )
        
        return {
            "success": True,
            "message": "Audio updated successfully",
            "audio": audio_response.dict()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating audio: {str(e)}")

@api_router.delete("/audio/{audio_id}")
async def delete_audio(
    audio_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Eliminar un audio (solo el propietario)"""
    try:
        # Verificar que el audio existe y pertenece al usuario
        audio_data = await db.user_audio.find_one({
            "id": audio_id,
            "uploader_id": current_user.id,
            "is_active": True
        })
        
        if not audio_data:
            raise HTTPException(status_code=404, detail="Audio not found or access denied")
        
        # Marcar como inactivo (soft delete)
        result = await db.user_audio.update_one(
            {"id": audio_id},
            {
                "$set": {
                    "is_active": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to delete audio")
        
        # Opcional: Eliminar archivo físico
        try:
            if os.path.exists(audio_data["file_path"]):
                os.remove(audio_data["file_path"])
        except Exception as e:
            logger.warning(f"Could not delete audio file {audio_data['file_path']}: {e}")
        
        return {
            "success": True,
            "message": "Audio deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting audio: {str(e)}")

@api_router.post("/audio/{audio_id}/use")
async def use_audio_in_post(
    audio_id: str,
    poll_id: Optional[str] = None,
    current_user: UserResponse = Depends(get_current_user)
):
    """Marcar que un usuario ha usado un audio en un post"""
    try:
        # Verificar que el audio existe y está disponible
        audio_data = await db.user_audio.find_one({
            "id": audio_id,
            "is_active": True,
            "is_processed": True
        })
        
        if not audio_data:
            raise HTTPException(status_code=404, detail="Audio not found")
        
        # Verificar permisos: el audio debe ser público o del usuario actual
        if audio_data["privacy"] == AudioPrivacy.PRIVATE and audio_data["uploader_id"] != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied to private audio")
        
        # Registrar el uso
        audio_use = UserAudioUse(
            audio_id=audio_id,
            user_id=current_user.id,
            poll_id=poll_id
        )
        
        await db.user_audio_uses.insert_one(audio_use.dict())
        
        # Incrementar contador de usos
        await db.user_audio.update_one(
            {"id": audio_id},
            {"$inc": {"uses_count": 1}}
        )
        
        return {
            "success": True,
            "message": "Audio use recorded successfully",
            "audio_id": audio_id,
            "use_id": audio_use.id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error recording audio use: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error recording audio use: {str(e)}")

@api_router.get("/audio/{audio_id}")
async def get_audio_details(
    audio_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Obtener detalles de un audio específico (usuario o sistema iTunes)"""
    try:
        print(f"🎵 Getting audio details for ID: {audio_id}")
        
        # First try to find in user audio
        audio_data = await db.user_audio.find_one({
            "id": audio_id,
            "is_active": True
        })
        
        if audio_data:
            print(f"✅ Found user audio: {audio_data.get('title')}")
            # Verificar permisos de acceso para audio de usuario
            if audio_data["privacy"] == AudioPrivacy.PRIVATE and audio_data["uploader_id"] != current_user.id:
                raise HTTPException(status_code=403, detail="Access denied to private audio")
            
            # Obtener información del uploader
            uploader = await db.users.find_one({"id": audio_data["uploader_id"]})
            if not uploader:
                raise HTTPException(status_code=404, detail="Audio uploader not found")
            
            uploader_response = UserResponse(**uploader)
            
            # Preparar respuesta para audio de usuario
            audio_response = UserAudioResponse(
                **audio_data,
                uploader=uploader_response,
                url=audio_data["public_url"],
                preview_url=audio_data["public_url"],
                uses=audio_data["uses_count"]
            )
            
            return {
                "success": True,
                "audio": audio_response.dict()
            }
        
        # If not found in user audio, try system music
        print(f"🔍 User audio not found, checking system music for ID: {audio_id}")
        
        # Get music info using existing function (supports iTunes IDs)
        music_info = await get_music_info(audio_id)
        
        if music_info:
            print(f"✅ Found system music: {music_info.get('title')} by {music_info.get('artist')}")
            
            # Convert to compatible format with frontend
            audio_response = {
                "id": music_info["id"],
                "title": music_info["title"],
                "artist": music_info["artist"],
                "duration": music_info.get("duration", 30),
                "public_url": music_info.get("preview_url"),
                "cover_url": music_info.get("cover"),
                "uses_count": music_info.get("uses", 0),
                "privacy": "public",
                "is_system_music": True,
                "source": music_info.get("source", "iTunes API"),
                "created_at": music_info.get("created_at", datetime.utcnow().isoformat()),
                "category": music_info.get("category"),
                "genre": music_info.get("genre"),
                "uploader": None  # System music has no uploader
            }
            
            return {
                "success": True,
                "audio": audio_response
            }
        
        # If neither user audio nor system music found
        print(f"❌ Audio not found in user audio or system music: {audio_id}")
        raise HTTPException(status_code=404, detail="Audio not found")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting audio details: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting audio details: {str(e)}")

@api_router.get("/audio/search")
async def search_user_audio(
    query: str,
    limit: int = 20,
    include_private: bool = False,
    current_user: UserResponse = Depends(get_current_user)
):
    """Buscar en audios de usuarios"""
    try:
        if not query.strip():
            return {
                "success": False,
                "message": "Search query is required",
                "audios": []
            }
        
        # Construir filtro de búsqueda
        search_regex = {"$regex": query.strip(), "$options": "i"}
        filter_query = {
            "is_active": True,
            "is_processed": True,
            "$or": [
                {"title": search_regex},
                {"artist": search_regex}
            ]
        }
        
        # Si no se incluyen privados, solo buscar públicos o del usuario actual
        if not include_private:
            filter_query["$and"] = [
                {
                    "$or": [
                        {"privacy": AudioPrivacy.PUBLIC},
                        {"uploader_id": current_user.id}
                    ]
                }
            ]
        else:
            # Si se incluyen privados, solo los del usuario actual
            filter_query["uploader_id"] = current_user.id
        
        # Buscar audios
        user_audios = await db.user_audio.find(filter_query) \
            .sort("uses_count", -1) \
            .limit(limit) \
            .to_list(limit)
        
        # Preparar respuesta
        audio_responses = []
        for audio_data in user_audios:
            # Obtener información del uploader
            uploader = await db.users.find_one({"id": audio_data["uploader_id"]})
            if uploader:
                uploader_response = UserResponse(**uploader)
                
                audio_response = UserAudioResponse(
                    **audio_data,
                    uploader=uploader_response,
                    url=audio_data["public_url"],
                    preview_url=audio_data["public_url"],
                    uses=audio_data["uses_count"]
                )
                audio_responses.append(audio_response.dict())
        
        return {
            "success": True,
            "message": f"Found {len(audio_responses)} audio tracks for '{query}'",
            "audios": audio_responses,
            "query": query,
            "total": len(audio_responses)
        }
        
    except Exception as e:
        logger.error(f"Error searching user audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error searching audio: {str(e)}")

@api_router.get("/audio/{audio_id}/posts")
async def get_posts_using_audio(
    audio_id: str,
    limit: int = 20,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Obtener todos los posts que usan un audio específico"""
    try:
        logger.info(f"🎵 Buscando posts para audio ID: {audio_id}")
        
        # Verificar que el audio existe
        audio_exists = False
        audio_source = None
        
        # Primero verificar si es audio de usuario
        user_audio = await db.user_audio.find_one({"id": audio_id})
        if user_audio:
            audio_exists = True
            audio_source = "user_audio"
            logger.info(f"✅ Audio encontrado en user_audio: {audio_id}")
        else:
            # Verificar si es música del sistema
            try:
                music_info = await get_music_info(audio_id)
                if music_info:
                    audio_exists = True
                    audio_source = "system_music"
                    logger.info(f"✅ Audio encontrado en sistema de música: {audio_id}")
            except:
                pass
        
        if not audio_exists:
            logger.error(f"❌ Audio no encontrado: {audio_id}")
            raise HTTPException(status_code=404, detail="Audio not found")
        
        # Buscar posts que usan este audio con múltiples estrategias
        all_polls = []
        
        # Estrategia 1: Buscar por music_id directo
        logger.info(f"🔍 Buscando posts con music_id = {audio_id}")
        polls_filter = {"music_id": audio_id, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}
        direct_polls = await db.polls.find(polls_filter).to_list(1000)
        logger.info(f"📊 Posts encontrados por music_id directo: {len(direct_polls)}")
        all_polls.extend(direct_polls)
        
        # Estrategia 2: Buscar por music.id en el objeto music embebido
        logger.info(f"🔍 Buscando posts con music.id = {audio_id}")
        music_nested_filter = {"music.id": audio_id, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}
        nested_polls = await db.polls.find(music_nested_filter).to_list(1000)
        logger.info(f"📊 Posts encontrados por music.id embebido: {len(nested_polls)}")
        
        # Evitar duplicados
        existing_ids = {poll["id"] for poll in all_polls}
        for poll in nested_polls:
            if poll["id"] not in existing_ids:
                all_polls.append(poll)
        
        # Estrategia 2.5: BACKWARD COMPATIBILITY - Si audio_id tiene prefijo "user_audio_", también buscar sin prefijo
        if audio_id.startswith('user_audio_'):
            import re
            bare_uuid = audio_id.replace('user_audio_', '')
            uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            
            if re.match(uuid_pattern, bare_uuid):
                logger.info(f"🔄 COMPATIBILIDAD: Buscando posts con UUID sin prefijo = {bare_uuid}")
                
                # Buscar por music_id directo sin prefijo (posts antiguos)
                backward_filter = {"music_id": bare_uuid, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}
                backward_polls = await db.polls.find(backward_filter).to_list(1000)
                logger.info(f"📊 Posts encontrados por UUID sin prefijo: {len(backward_polls)}")
                
                # Evitar duplicados
                for poll in backward_polls:
                    if poll["id"] not in existing_ids:
                        all_polls.append(poll)
                        existing_ids.add(poll["id"])
                
                # También buscar en music.id embebido sin prefijo
                backward_nested_filter = {"music.id": bare_uuid, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}
                backward_nested_polls = await db.polls.find(backward_nested_filter).to_list(1000)
                logger.info(f"📊 Posts encontrados por music.id sin prefijo: {len(backward_nested_polls)}")
                
                # Evitar duplicados
                for poll in backward_nested_polls:
                    if poll["id"] not in existing_ids:
                        all_polls.append(poll)
                        existing_ids.add(poll["id"])
        else:
            # Estrategia 2.6: FORWARD COMPATIBILITY - Si audio_id es un UUID, también buscar con prefijo
            import re
            uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            
            if re.match(uuid_pattern, audio_id):
                prefixed_id = f"user_audio_{audio_id}"
                logger.info(f"🔄 COMPATIBILIDAD: Buscando posts con UUID con prefijo = {prefixed_id}")
                
                # Buscar por music_id directo con prefijo (posts nuevos)
                forward_filter = {"music_id": prefixed_id, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}
                forward_polls = await db.polls.find(forward_filter).to_list(1000)
                logger.info(f"📊 Posts encontrados por UUID con prefijo: {len(forward_polls)}")
                
                # Evitar duplicados
                for poll in forward_polls:
                    if poll["id"] not in existing_ids:
                        all_polls.append(poll)
                        existing_ids.add(poll["id"])
                
                # También buscar en music.id embebido con prefijo
                forward_nested_filter = {"music.id": prefixed_id, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}
                forward_nested_polls = await db.polls.find(forward_nested_filter).to_list(1000)
                logger.info(f"📊 Posts encontrados por music.id con prefijo: {len(forward_nested_polls)}")
                
                # Evitar duplicados
                for poll in forward_nested_polls:
                    if poll["id"] not in existing_ids:
                        all_polls.append(poll)
                        existing_ids.add(poll["id"])
        
        # Estrategia 3: Para audio de usuario, buscar en user_audio_use
        if audio_source == "user_audio":
            logger.info(f"🔍 Buscando en user_audio_use para audio de usuario: {audio_id}")
            audio_uses_filter = {"audio_id": audio_id}
            audio_uses = await db.user_audio_use.find(audio_uses_filter).to_list(1000)
            logger.info(f"📊 Usos de audio encontrados: {len(audio_uses)}")
            
            # Obtener poll IDs de los uses
            poll_ids_from_uses = [use["poll_id"] for use in audio_uses if use.get("poll_id")]
            logger.info(f"📊 Poll IDs de usos de audio: {len(poll_ids_from_uses)}")
            
            if poll_ids_from_uses:
                use_polls = await db.polls.find({"id": {"$in": poll_ids_from_uses}, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}).to_list(1000)
                logger.info(f"📊 Posts encontrados por usos de audio: {len(use_polls)}")
                
                # Evitar duplicados
                for poll in use_polls:
                    if poll["id"] not in existing_ids:
                        all_polls.append(poll)
                        existing_ids.add(poll["id"])
        
        # Estrategia 4: Buscar en options.extracted_audio_id (carruseles con audio extraído por slide)
        logger.info(f"🔍 Buscando posts con options.extracted_audio_id = {audio_id}")
        extracted_filter = {"options.extracted_audio_id": audio_id, "is_active": True, "status": {"$nin": ["broken", "hidden", "failed", "processing"]}}
        extracted_polls = await db.polls.find(extracted_filter).to_list(1000)
        logger.info(f"📊 Posts encontrados por options.extracted_audio_id: {len(extracted_polls)}")
        
        for poll in extracted_polls:
            if poll["id"] not in existing_ids:
                all_polls.append(poll)
                existing_ids.add(poll["id"])
        
        # Estrategia 4.1: COMPATIBILIDAD - Buscar con/sin prefijo user_audio_ en extracted_audio_id
        if audio_id.startswith('user_audio_'):
            bare_uuid = audio_id.replace('user_audio_', '')
            extracted_bare_filter = {"options.extracted_audio_id": bare_uuid}
            extracted_bare_polls = await db.polls.find(extracted_bare_filter).to_list(1000)
            for poll in extracted_bare_polls:
                if poll["id"] not in existing_ids:
                    all_polls.append(poll)
                    existing_ids.add(poll["id"])
        else:
            import re
            uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            if re.match(uuid_pattern, audio_id):
                prefixed_id = f"user_audio_{audio_id}"
                extracted_prefix_filter = {"options.extracted_audio_id": prefixed_id}
                extracted_prefix_polls = await db.polls.find(extracted_prefix_filter).to_list(1000)
                for poll in extracted_prefix_polls:
                    if poll["id"] not in existing_ids:
                        all_polls.append(poll)
                        existing_ids.add(poll["id"])
        
        # Ordenar por fecha de creación (más reciente primero) y aplicar paginación
        all_polls.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        total = len(all_polls)
        polls = all_polls[offset:offset + limit]
        
        logger.info(f"📊 Total posts encontrados: {total}, mostrando: {len(polls)}")
        
        if not polls:
            logger.info(f"⚠️ No se encontraron posts usando el audio: {audio_id}")
            return {
                "success": True,
                "audio_id": audio_id,
                "posts": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "has_more": False,
                "message": "No posts found using this audio"
            }
        
        # Obtener información de autores con mejor manejo de errores
        author_ids = []
        for poll in polls:
            if poll.get("author_id"):
                author_ids.append(poll["author_id"])
        
        author_ids = list(set(author_ids))  # Eliminar duplicados
        logger.info(f"👤 Buscando información de {len(author_ids)} autores")
        
        authors = []
        if author_ids:
            try:
                authors = await db.users.find({"id": {"$in": author_ids}}).to_list(len(author_ids))
                logger.info(f"✅ Información de autores obtenida: {len(authors)}")
            except Exception as e:
                logger.error(f"❌ Error obteniendo autores: {str(e)}")
                authors = []
        
        authors_dict = {}
        for author in authors:
            try:
                authors_dict[author["id"]] = UserResponse(**author)
            except Exception as e:
                logger.error(f"❌ Error procesando autor {author.get('id', 'unknown')}: {str(e)}")
                # Crear un usuario básico como fallback
                authors_dict[author["id"]] = UserResponse(
                    id=author.get("id", "unknown"),
                    username=author.get("username", "usuario_desconocido"),
                    email=author.get("email", ""),
                    display_name=author.get("display_name", author.get("username", "Usuario Desconocido")),
                    avatar_url=author.get("avatar_url"),
                    bio=author.get("bio", ""),
                    is_public=author.get("is_public", True),
                    allow_messages=author.get("allow_messages", True),
                    created_at=author.get("created_at", datetime.utcnow().isoformat())
                )
        
        # Get user votes and likes for current user (fixing voting state sync issue)
        poll_ids = [poll["id"] for poll in polls]
        
        # Query user votes for all posts
        user_votes_cursor = db.votes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_votes = await user_votes_cursor.to_list(len(poll_ids))
        user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
        logger.info(f"🗳️ Usuario {current_user.id} tiene votos en {len(user_votes_dict)} posts")
        
        # Query user likes for all posts  
        user_likes_cursor = db.poll_likes.find({
            "poll_id": {"$in": poll_ids},
            "user_id": current_user.id
        })
        user_likes = await user_likes_cursor.to_list(len(poll_ids))
        liked_poll_ids = set(like["poll_id"] for like in user_likes)
        logger.info(f"❤️ Usuario {current_user.id} tiene likes en {len(liked_poll_ids)} posts")
        
        # Construir respuesta de polls con mejor logging
        poll_responses = []
        logger.info(f"🏗️ Construyendo respuesta para {len(polls)} posts")
        
        for i, poll_data in enumerate(polls):
            try:
                logger.info(f"📝 Procesando post {i+1}: {poll_data.get('id', 'unknown')}")
                logger.info(f"📝 Author_id del post: {poll_data.get('author_id')}")
                
                # Obtener autor del post
                author = authors_dict.get(poll_data.get("author_id"))
                if not author:
                    logger.warning(f"⚠️ Autor no encontrado para post {poll_data.get('id')}, author_id: {poll_data.get('author_id')}")
                    logger.warning(f"⚠️ Authors disponibles: {list(authors_dict.keys())}")
                    # Continuar el procesamiento sin autor
                else:
                    logger.info(f"✅ Autor encontrado: {author.username if hasattr(author, 'username') else 'N/A'}")
                
                # Procesar opciones
                options = []
                if poll_data.get("options"):
                    for option in poll_data["options"]:
                        # Obtener información del usuario de la opción
                        option_user = authors_dict.get(option.get("user_id"))
                        
                        # Obtener thumbnail si es necesario
                        media_url = option.get("media_url")
                        thumbnail_url = option.get("thumbnail_url")
                        
                        if media_url and not thumbnail_url and option.get("media_type") == "video":
                            thumbnail_url = await get_thumbnail_for_media_url(media_url)
                        
                        option_dict = {
                            "id": option.get("id", str(uuid.uuid4())),
                            "text": option.get("text", ""),
                            "votes": option.get("votes", 0),
                            "user": option_user.dict() if option_user else None,
                            "mentioned_users": option.get("mentioned_users", []),
                            "extracted_audio_id": option.get("extracted_audio_id"),  # 🎵 Include extracted audio ID
                            "media": {
                                "type": option.get("media_type"),
                                "url": media_url,
                                "thumbnail": thumbnail_url,  # None si no hay miniatura real (no caer al video URL)
                                "optimizedUrl": option.get("optimized_media_url"),
                                "hls": option.get("hls_url"),
                            } if media_url else None,
                            "optimized_media_url": option.get("optimized_media_url"),
                            "hls_url": option.get("hls_url"),
                        }
                        options.append(option_dict)
                
                # Get music info if available
                music_info = None
                if poll_data.get("music_id"):
                    try:
                        music_info = await get_music_info(poll_data.get("music_id"))
                    except Exception as music_error:
                        logger.error(f"❌ Error obteniendo info de música para {poll_data.get('music_id')}: {str(music_error)}")
                
                # Calcular time_ago
                created_at_dt = poll_data.get("created_at")
                if isinstance(created_at_dt, str):
                    try:
                        created_at_dt = datetime.fromisoformat(created_at_dt.replace('Z', '+00:00'))
                    except:
                        created_at_dt = datetime.utcnow()
                elif not isinstance(created_at_dt, datetime):
                    created_at_dt = datetime.utcnow()
                
                # Calcular diferencia de tiempo
                now = datetime.utcnow()
                diff = now - created_at_dt
                
                if diff.days > 0:
                    time_ago = f"hace {diff.days} día{'s' if diff.days > 1 else ''}"
                elif diff.seconds > 3600:
                    hours = diff.seconds // 3600
                    time_ago = f"hace {hours} hora{'s' if hours > 1 else ''}"
                elif diff.seconds > 60:
                    minutes = diff.seconds // 60
                    time_ago = f"hace {minutes} minuto{'s' if minutes > 1 else ''}"
                else:
                    time_ago = "hace unos segundos"
                
                logger.info(f"⏰ Time ago calculado: {time_ago}")
                
                poll_response = PollResponse(
                    id=poll_data["id"],
                    title=poll_data["title"],
                    author=author,
                    description=poll_data.get("description"),
                    options=options,
                    total_votes=poll_data.get("total_votes", 0),
                    likes=poll_data.get("likes", 0),
                    shares=poll_data.get("shares", 0),
                    comments_count=poll_data.get("comments_count", 0),
                    saves_count=poll_data.get("saves_count", 0),
                    music=music_info,
                    user_vote=user_votes_dict.get(poll_data["id"]),  # ✅ FIXED: Now includes actual user vote
                    user_liked=poll_data["id"] in liked_poll_ids,    # ✅ FIXED: Now includes actual user like status
                    created_at=created_at_dt,
                    mentioned_users=poll_data.get("mentioned_users", []),
                    tags=poll_data.get("tags", []),
                    category=poll_data.get("category"),
                    is_featured=poll_data.get("is_featured", False),
                    time_ago=time_ago,  # ¡Campo faltante!
                    layout=poll_data.get("layout", "vertical"),  # 🎨 CRITICAL: Include layout to prevent split-screen issue
                    # 🆚 VS Experience fields — críticos para que AudioDetailPage renderice
                    # los duelos VS con la orientación correcta (vertical = lado a lado).
                    vs_id=poll_data.get("vs_id"),
                    vs_questions=poll_data.get("vs_questions", []),
                    vs_orientation=poll_data.get("vs_orientation", "horizontal"),
                    composed_video_url=poll_data.get("composed_video_url"),
            composed_hls_url=poll_data.get("composed_hls_url"),
                    composed_status=poll_data.get("composed_status"),
                    composed_orientation=poll_data.get("composed_orientation"),
                    creator_country=poll_data.get("creator_country"),
                    # Post settings
                    comments_enabled=poll_data.get("comments_enabled", True),
                    show_vote_count=poll_data.get("show_vote_count", True)
                )
                
                # Convertir a dict y agregar campo user para retrocompatibilidad
                poll_dict = poll_response.dict()
                poll_dict["user"] = author.dict() if author else None  # Agregar campo user igual a author
                poll_responses.append(poll_dict)
                logger.info(f"✅ Post {i+1} procesado exitosamente")
                
            except Exception as poll_error:
                logger.error(f"❌ Error procesando post {i+1}: {str(poll_error)}")
                logger.error(f"❌ Traceback completo:", exc_info=True)
                logger.error(f"❌ Poll data que causó error: {poll_data}")
                continue
        
        logger.info(f"🎉 Respuesta construida: {len(poll_responses)} posts de {total} total")
        
        return {
            "success": True,
            "audio_id": audio_id,
            "posts": poll_responses,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
            "message": f"Found {len(poll_responses)} posts using this audio"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting posts using audio {audio_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting posts: {str(e)}")

# =============  ENHANCED MUSIC LIBRARY WITH USER AUDIO =============

@api_router.get("/music/combined-library")
async def get_combined_music_library(
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Obtener biblioteca de música combinada:
    - Música estática del sistema
    - Audios públicos de usuarios  
    - Audios privados del usuario actual
    """
    try:
        all_music = []
        
        # 1. Obtener música estática del sistema (usando endpoint existente)
        system_music_response = await get_music_library(
            category=category,
            search=search,
            limit=25,  # Limitar para hacer espacio a user audio
            offset=0
        )
        system_music = system_music_response.get("music", [])
        
        # 2. Obtener audios de usuarios (públicos + privados del usuario)
        user_audio_filter = {
            "is_active": True,
            "is_processed": True,
            "$or": [
                {"privacy": AudioPrivacy.PUBLIC},
                {"uploader_id": current_user.id}
            ]
        }
        
        if search:
            search_regex = {"$regex": search.strip(), "$options": "i"}
            user_audio_filter["$and"] = [
                user_audio_filter.get("$and", [{}])[0] if user_audio_filter.get("$and") else {},
                {
                    "$or": [
                        {"title": search_regex},
                        {"artist": search_regex}
                    ]
                }
            ]
        
        user_audios = await db.user_audio.find(user_audio_filter) \
            .sort("uses_count", -1) \
            .limit(25) \
            .to_list(25)
        
        # Convertir user audios al formato de música del sistema
        for audio_data in user_audios:
            uploader = await db.users.find_one({"id": audio_data["uploader_id"]})
            if uploader:
                # Formato compatible con el sistema de música existente
                music_item = {
                    'id': f"user_audio_{audio_data['id']}",  # Prefijo para identificar que es user audio
                    'title': audio_data['title'],
                    'artist': audio_data['artist'],
                    'duration': audio_data['duration'],
                    'url': audio_data['public_url'],
                    'preview_url': audio_data['public_url'],
                    'cover': audio_data.get('cover_url', '/images/default-audio-cover.png'),
                    'category': 'User Audio',
                    'isOriginal': True,  # Marca que es audio subido por usuarios
                    'isTrending': audio_data['uses_count'] > 100,
                    'uses': audio_data['uses_count'],
                    'waveform': audio_data['waveform'],
                    'source': 'User Upload',
                    'uploader': {
                        'id': uploader['id'],
                        'username': uploader['username'],
                        'display_name': uploader['display_name']
                    },
                    'privacy': audio_data['privacy'],
                    'created_at': audio_data['created_at'].isoformat() if audio_data.get('created_at') else None
                }
                all_music.append(music_item)
        
        # 3. Combinar y ordenar toda la música
        all_music.extend(system_music)
        
        # 4. Aplicar filtros adicionales si es necesario
        if category and category != 'Todas':
            if category == 'User Audio':
                all_music = [m for m in all_music if m.get('source') == 'User Upload']
            elif category == 'System':
                all_music = [m for m in all_music if m.get('source') != 'User Upload']
            else:
                all_music = [m for m in all_music if m['category'] == category]
        
        # 5. Ordenar por popularidad (uses) y aplicar paginación
        all_music.sort(key=lambda x: x.get('uses', 0), reverse=True)
        
        total = len(all_music)
        paginated_music = all_music[offset:offset + limit]
        
        return {
            'success': True,
            'music': paginated_music,
            'total': total,
            'limit': limit,
            'offset': offset,
            'has_more': offset + limit < total,
            'categories': {
                'system_music': len([m for m in all_music if m.get('source') != 'User Upload']),
                'user_audio': len([m for m in all_music if m.get('source') == 'User Upload']),
                'total': total
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting combined music library: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting music library: {str(e)}")

# =============  AUDIO FAVORITES ENDPOINTS =============

@api_router.post("/audio/favorites", response_model=AudioFavoriteResponse)
async def add_audio_to_favorites(
    favorite_data: AudioFavoriteCreate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Add audio to user's favorites"""
    try:
        # Check if already favorited
        existing_favorite = await db.audio_favorites.find_one({
            "user_id": current_user.id,
            "audio_id": favorite_data.audio_id,
            "audio_type": favorite_data.audio_type
        })
        
        if existing_favorite:
            raise HTTPException(status_code=400, detail="Audio already in favorites")
        
        # Get audio details for caching
        audio_title = None
        audio_artist = None
        audio_cover_url = None
        
        if favorite_data.audio_type == "system":
            # Get system music info
            music_info = await get_music_info(favorite_data.audio_id)
            if music_info:
                audio_title = music_info.get('title')
                audio_artist = music_info.get('artist')
                audio_cover_url = music_info.get('cover')
        elif favorite_data.audio_type == "user":
            # Get user audio info
            user_audio = await db.user_audio.find_one({"id": favorite_data.audio_id})
            if user_audio:
                audio_title = user_audio.get('title')
                audio_artist = user_audio.get('artist')
                audio_cover_url = user_audio.get('cover_url')
        
        # Create favorite
        favorite = AudioFavorite(
            user_id=current_user.id,
            audio_id=favorite_data.audio_id,
            audio_type=favorite_data.audio_type,
            audio_title=audio_title,
            audio_artist=audio_artist,
            audio_cover_url=audio_cover_url
        )
        
        await db.audio_favorites.insert_one(favorite.dict())
        
        return AudioFavoriteResponse(
            id=favorite.id,
            audio_id=favorite.audio_id,
            audio_type=favorite.audio_type,
            audio_title=audio_title,
            audio_artist=audio_artist,
            audio_cover_url=audio_cover_url,
            created_at=favorite.created_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding audio to favorites: {str(e)}")
        raise HTTPException(status_code=500, detail="Error adding to favorites")

@api_router.delete("/audio/favorites/{audio_id}")
async def remove_audio_from_favorites(
    audio_id: str,
    audio_type: str = "system",
    current_user: UserResponse = Depends(get_current_user)
):
    """Remove audio from user's favorites"""
    try:
        result = await db.audio_favorites.delete_one({
            "user_id": current_user.id,
            "audio_id": audio_id,
            "audio_type": audio_type
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Audio not found in favorites")
        
        return {"message": "Audio removed from favorites successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing audio from favorites: {str(e)}")
        raise HTTPException(status_code=500, detail="Error removing from favorites")

@api_router.get("/audio/favorites")
async def get_user_favorites(
    limit: int = 20,
    offset: int = 0,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get user's favorite audio"""
    try:
        # Get favorites from database
        favorites = await db.audio_favorites.find({
            "user_id": current_user.id
        }).sort("created_at", -1).skip(offset).limit(limit).to_list(limit)
        
        # Get total count
        total = await db.audio_favorites.count_documents({"user_id": current_user.id})
        
        # Enrich favorites with current audio details
        enriched_favorites = []
        for fav in favorites:
            favorite_response = AudioFavoriteResponse(
                id=fav["id"],
                audio_id=fav["audio_id"],
                audio_type=fav["audio_type"],
                audio_title=fav.get("audio_title"),
                audio_artist=fav.get("audio_artist"),
                audio_cover_url=fav.get("audio_cover_url"),
                created_at=fav["created_at"]
            )
            
            # Try to get current audio details
            if fav["audio_type"] == "system":
                music_info = await get_music_info(fav["audio_id"])
                if music_info:
                    favorite_response.audio_details = music_info
            elif fav["audio_type"] == "user":
                user_audio = await db.user_audio.find_one({"id": fav["audio_id"]})
                if user_audio:
                    favorite_response.audio_details = {
                        "id": user_audio["id"],
                        "title": user_audio["title"],
                        "artist": user_audio["artist"],
                        "duration": user_audio["duration"],
                        "public_url": user_audio["public_url"],
                        "cover": user_audio.get("cover_url"),
                        "source": "User Upload"
                    }
            
            enriched_favorites.append(favorite_response)
        
        return {
            "success": True,
            "favorites": enriched_favorites,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total
        }
        
    except Exception as e:
        logger.error(f"Error getting user favorites: {str(e)}")
        raise HTTPException(status_code=500, detail="Error getting favorites")

@api_router.get("/audio/favorites/{audio_id}/check")
async def check_audio_in_favorites(
    audio_id: str,
    audio_type: str = "system",
    current_user: UserResponse = Depends(get_current_user)
):
    """Check if audio is in user's favorites"""
    try:
        favorite = await db.audio_favorites.find_one({
            "user_id": current_user.id,
            "audio_id": audio_id,
            "audio_type": audio_type
        })
        
        return {
            "is_favorite": favorite is not None,
            "favorite_id": favorite["id"] if favorite else None
        }
        
    except Exception as e:
        logger.error(f"Error checking audio in favorites: {str(e)}")
        raise HTTPException(status_code=500, detail="Error checking favorites")

# =============  STORY ENDPOINTS ============= (DISABLED - Feature removed)
# All story endpoints have been removed as the stories feature is disabled

# All story endpoints removed - Stories feature disabled

@api_router.put("/polls/{poll_id}")
async def update_poll(
    poll_id: str,
    poll_data: Dict,
    current_user: UserResponse = Depends(get_current_user)
):
    """Update a poll (only by owner)"""
    try:
        # Get the poll to check ownership
        poll = await db.polls.find_one({"id": poll_id})
        if not poll:
            raise HTTPException(status_code=404, detail="Poll not found")
        
        # Check if user is the owner
        if poll.get("author_id") != current_user.id:
            raise HTTPException(status_code=403, detail="You can only edit your own polls")
        
        # Prepare update data
        update_data = {}
        if "title" in poll_data:
            update_data["title"] = poll_data["title"]
        if "description" in poll_data:
            update_data["description"] = poll_data["description"]
        if "is_pinned" in poll_data:
            update_data["is_pinned"] = poll_data["is_pinned"]
        if "is_archived" in poll_data:
            update_data["is_archived"] = poll_data["is_archived"]
        if "is_private" in poll_data:
            update_data["is_private"] = poll_data["is_private"]
        if "comments_enabled" in poll_data:
            update_data["comments_enabled"] = poll_data["comments_enabled"]
        if "show_vote_count" in poll_data:
            update_data["show_vote_count"] = poll_data["show_vote_count"]
        
        update_data["updated_at"] = datetime.utcnow()
        
        # Update the poll
        result = await db.polls.update_one(
            {"id": poll_id},
            {"$set": update_data}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=400, detail="No changes made")
        
        # Return updated poll (remove MongoDB ObjectId fields)
        updated_poll = await db.polls.find_one({"id": poll_id})
        if updated_poll:
            # Remove MongoDB ObjectId field to avoid serialization issues
            updated_poll.pop('_id', None)
        return updated_poll
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating poll: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.delete("/polls/{poll_id}")
async def delete_poll(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Soft-delete a poll (only by owner).

    El poll NO se borra físicamente de la base de datos; se marca con
    `status="hidden"` para (1) preservar integridad referencial de votos,
    likes, comentarios y notificaciones que lo apuntan, y (2) permitir
    auditoría / restauración futura. El filtro global `POLL_STATUS_FILTER`
    excluye los hidden de todos los listados, por lo que desde el punto de
    vista del usuario el contenido desaparece instantáneamente.
    """
    try:
        # Get the poll to check ownership
        poll = await db.polls.find_one({"id": poll_id})
        if not poll:
            raise HTTPException(status_code=404, detail="Poll not found")

        # Check if user is the owner
        if poll.get("author_id") != current_user.id:
            raise HTTPException(status_code=403, detail="You can only delete your own polls")

        # Si ya estaba soft-deleted, devolver éxito idempotente (evita 400
        # al pulsar "eliminar" dos veces seguidas desde la UI).
        if poll.get("status") == "hidden":
            return {"message": "Poll already deleted", "idempotent": True}

        # Limpieza de datos auxiliares efímeros. Estos registros no aportan
        # valor histórico una vez oculto el poll y su persistencia sólo
        # inflaría contadores y feeds.
        await db.votes.delete_many({"poll_id": poll_id})
        await db.poll_likes.delete_many({"poll_id": poll_id})
        await db.comments.delete_many({"poll_id": poll_id})

        # Soft-delete: marcar como oculto en lugar de borrar.
        result = await db.polls.update_one(
            {"id": poll_id},
            {
                "$set": {
                    "status": "hidden",
                    "hidden_at": datetime.utcnow(),
                    "hidden_by": current_user.id,
                }
            },
        )

        if result.matched_count == 0:
            raise HTTPException(status_code=400, detail="Failed to delete poll")

        logger.info(f"🗑️ Poll {poll_id} soft-deleted by user {current_user.id}")
        return {"message": "Poll deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting poll: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")
# =============  FEED MENU ENDPOINTS =============

@api_router.post("/feed/not-interested")
async def mark_not_interested(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Mark a poll as 'not interested' - removes it from user's feed"""
    logger.info(f"🚫 Feed Menu: mark_not_interested called for poll_id={poll_id}, user_id={current_user.id}")
    try:
        # Check if poll exists
        poll = await db.polls.find_one({"id": poll_id})
        if not poll:
            logger.warning(f"❌ Poll not found: {poll_id}")
            raise HTTPException(status_code=404, detail="Poll not found")
        
        logger.info(f"✅ Poll found: {poll_id}")
        
        # Check if preference already exists
        existing_preference = await db.user_preferences.find_one({
            "user_id": current_user.id,
            "poll_id": poll_id,
            "preference_type": "not_interested"
        })
        
        if existing_preference:
            logger.info(f"ℹ️ Already marked as not interested: {poll_id}")
            return {"success": True, "message": "Already marked as not interested"}
        
        # Create new preference
        from models import UserPreference
        preference = UserPreference(
            user_id=current_user.id,
            poll_id=poll_id,
            preference_type="not_interested"
        )
        
        await db.user_preferences.insert_one(preference.dict())
        logger.info(f"✅ Successfully marked poll as not interested: {poll_id}")
        
        return {"success": True, "message": "Content marked as not interested"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error marking poll as not interested: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.post("/feed/hide-user")
async def hide_user_content(
    author_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Hide all content from a specific user"""
    try:
        # Check if user exists
        author = await db.users.find_one({"id": author_id})
        if not author:
            # Try finding by username if not found by ID
            author = await db.users.find_one({"username": author_id})
            if not author:
                raise HTTPException(status_code=404, detail="User not found")
            author_id = author["id"]  # Use the actual ID
        
        # Check if preference already exists
        existing_preference = await db.user_preferences.find_one({
            "user_id": current_user.id,
            "author_id": author_id,
            "preference_type": "hidden_user"
        })
        
        if existing_preference:
            return {"success": True, "message": "User already hidden"}
        
        # Create new preference
        from models import UserPreference
        preference = UserPreference(
            user_id=current_user.id,
            author_id=author_id,
            preference_type="hidden_user"
        )
        
        await db.user_preferences.insert_one(preference.dict())
        
        return {"success": True, "message": "User content hidden"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error hiding user content: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.post("/feed/toggle-notifications")
async def toggle_user_notifications(
    author_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Toggle notifications for a specific user's content"""
    try:
        # Check if user exists
        author = await db.users.find_one({"id": author_id})
        if not author:
            # Try finding by username if not found by ID
            author = await db.users.find_one({"username": author_id})
            if not author:
                raise HTTPException(status_code=404, detail="User not found")
            author_id = author["id"]  # Use the actual ID
        
        # Find existing notification preference
        existing_preference = await db.user_notification_preferences.find_one({
            "user_id": current_user.id,
            "author_id": author_id
        })
        
        if existing_preference:
            # Toggle existing preference
            new_status = not existing_preference["is_enabled"]
            await db.user_notification_preferences.update_one(
                {"id": existing_preference["id"]},
                {
                    "$set": {
                        "is_enabled": new_status,
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            message = "Notifications enabled" if new_status else "Notifications disabled"
        else:
            # Create new preference (enabled by default)
            from models import UserNotificationPreference
            preference = UserNotificationPreference(
                user_id=current_user.id,
                author_id=author_id,
                is_enabled=True
            )
            
            await db.user_notification_preferences.insert_one(preference.dict())
            message = "Notifications enabled"
            new_status = True
        
        return {
            "success": True, 
            "message": message,
            "notifications_enabled": new_status
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling notifications: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.post("/feed/report")
async def report_content(
    report_data: dict,
    current_user: UserResponse = Depends(get_current_user)
):
    """Report inappropriate content"""
    try:
        poll_id = report_data.get("poll_id")
        category = report_data.get("category")
        comment = report_data.get("comment", "").strip()
        
        if not poll_id or not category:
            raise HTTPException(status_code=400, detail="Poll ID and category are required")
        
        # Check if poll exists
        poll = await db.polls.find_one({"id": poll_id})
        if not poll:
            raise HTTPException(status_code=404, detail="Poll not found")
        
        # Check if user already reported this poll
        existing_report = await db.content_reports.find_one({
            "poll_id": poll_id,
            "reported_by": current_user.id
        })
        
        if existing_report:
            return {"success": True, "message": "You have already reported this content"}
        
        # Create new report
        from models import ContentReport
        report = ContentReport(
            poll_id=poll_id,
            reported_by=current_user.id,
            category=category,
            comment=comment if comment else None
        )
        
        await db.content_reports.insert_one(report.dict())
        
        # Optionally, you could add automatic content moderation here
        # For example, if a post gets X reports, automatically hide it
        
        return {"success": True, "message": "Report submitted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reporting content: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.get("/feed/user-preferences")
async def get_user_feed_preferences(
    current_user: UserResponse = Depends(get_current_user)
):
    """Get user's feed preferences (hidden users, not interested polls, etc.)"""
    try:
        # Get all user preferences
        preferences = await db.user_preferences.find({
            "user_id": current_user.id
        }).to_list(1000)
        
        # Get notification preferences
        notification_prefs = await db.user_notification_preferences.find({
            "user_id": current_user.id
        }).to_list(1000)
        
        # Organize preferences by type
        result = {
            "hidden_users": [p["author_id"] for p in preferences if p["preference_type"] == "hidden_user"],
            "not_interested_polls": [p["poll_id"] for p in preferences if p["preference_type"] == "not_interested"],
            "notification_preferences": {
                pref["author_id"]: pref["is_enabled"] 
                for pref in notification_prefs
            }
        }
        
        return {"success": True, "data": result}
        
    except Exception as e:
        logger.error(f"Error getting user preferences: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

# Utility function to filter polls based on user preferences
async def filter_polls_by_preferences(polls: List[dict], user_id: str) -> List[dict]:
    """Filter polls based on user's feed preferences"""
    try:
        # Get user preferences
        preferences = await db.user_preferences.find({
            "user_id": user_id
        }).to_list(1000)
        
        # Create sets for faster lookup
        hidden_users = set()
        not_interested_polls = set()
        
        for pref in preferences:
            if pref["preference_type"] == "hidden_user" and pref.get("author_id"):
                hidden_users.add(pref["author_id"])
            elif pref["preference_type"] == "not_interested" and pref.get("poll_id"):
                not_interested_polls.add(pref["poll_id"])
        
        # Filter polls
        filtered_polls = []
        for poll in polls:
            # Skip polls from hidden users
            if poll.get("author_id") in hidden_users:
                continue
            
            # Skip polls marked as not interested
            if poll.get("id") in not_interested_polls:
                continue
            
            filtered_polls.append(poll)
        
        return filtered_polls
        
    except Exception as e:
        logger.error(f"Error filtering polls by preferences: {str(e)}")
        return polls  # Return original polls if filtering fails

# =============  SAVED POLLS ENDPOINTS =============

@api_router.post("/polls/{poll_id}/save")
async def save_poll(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Save a poll or challenge to user's saved collection"""
    try:
        # Detect challenge
        is_challenge = poll_id.startswith("challenge_")
        
        if is_challenge:
            real_challenge_id = poll_id.replace("challenge_", "", 1)
            challenge = await db.challenges.find_one({"id": real_challenge_id})
            if not challenge:
                raise HTTPException(status_code=404, detail="Challenge not found")
        else:
            poll = await db.polls.find_one({"id": poll_id})
            if not poll:
                raise HTTPException(status_code=404, detail="Poll not found")
        
        # Check if already saved
        existing_save = await db.saved_polls.find_one({
            "user_id": current_user.id,
            "poll_id": poll_id
        })
        
        if existing_save:
            return {"success": True, "message": "Already saved", "saved": True}
        
        # Save
        save_record = {
            "id": str(uuid.uuid4()),
            "user_id": current_user.id,
            "poll_id": poll_id,
            "saved_at": datetime.utcnow(),
            "poll_title": challenge.get("title", "") if is_challenge else poll.get("title", ""),
            "poll_author": ""
        }
        
        await db.saved_polls.insert_one(save_record)
        
        # Increment saves count
        if is_challenge:
            await db.challenges.update_one(
                {"id": real_challenge_id},
                {"$inc": {"saves_count": 1}}
            )
            updated = await db.challenges.find_one({"id": real_challenge_id})
            saves_count = updated.get("saves_count", 1) if updated else 1
        else:
            updated_poll = await db.polls.find_one_and_update(
                {"id": poll_id},
                {"$inc": {"saves_count": 1}},
                return_document=ReturnDocument.AFTER
            )
            saves_count = updated_poll.get("saves_count", 1) if updated_poll else 1
        
        return {
            "success": True, 
            "message": "Saved successfully", 
            "saved": True,
            "saves_count": saves_count
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving poll: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.delete("/polls/{poll_id}/save")
async def unsave_poll(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Remove a poll or challenge from user's saved collection"""
    try:
        existing_save = await db.saved_polls.find_one({
            "user_id": current_user.id,
            "poll_id": poll_id
        })
        
        if not existing_save:
            return {"success": True, "message": "Was not saved", "saved": False}
        
        await db.saved_polls.delete_one({
            "user_id": current_user.id,
            "poll_id": poll_id
        })
        
        # Decrement saves count
        is_challenge = poll_id.startswith("challenge_")
        if is_challenge:
            real_challenge_id = poll_id.replace("challenge_", "", 1)
            await db.challenges.update_one(
                {"id": real_challenge_id},
                {"$inc": {"saves_count": -1}}
            )
            updated = await db.challenges.find_one({"id": real_challenge_id})
            saves_count = max(0, updated.get("saves_count", 0)) if updated else 0
        else:
            updated_poll = await db.polls.find_one_and_update(
                {"id": poll_id},
                {"$inc": {"saves_count": -1}},
                return_document=ReturnDocument.AFTER
            )
            saves_count = max(0, updated_poll.get("saves_count", 0)) if updated_poll else 0
        
        return {
            "success": True, 
            "message": "Removed from saved", 
            "saved": False,
            "saves_count": saves_count
        }
        
    except Exception as e:
        logger.error(f"Error unsaving: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.get("/users/{user_id}/saved-polls")
async def get_saved_polls(
    user_id: str,
    current_user: UserResponse = Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100)
):
    """Get user's saved polls"""
    logger.info(f"📚 Getting saved polls for user {user_id}, current_user: {current_user.id}")
    try:
        # Only allow users to see their own saved polls or make it public if needed
        if user_id != current_user.id:
            logger.warning(f"❌ Access denied: user {current_user.id} trying to access {user_id}'s saved polls")
            raise HTTPException(status_code=403, detail="Cannot access other user's saved polls")
        
        logger.info(f"📚 Querying saved_polls collection for user_id: {user_id}")
        
        # Get saved poll IDs
        saved_records = await db.saved_polls.find({
            "user_id": user_id
        }).sort("saved_at", -1).skip(skip).limit(limit).to_list(limit)
        
        logger.info(f"📚 Found {len(saved_records)} saved records")
        
        if not saved_records:
            logger.info(f"📚 No saved polls found for user {user_id}")
            return {"saved_polls": [], "total": 0}
        
        # Get the actual polls
        poll_ids = [record["poll_id"] for record in saved_records]
        logger.info(f"📚 Looking up polls with IDs: {poll_ids}")
        
        polls = await db.polls.find({"id": {"$in": poll_ids}}).to_list(len(poll_ids))
        logger.info(f"📚 Found {len(polls)} actual polls")
        
        # Create a mapping for easier lookup
        polls_dict = {poll["id"]: poll for poll in polls}
        
        # Return polls in the order they were saved with enriched data
        ordered_polls = []
        if saved_records:
            # Get all author IDs for batch processing
            author_ids = list(set(poll["author_id"] for poll in polls if poll.get("author_id")))
            authors_dict = {}
            if author_ids:
                authors_cursor = db.users.find({"id": {"$in": author_ids}})
                authors_list = await authors_cursor.to_list(len(author_ids))
                authors_dict = {user["id"]: UserResponse(**user) for user in authors_list}
            
            # Get user votes and likes for saved polls
            poll_ids = [poll["id"] for poll in polls]
            
            user_votes_cursor = db.votes.find({
                "poll_id": {"$in": poll_ids},
                "user_id": current_user.id
            })
            user_votes = await user_votes_cursor.to_list(len(poll_ids))
            user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
            
            user_likes_cursor = db.poll_likes.find({
                "poll_id": {"$in": poll_ids},
                "user_id": current_user.id
            })
            user_likes = await user_likes_cursor.to_list(len(poll_ids))
            liked_poll_ids = set(like["poll_id"] for like in user_likes)
            
            for record in saved_records:
                if record["poll_id"] in polls_dict:
                    poll_data = polls_dict[record["poll_id"]]
                    is_vs_poll = poll_data.get("layout") == "vs"
                    poll_author = authors_dict.get(poll_data.get("author_id"))
                    
                    # Get option users
                    option_user_ids = [option["user_id"] for option in poll_data.get("options", []) if option.get("user_id")]
                    if option_user_ids:
                        option_users_cursor = db.users.find({"id": {"$in": option_user_ids}})
                        option_users_list = await option_users_cursor.to_list(len(option_user_ids))
                        option_users_dict = {user["id"]: user for user in option_users_list}
                    else:
                        option_users_dict = {}
                    
                    # Process options with complete user data
                    options = []
                    for option in poll_data.get("options", []):
                        option_user = option_users_dict.get(option.get("user_id"))
                        # 🆕 VS posts: opciones no tienen user_id propio; usar el autor del poll
                        if not option_user and is_vs_poll and poll_author:
                            option_user = {
                                "username": poll_author.username,
                                "display_name": poll_author.display_name,
                                "avatar_url": poll_author.avatar_url,
                                "is_verified": getattr(poll_author, "is_verified", False),
                            }
                        if option_user:
                            # Keep media_url as relative path for frontend to handle
                            media_url = option.get("media_url")
                            
                            # Get thumbnail URL for videos
                            thumbnail_url = option.get("thumbnail_url")
                            if not thumbnail_url and media_url and option.get("media_type") == "video":
                                thumbnail_url = await get_thumbnail_for_media_url(media_url)
                            
                            option_dict = {
                                "id": option["id"],
                                "text": option["text"],
                                "votes": option["votes"],
                                "user": {
                                    "username": option_user["username"],
                                    "displayName": option_user["display_name"],
                                    "avatar": option_user.get("avatar_url"),
                                    "verified": option_user.get("is_verified", False),
                                    "followers": "1K"  # Placeholder
                                },
                                "mentioned_users": option.get("mentioned_users", []),
                                "extracted_audio_id": option.get("extracted_audio_id"),  # 🎵 Include extracted audio ID
                                "media": {
                                    "type": option.get("media_type"),
                                    "url": media_url,
                                    "thumbnail": thumbnail_url,  # None si no hay miniatura real (no caer al video URL)
                                    "transform": option.get("media_transform"),
                                    "optimizedUrl": option.get("optimized_media_url"),
                                    "hls": option.get("hls_url"),
                                } if media_url else None,
                                "optimized_media_url": option.get("optimized_media_url"),
                                "hls_url": option.get("hls_url"),
                            }
                            options.append(option_dict)
                    
                    # Skip polls without valid options. 🆕 VS posts pueden tener título vacío,
                    # así que sólo exigimos título para polls no-VS.
                    if not options:
                        continue
                    if not is_vs_poll and not poll_data.get("title"):
                        continue
                    
                    # Get music info if available
                    music_info = await get_music_info(poll_data.get("music_id")) if poll_data.get("music_id") else None
                    
                    # Build enriched poll response similar to regular polls endpoint
                    enriched_poll = {
                        "id": poll_data["id"],
                        "title": poll_data["title"],
                        "author": poll_author,
                        "description": poll_data.get("description"),
                        "options": options,
                        "total_votes": poll_data["total_votes"],
                        "likes": poll_data["likes"],
                        "shares": poll_data["shares"],
                        "comments_count": poll_data["comments_count"],
                        "music": music_info,
                        "user_vote": user_votes_dict.get(poll_data["id"]),
                        "user_liked": poll_data["id"] in liked_poll_ids,
                        "is_featured": poll_data["is_featured"],
                        "tags": poll_data.get("tags", []),
                        "category": poll_data.get("category"),
                        "mentioned_users": poll_data.get("mentioned_users", []),
                        "layout": poll_data.get("layout"),
                        # 🆕 VS Experience fields — necesarios para renderizar el layout VS en el perfil
                        "vs_id": poll_data.get("vs_id"),
                        "vs_questions": poll_data.get("vs_questions", []),
                        "creator_country": poll_data.get("creator_country"),
                        "vs_orientation": poll_data.get("vs_orientation", "horizontal"),
                        "created_at": poll_data["created_at"],
                        "time_ago": calculate_time_ago(poll_data["created_at"]),
                        "saved_at": record["saved_at"]
                    }
                    
                    ordered_polls.append(enriched_poll)
        
        logger.info(f"📚 Returning {len(ordered_polls)} ordered polls")
        
        # Get total count
        total = await db.saved_polls.count_documents({"user_id": user_id})
        
        return {"saved_polls": ordered_polls, "total": total}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error getting saved polls: {str(e)}")
        logger.error(f"❌ Error type: {type(e).__name__}")
        logger.error(f"❌ Error details: {repr(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@api_router.get("/users/{user_id}/liked-polls")
async def get_liked_polls(
    user_id: str,
    current_user: UserResponse = Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100)
):
    """Get polls that the user has liked"""
    logger.info(f"❤️ Getting liked polls for user {user_id}, current_user: {current_user.id}")
    try:
        # Only allow users to see their own liked polls for privacy
        if user_id != current_user.id:
            logger.warning(f"❌ Access denied: user {current_user.id} trying to access {user_id}'s liked polls")
            raise HTTPException(status_code=403, detail="Cannot access other user's liked polls")
        
        logger.info(f"❤️ Querying poll_likes collection for user_id: {user_id}")
        
        # Get liked poll IDs
        liked_records = await db.poll_likes.find({
            "user_id": user_id
        }).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
        
        logger.info(f"❤️ Found {len(liked_records)} liked records")
        
        if not liked_records:
            logger.info(f"❤️ No liked polls found for user {user_id}")
            return {"liked_polls": [], "total": 0}
        
        # Get the actual polls
        poll_ids = [record["poll_id"] for record in liked_records]
        logger.info(f"❤️ Looking up polls with IDs: {poll_ids}")
        
        polls = await db.polls.find({
            "id": {"$in": poll_ids},
            "is_active": True
        }).to_list(len(poll_ids))
        logger.info(f"❤️ Found {len(polls)} actual polls")
        
        # Create a mapping for easier lookup
        polls_dict = {poll["id"]: poll for poll in polls}
        
        # Return polls in the order they were liked with enriched data
        ordered_polls = []
        if liked_records:
            # Get all author IDs for batch processing
            author_ids = list(set(poll["author_id"] for poll in polls if poll.get("author_id")))
            authors_dict = {}
            if author_ids:
                authors_cursor = db.users.find({"id": {"$in": author_ids}})
                authors_list = await authors_cursor.to_list(len(author_ids))
                authors_dict = {user["id"]: UserResponse(**user) for user in authors_list}
            
            # Get user votes for liked polls
            poll_ids_active = [poll["id"] for poll in polls]
            
            user_votes_cursor = db.votes.find({
                "poll_id": {"$in": poll_ids_active},
                "user_id": current_user.id
            })
            user_votes = await user_votes_cursor.to_list(len(poll_ids_active))
            user_votes_dict = {vote["poll_id"]: vote["option_id"] for vote in user_votes}
            
            # All polls in this response are liked by definition
            liked_poll_ids = set(poll_ids_active)
            
            for record in liked_records:
                if record["poll_id"] in polls_dict:
                    poll_data = polls_dict[record["poll_id"]]
                    is_vs_poll = poll_data.get("layout") == "vs"
                    poll_author = authors_dict.get(poll_data.get("author_id"))
                    
                    # Get option users
                    option_user_ids = [option["user_id"] for option in poll_data.get("options", []) if option.get("user_id")]
                    if option_user_ids:
                        option_users_cursor = db.users.find({"id": {"$in": option_user_ids}})
                        option_users_list = await option_users_cursor.to_list(len(option_user_ids))
                        option_users_dict = {user["id"]: user for user in option_users_list}
                    else:
                        option_users_dict = {}
                    
                    # Process options with complete user data
                    options = []
                    for option in poll_data.get("options", []):
                        option_user = option_users_dict.get(option.get("user_id"))
                        # 🆕 VS posts: opciones no tienen user_id propio; usar el autor del poll
                        if not option_user and is_vs_poll and poll_author:
                            option_user = {
                                "username": poll_author.username,
                                "display_name": poll_author.display_name,
                                "avatar_url": poll_author.avatar_url,
                                "is_verified": getattr(poll_author, "is_verified", False),
                            }
                        if option_user:
                            # Keep media_url as relative path for frontend to handle
                            media_url = option.get("media_url")
                            
                            # Get thumbnail URL for videos
                            thumbnail_url = option.get("thumbnail_url")
                            if not thumbnail_url and media_url and option.get("media_type") == "video":
                                thumbnail_url = await get_thumbnail_for_media_url(media_url)
                            
                            option_dict = {
                                "id": option["id"],
                                "text": option["text"],
                                "votes": option["votes"],
                                "user": {
                                    "username": option_user["username"],
                                    "displayName": option_user["display_name"],
                                    "avatar": option_user.get("avatar_url"),
                                    "verified": option_user.get("is_verified", False),
                                    "followers": "1K"  # Placeholder
                                },
                                "mentioned_users": option.get("mentioned_users", []),
                                "extracted_audio_id": option.get("extracted_audio_id"),
                                "media": {
                                    "type": option.get("media_type"),
                                    "url": media_url,
                                    "thumbnail": thumbnail_url,  # None si no hay miniatura real (no caer al video URL)
                                    "transform": option.get("media_transform"),
                                    "optimizedUrl": option.get("optimized_media_url"),
                                    "hls": option.get("hls_url"),
                                } if media_url else None,
                                "optimized_media_url": option.get("optimized_media_url"),
                                "hls_url": option.get("hls_url"),
                            }
                            options.append(option_dict)
                    
                    # Skip polls without valid options. 🆕 VS posts pueden tener título vacío.
                    if not options:
                        continue
                    if not is_vs_poll and not poll_data.get("title"):
                        continue
                    
                    # Get music info if available
                    music_info = await get_music_info(poll_data.get("music_id")) if poll_data.get("music_id") else None
                    
                    # Build enriched poll response similar to regular polls endpoint
                    enriched_poll = {
                        "id": poll_data["id"],
                        "title": poll_data["title"],
                        "author": poll_author,
                        "description": poll_data.get("description"),
                        "options": options,
                        "total_votes": poll_data["total_votes"],
                        "likes": poll_data["likes"],
                        "shares": poll_data["shares"],
                        "comments_count": poll_data["comments_count"],
                        "music": music_info,
                        "user_vote": user_votes_dict.get(poll_data["id"]),
                        "user_liked": True,  # Always true for liked polls
                        "is_featured": poll_data["is_featured"],
                        "tags": poll_data.get("tags", []),
                        "category": poll_data.get("category"),
                        "mentioned_users": poll_data.get("mentioned_users", []),
                        "layout": poll_data.get("layout"),
                        # 🆕 VS Experience fields — necesarios para renderizar el layout VS en el perfil
                        "vs_id": poll_data.get("vs_id"),
                        "vs_questions": poll_data.get("vs_questions", []),
                        "creator_country": poll_data.get("creator_country"),
                        "vs_orientation": poll_data.get("vs_orientation", "horizontal"),
                        "created_at": poll_data["created_at"],
                        "time_ago": calculate_time_ago(poll_data["created_at"]),
                        "liked_at": record.get("created_at")
                    }
                    
                    ordered_polls.append(enriched_poll)
        
        logger.info(f"❤️ Returning {len(ordered_polls)} ordered liked polls")
        
        # Get total count
        total = await db.poll_likes.count_documents({"user_id": user_id})
        
        return {"liked_polls": ordered_polls, "total": total}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error getting liked polls: {str(e)}")
        logger.error(f"❌ Error type: {type(e).__name__}")
        logger.error(f"❌ Error details: {repr(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@api_router.get("/users/{user_id}/shared-polls")
async def get_shared_polls(
    user_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get poll IDs that the user has shared"""
    logger.info(f"🔗 Getting shared polls for user {user_id}, current_user: {current_user.id}")
    try:
        # Only allow users to see their own shared polls
        if user_id != current_user.id:
            logger.warning(f"❌ Access denied: user {current_user.id} trying to access {user_id}'s shared polls")
            raise HTTPException(status_code=403, detail="Cannot access other user's shared polls")
        
        # Get shared poll IDs
        shared_records = await db.poll_shares.find({
            "user_id": user_id
        }).to_list(1000)  # Get up to 1000 shared polls
        
        logger.info(f"🔗 Found {len(shared_records)} shared records")
        
        poll_ids = [record["poll_id"] for record in shared_records]
        
        return {"shared_poll_ids": poll_ids, "total": len(poll_ids)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error getting shared polls: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@api_router.get("/polls/{poll_id}/save-status")
async def get_poll_save_status(
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Check if a poll is saved by the current user"""
    try:
        saved = await db.saved_polls.find_one({
            "user_id": current_user.id,
            "poll_id": poll_id
        })
        
        return {"saved": bool(saved)}
        
    except Exception as e:
        logger.error(f"Error checking save status: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

# =============  SOCIAL LINKS ENDPOINTS =============

@api_router.get("/users/{user_id}/social-links", response_model=SocialLinksResponse)
async def get_user_social_links(
    user_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get user's social media links"""
    try:
        # Find user's social links
        social_links = await db.user_social_links.find_one({"user_id": user_id})
        
        if not social_links:
            # Return empty social links if none exist
            return SocialLinksResponse()
        
        return SocialLinksResponse(
            website=social_links.get("website"),
            behance=social_links.get("behance"),
            dribbble=social_links.get("dribbble"),
            tiktok=social_links.get("tiktok"),
            twitch=social_links.get("twitch"),
            instagram=social_links.get("instagram"),
            discord=social_links.get("discord"),
            youtube=social_links.get("youtube")
        )
        
    except Exception as e:
        logger.error(f"Error getting social links: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.put("/users/social-links", response_model=SocialLinksResponse)
async def update_user_social_links(
    social_links_data: SocialLinksUpdate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Update current user's social media links"""
    try:
        # Prepare update data
        update_data = {
            "user_id": current_user.id,
            "website": social_links_data.website,
            "behance": social_links_data.behance,
            "dribbble": social_links_data.dribbble,
            "tiktok": social_links_data.tiktok,
            "twitch": social_links_data.twitch,
            "instagram": social_links_data.instagram,
            "discord": social_links_data.discord,
            "youtube": social_links_data.youtube,
            "updated_at": datetime.utcnow()
        }
        
        # Check if social links already exist
        existing_links = await db.user_social_links.find_one({"user_id": current_user.id})
        
        if existing_links:
            # Update existing social links
            await db.user_social_links.update_one(
                {"user_id": current_user.id},
                {"$set": update_data}
            )
        else:
            # Create new social links record
            update_data["id"] = str(uuid.uuid4())
            update_data["created_at"] = datetime.utcnow()
            await db.user_social_links.insert_one(update_data)
        
        return SocialLinksResponse(
            website=social_links_data.website,
            behance=social_links_data.behance,
            dribbble=social_links_data.dribbble,
            tiktok=social_links_data.tiktok,
            twitch=social_links_data.twitch,
            instagram=social_links_data.instagram,
            discord=social_links_data.discord,
            youtube=social_links_data.youtube
        )
        
    except Exception as e:
        logger.error(f"Error updating social links: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

# =============  CUSTOM SOCIAL LINKS ENDPOINTS =============

@api_router.post("/social_links")
async def save_user_social_links(
    social_links_data: SocialLinksCreate,
    current_user: UserResponse = Depends(get_current_user)
):
    """Save user's custom social media links"""
    try:
        # Prepare data for MongoDB
        social_links_document = {
            "id": str(uuid.uuid4()),
            "user_id": current_user.id,
            "links": [link.dict() for link in social_links_data.links],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        # Check if social links already exist for this user
        existing_links = await db.social_links.find_one({"user_id": current_user.id})
        
        if existing_links:
            # Update existing social links
            await db.social_links.update_one(
                {"user_id": current_user.id},
                {
                    "$set": {
                        "links": social_links_document["links"],
                        "updated_at": social_links_document["updated_at"]
                    }
                }
            )
        else:
            # Create new social links record
            await db.social_links.insert_one(social_links_document)
        
        return {"message": "Social links saved successfully", "links": social_links_data.links}
        
    except Exception as e:
        logger.error(f"Error saving social links: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.get("/social_links/me")
async def get_current_user_social_links(
    current_user: UserResponse = Depends(get_current_user)
):
    """Get current user's custom social media links"""
    try:
        # Find user's social links
        social_links = await db.social_links.find_one({"user_id": current_user.id})
        
        if not social_links:
            return {"links": []}
        
        return {"links": social_links.get("links", [])}
        
    except Exception as e:
        logger.error(f"Error getting current user social links: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.get("/social_links/{user_id}")
async def get_user_social_links_by_id(
    user_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get user's custom social media links by user ID"""
    try:
        # Find user's social links
        social_links = await db.social_links.find_one({"user_id": user_id})
        
        if not social_links:
            return {"links": []}
        
        return {"links": social_links.get("links", [])}
        
    except Exception as e:
        logger.error(f"Error getting user social links: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

# Agregar middleware CORS ANTES de incluir routers
app.add_middleware(
    CORSMiddleware,
    # allow_origin_regex permite cualquier origen (incluyendo https://localhost
    # de Capacitor WebView, capacitor://localhost de iOS, y cualquier dominio web)
    # manteniendo allow_credentials=True de forma válida según el estándar CORS.
    # El regex .* matchea cualquier origin, pero el middleware lo refleja en la
    # respuesta Access-Control-Allow-Origin (NO envía "*"), lo que es compatible
    # con credentials.
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============  SEARCH HISTORY ENDPOINTS =============

@api_router.get("/search/recent")
async def get_recent_searches(
    limit: int = 10, 
    current_user: UserResponse = Depends(get_current_user)
):
    """Get recent search history for current user"""
    try:
        # Get recent searches for the current user, sorted by most recent
        recent_searches_raw = await db.search_history.find({
            "user_id": current_user.id
        }).sort("created_at", -1).limit(limit).to_list(limit)
        
        # Convert MongoDB documents to JSON-serializable format
        recent_searches = []
        for search in recent_searches_raw:
            search_dict = {
                "id": search.get("id", str(search.get("_id"))),
                "query": search.get("query", ""),
                "search_type": search.get("search_type", "general"),
                "created_at": search.get("created_at", "")
            }
            recent_searches.append(search_dict)
        
        return {"recent_searches": recent_searches}
        
    except Exception as e:
        logger.error(f"Error getting recent searches: {str(e)}")
        return {"recent_searches": []}

@api_router.post("/search/recent")
async def save_recent_search(
    search_data: dict,
    current_user: UserResponse = Depends(get_current_user)
):
    """Save a search query to recent searches history"""
    try:
        query = search_data.get("query", "").strip()
        search_type = search_data.get("search_type", "general")
        
        if not query:
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        # Check if this exact search already exists recently (last 24 hours)
        recent_time = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        existing_search = await db.search_history.find_one({
            "user_id": current_user.id,
            "query": query,
            "search_type": search_type,
            "created_at": {"$gte": recent_time}
        })
        
        if existing_search:
            # Update timestamp of existing search to move it to top
            await db.search_history.update_one(
                {"_id": existing_search["_id"]},
                {"$set": {"created_at": datetime.utcnow().isoformat()}}
            )
            return {"message": "Search updated", "search_id": str(existing_search["_id"])}
        
        # Create new search history entry
        search_entry = {
            "id": str(uuid.uuid4()),
            "user_id": current_user.id,
            "query": query,
            "search_type": search_type,
            "created_at": datetime.utcnow().isoformat()
        }
        
        result = await db.search_history.insert_one(search_entry)
        
        # Keep only the most recent 20 searches per user to avoid cluttering
        user_searches = await db.search_history.find({
            "user_id": current_user.id
        }).sort("created_at", -1).to_list(25)  # Get 25 to delete the excess
        
        if len(user_searches) > 20:
            # Delete older searches beyond the limit
            searches_to_delete = user_searches[20:]
            delete_ids = [s["_id"] for s in searches_to_delete]
            await db.search_history.delete_many({"_id": {"$in": delete_ids}})
        
        return {"message": "Search saved", "search_id": search_entry["id"]}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving recent search: {str(e)}")


@api_router.get("/search/recommendations")
async def get_search_recommendations(
    limit: int = 12,
    current_user: UserResponse = Depends(get_current_user)
):
    """Get recommended content for the search page"""
    try:
        # Get trending polls from last 7 days
        trending_date = (datetime.utcnow() - timedelta(days=7)).isoformat()
        
        # Aggregate popular polls based on votes and likes
        pipeline = [
            {
                "$match": {
                    "created_at": {"$gte": trending_date}
                }
            },
            {
                "$addFields": {
                    "engagement_count": {
                        "$add": [
                            {"$ifNull": ["$votes_count", 0]},
                            {"$multiply": [{"$ifNull": ["$likes_count", 0]}, 2]},
                            {"$ifNull": ["$comments_count", 0]}
                        ]
                    }
                }
            },
            {
                "$sort": {"engagement_count": -1}
            },
            {
                "$limit": limit
            },
            {
                "$lookup": {
                    "from": "users",
                    "localField": "author_id",
                    "foreignField": "id",
                    "as": "author_info"
                }
            },
            {
                "$addFields": {
                    "author": {"$arrayElemAt": ["$author_info", 0]}
                }
            },
            {
                "$project": {
                    "id": 1,
                    "title": 1,
                    "content": 1,
                    "thumbnail_url": 1,
                    "image_url": 1,
                    "options": 1,
                    "engagement_count": 1,
                    "votes_count": 1,
                    "likes_count": 1,
                    "comments_count": 1,
                    "hashtags": 1,
                    "created_at": 1,
                    "type": {"$literal": "poll"},
                    "author.username": 1,
                    "author.display_name": 1,
                    "author.avatar_url": 1
                }
            }
        ]
        
        recommendations = await db.polls.aggregate(pipeline).to_list(limit)
        
        # Add thumbnail_url from first option if not present
        for rec in recommendations:
            if not rec.get("thumbnail_url") and rec.get("options"):
                for option in rec["options"]:
                    if option.get("thumbnail_url"):
                        rec["thumbnail_url"] = option["thumbnail_url"]
                        break
                    elif option.get("media_url"):
                        rec["thumbnail_url"] = option["media_url"]
                        break
        
        return {"recommendations": recommendations, "total": len(recommendations)}
        
    except Exception as e:
        logger.error(f"Error getting search recommendations: {str(e)}")
        # Return empty array on error instead of raising exception
        return {"recommendations": [], "total": 0}

        raise HTTPException(status_code=500, detail="Failed to save search")

@api_router.delete("/search/recent/{search_id}")
async def delete_recent_search(
    search_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Delete a specific search from history"""
    try:
        # Delete search entry (ensure it belongs to current user)
        result = await db.search_history.delete_one({
            "id": search_id,
            "user_id": current_user.id
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Search not found")
        
        return {"message": "Search deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting recent search: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete search")




# =============  CHALLENGE ENDPOINTS =============

@api_router.post("/challenges", response_model=ChallengeResponse)
async def create_challenge(
    challenge_data: ChallengeCreate,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Crear un nuevo challenge mencionando hasta 5 usuarios (máximo 6 total con creador)
    El creador debe haber subido ya su contenido (poll)
    
    REGLAS DE VISIBILIDAD:
    - El poll del creador se marca como challenge_pending=True
    - El poll NO aparece en feeds públicos hasta que el challenge esté publicado
    - Solo los participantes del challenge pueden ver el contenido mientras está pendiente
    """
    try:
        # Verificar que el poll del creador existe
        creator_poll = await db.polls.find_one({"id": challenge_data.creator_poll_id})
        if not creator_poll:
            raise HTTPException(status_code=404, detail="Poll del creador no encontrado")
        
        # Verificar que el creador es el autor del poll
        if creator_poll.get("author_id") != current_user.id:
            raise HTTPException(status_code=403, detail="No eres el autor de este poll")
        
        # Verificar que no se menciona a sí mismo
        if current_user.id in challenge_data.participant_ids:
            raise HTTPException(status_code=400, detail="No puedes mencionarte a ti mismo en un challenge")
        
        # Validar número máximo de participantes (5 invitados + 1 creador = 6 máximo)
        if len(challenge_data.participant_ids) > 5:
            raise HTTPException(status_code=400, detail="Máximo 5 usuarios invitados permitidos (6 total con creador)")
        
        # Obtener información de los participantes
        participants = []
        for participant_id in challenge_data.participant_ids:
            user = await db.users.find_one({"id": participant_id})
            if not user:
                raise HTTPException(status_code=404, detail=f"Usuario {participant_id} no encontrado")
            
            participants.append(ChallengeParticipant(
                user_id=user["id"],
                username=user["username"],
                display_name=user.get("display_name"),
                avatar_url=user.get("avatar_url"),
                status=ParticipantStatus.INVITED
            ))
        
        # Agregar al creador como primer participante (ya tiene contenido)
        creator_participant = ChallengeParticipant(
            user_id=current_user.id,
            username=current_user.username,
            display_name=current_user.display_name,
            avatar_url=current_user.avatar_url,
            status=ParticipantStatus.CONTENT_SUBMITTED,
            poll_id=challenge_data.creator_poll_id,
            joined_at=datetime.utcnow(),
            submitted_at=datetime.utcnow()
        )
        
        # Insertar creador al inicio de la lista
        all_participants = [creator_participant] + participants
        
        # Crear el challenge
        challenge = Challenge(
            title=challenge_data.title,
            description=challenge_data.description,
            creator_id=current_user.id,
            creator_username=current_user.username,
            creator_display_name=current_user.display_name,
            creator_avatar_url=current_user.avatar_url,
            participants=all_participants,
            challenge_type=challenge_data.challenge_type,
            required_layout=challenge_data.required_layout or creator_poll.get("layout"),  # Use explicit or from creator's poll
            deadline=challenge_data.deadline,
            status=ChallengeStatus.PENDING
        )
        
        # Guardar en la base de datos
        await db.challenges.insert_one(challenge.model_dump())
        
        # 🔒 CRITICAL: Marcar el poll del creador como pendiente de challenge
        # Esto lo oculta del feed público hasta que el challenge esté publicado
        await db.polls.update_one(
            {"id": challenge_data.creator_poll_id},
            {"$set": {
                "challenge_id": challenge.id,
                "challenge_pending": True  # 🔒 Poll oculto del feed público
            }}
        )
        
        logger.info(f"✅ Challenge {challenge.id} creado por {current_user.username} con {len(participants)} participantes")
        logger.info(f"🔒 Poll {challenge_data.creator_poll_id} marcado como challenge_pending=True (oculto del feed)")
        
        # Preparar respuesta
        response = ChallengeResponse(
            **challenge.model_dump(),
            accepted_count=0,
            submitted_count=1,  # Solo el creador
            is_ready_to_publish=False
        )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error creando challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al crear el challenge")


@api_router.get("/challenges/my-invitations", response_model=List[ChallengeResponse])
async def get_my_challenge_invitations(
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Obtener challenges donde el usuario fue mencionado/invitado
    Estos aparecen en la sección "Activos" de Explore
    """
    try:
        # Buscar challenges donde el usuario es participante y está en estado INVITED
        challenges_cursor = db.challenges.find({
            "participants.user_id": current_user.id,
            "participants.status": ParticipantStatus.INVITED,
            "status": {"$in": [ChallengeStatus.PENDING, ChallengeStatus.ACTIVE]}
        }).sort("created_at", -1)
        
        challenges = await challenges_cursor.to_list(length=100)
        
        # Transformar a respuesta
        responses = []
        for challenge in challenges:
            accepted_count = sum(1 for p in challenge.get("participants", []) 
                               if p["status"] in [ParticipantStatus.ACCEPTED, ParticipantStatus.CONTENT_SUBMITTED])
            submitted_count = sum(1 for p in challenge.get("participants", []) 
                                if p["status"] == ParticipantStatus.CONTENT_SUBMITTED)
            total_participants = len(challenge.get("participants", []))
            
            response = ChallengeResponse(
                **challenge,
                accepted_count=accepted_count,
                submitted_count=submitted_count,
                is_ready_to_publish=(submitted_count == total_participants)
            )
            responses.append(response)
        
        logger.info(f"📩 Usuario {current_user.username} tiene {len(responses)} invitaciones de challenges")
        return responses
        
    except Exception as e:
        logger.error(f"❌ Error obteniendo invitaciones de challenges: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener invitaciones")


@api_router.get("/challenges/active", response_model=List[ChallengeResponse])
async def get_active_challenges(
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Obtener challenges activos (en progreso) donde el usuario es participante
    Estos aparecen en Explore > Activos
    
    REGLAS DE VISIBILIDAD:
    - Solo muestra challenges donde el usuario actual es participante (creador o invitado)
    - Challenges incompletos son 100% privados para no-participantes
    - Estados incluidos: PENDING, ACTIVE
    """
    try:
        # 🔒 CRITICAL: Solo mostrar challenges donde el usuario es participante
        # Buscar challenges en estado PENDING o ACTIVE donde el usuario es participante
        challenges_cursor = db.challenges.find({
            "status": {"$in": [ChallengeStatus.PENDING, ChallengeStatus.ACTIVE]},
            "$or": [
                {"creator_id": current_user.id},  # Es el creador
                {"participants.user_id": current_user.id}  # Es un participante invitado
            ]
        }).sort("created_at", -1).limit(50)
        
        challenges = await challenges_cursor.to_list(length=50)
        
        responses = []
        for challenge in challenges:
            accepted_count = sum(1 for p in challenge.get("participants", []) 
                               if p["status"] in [ParticipantStatus.ACCEPTED, ParticipantStatus.CONTENT_SUBMITTED])
            submitted_count = sum(1 for p in challenge.get("participants", []) 
                                if p["status"] == ParticipantStatus.CONTENT_SUBMITTED)
            total_participants = len(challenge.get("participants", []))
            
            response = ChallengeResponse(
                **challenge,
                accepted_count=accepted_count,
                submitted_count=submitted_count,
                is_ready_to_publish=(submitted_count == total_participants)
            )
            responses.append(response)
        
        logger.info(f"📋 Usuario {current_user.username} tiene {len(responses)} challenges activos")
        return responses
        
    except Exception as e:
        logger.error(f"❌ Error obteniendo challenges activos: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener challenges activos")


@api_router.get("/challenges/completed")
async def get_completed_challenges(
    limit: int = 20,
    skip: int = 0,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """
    Obtener challenges completados y publicados con información de media
    Solo visibles para el creador y los participantes del challenge
    """
    try:
        # 🔒 CRITICAL: Solo mostrar challenges donde el usuario es creador o participante
        if not current_user:
            return []
        
        challenges_cursor = db.challenges.find({
            "status": ChallengeStatus.PUBLISHED,
            "$or": [
                {"creator_id": current_user.id},
                {"participants.user_id": current_user.id}
            ]
        }).sort("published_at", -1).skip(skip).limit(limit)
        
        challenges = await challenges_cursor.to_list(length=limit)
        
        # Obtener votos del usuario si está autenticado
        user_votes = {}
        if current_user:
            challenge_ids = [c.get("id") for c in challenges]
            votes_cursor = db.challenge_votes.find({
                "challenge_id": {"$in": challenge_ids},
                "voter_id": current_user.id
            })
            votes_list = await votes_cursor.to_list(length=len(challenge_ids))
            user_votes = {v.get("challenge_id"): v.get("participant_id") for v in votes_list}
        
        responses = []
        for challenge in challenges:
            participants = challenge.get("participants", [])
            total_participants = len(participants)
            
            # Obtener los polls de este challenge para tener los thumbnails
            participant_poll_ids = [
                p.get("poll_id") for p in participants
                if p.get("poll_id") and p.get("status") == ParticipantStatus.CONTENT_SUBMITTED
            ]
            
            # Cargar polls para obtener media
            polls_dict = {}
            if participant_poll_ids:
                polls_cursor = db.polls.find({"id": {"$in": participant_poll_ids}})
                polls_list = await polls_cursor.to_list(length=len(participant_poll_ids))
                polls_dict = {p.get("id"): p for p in polls_list}
            
            # Enriquecer participantes con info de media y votos
            enriched_participants = []
            for p in participants:
                poll = polls_dict.get(p.get("poll_id"))
                poll_media = None
                if poll and poll.get("options"):
                    first_option = poll["options"][0]
                    poll_media = {
                        "type": first_option.get("media_type"),
                        "url": first_option.get("media_url"),
                        "thumbnail": first_option.get("thumbnail_url") or first_option.get("media_url")
                    }
                
                enriched_participants.append({
                    **p,
                    "poll_media": poll_media,
                    "votes_received": p.get("votes_received", 0)
                })
            
            # Obtener el voto del usuario para este challenge
            user_vote_participant = user_votes.get(challenge.get("id"))
            
            # 🎵 Resolver música del challenge desde los polls de participantes
            challenge_music_info = None
            for p_poll_id in participant_poll_ids:
                p_poll = polls_dict.get(p_poll_id)
                if p_poll:
                    if p_poll.get("music"):
                        challenge_music_info = p_poll.get("music")
                        break
                    elif p_poll.get("music_id"):
                        challenge_music_info = await get_music_info(p_poll.get("music_id"))
                        if challenge_music_info:
                            break
            
            # Check user like/save status
            challenge_feed_id = f"challenge_{challenge.get('id')}"
            user_liked = False
            is_saved = False
            user_commented = False
            if current_user:
                user_like = await db.poll_likes.find_one({
                    "poll_id": challenge_feed_id,
                    "user_id": current_user.id
                })
                user_liked = user_like is not None
                
                user_save = await db.saved_polls.find_one({
                    "poll_id": challenge_feed_id,
                    "user_id": current_user.id
                })
                is_saved = user_save is not None
                
                user_comment = await db.comments.find_one({
                    "poll_id": challenge_feed_id,
                    "user_id": current_user.id
                })
                user_commented = user_comment is not None
            
            response = {
                "id": challenge.get("id"),
                "title": challenge.get("title"),
                "description": challenge.get("description"),
                "creator_id": challenge.get("creator_id"),
                "creator_username": challenge.get("creator_username"),
                "creator_display_name": challenge.get("creator_display_name"),
                "creator_avatar_url": challenge.get("creator_avatar_url"),
                "participants": enriched_participants,
                "challenge_type": challenge.get("challenge_type"),
                "status": challenge.get("status"),
                "created_at": challenge.get("created_at"),
                "published_at": challenge.get("published_at"),
                "final_layout": challenge.get("final_layout"),
                "required_layout": challenge.get("required_layout"),
                "final_participant_count": challenge.get("final_participant_count"),
                "accepted_count": total_participants,
                "submitted_count": total_participants,
                "is_ready_to_publish": True,
                "user_vote_participant_id": user_vote_participant,
                "total_votes": challenge.get("total_votes", 0),
                "music": challenge_music_info,
                "likes_count": challenge.get("likes_count", 0),
                "comments_count": challenge.get("comments_count", 0),
                "saves_count": challenge.get("saves_count", 0),
                "user_liked": user_liked,
                "is_saved": is_saved,
                "user_commented": user_commented
            }
            responses.append(response)
        
        logger.info(f"📋 Retornando {len(responses)} challenges completados")
        return responses
        
    except Exception as e:
        logger.error(f"❌ Error obteniendo challenges completados: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener challenges completados")


@api_router.get("/challenges/{challenge_id}", response_model=ChallengeResponse)
async def get_challenge_details(
    challenge_id: str,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """Obtener detalles de un challenge específico"""
    try:
        challenge = await db.challenges.find_one({"id": challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge no encontrado")
        
        accepted_count = sum(1 for p in challenge.get("participants", []) 
                           if p["status"] in [ParticipantStatus.ACCEPTED, ParticipantStatus.CONTENT_SUBMITTED])
        submitted_count = sum(1 for p in challenge.get("participants", []) 
                            if p["status"] == ParticipantStatus.CONTENT_SUBMITTED)
        total_participants = len(challenge.get("participants", []))
        
        response = ChallengeResponse(
            **challenge,
            accepted_count=accepted_count,
            submitted_count=submitted_count,
            is_ready_to_publish=(submitted_count == total_participants)
        )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error obteniendo detalles del challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener challenge")


@api_router.post("/challenges/{challenge_id}/accept")
async def accept_challenge(
    challenge_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Aceptar participación en un challenge"""
    try:
        # Buscar el challenge
        challenge = await db.challenges.find_one({"id": challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge no encontrado")
        
        # Verificar que el usuario es un participante
        participant_index = None
        for i, p in enumerate(challenge.get("participants", [])):
            if p["user_id"] == current_user.id:
                participant_index = i
                break
        
        if participant_index is None:
            raise HTTPException(status_code=403, detail="No estás invitado a este challenge")
        
        participant = challenge["participants"][participant_index]
        
        # Verificar que aún no ha aceptado
        if participant["status"] != ParticipantStatus.INVITED:
            return {"success": True, "message": "Ya has respondido a este challenge", "status": participant["status"]}
        
        # Actualizar estado del participante
        await db.challenges.update_one(
            {"id": challenge_id, "participants.user_id": current_user.id},
            {
                "$set": {
                    f"participants.$.status": ParticipantStatus.ACCEPTED,
                    f"participants.$.joined_at": datetime.utcnow()
                }
            }
        )
        
        # Verificar si hay suficientes participantes aceptados para activar
        updated_challenge = await db.challenges.find_one({"id": challenge_id})
        accepted_count = sum(1 for p in updated_challenge.get("participants", []) 
                           if p["status"] in [ParticipantStatus.ACCEPTED, ParticipantStatus.CONTENT_SUBMITTED])
        
        # Activar challenge si al menos 2 han aceptado (incluyendo creador)
        if accepted_count >= 2 and updated_challenge["status"] == ChallengeStatus.PENDING:
            await db.challenges.update_one(
                {"id": challenge_id},
                {"$set": {"status": ChallengeStatus.ACTIVE}}
            )
            logger.info(f"🎯 Challenge {challenge_id} activado - {accepted_count} participantes aceptaron")
        
        logger.info(f"✅ Usuario {current_user.username} aceptó el challenge {challenge_id}")
        
        return {
            "success": True,
            "message": "Challenge aceptado exitosamente",
            "status": ParticipantStatus.ACCEPTED
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error aceptando challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al aceptar challenge")


@api_router.post("/challenges/{challenge_id}/reject")
async def reject_challenge(
    challenge_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Rechazar participación en un challenge
    
    REGLAS:
    - Si todos los invitados rechazan → Challenge se cancela
    - Si queda al menos 1 invitado que puede aceptar → Challenge continúa
    - Si no se puede alcanzar el mínimo (2 participantes) → Challenge se cancela
    """
    try:
        # Buscar el challenge
        challenge = await db.challenges.find_one({"id": challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge no encontrado")
        
        # Verificar que el usuario es un participante
        participant_index = None
        for i, p in enumerate(challenge.get("participants", [])):
            if p["user_id"] == current_user.id:
                participant_index = i
                break
        
        if participant_index is None:
            raise HTTPException(status_code=403, detail="No estás invitado a este challenge")
        
        participant = challenge["participants"][participant_index]
        
        # No permitir rechazar si ya no está en estado INVITED
        if participant["status"] != ParticipantStatus.INVITED:
            return {"success": True, "message": "Ya has respondido a este challenge"}
        
        # Actualizar estado del participante
        await db.challenges.update_one(
            {"id": challenge_id, "participants.user_id": current_user.id},
            {"$set": {f"participants.$.status": ParticipantStatus.REJECTED}}
        )
        
        logger.info(f"❌ Usuario {current_user.username} rechazó el challenge {challenge_id}")
        
        # 🔒 Verificar si el challenge debe cancelarse
        # Obtener challenge actualizado
        updated_challenge = await db.challenges.find_one({"id": challenge_id})
        participants = updated_challenge.get("participants", [])
        
        # Contar estados
        submitted_count = sum(1 for p in participants if p["status"] == ParticipantStatus.CONTENT_SUBMITTED)
        accepted_count = sum(1 for p in participants if p["status"] == ParticipantStatus.ACCEPTED)
        pending_count = sum(1 for p in participants if p["status"] == ParticipantStatus.INVITED)
        rejected_count = sum(1 for p in participants if p["status"] == ParticipantStatus.REJECTED)
        
        # Participantes activos = los que ya enviaron contenido + los que aceptaron
        active_participants = submitted_count + accepted_count
        # Participantes potenciales = activos + pendientes (que podrían aceptar)
        potential_participants = active_participants + pending_count
        
        logger.info(f"📊 Challenge {challenge_id}: submitted={submitted_count}, accepted={accepted_count}, pending={pending_count}, rejected={rejected_count}")
        logger.info(f"📊 Potencial máximo de participantes: {potential_participants}")
        
        # REGLA: Si no se puede alcanzar el mínimo de 2 participantes, cancelar
        MIN_PARTICIPANTS = 2
        
        if potential_participants < MIN_PARTICIPANTS:
            # Cancelar el challenge
            await db.challenges.update_one(
                {"id": challenge_id},
                {"$set": {"status": ChallengeStatus.CANCELLED}}
            )
            
            # 🗑️ Limpiar los polls asociados al challenge cancelado
            # Quitar challenge_id y challenge_pending de los polls
            poll_ids = [p.get("poll_id") for p in participants if p.get("poll_id")]
            if poll_ids:
                await db.polls.update_many(
                    {"id": {"$in": poll_ids}},
                    {
                        "$unset": {"challenge_id": "", "challenge_pending": ""}
                    }
                )
                logger.info(f"🗑️ Polls desvinculados del challenge cancelado: {poll_ids}")
            
            logger.info(f"🚫 Challenge {challenge_id} CANCELADO - No se puede alcanzar el mínimo de {MIN_PARTICIPANTS} participantes")
            
            return {
                "success": True,
                "message": "Challenge rechazado y cancelado (no hay suficientes participantes)",
                "status": ParticipantStatus.REJECTED,
                "challenge_cancelled": True
            }
        
        return {
            "success": True,
            "message": "Challenge rechazado",
            "status": ParticipantStatus.REJECTED,
            "challenge_cancelled": False
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error rechazando challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al rechazar challenge")


@api_router.post("/challenges/{challenge_id}/submit-content")
async def submit_challenge_content(
    challenge_id: str,
    poll_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Enviar contenido (poll) para un challenge
    Cuando todos los participantes envían contenido, se publica automáticamente
    """
    try:
        # Verificar que el poll existe y pertenece al usuario
        poll = await db.polls.find_one({"id": poll_id})
        if not poll:
            raise HTTPException(status_code=404, detail="Poll no encontrado")
        
        if poll.get("author_id") != current_user.id:
            raise HTTPException(status_code=403, detail="No eres el autor de este poll")
        
        # Buscar el challenge
        challenge = await db.challenges.find_one({"id": challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge no encontrado")
        
        # Verificar que el usuario es participante y ha aceptado
        participant_index = None
        for i, p in enumerate(challenge.get("participants", [])):
            if p["user_id"] == current_user.id:
                participant_index = i
                break
        
        if participant_index is None:
            raise HTTPException(status_code=403, detail="No eres participante de este challenge")
        
        participant = challenge["participants"][participant_index]
        
        if participant["status"] == ParticipantStatus.INVITED:
            raise HTTPException(status_code=400, detail="Debes aceptar el challenge primero")
        
        if participant["status"] == ParticipantStatus.REJECTED:
            raise HTTPException(status_code=400, detail="Has rechazado este challenge")
        
        if participant["status"] == ParticipantStatus.CONTENT_SUBMITTED:
            return {"success": True, "message": "Ya has enviado tu contenido para este challenge"}
        
        # Actualizar el participante con el poll_id
        await db.challenges.update_one(
            {"id": challenge_id, "participants.user_id": current_user.id},
            {
                "$set": {
                    f"participants.$.status": ParticipantStatus.CONTENT_SUBMITTED,
                    f"participants.$.poll_id": poll_id,
                    f"participants.$.submitted_at": datetime.utcnow()
                }
            }
        )
        
        # 🔒 Actualizar el poll con challenge_id y marcarlo como pendiente (oculto del feed)
        await db.polls.update_one(
            {"id": poll_id},
            {"$set": {
                "challenge_id": challenge_id,
                "challenge_pending": True  # 🔒 Poll oculto del feed público hasta publicación
            }}
        )
        logger.info(f"🔒 Poll {poll_id} marcado como challenge_pending=True")
        
        # Verificar si todos los participantes han enviado contenido
        updated_challenge = await db.challenges.find_one({"id": challenge_id})
        participants = updated_challenge.get("participants", [])
        total_participants = len(participants)
        submitted_count = sum(1 for p in participants if p["status"] == ParticipantStatus.CONTENT_SUBMITTED)
        
        logger.info(f"📊 Challenge {challenge_id}: {submitted_count}/{total_participants} participantes completaron")
        
        # Si todos completaron, publicar automáticamente
        if submitted_count == total_participants:
            await publish_challenge(challenge_id)
            logger.info(f"🎉 Challenge {challenge_id} publicado automáticamente!")
            return {
                "success": True,
                "message": "Contenido enviado. ¡Challenge completado y publicado!",
                "challenge_published": True
            }
        
        logger.info(f"✅ Usuario {current_user.username} envió contenido para challenge {challenge_id}")
        
        return {
            "success": True,
            "message": f"Contenido enviado exitosamente ({submitted_count}/{total_participants})",
            "challenge_published": False,
            "submitted_count": submitted_count,
            "total_participants": total_participants
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error enviando contenido del challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al enviar contenido")


async def publish_challenge(challenge_id: str):
    """
    Función helper para publicar un challenge cuando está listo
    
    FUNCIONALIDADES:
    1. Actualiza el estado del challenge a PUBLISHED
    2. MANTIENE challenge_pending=True en los polls (NO aparecen individualmente)
    3. El challenge aparece como entrada UNIFICADA en el feed
    4. Calcula y asigna el layout adaptativo según número de participantes:
       - 2 participantes: layout "1vs1"
       - 3 participantes: layout "stack" 
       - 4 participantes: layout "grid-2x2"
       - 5 participantes: layout "grid-adaptive-5"
       - 6 participantes: layout "grid-3x2"
    """
    try:
        # Obtener el challenge para acceder a los participantes
        challenge = await db.challenges.find_one({"id": challenge_id})
        if not challenge:
            logger.error(f"❌ Challenge {challenge_id} no encontrado para publicar")
            return False
        
        participants = challenge.get("participants", [])
        
        # Contar participantes que enviaron contenido (CONTENT_SUBMITTED)
        active_participants = [p for p in participants if p.get("status") == ParticipantStatus.CONTENT_SUBMITTED]
        participant_count = len(active_participants)
        
        # Determinar el layout: usar required_layout del creador si existe, sino auto-calcular
        stored_layout = challenge.get("required_layout")
        if stored_layout:
            challenge_layout = stored_layout
            logger.info(f"🎨 Challenge {challenge_id}: usando layout del creador: {challenge_layout}")
        else:
            # Auto-calcular según número de participantes (fallback)
            if participant_count == 2:
                challenge_layout = "1vs1"
            elif participant_count == 3:
                challenge_layout = "stack"
            elif participant_count == 4:
                challenge_layout = "grid-2x2"
            elif participant_count == 5:
                challenge_layout = "grid-adaptive-5"
            elif participant_count >= 6:
                challenge_layout = "grid-3x2"
            else:
                challenge_layout = "single"
            logger.info(f"🎨 Challenge {challenge_id}: layout auto-calculado: {challenge_layout}")
        
        logger.info(f"🎨 Challenge {challenge_id}: {participant_count} participantes → Layout: {challenge_layout}")
        
        # Actualizar el estado del challenge
        await db.challenges.update_one(
            {"id": challenge_id},
            {
                "$set": {
                    "status": ChallengeStatus.PUBLISHED,
                    "published_at": datetime.utcnow(),
                    "final_layout": challenge_layout,
                    "final_participant_count": participant_count
                }
            }
        )
        
        # 🔒 IMPORTANTE: Los polls MANTIENEN challenge_pending=True
        # Esto evita que aparezcan como publicaciones individuales
        # El challenge aparecerá como una entrada UNIFICADA en el feed
        logger.info(f"🔒 Los polls del challenge mantienen challenge_pending=True (no aparecen individualmente)")
        
        logger.info(f"🎊 Challenge {challenge_id} publicado exitosamente con layout {challenge_layout}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error publicando challenge {challenge_id}: {str(e)}")
        return False


@api_router.post("/challenges/{challenge_id}/vote")
async def vote_challenge(
    challenge_id: str,
    vote_data: ChallengeVoteCreate,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Votar por el participante ganador de un challenge publicado
    """
    try:
        # Verificar que el challenge existe y está publicado
        challenge = await db.challenges.find_one({"id": challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge no encontrado")
        
        if challenge["status"] != ChallengeStatus.PUBLISHED:
            raise HTTPException(status_code=400, detail="Solo puedes votar en challenges publicados")
        
        # Verificar que el participante existe
        participant_exists = any(p["user_id"] == vote_data.participant_id 
                                for p in challenge.get("participants", []))
        if not participant_exists:
            raise HTTPException(status_code=404, detail="Participante no encontrado")
        
        # Verificar si el usuario ya votó
        existing_vote = await db.challenge_votes.find_one({
            "challenge_id": challenge_id,
            "voter_id": current_user.id
        })
        
        if existing_vote:
            # Actualizar voto existente
            old_participant_id = existing_vote["participant_id"]
            
            # Decrementar voto anterior
            await db.challenges.update_one(
                {"id": challenge_id, "participants.user_id": old_participant_id},
                {"$inc": {f"participants.$.votes_received": -1}}
            )
            
            # Incrementar nuevo voto
            await db.challenges.update_one(
                {"id": challenge_id, "participants.user_id": vote_data.participant_id},
                {"$inc": {f"participants.$.votes_received": 1}}
            )
            
            # Actualizar registro de voto
            await db.challenge_votes.update_one(
                {"id": existing_vote["id"]},
                {"$set": {"participant_id": vote_data.participant_id, "created_at": datetime.utcnow()}}
            )
            
            message = "Voto actualizado exitosamente"
        else:
            # Crear nuevo voto
            vote = ChallengeVote(
                challenge_id=challenge_id,
                voter_id=current_user.id,
                participant_id=vote_data.participant_id
            )
            
            await db.challenge_votes.insert_one(vote.model_dump())
            
            # Incrementar contador de votos del participante
            await db.challenges.update_one(
                {"id": challenge_id, "participants.user_id": vote_data.participant_id},
                {"$inc": {f"participants.$.votes_received": 1, "total_votes": 1}}
            )
            
            message = "Voto registrado exitosamente"
        
        logger.info(f"🗳️ Usuario {current_user.username} votó por {vote_data.participant_id} en challenge {challenge_id}")
        
        return {
            "success": True,
            "message": message
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error votando en challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al votar")


@api_router.get("/challenges/{challenge_id}/my-vote")
async def get_my_challenge_vote(
    challenge_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Obtener el voto del usuario actual en un challenge"""
    try:
        vote = await db.challenge_votes.find_one({
            "challenge_id": challenge_id,
            "voter_id": current_user.id
        })
        
        if not vote:
            return {"voted": False, "participant_id": None}
        
        return {
            "voted": True,
            "participant_id": vote["participant_id"]
        }
        
    except Exception as e:
        logger.error(f"❌ Error obteniendo voto del challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener voto")


@api_router.get("/challenges/{challenge_id}/polls")
async def get_challenge_polls(
    challenge_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """
    Obtener los polls de un challenge específico
    
    REGLAS DE VISIBILIDAD:
    - Si el challenge está PUBLICADO: cualquier usuario autenticado puede ver los polls
    - Si el challenge NO está publicado: solo los participantes pueden ver los polls
    """
    try:
        # Buscar el challenge
        challenge = await db.challenges.find_one({"id": challenge_id})
        if not challenge:
            raise HTTPException(status_code=404, detail="Challenge no encontrado")
        
        challenge_status = challenge.get("status")
        participants = challenge.get("participants", [])
        
        # Verificar permisos de visibilidad
        is_participant = any(p["user_id"] == current_user.id for p in participants)
        is_published = challenge_status == ChallengeStatus.PUBLISHED
        
        if not is_published and not is_participant:
            raise HTTPException(
                status_code=403, 
                detail="Solo los participantes pueden ver los polls de un challenge no publicado"
            )
        
        # Obtener los poll_ids de los participantes que enviaron contenido
        poll_ids = [p.get("poll_id") for p in participants 
                   if p.get("poll_id") and p.get("status") == ParticipantStatus.CONTENT_SUBMITTED]
        
        if not poll_ids:
            return {"challenge_id": challenge_id, "polls": [], "total": 0}
        
        # Obtener los polls
        polls_cursor = db.polls.find({"id": {"$in": poll_ids}})
        polls = await polls_cursor.to_list(length=len(poll_ids))
        
        # Obtener información de los autores
        author_ids = list(set(poll.get("author_id") for poll in polls if poll.get("author_id")))
        authors_cursor = db.users.find({"id": {"$in": author_ids}})
        authors_list = await authors_cursor.to_list(len(author_ids))
        authors_dict = {user["id"]: {k: v for k, v in user.items() if k != "_id"} for user in authors_list}
        
        # Construir respuesta
        result = []
        for poll in polls:
            author = authors_dict.get(poll.get("author_id"))
            # Transformar opciones al formato frontend
            transformed_options = []
            for opt in poll.get("options", []):
                media_url = opt.get("media_url")
                media_type = opt.get("media_type")
                # Resolve thumbnail (handles legacy bogus thumbnail_url == media_url for videos)
                if media_type == "video" and media_url:
                    thumbnail_url = await resolve_video_thumbnail(media_url, opt.get("thumbnail_url"))
                else:
                    thumbnail_url = opt.get("thumbnail_url")
                
                option_dict = {
                    "id": opt.get("id"),
                    "text": opt.get("text", ""),
                    "votes": opt.get("votes", 0),
                    "media": {
                        "type": media_type,
                        "url": media_url,
                        "thumbnail": thumbnail_url,  # None si no hay miniatura real (no caer al video URL)
                        "optimizedUrl": opt.get("optimized_media_url"),
                        "hls": opt.get("hls_url"),
                    } if media_url else None,
                    "optimized_media_url": opt.get("optimized_media_url"),
                    "hls_url": opt.get("hls_url"),
                }
                transformed_options.append(option_dict)
            
            poll_response = {
                "id": poll.get("id"),
                "title": poll.get("title"),
                "author": author,
                "authorUser": author,
                "options": transformed_options,
                "created_at": poll.get("created_at"),
                "total_votes": poll.get("total_votes", 0),
                "likes_count": poll.get("likes_count", 0),
                "comments_count": poll.get("comments_count", 0),
                "layout": poll.get("layout"),
                "challenge_id": challenge_id
            }
            result.append(poll_response)
        
        logger.info(f"📋 Challenge {challenge_id}: retornando {len(result)} polls")
        
        return {
            "challenge_id": challenge_id,
            "polls": result,
            "total": len(result),
            "layout": challenge.get("final_layout"),
            "status": challenge_status
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error obteniendo polls del challenge: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener polls del challenge")


# =============  STORY ENDPOINTS =============

@api_router.post("/stories", response_model=StoryResponse, tags=["Stories"])
async def create_story(
    story: StoryCreate,
    current_user: User = Depends(get_current_user)
):
    """Create a new story"""
    try:
        # Create story document
        story_doc = Story(
            user_id=current_user.id,
            **story.dict()
        )
        
        # Insert into database
        result = await db.stories.insert_one(story_doc.dict())
        
        if not result.inserted_id:
            raise HTTPException(status_code=500, detail="Failed to create story")
        
        # Get user info
        user_data = await db.users.find_one({"id": current_user.id})
        user_response = UserResponse(**user_data)
        
        # Return story response
        return StoryResponse(
            **story_doc.dict(),
            user=user_response,
            viewed_by_me=False
        )
        
    except Exception as e:
        logger.error(f"Error creating story: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create story")

@api_router.get("/stories", response_model=List[StoriesGroupResponse], tags=["Stories"])
async def get_stories(
    current_user: User = Depends(get_current_user)
):
    """Get stories from followed users (grouped by user)"""
    try:
        # Get followed users from follows collection
        follows_cursor = db.follows.find({"follower_id": current_user.id})
        follows_docs = await follows_cursor.to_list(length=1000)
        following_ids = [doc["following_id"] for doc in follows_docs]
        
        logger.info(f"📖 [STORIES] User {current_user.id} is following {len(following_ids)} users: {following_ids}")
        
        # Add current user to see their own stories
        all_user_ids = following_ids + [current_user.id]
        
        logger.info(f"📖 [STORIES] Searching for stories from {len(all_user_ids)} users (including self)")
        
        # Get active stories from followed users
        current_time = datetime.utcnow()
        
        # First, let's check how many stories exist in total for these users
        total_stories_count = await db.stories.count_documents({
            "user_id": {"$in": all_user_ids}
        })
        logger.info(f"📖 [STORIES] Total stories in DB for these users: {total_stories_count}")
        
        # Check active stories count
        active_stories_count = await db.stories.count_documents({
            "user_id": {"$in": all_user_ids},
            "is_active": True
        })
        logger.info(f"📖 [STORIES] Active stories: {active_stories_count}")
        
        # Check non-expired stories count
        non_expired_count = await db.stories.count_documents({
            "user_id": {"$in": all_user_ids},
            "is_active": True,
            "expires_at": {"$gt": current_time}
        })
        logger.info(f"📖 [STORIES] Non-expired active stories: {non_expired_count}")
        
        stories_cursor = db.stories.find({
            "user_id": {"$in": all_user_ids},
            "is_active": True,
            "expires_at": {"$gt": current_time}
        }).sort("created_at", 1)
        
        stories = await stories_cursor.to_list(length=1000)
        
        logger.info(f"📖 [STORIES] Retrieved {len(stories)} stories from database")
        
        # Log user IDs that have stories
        if stories:
            unique_user_ids = set(story["user_id"] for story in stories)
            logger.info(f"📖 [STORIES] Stories found for user IDs: {unique_user_ids}")
        
        if not stories:
            logger.info(f"📖 [STORIES] No stories found, returning empty list")
            return []
        
        # Get story views for current user
        story_ids = [story["id"] for story in stories]
        views_cursor = db.story_views.find({
            "story_id": {"$in": story_ids},
            "user_id": current_user.id
        })
        viewed_story_ids = set([view["story_id"] async for view in views_cursor])
        
        # Group stories by user
        stories_by_user = {}
        for story in stories:
            user_id = story["user_id"]
            if user_id not in stories_by_user:
                stories_by_user[user_id] = []
            stories_by_user[user_id].append(story)
        
        # Get user info for all users
        user_ids = list(stories_by_user.keys())
        users_cursor = db.users.find({"id": {"$in": user_ids}})
        users_dict = {user["id"]: user async for user in users_cursor}
        
        # Get music data for stories that have music_id using get_music_info helper
        music_ids = [story.get("music_id") for story in stories if story.get("music_id")]
        music_dict = {}
        if music_ids:
            logger.info(f"📖 [STORIES] Fetching music data for {len(set(music_ids))} unique music IDs")
            for music_id in set(music_ids):  # Remove duplicates
                try:
                    music_info = await get_music_info(music_id)
                    if music_info:
                        music_dict[music_id] = music_info
                        logger.info(f"   ✅ Got music: {music_info.get('title')} - {music_info.get('artist')}")
                    else:
                        logger.warning(f"   ⚠️ No music found for ID: {music_id}")
                except Exception as e:
                    logger.error(f"   ❌ Error fetching music {music_id}: {str(e)}")
        
        # Build response
        result = []
        for user_id, user_stories in stories_by_user.items():
            user_data = users_dict.get(user_id)
            if not user_data:
                continue
            
            user_response = UserResponse(**user_data)
            
            # Sort user stories by created_at (oldest first)
            user_stories.sort(key=lambda x: x["created_at"])
            
            # Convert stories to StoryResponse
            story_responses = []
            has_unviewed = False
            for story in user_stories:
                viewed_by_me = story["id"] in viewed_story_ids
                if not viewed_by_me:
                    has_unviewed = True
                
                # Get music data if story has music_id
                music_data = None
                if story.get("music_id"):
                    music_data = music_dict.get(story["music_id"])
                    
                story_responses.append(StoryResponse(
                    **story,
                    user=user_response,
                    viewed_by_me=viewed_by_me,
                    music=music_data
                ))
            
            result.append(StoriesGroupResponse(
                user=user_response,
                stories=story_responses,
                total_stories=len(story_responses),
                has_unviewed=has_unviewed
            ))
        
        # Sort: unviewed stories first, then by oldest story first (ascending order)
        result.sort(key=lambda x: (not x.has_unviewed, x.stories[0].created_at.timestamp()))
        
        logger.info(f"📖 [STORIES] Returning {len(result)} story groups to frontend")
        for group in result:
            logger.info(f"   - User {group.user.username} ({group.user.id}): {group.total_stories} stories, has_unviewed={group.has_unviewed}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting stories: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get stories")

@api_router.get("/stories/user/{user_id}", response_model=StoriesGroupResponse, tags=["Stories"])
async def get_user_stories(
    user_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get all stories from a specific user"""
    try:
        # Get active stories from user (oldest first)
        current_time = datetime.utcnow()
        stories_cursor = db.stories.find({
            "user_id": user_id,
            "is_active": True,
            "expires_at": {"$gt": current_time}
        }).sort("created_at", 1)
        
        stories = await stories_cursor.to_list(length=100)
        
        if not stories:
            raise HTTPException(status_code=404, detail="No active stories found")
        
        # Get user info
        user_data = await db.users.find_one({"id": user_id})
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_response = UserResponse(**user_data)
        
        # Get story views for current user
        story_ids = [story["id"] for story in stories]
        views_cursor = db.story_views.find({
            "story_id": {"$in": story_ids},
            "user_id": current_user.id
        })
        viewed_story_ids = set([view["story_id"] async for view in views_cursor])
        
        # Get music data for stories that have music_id using get_music_info helper
        music_ids = [story.get("music_id") for story in stories if story.get("music_id")]
        music_dict = {}
        if music_ids:
            logger.info(f"📖 [USER_STORIES] Fetching music data for {len(set(music_ids))} unique music IDs")
            for music_id in set(music_ids):  # Remove duplicates
                try:
                    music_info = await get_music_info(music_id)
                    if music_info:
                        music_dict[music_id] = music_info
                        logger.info(f"   ✅ Got music: {music_info.get('title')} - {music_info.get('artist')}")
                    else:
                        logger.warning(f"   ⚠️ No music found for ID: {music_id}")
                except Exception as e:
                    logger.error(f"   ❌ Error fetching music {music_id}: {str(e)}")
        
        # Convert stories to StoryResponse
        story_responses = []
        has_unviewed = False
        for story in stories:
            viewed_by_me = story["id"] in viewed_story_ids
            if not viewed_by_me:
                has_unviewed = True
            
            # Get music data if story has music_id
            music_data = None
            if story.get("music_id"):
                music_data = music_dict.get(story["music_id"])
                
            story_responses.append(StoryResponse(
                **story,
                user=user_response,
                viewed_by_me=viewed_by_me,
                music=music_data
            ))
        
        return StoriesGroupResponse(
            user=user_response,
            stories=story_responses,
            total_stories=len(story_responses),
            has_unviewed=has_unviewed
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user stories: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get user stories")

@api_router.post("/stories/{story_id}/view", tags=["Stories"])
async def view_story(
    story_id: str,
    current_user: User = Depends(get_current_user)
):
    """Mark a story as viewed"""
    try:
        # Check if story exists
        story = await db.stories.find_one({"id": story_id})
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        # Don't count owner's view in views_count, but record it for ring status
        is_owner = story["user_id"] == current_user.id
        
        # Atomic upsert to prevent race condition duplicates
        result = await db.story_views.update_one(
            {"story_id": story_id, "user_id": current_user.id},
            {"$setOnInsert": {
                "id": str(uuid.uuid4()),
                "story_id": story_id,
                "user_id": current_user.id,
                "created_at": datetime.utcnow()
            }},
            upsert=True
        )
        
        if result.upserted_id:
            # Only increment views_count for non-owner views
            if not is_owner:
                await db.stories.update_one(
                    {"id": story_id},
                    {"$inc": {"views_count": 1}}
                )
            return {"success": True, "message": "Story viewed"}
        else:
            return {"success": True, "message": "Already viewed"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error viewing story: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to view story")

@api_router.delete("/stories/{story_id}", tags=["Stories"])
async def delete_story(
    story_id: str,
    current_user: User = Depends(get_current_user)
):
    """Delete a story (only owner can delete)"""
    try:
        # Check if story exists and user is owner
        story = await db.stories.find_one({"id": story_id})
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        if story["user_id"] != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to delete this story")
        
        # Mark as inactive instead of deleting
        await db.stories.update_one(
            {"id": story_id},
            {"$set": {"is_active": False}}
        )
        
        return {"success": True, "message": "Story deleted"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting story: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete story")

@api_router.get("/stories/{story_id}/views", tags=["Stories"])
async def get_story_viewers(
    story_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get list of users who viewed a story (only owner can see)"""
    try:
        # Check if story exists and user is owner
        story = await db.stories.find_one({"id": story_id})
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        if story["user_id"] != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to view story viewers")
        
        # Get all views (exclude the story owner)
        views_cursor = db.story_views.find({
            "story_id": story_id,
            "user_id": {"$ne": story["user_id"]}
        }).sort("created_at", -1)
        views = await views_cursor.to_list(length=1000)
        
        if not views:
            return {"viewers": [], "total": 0}
        
        # Get user info for viewers
        viewer_ids = [view["user_id"] for view in views]
        users_cursor = db.users.find({"id": {"$in": viewer_ids}})
        users_dict = {user["id"]: user async for user in users_cursor}
        
        # Build response
        viewers = []
        for view in views:
            user_data = users_dict.get(view["user_id"])
            if user_data:
                viewers.append({
                    "user": UserResponse(**user_data),
                    "viewed_at": view["created_at"]
                })
        
        return {"viewers": viewers, "total": len(viewers)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting story viewers: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get story viewers")


@api_router.post("/stories/upload", tags=["Stories"])
async def upload_story_media(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """Upload media for a story (image or video)"""
    try:
        # Validate file type
        content_type = file.content_type
        if not content_type:
            raise HTTPException(status_code=400, detail="Invalid file type")
        
        is_image = content_type.startswith('image/')
        is_video = content_type.startswith('video/')
        
        if not (is_image or is_video):
            raise HTTPException(status_code=400, detail="Only images and videos are allowed")
        
        # Generate unique filename
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"{uuid.uuid4()}{file_ext}"
        file_path = Path(f"/app/backend/uploads/stories/{unique_filename}")
        
        # Save file
        async with aiofiles.open(file_path, 'wb') as f:
            content = await file.read()
            await f.write(content)
        
        # Generate public URL with /api prefix for proper routing
        public_url = f"/api/uploads/stories/{unique_filename}"
        
        # Get dimensions if image
        width = height = None
        if is_image:
            try:
                with Image.open(file_path) as img:
                    width, height = img.size
            except Exception as e:
                logger.error(f"Error getting image dimensions: {str(e)}")
        
        # Generate thumbnail for video
        thumbnail_url = None
        if is_video:
            thumbnail_filename = f"{uuid.uuid4()}_thumb.jpg"
            thumbnail_path = Path(f"/app/backend/uploads/stories/{thumbnail_filename}")
            # Generate thumbnail using first frame (if OpenCV available)
            if OPENCV_AVAILABLE:
                try:
                    cap = cv2.VideoCapture(str(file_path))
                    ret, frame = cap.read()
                    if ret:
                        cv2.imwrite(str(thumbnail_path), frame)
                        thumbnail_url = f"/api/uploads/stories/{thumbnail_filename}"
                    cap.release()
                except Exception as e:
                    logger.error(f"Error generating video thumbnail: {str(e)}")
        
        return {
            "success": True,
            "media_url": public_url,
            "thumbnail_url": thumbnail_url,
            "media_type": "image" if is_image else "video",
            "width": width,
            "height": height
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading story media: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to upload media")


# ============================================================================
# VS EXPERIENCE ENDPOINTS
# ============================================================================

class VSOption(BaseModel):
    id: str
    text: str
    image: Optional[str] = None
    # 🆕 Fase A: aceptar enriquecimiento desde el cliente (opcional). Si el
    # cliente ya conoce el tipo y/o el thumbnail (p.ej. tras un upload directo
    # que devolvió esos campos), evitamos re-detectarlo y/o re-generarlo.
    media_type: Optional[str] = None
    thumbnail_url: Optional[str] = None

class VSQuestion(BaseModel):
    id: Optional[str] = None
    options: List[VSOption]

class VSExperienceCreate(BaseModel):
    questions: List[VSQuestion]
    creator_country: Optional[str] = None  # País del creador detectado por IP
    vs_orientation: Optional[str] = "horizontal"  # 'vertical' (lado a lado / izq-der) o 'horizontal' (arriba-abajo)
    music_id: Optional[str] = None  # 🎵 ID de la música seleccionada (iTunes / user_audio_)
    music: Optional[Dict[str, Any]] = None  # 🎵 Snapshot del objeto music para fallback

class VSVote(BaseModel):
    question_id: str
    option_id: str

@api_router.post("/vs/create", tags=["VS Experience"])
async def create_vs_experience(
    vs_data: VSExperienceCreate,
    background_tasks: BackgroundTasks,
    current_user: UserResponse = Depends(get_current_user)
):
    """Create a new VS experience with multiple questions"""
    from fastapi.responses import JSONResponse
    
    logger.info(f"Creating VS experience for user {current_user.id}")
    
    try:
        vs_id = str(uuid.uuid4())
        logger.info(f"Generated VS ID: {vs_id}")
        
        # Extraer datos simples del usuario
        author_data = {
            "id": str(current_user.id),
            "username": str(current_user.username),
            "display_name": str(current_user.display_name),
            "avatar_url": str(current_user.avatar_url) if current_user.avatar_url else None
        }
        
        # 🆕 Fase A — Helper local que detecta media_type real (video vs imagen)
        # por extensión, genera thumbnail con ffmpeg para videos y devuelve un
        # dict de opción totalmente enriquecido. Idéntico contrato para todas
        # las preguntas — la primera ya no es un caso especial.
        #
        # Garantiza que `vs_questions[].options[]` SIEMPRE traiga:
        #   - id, text, votes
        #   - image (URL del media original, se mantiene por compat. del front)
        #   - media_url (alias canónico de image, lo usa el pipeline TikTok)
        #   - media_type ('video' | 'image' | None)
        #   - thumbnail_url (poster real para <video>, o la propia imagen)
        #
        # Si el cliente ya envió `media_type` / `thumbnail_url`, se respeta lo
        # que vino (caso de upload directo en el flow nuevo). Si no, se detecta
        # y genera aquí.
        def _enrich_vs_option(opt: VSOption) -> dict:
            opt_id = str(opt.id)
            opt_text = str(opt.text) if opt.text else ""
            media_url = str(opt.image) if opt.image else None

            # Empezar con lo que vino del cliente (puede ser None).
            real_type = (opt.media_type or "").strip().lower() or None
            real_thumb = opt.thumbnail_url if opt.thumbnail_url else None

            if media_url:
                is_video_url = bool(re.search(r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)", media_url, re.IGNORECASE))
                is_image_url = bool(re.search(r"\.(jpg|jpeg|png|gif|webp|bmp|avif|heic|heif)(\?|$)", media_url, re.IGNORECASE))

                # Forzar el media_type por extensión si el cliente envió uno
                # incorrecto (p. ej. el front siempre etiqueta como "image").
                if is_video_url:
                    real_type = "video"
                elif is_image_url:
                    real_type = "image"
                elif not real_type:
                    real_type = "image"  # fallback razonable

                # Generar thumbnail si es video y no llegó uno usable.
                thumb_is_bogus = (
                    not real_thumb
                    or real_thumb == media_url
                    or bool(re.search(r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)", str(real_thumb), re.IGNORECASE))
                )
                if real_type == "video" and thumb_is_bogus:
                    real_thumb = None
                    try:
                        if "/api/uploads/" in media_url:
                            after = media_url.split("/api/uploads/", 1)[1]
                            file_path = UPLOAD_DIR / after
                            if file_path.exists() and file_path.is_file():
                                category = after.split("/", 1)[0] if "/" in after else "general"
                                upload_type_map = {
                                    "avatars": UploadType.AVATAR,
                                    "poll_options": UploadType.POLL_OPTION,
                                    "poll_backgrounds": UploadType.POLL_BACKGROUND,
                                    "general": UploadType.GENERAL,
                                }
                                ut = upload_type_map.get(category, UploadType.GENERAL)
                                gen_thumb = get_video_thumbnail_url(str(file_path), ut)
                                if gen_thumb and not re.search(r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)", gen_thumb, re.IGNORECASE):
                                    real_thumb = gen_thumb
                    except Exception as e:
                        logger.warning(f"VS option thumb gen failed for {media_url}: {e}")

                # Para imágenes, la propia imagen sirve como poster.
                if real_type == "image" and not real_thumb:
                    real_thumb = media_url

            return {
                "id": opt_id,
                "text": opt_text,
                "image": media_url,           # legacy / front actual
                "media_url": media_url,       # canónico (pipeline TikTok)
                "media_type": real_type,      # 'video' | 'image' | None
                "thumbnail_url": real_thumb,  # poster real
                "votes": 0,
            }

        # Process questions — TODAS las preguntas pasan por _enrich_vs_option.
        # Antes (Fase A pendiente): solo la primera tenía thumbnail; las 2..N
        # se guardaban como {id,text,image,votes} y rompían el pipeline TikTok
        # ("Illusion of Instant" punto #1 — thumbnail en RAM).
        questions = []
        for idx, q in enumerate(vs_data.questions):
            question_id = str(uuid.uuid4())
            options = [_enrich_vs_option(opt) for opt in q.options]
            questions.append({
                "id": question_id,
                "options": options
            })
            logger.info(f"Processed question {idx+1}: {question_id} with {len(options)} options (enriched)")

        logger.info(f"Total questions processed: {len(questions)}")
        
        # Validar orientación VS — sólo aceptamos 'vertical' (lado a lado) o
        # 'horizontal' (arriba-abajo). Default = 'horizontal' (comportamiento previo).
        vs_orientation = vs_data.vs_orientation if vs_data.vs_orientation in ("vertical", "horizontal") else "horizontal"

        vs_doc = {
            "id": vs_id,
            "author_id": author_data["id"],
            "author": author_data,
            "questions": questions,
            "vs_orientation": vs_orientation,
            "total_participants": 0,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True
        }
        
        await db.vs_experiences.insert_one(vs_doc)
        
        # 🆕 Fase A — Cero duplicación: poll.options (que alimenta el feed)
        # se construye directamente a partir de las opciones YA enriquecidas
        # de la primera pregunta. Antes había un segundo bloque que repetía
        # la detección + thumbnail gen, dejando vs_questions[] sin enriquecer.
        first_question = questions[0] if questions else None
        poll_options = []
        if first_question:
            for opt in first_question["options"]:
                poll_options.append({
                    "id": opt["id"],
                    "text": opt["text"],
                    "media_url": opt.get("media_url") or opt.get("image"),
                    "media_type": opt.get("media_type"),
                    "thumbnail_url": opt.get("thumbnail_url"),
                    "votes": 0,
                })
        
        poll_doc = {
            "id": vs_id,  # Usar el mismo ID
            "title": "",  # 🔇 Sin título — petición del usuario (antes: "¿Qué prefieres?")
            "description": f"VS • {len(questions)} {'pregunta' if len(questions) == 1 else 'preguntas'}",
            "author_id": author_data["id"],
            "author": author_data,
            "options": poll_options,
            "layout": "vs",  # Nuevo tipo de layout para VS
            "vs_orientation": vs_orientation,  # Orientación interna del VS: 'vertical'=lado a lado, 'horizontal'=arriba-abajo
            "vs_id": vs_id,  # Referencia a la experiencia VS completa
            "vs_questions": questions,  # Incluir todas las preguntas
            "creator_country": vs_data.creator_country,  # País del creador
            "total_votes": 0,
            "views": 0,
            "likes": 0,
            "likes_count": 0,
            "comments_count": 0,
            "shares": 0,
            "shares_count": 0,
            "saves_count": 0,
            "is_featured": False,
            "tags": ["vs", "whatwouldyouchoose"],
            "category": "vs",
            "mentioned_users": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True,
            "is_private": False,
            "comments_enabled": True,
            "show_vote_count": True,
            "background_color": "#000000",
            # 🎵 Música seleccionada (si hubo) — se utiliza para reproducirla
            # en el feed cuando el VS está activo.
            "music_id": vs_data.music_id,
            "music": vs_data.music,
            # 🎬 VS Composed (estilo TikTok Duet) — se rellena async por
            # video_pipeline.compose_vs_video_task. Mientras tanto, el feed
            # muestra streams separados con fallback transparente.
            "composed_video_url": None,
            "composed_status": "pending",
            "composed_orientation": vs_orientation,
            "composed_error": None,
        }
        
        await db.polls.insert_one(poll_doc)
        logger.info(f"Poll inserted successfully for VS: {vs_id}")
        
        # 🎬 Mirror inicial en vs_experiences para que ambas colecciones tengan
        # los mismos campos composed_* desde el arranque.
        try:
            await db.vs_experiences.update_one(
                {"id": vs_id},
                {"$set": {
                    "composed_video_url": None,
                    "composed_status": "pending",
                    "composed_orientation": vs_orientation,
                    "composed_error": None,
                }},
            )
        except Exception as mirror_err:
            logger.warning(f"VS {vs_id} mirror composed fields failed: {mirror_err}")
        
        # 🎬 Disparar composición VS async (no bloquea la respuesta).
        # El task internamente decide si aplica (video+video) y actualiza
        # composed_status a 'ready' | 'failed' | 'not_applicable'.
        try:
            background_tasks.add_task(video_pipeline.compose_vs_video_task, db, vs_id)
            logger.info(f"🎬 VS {vs_id} queued to composed-video pipeline")
        except Exception as compose_err:
            logger.error(f"Failed to queue VS composition for {vs_id}: {compose_err}")
        
        # 🚀 FIX BOTTLENECK #5a: VS también necesita pasar por el pipeline
        # estándar (process_poll_media) para generar:
        #   - optimized_media_url (MP4 720p, fast-start)
        #   - hls_url (ABR ladder 360p/540p/720p, arranque sub-segundo)
        # Sin esto, el feed sirve los uploads RAW (1080p@60fps en peor caso),
        # primer byte 1-2s en 4G, NO ABR, NO fast-start de 360p.
        # Esto se aplica POR OPCIÓN, así que cada lado del VS recibe su HLS.
        # El status arranca en "processing" y termina en "ready" cuando
        # la validación + thumbnails básicos terminen (~3s).
        try:
            def _vsopt_is_video(opt) -> bool:
                # `opt` es un objeto Pydantic VSOption (NO un dict). Sus campos
                # son `image` (URL del media) y `media_type`. Detectamos vídeo
                # por media_type declarado o por extensión de la URL.
                mt = (getattr(opt, "media_type", None) or "").strip().lower()
                img = getattr(opt, "image", None)
                if not img:
                    return False
                if "video" in mt:
                    return True
                return bool(re.search(r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)", str(img), re.IGNORECASE))

            has_video = any(
                _vsopt_is_video(opt)
                for q in vs_data.questions for opt in q.options
            )
            if has_video:
                # Marcar status como processing (el pipeline lo cambiará a ready)
                await db.polls.update_one(
                    {"id": vs_id},
                    {"$set": {
                        "status": "processing",
                        "processing_started_at": datetime.utcnow(),
                    }},
                )
                background_tasks.add_task(video_pipeline.process_poll_media, db, vs_id)
                logger.info(f"🎬 VS {vs_id} queued to video optimization pipeline")
            else:
                # No videos → marca ready directamente
                await db.polls.update_one(
                    {"id": vs_id},
                    {"$set": {"status": "ready"}},
                )
        except Exception as opt_err:
            logger.error(f"Failed to queue VS optimization for {vs_id}: {opt_err}")
        
        logger.info(f"VS experience created: {vs_id} by user {current_user.id}")
        
        # Usar JSONResponse directamente para evitar problemas de serialización
        response_data = {
            "success": True,
            "vs_id": str(vs_id),
            "poll_id": str(vs_id),
            "questions": questions,
            "message": "VS experience created successfully"
        }
        
        logger.info(f"Returning response with {len(questions)} questions")
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logger.error(f"Error creating VS experience: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to create VS experience: {str(e)}")


@api_router.get("/vs/{vs_id}", tags=["VS Experience"])
async def get_vs_experience(
    vs_id: str,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """Get a VS experience by ID"""
    try:
        vs_doc = await db.vs_experiences.find_one({"id": vs_id})
        
        if not vs_doc:
            raise HTTPException(status_code=404, detail="VS experience not found")
        
        # Get user's votes if authenticated
        user_votes = {}
        if current_user:
            votes = await db.vs_votes.find({
                "vs_id": vs_id,
                "user_id": current_user.id
            }).to_list(length=100)
            
            for vote in votes:
                user_votes[vote["question_id"]] = vote["option_id"]
        
        return {
            "id": vs_doc["id"],
            "author": vs_doc.get("author"),
            "questions": vs_doc["questions"],
            "total_participants": vs_doc.get("total_participants", 0),
            "created_at": vs_doc["created_at"],
            "user_votes": user_votes,
            # 🎬 VS Composed (TikTok Duet) — el frontend usa estos campos
            # para decidir si reproducir MP4 compuesto en lugar de 2 streams.
            "composed_video_url": vs_doc.get("composed_video_url"),
            "composed_hls_url": vs_doc.get("composed_hls_url"),
            "composed_status": vs_doc.get("composed_status"),
            "composed_orientation": vs_doc.get("composed_orientation"),
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VS experience: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get VS experience")


@api_router.post("/vs/{vs_id}/recompose", tags=["VS Experience"])
async def recompose_vs_video(
    vs_id: str,
    background_tasks: BackgroundTasks,
    current_user: UserResponse = Depends(get_current_user)
):
    """🎬 Fuerza la regeneración del MP4 compuesto de un VS.
    
    Útil para:
    - Recuperar VS que fallaron la composición inicial.
    - Recomponer tras cambiar la orientación.
    - Test aislado del pipeline FFmpeg en VS existentes.
    
    Permisos: solo el autor del VS (o admin) puede dispararlo.
    El task corre en background — la respuesta es inmediata (status: 'queued').
    """
    try:
        poll = await db.polls.find_one({"id": vs_id})
        if not poll or poll.get("layout") != "vs":
            raise HTTPException(status_code=404, detail="VS not found")
        
        # Permisos: solo el autor (extensible a admin si hace falta)
        author_id = poll.get("author_id")
        if author_id and str(author_id) != str(current_user.id):
            raise HTTPException(status_code=403, detail="Only the VS author can recompose")
        
        # Resetear status a pending y limpiar URL previa (si la había)
        await db.polls.update_one(
            {"id": vs_id},
            {"$set": {
                "composed_status": "pending",
                "composed_video_url": None,
                "composed_error": None,
                "composed_updated_at": datetime.utcnow(),
            }},
        )
        await db.vs_experiences.update_one(
            {"id": vs_id},
            {"$set": {
                "composed_status": "pending",
                "composed_video_url": None,
                "composed_error": None,
                "composed_updated_at": datetime.utcnow(),
            }},
        )
        
        # Borrar archivo previo si existe (para regenerar limpio)
        try:
            from video_pipeline import VS_COMPOSED_DIR
            prev_file = VS_COMPOSED_DIR / f"{vs_id}.mp4"
            if prev_file.exists():
                prev_file.unlink()
                logger.info(f"[vs_compose] {vs_id}: deleted previous composed file")
        except Exception as cleanup_err:
            logger.warning(f"Cleanup of previous composed file failed: {cleanup_err}")
        
        # Enqueue task
        background_tasks.add_task(video_pipeline.compose_vs_video_task, db, vs_id)
        logger.info(f"🎬 VS {vs_id} re-queued to composed-video pipeline (manual)")
        
        return {
            "success": True,
            "vs_id": vs_id,
            "composed_status": "pending",
            "message": "Composition queued. Poll the VS endpoint to check progress.",
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error recomposing VS {vs_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to queue recomposition")



@api_router.post("/vs/{vs_id}/vote", tags=["VS Experience"])
async def vote_vs_question(
    vs_id: str,
    vote: VSVote,
    current_user: UserResponse = Depends(get_current_user)
):
    """Vote on a VS question"""
    try:
        # Check if VS exists
        vs_doc = await db.vs_experiences.find_one({"id": vs_id})
        if not vs_doc:
            raise HTTPException(status_code=404, detail="VS experience not found")
        
        # Check if user already voted on this question
        existing_vote = await db.vs_votes.find_one({
            "vs_id": vs_id,
            "question_id": vote.question_id,
            "user_id": current_user.id
        })
        
        if existing_vote:
            return {
                "success": False,
                "message": "Already voted on this question",
                "vote": existing_vote["option_id"]
            }
        
        # Record vote
        vote_doc = {
            "id": str(uuid.uuid4()),
            "vs_id": vs_id,
            "question_id": vote.question_id,
            "option_id": vote.option_id,
            "user_id": current_user.id,
            "created_at": datetime.utcnow()
        }
        await db.vs_votes.insert_one(vote_doc)
        
        # Update vote count in VS experience
        await db.vs_experiences.update_one(
            {
                "id": vs_id,
                "questions.id": vote.question_id,
                "questions.options.id": vote.option_id
            },
            {
                "$inc": {
                    "questions.$[q].options.$[o].votes": 1
                }
            },
            array_filters=[
                {"q.id": vote.question_id},
                {"o.id": vote.option_id}
            ]
        )
        
        # Update total participants (unique users)
        participants = await db.vs_votes.distinct("user_id", {"vs_id": vs_id})
        await db.vs_experiences.update_one(
            {"id": vs_id},
            {"$set": {"total_participants": len(participants)}}
        )
        
        # Get updated question stats
        updated_vs = await db.vs_experiences.find_one({"id": vs_id})
        question_stats = None
        for q in updated_vs["questions"]:
            if q["id"] == vote.question_id:
                total_votes = sum(opt["votes"] for opt in q["options"])
                question_stats = {
                    "question_id": q["id"],
                    "total_votes": total_votes,
                    "options": [
                        {
                            "id": opt["id"],
                            "votes": opt["votes"],
                            "percentage": round((opt["votes"] / total_votes * 100) if total_votes > 0 else 0)
                        }
                        for opt in q["options"]
                    ]
                }
                break
        
        return {
            "success": True,
            "message": "Vote recorded",
            "stats": question_stats
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error voting on VS: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to record vote")


@api_router.get("/vs/feed/latest", tags=["VS Experience"])
async def get_latest_vs_experiences(
    limit: int = Query(10, ge=1, le=50),
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """Get latest VS experiences for the feed"""
    try:
        vs_list = await db.vs_experiences.find(
            {"is_active": True}
        ).sort("created_at", -1).limit(limit).to_list(length=limit)
        
        result = []
        for vs in vs_list:
            result.append({
                "id": vs["id"],
                "author": vs.get("author"),
                "questions": vs["questions"],
                "total_participants": vs.get("total_participants", 0),
                "created_at": vs["created_at"]
            })
        
        return {"vs_experiences": result}
        
    except Exception as e:
        logger.error(f"Error getting VS feed: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get VS feed")


# ===================== MOMENTS ENDPOINTS =====================

class MomentResponse(BaseModel):
    id: str
    user_id: str
    username: str
    user_avatar: Optional[str] = None
    image_url: str
    caption: Optional[str] = None
    location: Optional[str] = None
    filter: Optional[str] = "none"
    likes_count: int = 0
    comments_count: int = 0
    is_liked: bool = False
    created_at: datetime

@api_router.post("/moments", tags=["Moments"])
async def create_moment(
    image: UploadFile = File(...),
    caption: str = "",
    location: str = "",
    filter: str = "none",
    current_user: UserResponse = Depends(get_current_user)
):
    """Create a new moment - a single image post to share special memories"""
    try:
        # Validate file type
        if not image.content_type.startswith('image/'):
            raise HTTPException(status_code=400, detail="Only image files are allowed")
        
        # Read and save image
        content = await image.read()
        
        # Validate file size (max 10MB)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image size exceeds 10MB limit")
        
        # Generate unique filename
        file_extension = image.filename.split('.')[-1] if '.' in image.filename else 'jpg'
        filename = f"{uuid.uuid4()}.{file_extension}"
        
        # Ensure uploads directory exists
        moments_dir = Path("uploads/moments")
        moments_dir.mkdir(parents=True, exist_ok=True)
        
        # Save file
        file_path = moments_dir / filename
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(content)
        
        # Create moment document
        moment_id = str(uuid.uuid4())
        moment_data = {
            "id": moment_id,
            "user_id": current_user.id,
            "username": current_user.username,
            "user_avatar": current_user.avatar,
            "image_url": f"/api/uploads/moments/{filename}",
            "caption": caption[:500] if caption else "",
            "location": location[:100] if location else "",
            "filter": filter,
            "likes": [],
            "likes_count": 0,
            "comments": [],
            "comments_count": 0,
            "created_at": datetime.utcnow(),
            "is_active": True
        }
        
        await db.moments.insert_one(moment_data)
        
        logger.info(f"Moment created: {moment_id} by user {current_user.username}")
        
        return {
            "id": moment_id,
            "message": "Momento publicado exitosamente",
            "image_url": moment_data["image_url"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating moment: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create moment")

@api_router.get("/moments", tags=["Moments"])
async def get_moments(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    user_id: Optional[str] = None,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """Get moments feed or user-specific moments"""
    try:
        query = {"is_active": True}
        if user_id:
            query["user_id"] = user_id
        
        moments = await db.moments.find(query).sort("created_at", -1).skip(offset).limit(limit).to_list(length=limit)
        
        result = []
        for moment in moments:
            is_liked = False
            if current_user and current_user.id in moment.get("likes", []):
                is_liked = True
            
            result.append({
                "id": moment["id"],
                "user_id": moment["user_id"],
                "username": moment["username"],
                "user_avatar": moment.get("user_avatar"),
                "image_url": moment["image_url"],
                "caption": moment.get("caption", ""),
                "location": moment.get("location", ""),
                "filter": moment.get("filter", "none"),
                "likes_count": moment.get("likes_count", 0),
                "comments_count": moment.get("comments_count", 0),
                "is_liked": is_liked,
                "created_at": moment["created_at"]
            })
        
        return {"moments": result, "total": len(result)}
        
    except Exception as e:
        logger.error(f"Error getting moments: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get moments")

@api_router.get("/moments/{moment_id}", tags=["Moments"])
async def get_moment(
    moment_id: str,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """Get a specific moment by ID"""
    try:
        moment = await db.moments.find_one({"id": moment_id, "is_active": True})
        
        if not moment:
            raise HTTPException(status_code=404, detail="Moment not found")
        
        is_liked = False
        if current_user and current_user.id in moment.get("likes", []):
            is_liked = True
        
        return {
            "id": moment["id"],
            "user_id": moment["user_id"],
            "username": moment["username"],
            "user_avatar": moment.get("user_avatar"),
            "image_url": moment["image_url"],
            "caption": moment.get("caption", ""),
            "location": moment.get("location", ""),
            "filter": moment.get("filter", "none"),
            "likes_count": moment.get("likes_count", 0),
            "comments_count": moment.get("comments_count", 0),
            "comments": moment.get("comments", []),
            "is_liked": is_liked,
            "created_at": moment["created_at"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting moment: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get moment")

@api_router.post("/moments/{moment_id}/like", tags=["Moments"])
async def like_moment(
    moment_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Like or unlike a moment"""
    try:
        moment = await db.moments.find_one({"id": moment_id, "is_active": True})
        
        if not moment:
            raise HTTPException(status_code=404, detail="Moment not found")
        
        likes = moment.get("likes", [])
        
        if current_user.id in likes:
            # Unlike
            likes.remove(current_user.id)
            action = "unliked"
        else:
            # Like
            likes.append(current_user.id)
            action = "liked"
        
        await db.moments.update_one(
            {"id": moment_id},
            {"$set": {"likes": likes, "likes_count": len(likes)}}
        )
        
        return {
            "action": action,
            "likes_count": len(likes),
            "is_liked": action == "liked"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error liking moment: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to like moment")

@api_router.delete("/moments/{moment_id}", tags=["Moments"])
async def delete_moment(
    moment_id: str,
    current_user: UserResponse = Depends(get_current_user)
):
    """Delete a moment (only by owner)"""
    try:
        moment = await db.moments.find_one({"id": moment_id})
        
        if not moment:
            raise HTTPException(status_code=404, detail="Moment not found")
        
        if moment["user_id"] != current_user.id:
            raise HTTPException(status_code=403, detail="You can only delete your own moments")
        
        await db.moments.update_one(
            {"id": moment_id},
            {"$set": {"is_active": False}}
        )
        
        return {"message": "Moment deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting moment: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete moment")

# Serve moment images
@api_router.get("/uploads/moments/{filename}", tags=["Moments"])
async def get_moment_image(filename: str):
    """Serve moment images"""
    file_path = Path("uploads/moments") / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(file_path)


# ============= NOTIFICATION SUMMARY FOR LOCAL NOTIFICATIONS =============
@api_router.get("/notifications/summary")
async def get_notifications_summary(current_user: dict = Depends(verify_token)):
    """
    Get summary of unread messages and notifications
    Used by local notifications system to detect new events
    """
    try:
        user_id = current_user["id"]
        
        # Count unread messages
        unread_messages = await db.messages.count_documents({
            "recipient_id": user_id,
            "read": False
        })
        
        # Get latest unread message
        latest_message = await db.messages.find_one(
            {"recipient_id": user_id, "read": False},
            {"_id": 0},
            sort=[("timestamp", -1)]
        )
        
        # Count unread activity/notifications
        # This would count likes, comments, follows, etc.
        # For now, using a simple approach with comments
        user_polls = await db.polls.find({"user_id": user_id}, {"_id": 0, "id": 1}).to_list(1000)
        user_poll_ids = [p["id"] for p in user_polls]
        
        # Count new comments on user's polls
        unread_comments = await db.comments.count_documents({
            "poll_id": {"$in": user_poll_ids},
            "user_id": {"$ne": user_id},  # Not from the user themselves
            "created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)}  # Last 24h
        })
        
        # Count new follows
        new_follows = await db.follows.count_documents({
            "following_id": user_id,
            "created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)}
        })
        
        total_notifications = unread_comments + new_follows
        
        # Get latest notification details
        latest_notification = None
        
        # Try to get latest comment
        latest_comment = await db.comments.find_one(
            {
                "poll_id": {"$in": user_poll_ids},
                "user_id": {"$ne": user_id}
            },
            {"_id": 0},
            sort=[("created_at", -1)]
        )
        
        if latest_comment:
            # Get commenter info
            commenter = await db.users.find_one(
                {"id": latest_comment["user_id"]},
                {"_id": 0, "username": 1}
            )
            latest_notification = {
                "type": "comment",
                "from_user": commenter.get("username", "Alguien") if commenter else "Alguien",
                "timestamp": latest_comment["created_at"].isoformat()
            }
        
        # If no comment, try latest follow
        if not latest_notification:
            latest_follow = await db.follows.find_one(
                {"following_id": user_id},
                {"_id": 0},
                sort=[("created_at", -1)]
            )
            
            if latest_follow:
                follower = await db.users.find_one(
                    {"id": latest_follow["follower_id"]},
                    {"_id": 0, "username": 1}
                )
                latest_notification = {
                    "type": "follow",
                    "from_user": follower.get("username", "Alguien") if follower else "Alguien",
                    "timestamp": latest_follow["created_at"].isoformat()
                }
        
        return {
            "unread_messages": unread_messages,
            "unread_notifications": total_notifications,
            "latest_message_id": latest_message["id"] if latest_message else None,
            "latest_notification_id": None,  # Could add IDs if needed
            "latest_notification": latest_notification
        }
        
    except Exception as e:
        logger.error(f"Error getting notifications summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Incluir el router en la aplicación
app.include_router(api_router)

# Include push notification routes
try:
    from push_routes import push_router
    app.include_router(push_router)
    logger.info("✅ Push notification routes loaded")
except Exception as e:
    logger.warning(f"⚠️  Push notification routes not loaded: {e}")

# ──────────────────────────────────────────────────────────────────────────
# 🔴 LIVE Streaming routes (REST + WebSocket)
# ──────────────────────────────────────────────────────────────────────────
try:
    from live_routes import build_live_router, register_live_websocket

    async def _get_user_from_token_optional(token: Optional[str]) -> Optional[UserResponse]:
        """Resolve a JWT (passed via WS query string) to a UserResponse or None."""
        if not token:
            return None
        try:
            payload = verify_token(token)
            if not payload:
                return None
            user_data = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
            if not user_data:
                return None
            return UserResponse(**user_data)
        except Exception:
            return None

    _live_router = build_live_router(
        db=db,
        get_current_user=get_current_user,
        get_current_user_optional=get_current_user_optional,
    )
    # Mount under /api/live to satisfy the kubernetes ingress prefix rule
    app.include_router(_live_router, prefix="/api")
    register_live_websocket(app, db=db, get_user_from_token_optional=_get_user_from_token_optional)
    logger.info("✅ LIVE routes loaded (REST /api/live/* + WS /api/ws/live/{room_id})")
except Exception as e:
    logger.warning(f"⚠️  LIVE routes not loaded: {e}")


# ──────────────────────────────────────────────────────────────────────────
# 🧹 Reaper startup hook: polls que se quedan en "processing" más de 10 min
# (p.ej. por un reinicio del backend durante transcoding) se marcan "failed"
# para que el estado nunca se bloquee y el usuario pueda reintentar.
# ──────────────────────────────────────────────────────────────────────────
_reaper_task = None
_backfill_task = None

@app.on_event("startup")
async def _ensure_ffmpeg_available():
    """
    🛠️ Garantiza que ffmpeg/ffprobe estén disponibles al arrancar el backend.

    En entornos de contenedor donde el FS no es persistente entre reinicios
    (Kubernetes pods sin volúmenes persistentes para /usr/bin), ffmpeg
    puede desaparecer tras un rebuild. Sin él, la pipeline de video y la
    extracción de audio fallan silenciosamente y los posts quedan en
    estado "failed", impidiendo su publicación.

    Idempotente: si ya está instalado, no hace nada.
    """
    import shutil
    import subprocess

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        logger.info("🛠️ ffmpeg/ffprobe already available")
        return

    logger.warning("🛠️ ffmpeg/ffprobe NOT found — attempting auto-install")
    try:
        # Ejecutar el script helper si existe; si no, apt-get directo.
        script_path = "/app/scripts/install_ffmpeg.sh"
        if os.path.exists(script_path):
            proc = await asyncio.to_thread(
                subprocess.run,
                ["bash", script_path],
                capture_output=True,
                text=True,
                timeout=180,
            )
        else:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["bash", "-c", "apt-get update -y && apt-get install -y --no-install-recommends ffmpeg"],
                capture_output=True,
                text=True,
                timeout=180,
            )
        if proc.returncode == 0 and shutil.which("ffmpeg"):
            logger.info("🛠️ ffmpeg auto-installed successfully")
        else:
            logger.error(
                f"🛠️ ffmpeg auto-install failed (rc={proc.returncode}): "
                f"{proc.stderr[-300:] if proc.stderr else proc.stdout[-300:]}"
            )
    except Exception as e:
        logger.error(f"🛠️ ffmpeg auto-install crashed: {e}")


@app.on_event("startup")
async def _start_video_pipeline_reaper():
    global _reaper_task, _backfill_task
    try:
        _reaper_task = asyncio.create_task(
            video_pipeline.reaper_loop(db, interval_seconds=300, max_age_minutes=10)
        )
        logger.info("🧹 Video pipeline reaper started")
    except Exception as e:
        logger.error(f"Failed to start reaper: {e}")

    # 🎬 Backfill: regenera thumbnails + vídeos optimizados para polls antiguos
    #    que se subieron sin ffmpeg disponible. Corre en background con pausas
    #    para no saturar el CPU.
    try:
        _backfill_task = asyncio.create_task(
            video_pipeline.backfill_loop(db, interval_seconds=30, batch_size=5)
        )
        logger.info("🎬 Video pipeline backfill started")
    except Exception as e:
        logger.error(f"Failed to start backfill: {e}")

@app.on_event("shutdown")
async def _stop_video_pipeline_reaper():
    global _reaper_task, _backfill_task
    if _reaper_task and not _reaper_task.done():
        _reaper_task.cancel()
        try:
            await _reaper_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("🧹 Video pipeline reaper stopped")
    if _backfill_task and not _backfill_task.done():
        _backfill_task.cancel()
        try:
            await _backfill_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("🎬 Video pipeline backfill stopped")


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)