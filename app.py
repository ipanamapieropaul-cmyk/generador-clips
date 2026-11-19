import streamlit as st
import yt_dlp
import json
import os
import time
import glob
import subprocess
from concurrent.futures import ThreadPoolExecutor
from google import genai
from google.genai import types

# Configuración de página
st.set_page_config(page_title="Cliperos AI — Generador Viral", page_icon="🎬", layout="wide")

# Buscar FFmpeg (Windows local o Linux en la nube)
rutas_winget = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\**\bin"), recursive=True)
carpeta_ffmpeg = rutas_winget[0] if rutas_winget else None
ffmpeg_bin = os.path.join(carpeta_ffmpeg, "ffmpeg.exe") if carpeta_ffmpeg else "ffmpeg"
if carpeta_ffmpeg and carpeta_ffmpeg not in os.environ["PATH"]:
    os.environ["PATH"] = carpeta_ffmpeg + os.pathsep + os.environ["PATH"]

# Crear archivo temporal de cookies si existen en Secrets
RUTA_COOKIES = "cookies_temp.txt"
if "YOUTUBE_COOKIES" in st.secrets and st.secrets["YOUTUBE_COOKIES"].strip():
    with open(RUTA_COOKIES, "w", encoding="utf-8") as f:
        f.write(st.secrets["YOUTUBE_COOKIES"].strip() + "\n")
    tiene_cookies = True
else:
    tiene_cookies = False

# --- BARRA LATERAL DIDÁCTICA ---
with st.sidebar:
    st.header("💡 Guía Rápida")
    st.markdown("""
    **¿Cómo funciona esta herramienta?**
    1. **Extrae una pista de audio ultraligera** del video o tramo.
    2. **La IA escucha el audio directamente** en la nube.
    3. **Detecta los momentos más virales** con tiempos exactos.
    4. **Renderiza en vertical (9:16)** listo para TikTok o Reels.
    """)
    st.divider()
    st.subheader("⏱️ Directos largos (Kick / Twitch)")
    st.info(
        "En directos de **2 a 8 horas**, activa **'Analizar solo un tramo por minutos'** (ej. min 20 al 50) para sacar clips en menos de 1 minuto."
    )
    st.subheader("🎯 ¿Qué formato elegir?")
    st.info(
        "**• Zoom centrado (9:16):** Ideal para podcasts, charlas o reacciones (el más rápido).\n\n"
        "**• Fondo borroso:** Ideal para gameplays, fútbol o streams completos."
    )

# --- CABECERA VISUAL ---
st.title("🎬 Cliperos AI: Convierte videos largos en Clips Virales")
st.caption("Extrae automáticamente los mejores momentos en formato vertical (9:16) listos para TikTok, Reels y Shorts.")

col1, col2, col3 = st.columns(3)
with col1:
    st.info("**Paso 1: Elige tu fuente**\n\nPega un enlace de Kick, Twitch o YouTube, o sube un MP4.")
with col2:
    st.info("**Paso 2: Encuadre y tramo**\n\nElige zoom o fondo borroso (y filtra minutos si es un stream largo).")
with col3:
    st.info("**Paso 3: Descarga y sube**\n\nRevisa el puntaje viral de cada clip y descárgalo en MP4.")

st.divider()

api_key = st.secrets["GEMINI_API_KEY"].strip()

tab_enlace, tab_archivo = st.tabs(["🔗 Pegar Enlace (Kick / Twitch / YouTube)", "📁 Subir Video MP4 Directo"])

url = ""
archivo_subido = None

with tab_enlace:
    url = st.text_input(
        "Pega aquí el enlace del video o directo:",
        placeholder="Ejemplo: https://kick.com/... o https://www.youtube.com/watch?v=..."
    )

with tab_archivo:
    archivo_subido = st.file_uploader(
        "O sube tu video descargado en MP4 (hasta 200 MB):",
        type=["mp4", "mov", "mkv"]
    )

usar_tramo = st.checkbox("⏱️ Analizar solo un tramo específico por minutos (Recomendado para directos de +1 hora)")
min_inicio = 0
min_fin = 35
if usar_tramo:
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        min_inicio = st.number_input("Minuto de inicio:", min_value=0, max_value=1440, value=0, step=5)
    with col_t2:
        min_fin = st.number_input("Minuto de fin:", min_value=1, max_value=1440, value=35, step=5)

formato_video = st.radio(
    "📐 Elige el estilo de encuadre vertical:",
    options=[
        "Zoom centrado (9:16 — Ideal para podcasts y charlas)",
        "Fondo borroso (Pantalla completa — Ideal para gameplays y streams)"
    ],
    horizontal=True
)

OPCIONES_YTDLP_BASE = {
    'nocheckcertificate': True,
    'geo_bypass': True,
    'concurrent_fragment_downloads': 8,
    'quiet': True
}
if tiene_cookies:
    OPCIONES_YTDLP_BASE['cookiefile'] = RUTA_COOKIES
