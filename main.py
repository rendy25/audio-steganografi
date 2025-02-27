import os
import struct
import random
import string
import boto3
import numpy as np
import wave
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydub import AudioSegment
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from botocore.client import Config
from mangum import Mangum
import io

# Cloudflare R2 configuration
AccountID = "your-account-id"
Bucket = "your-bucket-id"
ClientAccessKey = "your-client-access-key"
ClientSecret = "your-client-secret"
ConnectionUrl = f"https://{AccountID}.r2.cloudflarestorage.com"

# Create a client to connect to Cloudflare's R2 Storage
S3Connect = boto3.client(
    's3',
    endpoint_url=ConnectionUrl,
    aws_access_key_id=ClientAccessKey,
    aws_secret_access_key=ClientSecret,
    config=Config(signature_version='s3v4'),
    region_name='us-east-1'
)

# AudioSegment.ffmpeg = which("ffmpeg")
# AudioSegment.ffprobe = which("ffprobe")
AudioSegment.ffmpeg = "/opt/bin/ffmpeg"
AudioSegment.ffprobe = "/opt/bin/ffprobe"


app = FastAPI()
handler = Mangum(app)
result_dir = "/tmp"
base_url = "url-lambda/download?filename="
def encrypt_data(data, key):
    cipher = AES.new(key, AES.MODE_CBC)
    ciphertext = cipher.encrypt(pad(data, AES.block_size))
    return cipher.iv + ciphertext


def embed_lsb(audio_data, secret_data):
    audio_data = np.frombuffer(audio_data, dtype=np.int16)
    secret_bits = np.unpackbits(np.frombuffer(secret_data, dtype=np.uint8))

    if len(secret_bits) > len(audio_data) * 2:
        raise ValueError("Secret data is too large to embed in the provided audio.")

    secret_bits = np.append(secret_bits, np.zeros(len(audio_data) * 2 - len(secret_bits), dtype=np.uint8))
    audio_data = (audio_data & ~1) | secret_bits[:len(audio_data)]

    return audio_data.tobytes()


def append_data_length_to_audio(audio_file_path, data_length):
    with open(audio_file_path, 'ab') as f:
        length_bytes = struct.pack('I', data_length)
        f.write(length_bytes)

def extract_lsb(audio_data, data_length):
    audio_data = np.frombuffer(audio_data, dtype=np.int16)
    extracted_bits = audio_data & 1
    extracted_data = np.packbits(extracted_bits)[:data_length]
    return extracted_data.tobytes()

def decrypt_data(data, key):
    iv = data[:AES.block_size]
    ciphertext = data[AES.block_size:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ciphertext), AES.block_size)

def upload_to_r2(file_data, file_name):
    """Upload a file to Cloudflare R2 and return the public URL."""
    try:
        S3Connect.put_object(Bucket=Bucket, Key=file_name, Body=file_data)
        public_url = f"{ConnectionUrl}/{Bucket}/{file_name}"
        return public_url
    except Exception as e:
        raise Exception(f"Failed to upload to R2: {str(e)}")

def extract_data_length_from_audio(audio_file_path):
    with open(audio_file_path, 'rb') as f:
        f.seek(-4, os.SEEK_END)
        length_bytes = f.read(4)
        data_length = struct.unpack('I', length_bytes)[0]
    return data_length


@app.post("/embed")
async def embed(
    audio: UploadFile = File(...),
    secret: UploadFile = File(None),
    type: str = Form(...),
    key: str = Form(...)
):
    try:
        aes_key = key.encode('utf-8')

        if type == 'text':
            secret_data = (await secret.read()).decode('utf-8').encode('utf-8')
        elif type == 'image':
            secret_data = await secret.read()
        else:
            raise HTTPException(status_code=400, detail="Invalid secret type")

        encrypted_data = encrypt_data(secret_data, aes_key)
        data_length = len(encrypted_data)

        audio_data = AudioSegment.from_file(audio.file)
        raw_audio_data = audio_data.raw_data
        embedded_audio_data = embed_lsb(raw_audio_data, encrypted_data)

        random_string = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        file_name = f"embedded_audio_{random_string}.wav"
        output_path = os.path.join(result_dir, file_name)

        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(audio_data.channels)
            wav_file.setsampwidth(audio_data.sample_width)
            wav_file.setframerate(audio_data.frame_rate)
            wav_file.writeframes(embedded_audio_data)

        append_data_length_to_audio(output_path, data_length)

        with open(output_path, "rb") as file:
            upload_object = file.read()

        S3Connect.put_object(Bucket=Bucket, Key=file_name, Body=upload_object)

        if os.path.exists(output_path):
            os.remove(output_path)

        public_path = f"{base_url}{file_name}"
        return JSONResponse(content={"message": "Audio successfully embedded", "path": public_path})

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/extract")
async def extract(
    audio: UploadFile = File(...),
    type: str = Form(...),
    key: str = Form(...)
):
    try:
        aes_key = key.encode('utf-8')

        temp_audio_path = os.path.join(result_dir, 'temp_audio.wav')
        with open(temp_audio_path, "wb") as temp_file:
            temp_file.write(await audio.read())

        data_length = extract_data_length_from_audio(temp_audio_path)

        audio = AudioSegment.from_file(temp_audio_path)
        raw_audio_data = audio.raw_data

        extracted_data = extract_lsb(raw_audio_data, data_length)
        decrypted_data = decrypt_data(extracted_data, aes_key)

        if type == 'text':
            extracted_text = decrypted_data.decode('utf-8')
            return JSONResponse(content={"secret": extracted_text})
        elif type == 'image':
            random_string = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
            file_name = f"extracted_image_{random_string}.png"
            output_path = os.path.join(result_dir, file_name)

            with open(output_path, "wb") as img_file:
                img_file.write(decrypted_data)

            public_url = upload_to_r2(decrypted_data, file_name)
   
            if os.path.exists(output_path):
                os.remove(output_path)
            
            if os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

            public_path = f"{base_url}{file_name}"
            return JSONResponse(content={"secret": public_path})
        else:
            raise HTTPException(status_code=400, detail="Invalid data type")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download")
async def download(filename: str):
    """Download file from Cloudflare R2 Storage."""
    try:
        if not filename:
            raise HTTPException(status_code=400, detail="Filename is required")

        try:
            file_obj = S3Connect.get_object(Bucket=Bucket, Key=filename)
            file_content = file_obj['Body'].read()
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"File not found: {str(e)}")

        return StreamingResponse(
            io.BytesIO(file_content),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))