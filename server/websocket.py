import asyncio
import websockets
import pyaudio
import numpy as np
import whisper
import torch
import webrtcvad
import logging
import librosa
from datetime import datetime, timedelta
import torch.backends.cudnn as cudnn

cudnn.benchmark = True

# Whisper model initialization (uses GPU if available)
model = whisper.load_model("turbo", device="cuda" if torch.cuda.is_available() else "cpu")

# Audio configuration
WHISPER_SAMPLE_RATE = 16000  # Whisper expects 16kHz
CHANNELS = 1  # Mono audio
FORMAT = pyaudio.paInt16  # 16-bit PCM
PHRASE_TIMEOUT = 0.3  # Seconds of silence to consider as a new phrase

# Voice Activity Detector
vad = webrtcvad.Vad()
vad.set_mode(2)  # Moderate aggressiveness

# Global buffers
transcription_queue = asyncio.Queue()
audio_queue = asyncio.Queue()
phrase_time = None
start_time = datetime.utcnow()

def is_speech(audio_chunk, sample_rate):
    """Check if the audio chunk contains speech using WebRTC VAD."""
    try:
        # Validate audio size (WebRTC VAD requires 10ms, 20ms, or 30ms frame size)
        frame_duration_ms = (len(audio_chunk) / (sample_rate / 1000))
        if frame_duration_ms not in [10, 20, 30]:
            raise ValueError(f"Invalid frame size: {frame_duration_ms}ms. Expected 10ms, 20ms, or 30ms.")
        
        return vad.is_speech(audio_chunk, sample_rate)
    except Exception as e:
        print(f"Error in is_speech: {e}")
        return False  # Return False as a fallback


async def handle_connection(websocket):
    global phrase_time
    try:
        async for message in websocket:
            if isinstance(message, bytes):  # Check if the message contains PCM audio data
                if is_speech(message, sample_rate=WHISPER_SAMPLE_RATE): 
                    phrase_time = datetime.utcnow() - start_time
                    await audio_queue.put({
                        "time": phrase_time,
                        "audio": message
                    })
            else:
                print("Received text:", message)
    except websockets.ConnectionClosed:
        print("Client disconnected.")
    except Exception as e:
        print(f"Error in connection: {e}")

def preprocess_audio(raw_data):
    # Load the raw audio and resample using librosa (faster and optimized for GPU)
    samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    target_length = WHISPER_SAMPLE_RATE * 30  # Whisper expects up to 30 seconds of audio

    # Fix the length of the audio (pad or truncate)
    samples = librosa.util.fix_length(samples, size=target_length)
    return samples

def format_time(delta_seconds):
    """Convert seconds to HH:MM:SS.mmm format."""
    if isinstance(delta_seconds, timedelta):
        delta_seconds = delta_seconds.total_seconds()

    print(delta_seconds)

    hours = int(delta_seconds // 3600)
    minutes = int((delta_seconds % 3600) // 60)
    seconds = delta_seconds % 60
    return f"{hours:02}:{minutes:02}:{seconds:06.3f}"

def process_time_segments(phrase_timestamp, segments):
    """
    Formats the Whisper segments with timestamps relative to the base phrase timestamp.

    Args:
        phrase_timestamp (datetime): The base timestamp of the phrase.
        segments (list): List of Whisper's segment dictionaries.

    Returns:
        list: Formatted transcript lines with timestamps.
    """
    transcript_lines = []
    transcript_obj = []
    
    for segment in segments:
        start_time = phrase_timestamp + timedelta(seconds=segment['start'])
        end_time = phrase_timestamp + timedelta(seconds=segment['end'])
        
        formatted_start = format_time(start_time)
        formatted_end = format_time(end_time)

        transcript_lines.append(f"[{formatted_start} - {formatted_end}] {segment['text']}")
        transcript_obj.append({
            "text": segment['text'], 
            "start": formatted_start, 
            "end": formatted_end
        })

    return transcript_lines, transcript_obj

async def transcribe_loop():
    """
    Periodically reads the global audio buffer, processes it, and transcribes it using Whisper.
    """
    logging.info("Starting transcription loop...")
    batch_buffer = bytearray()  # Buffer for the current phrase
    transcription = ['']
    phrase_timestamp = timedelta(0)

    while True:

        while not audio_queue.empty():
            # Get the next item in the queue (a dict with time and audio data)
            data = await audio_queue.get()
            audio_data = data["audio"]
            
            if phrase_complete:
                phrase_timestamp = data["time"]  # Access the timestamp if needed
            
            # Extend the batch buffer with the audio data
            batch_buffer.extend(audio_data)

        phrase_complete = False

        if (len(batch_buffer) > 0):
            audio_tensor = preprocess_audio(batch_buffer)

            # Move to GPU for processing
            audio_tensor = torch.from_numpy(audio_tensor).to("cuda", non_blocking=True)

            # Run Whisper transcription
            result = model.transcribe(
                audio_tensor,
                fp16=True,
                logprob_threshold=-1.0,
                no_speech_threshold=2.0,
                hallucination_silence_threshold=1.0,
                compression_ratio_threshold=1.0,
                language="en",
                suppress_tokens=""  # Adjusted to reduce hallucinations
            )

            segments = result.get("segments", [])
            formatted_segments, timestamp_obj = process_time_segments(phrase_timestamp, segments)

            now = datetime.utcnow() - start_time
            # Check if enough time has passed since the last detected speech
            if phrase_time and now - phrase_time > timedelta(seconds=PHRASE_TIMEOUT):
                phrase_complete = True

            # Append or update the transcription
            if phrase_complete:
                # If the phrase is complete, override the current phrase and finalize it
                transcription.append("".join(formatted_segments))  # Add the finalized phrase to the transcription
                batch_buffer.clear()
            else:
                transcription[-1] = ("".join(formatted_segments))   # Update the current line

            print("[Transcription]")
            print(" ".join(transcription), end="\n")

        await asyncio.sleep(0.1)  # Prevent excessive CPU usage

async def main():
    # Start the transcription loop and worker as an async task
    asyncio.create_task(transcribe_loop())
    
    # Start the WebSocket server
    async with websockets.serve(handle_connection, "localhost", 8765):
        print("Starting WebSocket server at ws://localhost:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
