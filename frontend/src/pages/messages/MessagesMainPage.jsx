import React, { useState, useEffect, useCallback } from 'react';
import { motion } from 'framer-motion';
import { MessageCircle, X, ArrowLeft, Users, Bell, Send, Plus, Inbox, User } from 'lucide-react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import AppConfig from '../../config/config.js';
import { resolveAssetUrl } from '../../utils/resolveAssetUrl';
import useLivePoll from '../../hooks/useLivePoll';
import { useTranslation } from '../../hooks/useTranslation';
import DefaultAvatarSvg from '../../components/common/DefaultAvatarSvg';

const MessagesMainPage = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();
  
  // Debug: Log inicial
  console.log('🔍 MessagesMainPage - URL actual:', location.pathname + location.search);
  console.log('🔍 MessagesMainPage - Usuario:', user?.username);
  const [conversations, setConversations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedConversation, setSelectedConversation] = useState(null);
  const [showChat, setShowChat] = useState(false);
  const [messages, setMessages] = useState([]);
  const [newMessage, setNewMessage] = useState('');
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [showNewChatModal, setShowNewChatModal] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [segmentData, setSegmentData] = useState({
    followers: { count: 0, loading: true },
    activity: { count: 0, loading: true },
    messages: { count: 0, loading: true }
  });

  // Debug: Track message state changes
  useEffect(() => {
    console.log('📊 Message state updated:', {
      messageCount: messages.length,
      selectedConversation: selectedConversation?.id,
      messages: messages.map(m => ({ 
        id: m.id, 
        content: m.content.substring(0, 30) + '...', 
        sender: m.sender?.username,
        status: m.status 
      }))
    });
  }, [messages, selectedConversation]);

  // Función para hacer peticiones autenticadas
  const apiRequest = async (endpoint, options = {}) => {
    const token = localStorage.getItem('token');
    const headers = {
      'Content-Type': 'application/json',
      ...(token && { 'Authorization': `Bearer ${token}` }),
      ...options.headers
    };

    const response = await fetch(`${AppConfig.BACKEND_URL}${endpoint}`, {
      ...options,
      headers
    });

    if (!response.ok) {
      // Try to get error message from response body
      let errorMessage = `HTTP ${response.status}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch (e) {
        console.warn('Could not parse error response body:', e);
      }
      
      const error = new Error(errorMessage);
      error.status = response.status;
      throw error;
    }

    return response.json();
  };

  // Helper function for avatar rendering with error handling
  const renderAvatar = (avatarUrl, displayName, username, size = 'w-8 h-8') => {
    return (
      <div className={`${size} rounded-full bg-gray-200 flex items-center justify-center flex-shrink-0 relative overflow-hidden`}>
        {avatarUrl ? (
          <>
            <img 
              src={avatarUrl} 
              alt="Avatar" 
              className="w-full h-full rounded-full object-cover"
              onError={(e) => {
                console.warn('Avatar failed to load:', avatarUrl);
                e.target.style.display = 'none';
                const fallback = e.target.parentNode.querySelector('.avatar-fallback');
                if (fallback) fallback.style.display = 'flex';
              }}
            />
            <div className="avatar-fallback w-full h-full rounded-full overflow-hidden items-center justify-center" style={{ display: 'none' }}>
              <DefaultAvatarSvg className="w-full h-full" />
            </div>
          </>
        ) : (
          <div className="w-full h-full rounded-full overflow-hidden flex items-center justify-center">
            <DefaultAvatarSvg className="w-full h-full" />
          </div>
        )}
      </div>
    );
  };

  // Función para buscar usuarios
  const searchUsers = async (query) => {
    if (!query.trim()) {
      setSearchResults([]);
      return;
    }

    try {
      setSearchLoading(true);
      const results = await apiRequest(`/api/users/search?q=${encodeURIComponent(query)}`);
      // Filtrar el usuario actual de los resultados
      const filteredResults = results.filter(result => result.id !== user?.id);
      setSearchResults(filteredResults);
    } catch (error) {
      console.error('Error searching users:', error);
      setSearchResults([]);
    } finally {
      setSearchLoading(false);
    }
  };

  // Función para iniciar conversación con un usuario
  const startConversation = async (selectedUser) => {
    try {
      setShowNewChatModal(false);
      setSearchQuery('');
      setSearchResults([]);

      // Crear una conversación temporal para mostrar la interfaz de chat
      const tempConversation = {
        id: null, // null indica que es una conversación nueva
        participants: [
          {
            id: selectedUser.id,
            username: selectedUser.username,
            display_name: selectedUser.display_name || selectedUser.username,
            avatar_url: selectedUser.avatar_url
          }
        ],
        last_message: null,
        last_message_at: null,
        unread_count: 0
      };

      setSelectedConversation(tempConversation);
      setShowChat(true);
      setMessages([]);

      console.log('🔍 Starting conversation with:', selectedUser.username);

    } catch (error) {
      console.error('Error starting conversation:', error);
    }
  };

  // Función para cerrar el modal y limpiar búsqueda
  const closeNewChatModal = () => {
    setShowNewChatModal(false);
    setSearchQuery('');
    setSearchResults([]);
  };

  // Función para obtener avatar del usuario
  const getAvatarForUser = (user) => {
    if (!user) return '👤';
    
    if (user.avatar_url) {
      return user.avatar_url;
    }
    
    if (user.display_name || user.username) {
      const name = user.display_name || user.username;
      return name.charAt(0).toUpperCase();
    }
    
    return '👤';
  };

  // Función para formatear tiempo
  const formatTimeForInbox = (dateString) => {
    if (!dateString) return '';
    
    try {
      const now = new Date();
      // Asegurar que el dateString se interprete como UTC si no tiene 'Z'
      const dateStr = dateString.endsWith('Z') ? dateString : dateString + 'Z';
      const date = new Date(dateStr);
      
      // Validar que la fecha es válida
      if (isNaN(date.getTime())) return '';
      
      const diffMs = now - date;
      const diffMins = Math.floor(diffMs / (1000 * 60));
      const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
      const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

      if (diffMins < 1) return 'ahora';
      if (diffMins < 60) return `${diffMins}m`;
      if (diffHours < 24) return `${diffHours}h`;
      if (diffDays < 7) return `${diffDays}d`;
      
      return date.toLocaleDateString('es-ES', { 
        day: 'numeric', 
        month: 'short' 
      });
    } catch (error) {
      console.error('Error formatting time:', error);
      return '';
    }
  };

  // Cargar conversaciones
  const loadConversations = async () => {
    try {
      setLoading(true);
      const conversationsData = await apiRequest('/api/conversations');
      setConversations(conversationsData);
    } catch (error) {
      console.log('Error loading conversations:', error.message);
      setConversations([]);
    } finally {
      setLoading(false);
    }
  };

  // Cargar datos de badges para navegación (solo no leídos)
  const loadSegmentData = async () => {
    try {
      let followersCount = 0;
      let activityCount = 0;
      let messageRequestsCount = 0;

      // Cargar conteo de seguidores NO LEÍDOS
      try {
        const followersResponse = await apiRequest('/api/users/followers/unread-count');
        followersCount = followersResponse?.unread_count || 0;
        console.log('✅ Followers unread count:', followersCount);
      } catch (e) {
        console.log('Followers unread count API not available, falling back to total count');
        try {
          const followersResponse = await apiRequest('/api/users/followers/recent');
          followersCount = followersResponse?.length || 0;
        } catch (e2) {
          console.log('Followers API not available');
        }
      }

      // Cargar conteo de actividad NO LEÍDA
      try {
        const activityResponse = await apiRequest('/api/users/activity/unread-count');
        activityCount = activityResponse?.unread_count || 0;
        console.log('✅ Activity unread count:', activityCount);
      } catch (e) {
        console.log('Activity unread count API not available, falling back to total count');
        try {
          const activityResponse = await apiRequest('/api/users/activity/recent');
          activityCount = activityResponse?.length || 0;
        } catch (e2) {
          console.log('Activity API not available');
        }
      }

      // Cargar conteo de solicitudes NO LEÍDAS
      try {
        const requestsResponse = await apiRequest('/api/messages/requests/unread-count');
        messageRequestsCount = requestsResponse?.unread_count || 0;
        console.log('✅ Message requests unread count:', messageRequestsCount);
      } catch (e) {
        console.log('Message requests unread count API not available, falling back to total count');
        try {
          const requestsResponse = await apiRequest('/api/messages/requests');
          messageRequestsCount = requestsResponse?.length || 0;
        } catch (e2) {
          console.log('Message requests API not available');
        }
      }

      setSegmentData({
        followers: { count: followersCount, loading: false },
        activity: { count: activityCount, loading: false },
        messages: { count: messageRequestsCount, loading: false }
      });

    } catch (error) {
      console.log('Error loading segment data:', error.message);
      setSegmentData({
        followers: { count: 0, loading: false },
        activity: { count: 0, loading: false },
        messages: { count: 0, loading: false }
      });
    }
  };

  // Cargar datos al montar
  useEffect(() => {
    console.log('🚀 MessagesMainPage montado, usuario:', user?.username);
    if (user) {
      loadConversations();
      loadSegmentData();
    }
  }, [user]);

  // 🔴 LIVE REFRESH — refrescar lista de conversaciones y badges cada 10s.
  // Solo cuando NO está abierto el chat (si el chat está abierto, MessagesPage ya poll).
  const livePollInbox = useCallback(() => {
    if (!user) return;
    if (showChat) return; // evita doble polling cuando hay chat abierto
    loadConversations();
    loadSegmentData();
  }, [user, showChat]);

  useLivePoll(livePollInbox, 10000, {
    enabled: Boolean(user) && !showChat,
    pauseWhenHidden: true,
    refreshOnFocus: true,
  });

  // Refrescar badges cuando la página vuelve a enfocarse (al volver de subpáginas)
  useEffect(() => {
    const handleFocus = () => {
      if (user) loadSegmentData();
    };
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  }, [user]);

  // Refrescar badges cuando cambia la ruta (volver de subpáginas)
  useEffect(() => {
    if (user && location.pathname === '/messages') {
      loadSegmentData();
    }
  }, [location.pathname, user]);

  // Manejar apertura de conversación desde navegación
  useEffect(() => {
    if (location.state?.openConversation) {
      setSelectedConversation(location.state.openConversation);
      setShowChat(true);
      // Limpiar el state
      navigate(location.pathname, { replace: true });
    }
  }, [location.state, navigate, location.pathname]);

  // Estado para manejar navegación directa a usuario
  const [pendingUserToOpen, setPendingUserToOpen] = useState(null);
  
  // Cache para búsquedas de usuarios para evitar rate limiting
  const [userSearchCache, setUserSearchCache] = useState({});
  
  // Estado para estadísticas del usuario en chat
  const [userStats, setUserStats] = useState({});
  
  // Estado temporal para debug en móvil
  const [chatDebugInfo, setChatDebugInfo] = useState(null);

  // Función de búsqueda de usuarios con cache y rate limiting protection
  const searchUserWithCache = async (username) => {
    const cacheKey = username.toLowerCase();
    
    // Check cache first (expires after 5 minutes)
    if (userSearchCache[cacheKey] && 
        Date.now() - userSearchCache[cacheKey].timestamp < 5 * 60 * 1000) {
      console.log('📋 Using cached user search result for:', username);
      return userSearchCache[cacheKey].data;
    }

    try {
      console.log('🔍 Making API search for user:', username);
      const users = await apiRequest(`/api/users/search?q=${encodeURIComponent(username)}`);
      
      // Cache the result
      setUserSearchCache(prev => ({
        ...prev,
        [cacheKey]: {
          data: users,
          timestamp: Date.now()
        }
      }));
      
      return users;
    } catch (error) {
      if (error.message.includes('429')) {
        // Rate limit error - try to use stale cache if available
        if (userSearchCache[cacheKey]) {
          console.log('⚠️ Rate limited - using stale cache for:', username);
          return userSearchCache[cacheKey].data;
        }
        throw new Error('Demasiadas búsquedas. Intenta de nuevo en unos momentos.');
      }
      throw error;
    }
  };

  // Detectar parámetro user en URL inmediatamente
  useEffect(() => {
    const urlParams = new URLSearchParams(location.search);
    const targetUsername = urlParams.get('user');
    
    console.log('🔍 useEffect URL - Parámetro user detectado:', targetUsername);
    console.log('🔍 useEffect URL - location.search:', location.search);
    
    if (targetUsername) {
      setPendingUserToOpen(targetUsername);
      // Limpiar la URL inmediatamente
      navigate('/messages', { replace: true });
    }
  }, [location.search, navigate]);

  // Procesar usuario pendiente cuando las conversaciones estén listas (with debouncing)
  useEffect(() => {
    if (pendingUserToOpen && user) {
      console.log('🔍 Procesando usuario pendiente:', pendingUserToOpen);
      console.log('🔍 Conversaciones disponibles:', conversations.length);
      
      // Debounce the processing to prevent rapid API calls
      const timeoutId = setTimeout(() => {
        // Actualizar debug info
        setChatDebugInfo({
          pendingUser: pendingUserToOpen,
          currentUser: user.username,
          conversationsCount: conversations.length,
          timestamp: new Date().toLocaleTimeString()
        });
        
        // Buscar conversación existente
        const existingConversation = conversations.find(conv => {
          const otherUser = conv.participants?.find(p => p.id !== user?.id);
          const found = otherUser?.username === pendingUserToOpen;
          if (found) {
            console.log('✅ Conversación encontrada con:', otherUser.username);
            console.log('🔍 Conversación completa:', conv);
            console.log('🔍 Otros participantes:', otherUser);
          }
          return found;
        });

        if (existingConversation) {
          console.log('✅ Abriendo conversación existente:', existingConversation.id);
          setSelectedConversation(existingConversation);
          setShowChat(true);
        } else {
          console.log('🆕 Creando nueva conversación con:', pendingUserToOpen);
          console.log('🔍 Usuario actual para nueva conversación:', user.username, user.id);
          handleStartNewConversationWithUser(pendingUserToOpen);
        }
        
        // Limpiar usuario pendiente
        setPendingUserToOpen(null);
      }, 300); // 300ms debounce

      // Cleanup function to clear timeout
      return () => clearTimeout(timeoutId);
    }
  }, [pendingUserToOpen, conversations, user]);

  // Cargar mensajes de una conversación
  const loadMessages = async (conversationId) => {
    try {
      console.log('📥 Cargando mensajes para conversación:', conversationId);
      
      // Si es una conversación nueva (id empieza con 'new-'), no hay mensajes que cargar
      if (conversationId.startsWith('new-')) {
        setMessages([]);
        return;
      }
      
      // Si es una solicitud de chat (id empieza con 'request-'), usar endpoint de chat requests
      let messagesData;
      if (conversationId.startsWith('request-')) {
        const requestId = conversationId.replace('request-', '');
        console.log('📨 Cargando mensajes de solicitud de chat:', requestId);
        messagesData = await apiRequest(`/api/chat-requests/${requestId}/messages`);
      } else {
        // Conversación normal
        messagesData = await apiRequest(`/api/conversations/${conversationId}/messages`);
      }
      
      console.log('✅ Mensajes cargados:', messagesData?.length || 0);
      setMessages(messagesData || []);
    } catch (error) {
      console.error('❌ Error cargando mensajes:', error);
      setMessages([]);
    }
  };

  // Cargar estadísticas del usuario
  const loadUserStats = async (userId) => {
    try {
      console.log('📊 Cargando estadísticas para usuario:', userId);
      console.log('📊 Tipo de userId:', typeof userId);
      console.log('📊 UserStats cache actual:', userStats);
      
      // Si ya tenemos las estadísticas cached, no recargar
      if (userStats[userId]) {
        console.log('📊 Estadísticas encontradas en cache:', userStats[userId]);
        return userStats[userId];
      }
      
      // Cargar estadísticas del usuario desde el backend
      console.log('📊 Haciendo request a API:', `/api/user/profile/${userId}`);
      const userProfile = await apiRequest(`/api/user/profile/${userId}`);
      console.log('📊 Respuesta del API completa:', userProfile);
      
      // Extraer estadísticas del perfil del usuario
      const stats = {
        votes: userProfile.total_votes || 0,
        followers: userProfile.followers_count || 0,
        following: userProfile.following_count || 0,
        votes_made: userProfile.votes_count || 0
      };
      
      console.log('✅ Estadísticas procesadas:', stats);
      
      // Cachear las estadísticas
      setUserStats(prev => {
        const newStats = {
          ...prev,
          [userId]: stats
        };
        console.log('📊 Actualizando cache con:', newStats);
        return newStats;
      });
      
      return stats;
    } catch (error) {
      console.error('❌ Error cargando estadísticas completo:', error);
      console.error('❌ Error message:', error.message);
      console.error('❌ Error stack:', error.stack);
      
      // Retornar estadísticas por defecto en caso de error
      const defaultStats = {
        votes: 0,
        followers: 0,
        following: 0,
        votes_made: 0
      };
      
      console.log('📊 Usando estadísticas por defecto:', defaultStats);
      
      // Cachear las estadísticas por defecto para evitar llamadas repetidas
      setUserStats(prev => ({
        ...prev,
        [userId]: defaultStats
      }));
      
      return defaultStats;
    }
  };

  // Cuando se selecciona una conversación, cargar estadísticas del otro usuario
  useEffect(() => {
    console.log('🔄 useEffect selectedConversation cambió:', selectedConversation);
    
    if (selectedConversation) {
      console.log('🔄 Conversation ID:', selectedConversation.id);
      console.log('🔄 Participants completos:', selectedConversation.participants);
      console.log('🔄 User actual completo:', user);
      
      // Buscar el otro participante (no el usuario actual)
      const otherUser = selectedConversation.participants?.find(p => {
        console.log('🔄 Comparando participant:', p.id, 'vs user:', user?.id);
        return p.id !== user?.id;
      });
      
      console.log('🔄 Other user encontrado:', otherUser);
      
      if (otherUser && otherUser.id) {
        console.log('🔄 Cargando estadísticas para:', otherUser.id, otherUser.username);
        loadUserStats(otherUser.id);
      } else {
        console.warn('⚠️ No se pudo encontrar otherUser o no tiene ID válido');
        console.warn('⚠️ Participants:', selectedConversation.participants);
        console.warn('⚠️ User ID:', user?.id);
      }
      
      loadMessages(selectedConversation.id);
    }
  }, [selectedConversation?.id, user]); // Solo reaccionar cuando el ID cambia, no todo el objeto
  const handleSendMessage = async () => {
    if (!newMessage.trim() || !selectedConversation) return;

    const messageContent = newMessage.trim();
    const tempMessageId = `temp-${Date.now()}`;
    
    // Determinar el destinatario ANTES del try para que esté disponible en el catch
    let recipient = selectedConversation.participants?.find(p => p.id !== user.id);
    
    // Crear mensaje temporal para mostrar inmediatamente en la UI
    const tempMessage = {
      id: tempMessageId,
      content: messageContent,
      sender_id: user.id,
      timestamp: new Date().toISOString(),
      sender: {
        id: user.id,
        username: user.username,
        display_name: user.display_name,
        avatar_url: user.avatar_url
      },
      status: 'sending' // Estado temporal
    };

    try {
      console.log('📤 Enviando mensaje:', messageContent);
      console.log('🔍 Conversación actual:', selectedConversation);
      
      // Agregar mensaje temporal a la UI inmediatamente
      setMessages(prevMessages => [...prevMessages, tempMessage]);
      setNewMessage('');
      
      console.log('🔍 Debug recipient:', {
        conversationId: selectedConversation.id,
        isNewConversation: selectedConversation.isNewConversation,
        participants: selectedConversation.participants,
        userId: user.id,
        recipient: recipient,
        recipientId: recipient?.id
      });

      if (!recipient) {
        throw new Error('No se pudo encontrar el destinatario');
      }

      if (!recipient.id) {
        throw new Error('El destinatario no tiene ID válido');
      }
      
      if (!messageContent || messageContent.length === 0) {
        throw new Error('El mensaje no puede estar vacío');
      }
      
      if (messageContent.length > 1000) {
        throw new Error('El mensaje es demasiado largo (máximo 1000 caracteres)');
      }
      
      // Verificar que recipient.id sea un UUID válido
      const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
      if (!uuidRegex.test(recipient.id)) {
        throw new Error(`ID del destinatario tiene formato inválido: ${recipient.id}`);
      }

      // Enviar mensaje al backend - EL BACKEND CREARÁ LA CONVERSACIÓN AUTOMÁTICAMENTE
      const messagePayload = {
        recipient_id: recipient.id,
        content: messageContent
      };
      
      console.log('📤 Payload enviando al backend:', messagePayload);
      console.log('🔍 Tipo de recipient.id:', typeof recipient.id);
      console.log('🔍 Valor exacto recipient.id:', JSON.stringify(recipient.id));
      console.log('🔍 Tipo de content:', typeof messageContent);
      console.log('🔍 Valor exacto content:', JSON.stringify(messageContent));
      console.log('🔍 Usuario actual:', user.id, user.username);
      
      try {
        const response = await apiRequest('/api/messages', {
          method: 'POST',
          body: JSON.stringify(messagePayload)
        });

        console.log('✅ Respuesta del servidor:', response);
        
        // Manejar diferentes tipos de respuesta del backend
        if (response.type === 'chat_request') {
          // El mensaje se convirtió en una solicitud de chat
          console.log('📨 Solicitud de chat enviada:', response.request_id);
          
          // NO eliminar el mensaje temporal, sino actualizarlo para mostrar que es una solicitud
          setMessages(prevMessages =>
            prevMessages.map(msg =>
              msg.id === tempMessageId
                ? { 
                    ...msg, 
                    id: `chat-request-${response.request_id}`, // ID único para la solicitud
                    status: 'chat_request', // Estado especial para solicitudes de chat
                    isPending: true // Marcar como pendiente
                  }
                : msg
            )
          );
          
          // Agregar mensaje informativo del sistema DESPUÉS del mensaje del usuario
          const chatRequestMessage = {
            id: `system-${Date.now()}`,
            content: '📨 Tu mensaje fue enviado como solicitud de chat. El usuario debe aceptarla para que puedan intercambiar mensajes.',
            sender_id: 'system',
            isSystemMessage: true,
            created_at: new Date().toISOString()
          };
          
          setMessages(prevMessages => [...prevMessages, chatRequestMessage]);
          
          // Agregar la conversación a la lista si no existe
          const conversationExists = conversations.some(conv => 
            conv.participants?.some(p => p.id === recipient.id)
          );
          
          if (!conversationExists) {
            const newConversation = {
              id: selectedConversation.id || `temp-${Date.now()}`,
              participants: [recipient],
              last_message: messageContent,
              last_message_at: new Date().toISOString(),
              unread_count: 0,
              created_at: new Date().toISOString(),
              isPending: true // Marcar como solicitud pendiente
            };
            
            setConversations(prevConversations => [newConversation, ...prevConversations]);
            console.log('📋 Conversación agregada a la lista con solicitud pendiente');
          }
          
          // NO cerrar la conversación automáticamente - dejar que el usuario la cierre
          // El usuario puede seguir viendo su mensaje y el estado de la solicitud
          
        } else if (response.message_id) {
          // Mensaje enviado normalmente
          
          // Si era una conversación nueva, actualizar con los datos reales del backend
          if (selectedConversation.isNewConversation && response.conversation_id) {
            console.log('🔄 Actualizando conversación nueva con ID real:', response.conversation_id);
            setSelectedConversation(prev => ({
              ...prev,
              id: response.conversation_id,
              isNewConversation: false
            }));
          }
          
          // Actualizar el mensaje temporal con la respuesta del servidor
          setMessages(prevMessages =>
            prevMessages.map(msg =>
              msg.id === tempMessageId
                ? { 
                    ...msg, // Keep original temp message data (includes sender info)
                    id: response.message_id, // Use server-provided ID
                    timestamp: response.timestamp || msg.timestamp, // Use server timestamp if available
                    status: 'sent' // Mark as sent
                  }
                : msg
            )
          );

          // Actualizar la conversación con el último mensaje
          setSelectedConversation(prev => ({
            ...prev,
            last_message: {
              content: messageContent,
              timestamp: response.timestamp,
              sender_id: user.id
            }
          }));

          // Agregar o actualizar la conversación en la lista
          const conversationIndex = conversations.findIndex(conv => 
            conv.id === response.conversation_id || 
            conv.participants?.some(p => p.id === recipient.id)
          );
          
          if (conversationIndex === -1) {
            // Conversación nueva - agregar a la lista
            const newConversation = {
              id: response.conversation_id,
              participants: [recipient],
              last_message: messageContent,
              last_message_at: response.timestamp,
              unread_count: 0,
              created_at: new Date().toISOString()
            };
            
            setConversations(prevConversations => [newConversation, ...prevConversations]);
            console.log('📋 Nueva conversación agregada a la lista');
          } else {
            // Conversación existente - actualizar
            setConversations(prevConversations => {
              const updated = [...prevConversations];
              updated[conversationIndex] = {
                ...updated[conversationIndex],
                last_message: messageContent,
                last_message_at: response.timestamp
              };
              // Mover al inicio de la lista
              const [movedConv] = updated.splice(conversationIndex, 1);
              return [movedConv, ...updated];
            });
            console.log('📋 Conversación actualizada en la lista');
          }
        }
      } catch (error) {
        console.error('❌ Error enviando mensaje COMPLETO:', error);
        console.error('❌ Error message:', error.message);
        console.error('❌ Error stack:', error.stack);
        
        // Verificar si es error 422 específicamente
        if (error.message && error.message.includes('422')) {
          console.error('❌ Error 422 - Datos enviados:');
          console.error('  - messagePayload:', messagePayload);
          console.error('  - recipient.id tipo:', typeof recipient.id);
          console.error('  - recipient.id valor:', recipient.id);
          console.error('  - content tipo:', typeof messageContent);
          console.error('  - content valor:', messageContent);
        }
        
        throw error;
      }

    } catch (error) {
      console.error('❌ Error enviando mensaje COMPLETO:', error);
      console.error('❌ Error message:', error.message);
      console.error('❌ Error status:', error.status);
      console.error('❌ Error stack:', error.stack);
      console.error('❌ Error typeof:', typeof error);
      console.error('❌ Error keys:', Object.keys(error));
      
      // Eliminar mensaje temporal solo si NO es el error de "ya enviado"
      const isAlreadySentError = error.status === 403 && error.message && error.message.includes('Chat request already sent');
      
      if (!isAlreadySentError) {
        setMessages(prevMessages =>
          prevMessages.filter(msg => msg.id !== tempMessageId)
        );
      }

      // Manejar errores específicos
      if (error.status === 403 && error.message && error.message.includes('Chat request already sent')) {
        // El backend devolvió 403 con "Chat request already sent"
        console.log('📨 Chat request ya enviado - manteniendo mensaje con estado pendiente');
        
        // NO eliminar el mensaje, sino actualizarlo a estado de solicitud pendiente
        setMessages(prevMessages =>
          prevMessages.map(msg =>
            msg.id === tempMessageId
              ? { 
                  ...msg, 
                  id: `chat-request-pending-${Date.now()}`,
                  status: 'chat_request',
                  isPending: true
                }
              : msg
          )
        );
        
        const chatRequestPendingMessage = {
          id: `system-${Date.now()}`,
          content: 'Ya enviaste una solicitud de chat a este usuario. Espera a que la acepte para poder intercambiar mensajes.',
          sender_id: 'system',
          isSystemMessage: true,
          created_at: new Date().toISOString()
        };
        
        setMessages(prevMessages => [...prevMessages, chatRequestPendingMessage]);
        
        // Agregar la conversación a la lista si no existe
        const conversationExists = conversations.some(conv => 
          conv.participants?.some(p => p.id === recipient.id)
        );
        
        if (!conversationExists) {
          const newConversation = {
            id: selectedConversation.id || `pending-${recipient.id}`,
            participants: [recipient],
            last_message: messageContent,
            last_message_at: new Date().toISOString(),
            unread_count: 0,
            created_at: new Date().toISOString(),
            isPending: true // Marcar como solicitud pendiente
          };
          
          setConversations(prevConversations => [newConversation, ...prevConversations]);
          console.log('📋 Conversación agregada a la lista (solicitud ya enviada previamente)');
        }
        
        // NO cerrar la conversación - dejar que el usuario la cierre manualmente
        // El usuario puede seguir viendo su mensaje y el estado de la solicitud
        
      } else if (error.status === 403) {
        // Otros errores 403
        const permissionMessage = {
          id: `system-${Date.now()}`,
          content: '🚫 No tienes permiso para enviar mensajes a este usuario.',
          sender_id: 'system',
          isSystemMessage: true,
          created_at: new Date().toISOString()
        };
        
        setMessages(prevMessages => [...prevMessages, permissionMessage]);
        
      } else {
        // Otros errores - marcar mensaje como fallido y mostrar error genérico
        setMessages(prevMessages =>
          prevMessages.map(msg =>
            msg.id === tempMessageId
              ? { ...msg, status: 'failed' }
              : msg
          )
        );

        // Mejor manejo del mensaje de error
        let errorMessage = 'Error desconocido al enviar mensaje';
        
        if (error.message && typeof error.message === 'string') {
          errorMessage = error.message;
        } else if (error.status) {
          errorMessage = `Error del servidor: HTTP ${error.status}`;
        } else if (typeof error === 'string') {
          errorMessage = error;
        }
        
        console.error('📱 Mostrando error al usuario:', errorMessage);
        alert(`Error al enviar mensaje: ${errorMessage}`);
      }
    }
  };

  // Función para manejar envío con Enter
  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && newMessage.trim()) {
      handleSendMessage();
    }
  };

  // Función para manejar aceptar/rechazar solicitud de chat
  const handleChatRequestAction = async (action) => {
    if (!selectedConversation?.chat_request_id) return;
    
    try {
      console.log(`📨 ${action === 'accept' ? 'Aceptando' : 'Rechazando'} solicitud de chat:`, selectedConversation.chat_request_id);
      
      const response = await apiRequest(`/api/chat-requests/${selectedConversation.chat_request_id}`, {
        method: 'PUT',
        body: JSON.stringify({ action })
      });
      
      console.log('✅ Respuesta del servidor:', response);
      
      if (action === 'accept') {
        // Recargar conversaciones para obtener la conversación real
        await loadConversations();
        
        // Si se devolvió un conversation_id, seleccionarlo
        if (response.conversation_id) {
          const conversations = await apiRequest('/api/conversations');
          const newConv = conversations.find(c => c.id === response.conversation_id);
          if (newConv) {
            setSelectedConversation(newConv);
            await loadMessages(newConv.id);
          }
        }
        
        alert('✅ Solicitud aceptada. Ahora pueden chatear libremente.');
      } else {
        // Cerrar la conversación y recargar lista
        setSelectedConversation(null);
        await loadConversations();
        alert('❌ Solicitud rechazada.');
      }
    } catch (error) {
      console.error('❌ Error al procesar solicitud de chat:', error);
      alert('Error al procesar la solicitud. Por favor intenta de nuevo.');
    }
  };

  // Función para cancelar solicitud de chat (para el sender)
  const handleCancelChatRequest = async () => {
    if (!selectedConversation?.chat_request_id) return;
    
    try {
      console.log('🗑️ Cancelando solicitud de chat:', selectedConversation.chat_request_id);
      
      await apiRequest(`/api/chat-requests/${selectedConversation.chat_request_id}`, {
        method: 'DELETE'
      });
      
      console.log('✅ Solicitud cancelada');
      
      // Cerrar la conversación y recargar lista
      setSelectedConversation(null);
      await loadConversations();
      alert('🗑️ Solicitud cancelada.');
    } catch (error) {
      console.error('❌ Error al cancelar solicitud:', error);
      alert('Error al cancelar la solicitud. Por favor intenta de nuevo.');
    }
  };

  const handleStartNewConversationWithUser = async (username) => {
    try {
      console.log('🔍 === INICIANDO BÚSQUEDA DE USUARIO ===');
      console.log('🔍 Username buscado:', username);
      console.log('🔍 Tipo de username:', typeof username);
      console.log('🔍 Length de username:', username?.length);
      console.log('🔍 Usuario actual (user):', user);
      
      // VALIDACIÓN CRÍTICA: No buscar si es el mismo usuario
      if (username === user.username || username === user.display_name) {
        console.error('❌ Error: Intentando buscar al mismo usuario actual');
        alert('No puedes crear una conversación contigo mismo');
        return;
      }
      
      // Buscar el usuario por username usando cache
      const users = await searchUserWithCache(username);
      console.log('📝 === RESULTADOS DE BÚSQUEDA ===');
      console.log('📝 Número de usuarios encontrados:', users?.length || 0);
      console.log('📝 Resultados completos:', users);
      
      // El backend ya excluye al usuario actual, pero agregamos validación por seguridad
      const validUsers = users.filter(u => u.id !== user.id);
      console.log('📝 === DESPUÉS DE FILTRO DE SEGURIDAD ===');
      console.log('📝 Usuarios válidos (sin usuario actual):', validUsers.length);
      console.log('📝 Usuarios válidos:', validUsers.map(u => ({ id: u.id, username: u.username, display_name: u.display_name })));
      
      // Buscar usuario target con coincidencia más flexible
      const searchTerm = username.toLowerCase().trim();
      const targetUser = validUsers.find(u => {
        const matchUsername = u.username?.toLowerCase().trim() === searchTerm;
        const matchDisplayName = u.display_name?.toLowerCase().trim() === searchTerm;
        const partialUsername = u.username?.toLowerCase().includes(searchTerm);
        const partialDisplayName = u.display_name?.toLowerCase().includes(searchTerm);
        
        console.log(`🔍 Comparando con usuario ${u.username}:`, {
          searchTerm,
          username: u.username?.toLowerCase().trim(),
          display_name: u.display_name?.toLowerCase().trim(),
          matchUsername,
          matchDisplayName,
          partialUsername,
          partialDisplayName
        });
        
        return matchUsername || matchDisplayName || partialUsername || partialDisplayName;
      });
      
      console.log('📝 === RESULTADO DE MATCHING ===');
      console.log('📝 Target user encontrado:', targetUser);
      
      if (targetUser) {
        console.log('✅ Usuario encontrado:', targetUser);
        console.log('🔍 Comparación usuarios:');
        console.log('  - Usuario actual:', user.username, user.id);
        console.log('  - Usuario target:', targetUser.username, targetUser.id);
        
        // VALIDACIÓN DOBLE: Verificar que no es el mismo usuario
        if (targetUser.id === user.id) {
          console.error('❌ Error: Target user es el mismo que el usuario actual');
          alert('No puedes crear una conversación contigo mismo');
          return;
        }
        
        // En lugar de crear conversación simulada, usar datos reales para envío directo
        const realConversation = {
          id: null, // Sin ID - se creará en backend al enviar primer mensaje  
          participants: [
            {
              id: user.id,
              username: user.username,
              display_name: user.display_name || user.username,
              avatar_url: user.avatar_url
            },
            {
              id: targetUser.id,
              username: targetUser.username,
              display_name: targetUser.display_name || targetUser.username,
              avatar_url: targetUser.avatar_url
            }
          ],
          last_message: {
            content: '',
            timestamp: new Date().toISOString(),
            sender_id: user.id
          },
          unread_count: 0,
          isNewConversation: true // Flag para identificar conversaciones nuevas
        };
        
        console.log('✅ Conversación real preparada:', realConversation);
        console.log('🔍 Participantes de la conversación:');
        console.log(`  1. ${user.username} (${user.id}) - Usuario actual`);
        console.log(`  2. ${targetUser.username} (${targetUser.id}) - Usuario target`);
        
        setSelectedConversation(realConversation);
        setShowChat(true);
      } else {
        console.error('❌ === USUARIO NO ENCONTRADO ===');
        console.error('❌ Username buscado:', username);
        console.error('❌ Usuarios disponibles:');
        validUsers.forEach((u, index) => {
          console.error(`  ${index + 1}. ID: ${u.id}, Username: "${u.username}", Display: "${u.display_name}"`);
        });
        console.error('❌ Usuarios originales:');
        users.forEach((u, index) => {
          console.error(`  ${index + 1}. ID: ${u.id}, Username: "${u.username}", Display: "${u.display_name}"`);
        });
        
        // Mostrar mensaje de error más detallado al usuario
        alert(`No se pudo encontrar al usuario "${username}". Los usuarios disponibles son: ${validUsers.map(u => u.username).join(', ')}`);
      }
    } catch (error) {
      console.error('❌ Error buscando usuario:', error);
      // Mostrar mensaje de error al usuario con mensaje más amigable
      if (error.message.includes('Demasiadas búsquedas')) {
        alert(error.message);
      } else {
        alert(`Error al buscar usuario: ${error.message}`);
      }
    }
  };

  // Función para obtener badge count
  const getSegmentBadgeCount = (segmentId) => {
    const data = segmentData[segmentId];
    if (data?.loading) return '...';
    if (!data?.count || data.count === 0) return '';
    return data.count > 99 ? '99+' : data.count.toString();
  };

  // Manejar clic en conversación
  const handleConversationClick = (conversation) => {
    setSelectedConversation(conversation);
    setShowChat(true);
  };

  // Cerrar chat
  const handleCloseChat = () => {
    setShowChat(false);
    setSelectedConversation(null);
    setMessages([]);
  };

  return (
    <div className="flex flex-col h-full bg-white overflow-x-hidden">
      {!showChat ? (
        <>
          {/* Header */}
          <div className="flex-shrink-0 bg-white px-1 py-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center">
                <Inbox className="h-6 w-6 text-blue-500 mr-2" />
                <h1 className="text-xl font-semibold text-gray-900">{t('inbox.title')}</h1>
              </div>
              <button 
                onClick={() => setShowNewChatModal(true)}
                className="p-2 hover:bg-gray-100 rounded-full transition-colors"
              >
                <Plus className="h-5 w-5 text-gray-600" />
              </button>
            </div>
          </div>

          {/* Navigation Segments */}
          <div className="flex-shrink-0 bg-white px-0 py-3">
            <div className="flex items-center justify-between gap-1.5">
              {/* Navigation to other pages */}
              <button
                onClick={() => navigate('/messages/followers')}
                className="px-4 py-2.5 rounded-full flex items-center gap-2 transition-colors relative flex-1 justify-center hover:bg-gray-50"
              >
                <Users className="w-4 h-4 text-gray-500" strokeWidth={1.5} />
                <span className="text-sm font-medium text-gray-700">{t('inbox.newTab')}</span>
                {getSegmentBadgeCount('followers') && (
                  <span className="w-2 h-2 bg-red-500 rounded-full absolute -top-0.5 -right-0.5" />
                )}
              </button>

              <button
                onClick={() => navigate('/messages/activity')}
                className="px-4 py-2.5 rounded-full flex items-center gap-2 transition-colors relative flex-1 justify-center hover:bg-gray-50"
              >
                <Bell className="w-4 h-4 text-gray-500" strokeWidth={1.5} />
                <span className="text-sm font-medium text-gray-700">{t('inbox.activityTab')}</span>
                {getSegmentBadgeCount('activity') && (
                  <span className="w-2 h-2 bg-red-500 rounded-full absolute -top-0.5 -right-0.5" />
                )}
              </button>

              <button
                onClick={() => navigate('/messages/requests')}
                className="px-4 py-2.5 rounded-full flex items-center gap-2 transition-colors relative flex-1 justify-center hover:bg-gray-50"
              >
                <MessageCircle className="w-4 h-4 text-gray-500" strokeWidth={1.5} />
                <span className="text-sm font-medium text-gray-700">{t('inbox.requestsTab')}</span>
                {getSegmentBadgeCount('messages') && (
                  <span className="w-2 h-2 bg-red-500 rounded-full absolute -top-0.5 -right-0.5" />
                )}
              </button>
            </div>
          </div>

          {/* Conversations List */}
          <div className="flex-1 overflow-y-auto overflow-x-hidden">
            {loading ? (
              <div className="flex items-center justify-center h-full">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
              </div>
            ) : conversations.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full text-center px-6 bg-white">
                <div className="w-16 h-16 bg-blue-100 rounded-full flex items-center justify-center mb-4">
                  <MessageCircle className="h-8 w-8 text-blue-500" />
                </div>
                <h3 className="text-lg font-semibold text-gray-900 mb-2">
                  {t('chat.title')}
                </h3>
                <p className="text-gray-500">
                  {t('chat.emptyDescription')}
                </p>
              </div>
            ) : (
              <div className="px-2 py-2 flex flex-col gap-2">
                {conversations.map((conversation, index) => {
                  const otherUser = conversation.participants?.find(p => p.id !== user?.id) || conversation.participants?.[0];
                  if (!otherUser) return null;

                  return (
                    <motion.button
                      key={conversation.id}
                      initial={{ opacity: 0, y: 20 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: index * 0.05 }}
                      onClick={() => handleConversationClick(conversation)}
                      className="w-full flex items-center gap-3 p-4 rounded-2xl bg-gray-50 hover:bg-gray-100 active:bg-gray-200 transition-colors"
                      style={{ touchAction: 'manipulation' }}
                    >
                      {/* Avatar */}
                      <div className="w-12 h-12 rounded-full flex-shrink-0 relative overflow-hidden bg-white shadow-sm">
                        {otherUser.avatar_url ? (
                          <>
                            <img 
                              src={resolveAssetUrl(otherUser.avatar_url)}
                              alt="Avatar" 
                              className="w-full h-full rounded-full object-cover"
                              onError={(e) => {
                                e.target.style.display = 'none';
                                e.target.parentNode.querySelector('.avatar-fallback').style.display = 'flex';
                              }}
                            />
                            <div className="avatar-fallback w-full h-full rounded-full flex items-center justify-center bg-gray-50" style={{ display: 'none' }}>
                              <User className="w-6 h-6 text-gray-400" />
                            </div>
                          </>
                        ) : (
                          <div className="w-full h-full rounded-full flex items-center justify-center bg-gray-50">
                            <User className="w-6 h-6 text-gray-400" />
                          </div>
                        )}
                      </div>
                      
                      {/* Content */}
                      <div className="flex-1 min-w-0 text-left">
                        <div className="flex items-center justify-between mb-0.5">
                          <span className="text-sm font-semibold truncate text-gray-900">
                            {otherUser.display_name || otherUser.username || 'Usuario'}
                          </span>
                          <span className="text-xs text-gray-400 ml-2 flex-shrink-0">
                            {formatTimeForInbox(conversation.last_message_at || conversation.created_at)}
                          </span>
                        </div>
                        <p className="text-xs text-gray-500 truncate">
                          {conversation.isPending 
                            ? 'Solicitud de chat enviada' 
                            : conversation.last_message || 'Iniciar conversación'
                          }
                        </p>
                      </div>

                      {/* Unread Badge */}
                      {conversation.unread_count > 0 && (
                        <div className="w-2.5 h-2.5 rounded-full bg-red-500 flex-shrink-0" />
                      )}
                      
                      {/* Pending Badge */}
                      {conversation.isPending && (
                        <div className="min-w-[22px] h-[22px] rounded-full flex items-center justify-center flex-shrink-0 bg-white border border-gray-200">
                          <span className="text-[10px] text-gray-500 font-bold px-1.5">P</span>
                        </div>
                      )}
                    </motion.button>
                  );
                })}
              </div>
            )}
          </div>
        </>
      ) : (
        /* Chat View */
        <div className="flex flex-col h-full bg-white">
          {/* Chat Header */}
          <div className="flex-shrink-0 bg-white px-4 py-3">
            <div className="flex items-center">
              <button
                onClick={handleCloseChat}
                className="w-9 h-9 rounded-full hover:bg-gray-100 flex items-center justify-center transition-colors mr-3"
              >
                <ArrowLeft className="h-5 w-5 text-gray-700" strokeWidth={1.5} />
              </button>
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-full bg-gray-50 flex items-center justify-center relative overflow-hidden shadow-sm">
                  {(() => {
                    const otherUser = selectedConversation?.participants?.find(p => p.id !== user?.id);
                    return otherUser?.avatar_url ? (
                      <>
                        <img 
                          src={resolveAssetUrl(otherUser.avatar_url)} 
                          alt="Avatar" 
                          className="w-full h-full rounded-full object-cover"
                          onError={(e) => {
                            e.target.style.display = 'none';
                            e.target.parentNode.querySelector('.avatar-fallback').style.display = 'flex';
                          }}
                        />
                        <div className="avatar-fallback w-full h-full rounded-full flex items-center justify-center" style={{ display: 'none' }}>
                          <User className="w-5 h-5 text-gray-400" />
                        </div>
                      </>
                    ) : (
                      <User className="w-5 h-5 text-gray-400" />
                    );
                  })()}
                </div>
                <h2 className="text-base font-semibold text-gray-900">
                  {(() => {
                    const otherUser = selectedConversation?.participants?.find(p => p.id !== user?.id);
                    return otherUser?.display_name || otherUser?.username || 'Usuario';
                  })()}
                </h2>
              </div>
            </div>
          </div>

          {/* Perfil Central */}
          <div className="flex-shrink-0 bg-white px-4 py-6">
            <div className="flex flex-col items-center text-center">
              {/* Logo principal del perfil */}
              <div className="w-20 h-20 rounded-full bg-gray-50 flex items-center justify-center mb-4 relative overflow-hidden shadow-sm">
                {(() => {
                  const otherUser = selectedConversation?.participants?.find(p => p.id !== user?.id);
                  return otherUser?.avatar_url ? (
                    <>
                      <img 
                        src={resolveAssetUrl(otherUser.avatar_url)} 
                        alt="Avatar" 
                        className="w-full h-full rounded-full object-cover"
                        onError={(e) => {
                          e.target.style.display = 'none';
                          e.target.parentNode.querySelector('.avatar-fallback').style.display = 'flex';
                        }}
                      />
                      <div className="avatar-fallback w-full h-full rounded-full flex items-center justify-center" style={{ display: 'none' }}>
                        <User className="w-10 h-10 text-gray-400" />
                      </div>
                    </>
                  ) : (
                    <User className="w-10 h-10 text-gray-400" />
                  );
                })()}
              </div>
              
              {/* Nombre del perfil */}
              <h3 className="text-xl font-semibold text-gray-900 mb-1">
                {(() => {
                  const otherUser = selectedConversation?.participants?.find(p => p.id !== user?.id);
                  return otherUser?.display_name || otherUser?.username || 'Usuario';
                })()}
              </h3>
              
              {/* Nombre de usuario en gris claro */}
              <p className="text-base text-gray-400 mb-2">
                @{(() => {
                  const otherUser = selectedConversation?.participants?.find(p => p.id !== user?.id);
                  return otherUser?.username || 'usuario';
                })()}
              </p>
              
              {/* Estadísticas en gris medio y tamaño pequeño */}
              <div className="text-sm text-gray-500">
                {(() => {
                  const otherUser = selectedConversation?.participants?.find(p => p.id !== user?.id);
                  console.log('🎯 Renderizando estadísticas - otherUser:', otherUser);
                  console.log('🎯 UserStats actual:', userStats);
                  
                  const stats = otherUser ? userStats[otherUser.id] : null;
                  console.log('🎯 Stats encontradas para', otherUser?.id, ':', stats);
                  
                  if (stats) {
                    const displayText = `${stats.votes} voto${stats.votes !== 1 ? 's' : ''} • ${stats.followers} seguidor${stats.followers !== 1 ? 'es' : ''}`;
                    console.log('🎯 Texto a mostrar:', displayText);
                    return (
                      <span>
                        {displayText}
                      </span>
                    );
                  }
                  
                  // Mostrar loading o datos por defecto mientras cargan
                  console.log('🎯 Mostrando loading... otherUser presente:', !!otherUser);
                  return <span>{t('inbox.loadingStats')}</span>;
                })()}
              </div>
            </div>
          </div>

          {/* Messages Area */}
          <div className="flex-1 overflow-y-auto p-4 space-y-4">
            {/* Renderizar mensajes con separadores de fecha */}
            {messages.map((message, index) => {
              const isOwnMessage = message.sender_id === user?.id;
              const isSystemMessage = message.isSystemMessage || message.sender_id === 'system';
              
              // Avatar shows on LAST message of a group (bottom of group like reference)
              const isLastInGroup = !isOwnMessage && !isSystemMessage && (index === messages.length - 1 || messages[index + 1]?.sender_id !== message.sender_id || messages[index + 1]?.isSystemMessage);
              
              // Tighter spacing for consecutive messages from same sender
              const isSameSenderAsPrev = index > 0 && messages[index - 1].sender_id === message.sender_id && !messages[index - 1].isSystemMessage;
              
              // Determinar si necesitamos mostrar un separador de fecha
              const showDateSeparator = (() => {
                if (index === 0) {
                  console.log(`📅 Primer mensaje - Mostrando separador`, message);
                  return true;
                }
                
                const currentTimestamp = message.created_at || message.timestamp;
                const previousTimestamp = messages[index - 1].created_at || messages[index - 1].timestamp;
                
                console.log(`🔍 Comparando fechas:`, {
                  currentTimestamp,
                  previousTimestamp,
                  messageIndex: index,
                  senderId: message.sender_id,
                  userId: user?.id
                });
                
                if (!currentTimestamp || !previousTimestamp) {
                  console.log(`⚠️ Timestamp faltante - No mostrar separador`);
                  return false;
                }
                
                // Asegurar que ambos timestamps sean UTC
                const currentDateStr = currentTimestamp.endsWith('Z') ? currentTimestamp : currentTimestamp + 'Z';
                const previousDateStr = previousTimestamp.endsWith('Z') ? previousTimestamp : previousTimestamp + 'Z';
                
                const currentDate = new Date(currentDateStr);
                const previousDate = new Date(previousDateStr);
                
                console.log(`📆 Fechas parseadas:`, {
                  current: currentDate.toDateString(),
                  previous: previousDate.toDateString(),
                  different: currentDate.toDateString() !== previousDate.toDateString()
                });
                
                // Comparar solo la fecha (día/mes/año), ignorando hora
                return currentDate.toDateString() !== previousDate.toDateString();
              })();
              
              // Formatear la fecha para el separador (estilo Twyk)
              const formatDateSeparator = (dateString) => {
                if (!dateString) return '';
                const date = new Date(dateString.endsWith('Z') ? dateString : dateString + 'Z');
                const today = new Date();
                const yesterday = new Date(today);
                yesterday.setDate(yesterday.getDate() - 1);
                
                // Hoy
                if (date.toDateString() === today.toDateString()) {
                  return 'Hoy';
                }
                
                // Ayer
                if (date.toDateString() === yesterday.toDateString()) {
                  return 'Ayer';
                }
                
                // Mismo año: "26 ene"
                if (date.getFullYear() === today.getFullYear()) {
                  return date.toLocaleDateString('es-ES', { 
                    day: 'numeric', 
                    month: 'short'
                  }).replace('.', ''); // Eliminar el punto del mes
                }
                
                // Otro año: "26 ene 2023"
                return date.toLocaleDateString('es-ES', { 
                  day: 'numeric', 
                  month: 'short',
                  year: 'numeric'
                }).replace('.', ''); // Eliminar el punto del mes
              };
              
              // Renderizado especial para mensajes del sistema
              if (isSystemMessage) {
                return (
                  <React.Fragment key={message.id}>
                    {showDateSeparator && (
                      <div className="flex justify-center my-6">
                        <div className="bg-gray-50 text-gray-400 px-4 py-1.5 rounded-2xl text-xs font-medium select-none pointer-events-none">
                          {formatDateSeparator(message.created_at || message.timestamp)}
                        </div>
                      </div>
                    )}
                    <div className="flex justify-center mb-4">
                      <div className="bg-blue-50 text-blue-600 px-4 py-2.5 rounded-2xl text-sm max-w-md text-center">
                        {message.content}
                      </div>
                    </div>
                  </React.Fragment>
                );
              }
              
              return (
                <React.Fragment key={message.id}>
                  {showDateSeparator && (
                    <div className="flex justify-center my-6">
                      <div className="bg-gray-50 text-gray-400 px-4 py-1.5 rounded-2xl text-xs font-medium select-none pointer-events-none">
                        {formatDateSeparator(message.created_at || message.timestamp)}
                      </div>
                    </div>
                  )}
                  <div className={`flex ${isOwnMessage ? 'justify-end' : 'justify-start'} ${isSameSenderAsPrev ? 'mt-1' : 'mt-4'}`}>
                  <div className={`flex items-end space-x-2 max-w-[75%] ${isOwnMessage ? 'flex-row-reverse space-x-reverse' : ''}`}>
                    {/* Avatar for received messages - only on last message of group */}
                    {isLastInGroup && !isOwnMessage && renderAvatar(
                      message.sender?.avatar_url, 
                      message.sender?.display_name, 
                      message.sender?.username,
                      'w-7 h-7'
                    )}
                    
                    {/* Spacer when avatar not shown */}
                    {!isLastInGroup && !isOwnMessage && <div className="w-7" />}
                    
                    {/* Message bubble - pill shape */}
                    <div className={`relative px-4 py-2.5 ${
                      isOwnMessage 
                        ? message.status === 'chat_request' 
                          ? 'bg-yellow-100 text-gray-800 border-2 border-yellow-400 rounded-full' 
                          : 'text-white rounded-full'
                        : 'bg-gray-100 text-gray-900 rounded-full'
                    }`}
                    style={isOwnMessage && message.status !== 'chat_request' ? { backgroundColor: '#B061FF' } : {}}
                    >
                      <p className="text-sm">{message.content}</p>
                    </div>

                    {/* Read receipt for own messages - small checkmark on last message */}
                    {isOwnMessage && index === messages.length - 1 && message.status === 'sent' && (
                      <div className="flex-shrink-0 w-5 h-5 rounded-full bg-gray-200 flex items-center justify-center">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#666" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="20 6 9 17 4 12" />
                        </svg>
                      </div>
                    )}
                  </div>
                  </div>
                </React.Fragment>
              );
            })}
          </div>

          {/* Message Input or Chat Request Actions */}
          <div className="flex-shrink-0 bg-white px-4 py-3">
            {/* Si es una solicitud de chat pendiente y el usuario es el receptor */}
            {selectedConversation?.is_chat_request && selectedConversation?.is_request_receiver ? (
              <div className="space-y-3">
                <div className="text-center px-2">
                  <h3 className="text-sm font-medium text-gray-500">
                    Solicitud de mensaje de {selectedConversation?.other_user?.display_name || selectedConversation?.other_user?.username || 'este usuario'}
                  </h3>
                </div>
                <div className="flex gap-2 px-2">
                  <button
                    onClick={() => handleChatRequestAction('reject')}
                    className="flex-1 py-3 rounded-2xl bg-gray-50 hover:bg-gray-100 transition-colors"
                  >
                    <span className="text-sm font-medium text-gray-700">{t('inbox.reject')}</span>
                  </button>
                  <button
                    onClick={() => handleChatRequestAction('accept')}
                    className="flex-1 py-3 rounded-2xl bg-gray-50 hover:bg-gray-100 transition-colors"
                  >
                    <span className="text-sm font-medium text-gray-700">{t('inbox.accept')}</span>
                  </button>
                </div>
              </div>
            ) : selectedConversation?.is_chat_request && selectedConversation?.is_request_sender ? (
              <div className="space-y-3 py-2">
                <div className="text-center space-y-1">
                  <h2 className="text-sm font-semibold text-gray-900">{t('inbox.invitationSent')}</h2>
                  <p className="text-xs text-gray-400">{t('inbox.invitationSentDesc')}</p>
                </div>
                <button
                  onClick={() => handleCancelChatRequest()}
                  className="w-full py-3 rounded-2xl bg-gray-50 hover:bg-gray-100 text-gray-700 font-medium transition-colors text-sm"
                >
                  Cancelar solicitud
                </button>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={newMessage}
                  onChange={(e) => setNewMessage(e.target.value)}
                  placeholder={t('inbox.typeMessage')}
                  className="flex-1 px-4 py-2.5 bg-gray-50 rounded-2xl text-sm focus:outline-none focus:ring-1 focus:ring-gray-200 placeholder-gray-400"
                  onKeyPress={handleKeyPress}
                />
                <button
                  onClick={handleSendMessage}
                  disabled={!newMessage.trim()}
                  className="w-10 h-10 rounded-full flex items-center justify-center text-white disabled:opacity-40 transition-colors"
                  style={{ backgroundColor: '#B061FF' }}
                >
                  <Send className="h-4 w-4" />
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {/* New Chat Modal */}
      {showNewChatModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-md flex items-end justify-center z-50" onClick={closeNewChatModal}>
          <motion.div
            initial={{ y: '100%' }}
            animate={{ y: 0 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            className="bg-white rounded-t-3xl w-full max-h-[85vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="w-full pt-3 pb-1 flex justify-center">
              <div className="w-10 h-1 bg-gray-300 rounded-full" />
            </div>
            <div className="flex items-center justify-between px-5 py-3">
              <div className="w-9" />
              <h3 className="font-semibold text-gray-900 text-base">{t('inbox.newConversation')}</h3>
              <button
                onClick={closeNewChatModal}
                className="w-9 h-9 rounded-full hover:bg-gray-100 flex items-center justify-center transition-colors"
              >
                <X className="h-5 w-5 text-gray-500" />
              </button>
            </div>

            {/* Search Input */}
            <div className="relative mb-4">
              <input
                type="text"
                placeholder={t('inbox.searchUsers')}
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  searchUsers(e.target.value);
                }}
                className="w-full pl-4 pr-4 py-3 bg-gray-50 rounded-2xl focus:outline-none focus:ring-1 focus:ring-gray-200 text-sm placeholder-gray-400"
                autoFocus
              />
            </div>

            {/* Search Results */}
            <div className="max-h-60 overflow-y-auto px-4 pb-6">
              {searchLoading ? (
                <div className="flex items-center justify-center py-8">
                  <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-gray-400"></div>
                  <span className="ml-2 text-sm text-gray-400">{t('inbox.searching')}</span>
                </div>
              ) : searchResults.length > 0 ? (
                <div className="flex flex-col gap-2">
                  {searchResults.map((result) => (
                    <motion.button
                      key={result.id}
                      whileTap={{ scale: 0.98 }}
                      onClick={() => startConversation(result)}
                      className="w-full flex items-center gap-3 p-4 rounded-2xl bg-gray-50 hover:bg-gray-100 transition-colors text-left"
                    >
                      <div className="w-10 h-10 rounded-full bg-white flex items-center justify-center flex-shrink-0 relative overflow-hidden shadow-sm">
                        {result.avatar_url ? (
                          <>
                            <img 
                              src={resolveAssetUrl(result.avatar_url)} 
                              alt="Avatar" 
                              className="w-full h-full rounded-full object-cover"
                              onError={(e) => {
                                e.target.style.display = 'none';
                                e.target.parentNode.querySelector('.avatar-fallback').style.display = 'flex';
                              }}
                            />
                            <div className="avatar-fallback w-full h-full rounded-full flex items-center justify-center" style={{ display: 'none' }}>
                              <User className="w-5 h-5 text-gray-400" />
                            </div>
                          </>
                        ) : (
                          <User className="w-5 h-5 text-gray-400" />
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="font-medium text-sm text-gray-900 truncate">
                          {result.display_name || result.username}
                        </p>
                        <p className="text-xs text-gray-400 truncate">
                          @{result.username}
                        </p>
                      </div>
                    </motion.button>
                  ))}
                </div>
              ) : searchQuery.trim() ? (
                <div className="text-center py-8">
                  <p className="text-sm text-gray-400">{t('inbox.noUsersFound')}</p>
                </div>
              ) : (
                <div className="text-center py-8">
                  <p className="text-sm text-gray-400">{t('inbox.searchPrompt')}</p>
                </div>
              )}
            </div>
          </motion.div>
        </div>
      )}
    </div>
  );
};

export default MessagesMainPage;