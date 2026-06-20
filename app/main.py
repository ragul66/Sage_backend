import logging
import socket
import json
import asyncio
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from app.core.config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def get_local_ip() -> str:
    """
    Utility to discover the laptop's local network IP address on the active interface.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"

local_lan_ip = get_local_ip()

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Backend for SageAssistant Voice Agent integrated with Retell AI Custom LLM WebSocket protocol",
    version="1.0.0"
)

# CORS Middleware for physical/simulator mobile devices on the local Wi-Fi
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global Google Gen AI Client
ai_client: Optional[genai.Client] = None

# Store SMS chat sessions: phone_number -> genai.Chat
sms_chats = {}


@app.on_event("startup")
async def startup_event():
    global ai_client
    if settings.GEMINI_API_KEY and settings.GEMINI_API_KEY != "your_gemini_api_key_here":
        try:
            ai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
            logger.info("Google Gen AI client initialized successfully with gemini-2.5-flash.")
        except Exception as e:
            logger.error(f"Error configuring Google Gen AI client: {e}")
    else:
        logger.warning("GEMINI_API_KEY is not configured or set to default template value. Running in DEMO mode.")

    banner = f"""
================================================================================
  SAGEASSISTANT / RETELL INTERMEDIARY BACKEND STARTED SUCCESSFULLY!
================================================================================
  Local API Server:       http://localhost:{settings.PORT}
  Mobile Connection URL:  http://{local_lan_ip}:{settings.PORT}
  Retell WebSocket URL:   ws://{local_lan_ip}:{settings.PORT}/llm-websocket/{{call_id}}
