import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
import streamlit as st
import torch
import whisper
from google import genai
from google.genai import types

# ==========================================
# CONFIGURACIÓN DE PÁGINA Y ESTILOS
# ==========================================
st.set_page_config(
    page_title="Cliperos AI — Generador Viral",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inicializar estado persistente para evitar que las descargas reinicien los resultados
if "clips_generados" not in st.session_state:
    st.session_state["clips_generados"] = []
if "zip_bytes" not in st.session_state:
    st.session_state["zip_bytes"] = None

# ==========================================
# FUNCIONES DE APOYO (GPU, DESCARGA, IA Y FFMPEG)
# ==========================================
@st.cache_resource
def cargar_modelo_whisper():
    dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
    return whisper.load_model("base", device=dispositivo), dispositivo


def tiene_nvenc() -> bool:
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "h264_nvenc" in res.stdout and torch.cuda.is_available()
    except Exception:
        return False


def limpiar_nombre(texto: str) -> str:
    limpio = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    limpio = re.sub(r"[-\s]+", "_", limpio)
    return limpio[:45] or "clip_viral"


def segundos_a_hhmmss(segundos: float) -> str:
    segundos = max(0, int(segundos))
    h = segundos // 3600
    m = (segundos % 3600) // 60
    s = segundos % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def descargar_video_tramo(
    url: str,
    carpeta_trabajo: str,
    usar_tramo: bool,
    min_inicio: int,
    min_fin: int,
    cookies_txt: str = "",
) -> str:
    ruta_salida = os.path.join(carpeta_trabajo, "fuente.mp4")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--concurrent-fragments", "16",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "-o", ruta_salida,
    ]

    if usar_tramo and min_fin > min_inicio:
        inicio_str = segundos_a_hhmmss(min_inicio * 60)
        fin_str = segundos_a_hhmmss(min_fin * 60)
        cmd.extend(["--download-sections", f"*{inicio_str}-{fin_str}"])

    if cookies_txt and cookies_txt.strip():
        ruta_cookies = os.path.join(carpeta_trabajo, "cookies.txt")
        with open(ruta_cookies, "w", encoding="utf-8") as f:
            f.write(cookies_txt.strip() + "\n")
        cmd.extend(["--cookies", ruta_cookies])

    cmd.append(url.strip())
    proceso = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proceso.returncode != 0 or not os.path.exists(ruta_salida):
        raise RuntimeError(f"Error al descargar el video:\n{proceso.stderr[-800:]}")
    return ruta_salida


def extraer_audio_ligero(ruta_video: str, carpeta_trabajo: str) -> str:
    ruta_audio = os.path.join(carpeta_trabajo, "audio_ligero.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", ruta_video,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k",
        ruta_audio,
    ]
    proceso = subprocess.run(cmd, capture_output=True, text=True)
    if proceso.returncode != 0:
        raise RuntimeError(f"Error extrayendo audio:\n{proceso.stderr[-500:]}")
    return ruta_audio


def detectar_momentos_virales(api_key: str, segmentos: list, max_clips: int = 5) -> list:
    client = genai.Client(api_key=api_key.strip())

    lineas_transcripcion = []
    for seg in segmentos:
        ini = round(seg["start"], 1)
        fin = round(seg["end"], 1)
        txt = seg["text"].strip()
        if txt:
            lineas_transcripcion.append(f"[{ini}s