if carpeta_ffmpeg:
    OPCIONES_YTDLP_BASE['ffmpeg_location'] = carpeta_ffmpeg

def limpiar_temporales():
    for archivo in os.listdir():
        if archivo.startswith(("audio_temp", "audio_ligero", "fragmento_", "video_local_temp")):
            try:
                os.remove(archivo)
            except:
                pass

def formatear_tiempo(segundos_totales):
    s = int(segundos_totales)
    h, resto = divmod(s, 3600)
    m, seg = divmod(resto, 60)
    return f"{h:02d}:{m:02d}:{seg:02d}" if h > 0 else f"{m:02d}:{seg:02d}"

def renderizar_clip_ffmpeg(entrada_mp4, ruta_final, usar_fondo_borroso, inicio=None, fin=None):
    if usar_fondo_borroso:
        filtro_ffmpeg = [
            "-filter_complex",
            "[0:v]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,boxblur=8:2,scale=720:1280[bg];"
            "[0:v]scale=720:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        ]
    else:
        filtro_ffmpeg = ["-vf", "crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=720:1280"]

    recorte_tiempo = ["-ss", str(inicio), "-to", str(fin)] if (inicio is not None and fin is not None) else []

    comando_gpu = [
        ffmpeg_bin, "-y", *recorte_tiempo, "-i", entrada_mp4,
        *filtro_ffmpeg,
        "-c:v", "h264_nvenc", "-preset", "p1", "-cq", "24",
        "-c:a", "aac", "-b:a", "128k",
        ruta_final
    ]
    res = subprocess.run(comando_gpu, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        comando_cpu = [
            ffmpeg_bin, "-y", *recorte_tiempo, "-i", entrada_mp4,
            *filtro_ffmpeg,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-threads", "2",
            "-c:a", "aac", "-b:a", "128k",
            ruta_final
        ]
        subprocess.run(comando_cpu, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def procesar_clip_tarea(datos):
    i, clip, fuente, es_archivo_local, folder_clips, usar_fondo_borroso = datos
    inicio = float(clip['inicio'])
    fin = float(clip['fin'])
    ruta_final = os.path.join(folder_clips, f"clip_viral_{i+1}.mp4")

    if es_archivo_local:
        renderizar_clip_ffmpeg(fuente, ruta_final, usar_fondo_borroso, inicio, fin)
        return (i, clip, ruta_final)
    else:
        temp_fragmento = f"fragmento_{i}.mp4"
        ydl_clip_opts = {
            **OPCIONES_YTDLP_BASE,
            'format': 'best[height<=720][ext=mp4]/best[height<=720]/best',
            'outtmpl': temp_fragmento,
            'download_ranges': yt_dlp.utils.download_range_func(None, [(inicio, fin)]),
        }
        with yt_dlp.YoutubeDL(ydl_clip_opts) as ydl:
            ydl.download([fuente])

        if os.path.exists(temp_fragmento):
            renderizar_clip_ffmpeg(temp_fragmento, ruta_final, usar_fondo_borroso)
            try:
                os.remove(temp_fragmento)
            except:
                pass
            return (i, clip, ruta_final)
    return None

if st.button("🚀 Generar Clips Virales Ahora", type="primary", use_container_width=True):
    if not archivo_subido and not url:
        st.warning("⚠️ Por favor, pega un enlace o sube un archivo MP4 en las pestañas de arriba.")
    elif usar_tramo and min_fin <= min_inicio:
        st.warning("⚠️ El minuto de fin debe ser mayor que el minuto de inicio.")
    else:
        limpiar_temporales()
        client = genai.Client(api_key=api_key)
        barra = st.progress(0, text="Iniciando proceso...")
        usar_fondo_borroso = "Fondo borroso" in formato_video
        es_archivo_local = archivo_subido is not None
        desfase_segundos = float(min_inicio * 60) if usar_tramo else 0.0

        # Paso 1: Extraer audio ultraligero (16kHz / 24kbps mono)
        barra.progress(20, text="📥 Paso 1/3: Extrayendo audio ultraligero...")
        audio_mp3_ligero = "audio_ligero.mp3"

        if es_archivo_local:
            ruta_video_local = "video_local_temp.mp4"
            with open(ruta_video_local, "wb") as f:
                f.write(archivo_subido.getbuffer())
            recorte_audio = ["-ss", str(min_inicio * 60), "-to", str(min_fin * 60)] if usar_tramo else []
            subprocess.run(
                [ffmpeg_bin, "-y", *recorte_audio, "-i", ruta_video_local, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "24k", audio_mp3_ligero],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            fuente_clips = ruta_video_local
        else:
            ydl_opts = {
                **OPCIONES_YTDLP_BASE,
                'format': 'worstaudio/worst[ext=mp4]/worst',
                'outtmpl': 'audio_temp.%(ext)s',
            }
            if usar_tramo:
                ydl_opts['download_ranges'] = yt_dlp.utils.download_