================================================================================
    """
    print(banner)

@app.get("/", summary="Root health check endpoint")
async def root():
    return {
        "status": "healthy",
        "message": "SageAssistant API is running. Direct mobile app requests to /api/generate or health checks to /health."
    }

# --- WebSocket Endpoint for Retell AI Custom LLM Protocol ---

@app.websocket("/llm-websocket/{call_id}")
async def llm_websocket(websocket: WebSocket, call_id: str):
    await websocket.accept()
    logger.info(f"Retell WebSocket connection accepted for call_id: {call_id}")

    # 1. Instantly send a Retell-compliant initial greeting JSON payload
    greeting_payload = {
        "response_type": "response",
        "response_id": 0,
        "content": "Hi! Thanks for calling. I'm an AI assistant. I can take a message and pass it along immediately. Who do I have the pleasure of speaking with?",
        "content_complete": True,
        "end_of_call": False
    }

    try:
        await websocket.send_json(greeting_payload)
        logger.info(f"Initial greeting sent successfully for call_id: {call_id}")
    except Exception as e:
        logger.error(f"Error sending initial greeting: {e}")
        await websocket.close()
        return

    # Check if Gemini AI Client is initialized
    if not ai_client:
        logger.error("Cannot proceed with chat session: Gemini client is not initialized.")
        try:
            await websocket.send_json({
                "response_type": "response",
                "response_id": 0,
                "content": "Hello. I'm currently experiencing system issues. Please try calling back later.",
                "content_complete": True,
                "end_of_call": True
            })
        except Exception:
            pass
        await websocket.close()
        return

    # 2. Chat Management & Gemini Integration: Initialize multi-turn chat session
    try:
        system_instruction = (
            "You are Sage AI Assistant, a professional mobile answering machine. "
            "Your name is Sage. "
            "You are helping take a message for the owner because they are currently busy or unavailable. "
            "Keep your responses short, concise, and optimized for phone speech. "
            "Never use markdown, bolding, asterisks, or bullet points in your output. "
            "If the user is saying goodbye, thank you, or indicates they are done leaving their message, "
            "politely say goodbye or wish them a great day so the call can end. "
            "Always respond in English. Do not write responses in Tamil, Tamil script, or any other language, "
            "even if the user writes in Tamil, Tanglish, or any other language. "
            "If the user replies with a simple confirmation like 'yes', 'sure', 'ok', or 'okay' indicating they want to leave a message, "
            "politely ask them what message they would like to leave."
        )
        chat = ai_client.chats.create(
            model=settings.GEMINI_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7
            )
        )
        logger.info(f"Multi-turn Gemini chat session established for call_id: {call_id}")
    except Exception as e:
        logger.error(f"Failed to create Gemini chat session: {e}")
        await websocket.close()
        return

    # 3. Retell Protocol Event Loop
    try:
        while True:
            # Wait for incoming messages
            message_text = await websocket.receive_text()
            logger.info(f"Received data from Retell for call_id {call_id}: {message_text[:100]}...")

            try:
                request = json.loads(message_text)
            except Exception as e:
                logger.error(f"Invalid JSON payload from Retell: {e}")
                continue

            interaction_type = request.get("interaction_type")

            if interaction_type == "response_required":
                response_id = request.get("response_id", 0)
                transcript = request.get("transcript", [])

                if not transcript:
                    logger.warning("response_required event received, but transcript list is empty.")
                    continue

                # Extract latest user message
                user_message = transcript[-1].get("content", "").strip()
                logger.info(f"User transcript: '{user_message}'")

                # Send message to Gemini in a separate thread to prevent blocking
                try:
                    response = await asyncio.to_thread(chat.send_message, user_message)
                    gemini_response_text = response.text.strip()
                    logger.info(f"Gemini reply: '{gemini_response_text}'")
                except Exception as e:
                    logger.error(f"Error calling Gemini SDK send_message: {e}")
                    gemini_response_text = "I'm sorry, I'm having trouble understanding you right now. Could you say that again?"

                # Case-insensitive end-of-call detection
                text_lower = gemini_response_text.lower()
                end_of_call = "goodbye" in text_lower or "have a great day" in text_lower

                # Send back the exact payload format
                response_payload = {
                    "response_type": "response",
                    "response_id": response_id,
                    "content": gemini_response_text,
                    "content_complete": True,
                    "end_of_call": end_of_call
                }

                await websocket.send_json(response_payload)
                logger.info(f"Sent reply to Retell (response_id={response_id}, end_of_call={end_of_call})")

                if end_of_call:
                    logger.info(f"Terminating WebSocket call session {call_id} gracefully based on goodbye.")
                    break

            elif interaction_type == "ping":
                await websocket.send_json({"response_type": "pong"})
            else:
                logger.info(f"Ignored non-actionable interaction: {interaction_type}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected by Retell for call_id: {call_id}")
    except Exception as e:
        logger.error(f"Error in Retell WebSocket event loop for call_id {call_id}: {e}", exc_info=True)
    finally:
        logger.info(f"Closing WebSocket for call_id: {call_id}")
        try:
            await websocket.close()
        except Exception:
            pass

# --- Mobile App SMS Integration Endpoint ---

class GenerateTextRequest(BaseModel):
    prompt: str = Field(..., description="The prompt or SMS text received from the mobile app.")
    phone_number: Optional[str] = Field(None, description="The caller's phone number to identify the conversation session.")
    custom_greeting: Optional[str] = Field(None, description="A custom initial greeting to set as the starting message.")

class GenerateResponse(BaseModel):
    text: str = Field(..., description="SMS text response generated by the agent.")
    audio: str = Field("", description="Base64 encoded audio placeholder.")
    format: str = Field("mp3", description="Audio format.")


@app.post("/api/generate", response_model=GenerateResponse, summary="SMS AI reply endpoint for mobile application")
async def generate_from_text(request: GenerateTextRequest):
    """
    HTTP route targeted by the mobile app's SmsReplyReceiver and SageAIHandlerService.
    Ensures backward compatibility by generating text with the new SDK and returning the expected JSON structure.
    """
    logger.info(f"Received SMS request: '{request.prompt[:50]}' (phone={request.phone_number})")
    prompt = request.prompt
    phone_number = request.phone_number
    custom_greeting = request.custom_greeting

    system_instruction = (
        "You are Sage AI Assistant, a professional mobile answering machine. "
        "Your name is Sage. "
        "You are helping take a message for the owner because they are currently busy or unavailable. "
        "Keep your responses short, concise, and optimized for text/SMS conversation. "
        "Never use markdown, bolding, asterisks, or bullet points in your output. "
        "Always respond in English. Do not write responses in Tamil, Tamil script, or any other language, "
        "even if the user writes in Tamil, Tanglish, or any other language. "
        "If the user replies with a simple confirmation like 'yes', 'sure', 'ok', or 'okay' indicating they want to leave a message, "
        "politely ask them what message they would like to leave."
    )

    if not ai_client:
        demo_greeting = (
            custom_greeting or 
            "Hi! I'm currently unavailable. This is Sage, my AI assistant. I can help take a message — please reply here."
        )
        return GenerateResponse(text=demo_greeting)

    try:
        # If it's the initial greeting setup
        if prompt == "INITIAL_GREETING":
            # 1. Determine initial greeting text
            if custom_greeting and custom_greeting.strip():
                greeting_text = custom_greeting.strip()
            else:
                # Generate initial greeting using Gemini
                gen_prompt = (
                    "Generate a short, friendly SMS greeting as Sage AI Assistant. "
                    "Tell the caller that the person they tried to call is busy right now, and that you can help via text. "
                    "Ask who they are and how you can help. Keep it to 2-3 sentences max. Write as a text message, no emojis."
                )
                response = await asyncio.to_thread(
                    ai_client.models.generate_content,
                    model=settings.GEMINI_MODEL,
                    contents=gen_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.7
                    )
                )
                greeting_text = response.text.strip()

            # 2. Initialize new multi-turn chat session for this phone number if provided
            if phone_number:
                # We initialize the history with a single content block showing the model said the greeting
                history = [
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=greeting_text)]
                    )
                ]
                chat = ai_client.chats.create(
                    model=settings.GEMINI_MODEL,
                    history=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.7
                    )
                )
                sms_chats[phone_number] = chat
                logger.info(f"Initialized new SMS chat session for phone: {phone_number}")

            return GenerateResponse(text=greeting_text)

        else:
            # For user SMS reply messages (prompt is the reply)
            # Try to fetch existing session if phone_number is provided
            if phone_number and phone_number in sms_chats:
                logger.info(f"Using existing SMS chat session for phone: {phone_number}")
                chat = sms_chats[phone_number]
                response = await asyncio.to_thread(chat.send_message, prompt)
                reply_text = response.text.strip()
            else:
                # Fallback: No session or server restarted, create new and process message
                logger.info(f"No existing SMS chat session for phone: {phone_number}. Creating new session and recovering context.")
                
                # Reconstruct the initial greeting as context for history
                if custom_greeting and custom_greeting.strip():
                    initial_msg = custom_greeting.strip()
                else:
                    initial_msg = "Hi! I'm currently unavailable. This is Sage, my AI assistant. I can help take a message — please reply here."
                
                history = [
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=initial_msg)]
                    )
                ]
                
                chat = ai_client.chats.create(
                    model=settings.GEMINI_MODEL,
                    history=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.7
                    )
                )
                if phone_number:
                    sms_chats[phone_number] = chat
                response = await asyncio.to_thread(chat.send_message, prompt)
                reply_text = response.text.strip()

            return GenerateResponse(text=reply_text)

    except Exception as e:
        logger.error(f"Error in generate_from_text: {e}")
        fallback_msg = "Hello, the user is currently busy. Please send your message, and I'll forward it to them."
        return GenerateResponse(text=fallback_msg)


@app.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "project": settings.PROJECT_NAME,
        "local_ip": local_lan_ip,
        "gemini_configured": ai_client is not None,
        "gemini_model": settings.GEMINI_MODEL
    }

