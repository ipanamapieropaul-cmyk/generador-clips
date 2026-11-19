import io
import json
import os
import re
import shutil
import subprocess
import tempfile
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
            lineas_transcripcion.append(f"[{ini}s - {fin}s] {txt}")

    texto_completo = "\n".join(lineas_transcripcion)
    if len(texto_completo) > 120000:
        texto_completo = texto_completo[:120000]

    prompt = f"""Eres un editor experto en retención viral para TikTok, Instagram Reels y YouTube Shorts.
Analiza la siguiente transcripción con marcas de tiempo (en segundos) y selecciona hasta {max_clips} momentos con mayor potencial viral (ganchos fuertes, risas, polémicas, historias completas o clímax).

Reglas estrictas:
1. Cada clip debe durar entre 25 y 65 segundos.
2. Usa exactamente los segundos reales de la transcripción sin inventar tiempos fuera de rango.
3. Devuelve ÚNICAMENTE un arreglo JSON válido (sin texto adicional) con esta estructura exacta:
[
  {{
    "titulo": "Título llamativo corto",
    "inicio": 12.5,
    "fin": 54.0,
    "puntaje_viral": 94,
    "razon": "Por qué engancha en los primeros 3 segundos"
  }}
]

Transcripción:
{texto_completo}
"""

    respuesta = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.4,
        ),
    )

    datos = json.loads(respuesta.text)
    if not isinstance(datos, list):
        raise ValueError("La respuesta de Gemini no devolvió una lista de clips.")
    return datos


def renderizar_clip_vertical(
    ruta_video: str,
    inicio: float,
    fin: float,
    estilo: str,
    ruta_salida: str,
    usar_gpu: bool,
):
    duracion = max(5.0, float(fin) - float(inicio))

    if "Zoom" in estilo:
        # Recorte centrado 9:16 (1080x1920)
        filtro_video = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    else:
        # Pantalla completa centrada con fondo negro vertical (1080x1920)
        filtro_video = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1"

    if usar_gpu:
        codec_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-b:v", "5M"]
    else:
        codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{max(0.0, float(inicio)):.2f}",
        "-i", ruta_video,
        "-t", f"{duracion:.2f}",
        "-vf", filtro_video,
        *codec_args,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        ruta_salida,
    ]
    proceso = subprocess.run(cmd, capture_output=True, text=True)
    if proceso.returncode != 0:
        raise RuntimeError(f"Error renderizando clip:\n{proceso.stderr[-600:]}")


# ==========================================
# BARRA LATERAL (SIDEBAR)
# ==========================================
modelo_whisper, dispositivo_whisper = cargar_modelo_whisper()
gpu_nvenc_activa = tiene_nvenc()

with st.sidebar:
    st.markdown("### 💡 Guía Rápida")
    if dispositivo_whisper == "cuda":
        st.success("⚡ **Modo Turbo GPU Activo**\n(Whisper CUDA + NVENC)")
    else:
        st.warning("🐢 Modo CPU Activo (Activa GPU T4 en Colab para velocidad máxima)")

    st.markdown(
        """
**¿Cómo funciona esta herramienta?**
1. **Extrae una pista de audio** ultraligera del video o tramo.
2. **Analiza el contenido en segundos** con IA.
3. **Detecta los momentos más virales** con tiempos exactos.
4. **Renderiza en vertical (9:16)** listo para TikTok o Reels.
"""
    )

    st.info(
        "⏱️ **Directos largos (Kick / Twitch)**\n\n"
        "En directos de **2 a 8 horas**, activa `'Analizar solo un tramo por minutos'` "
        "(ej. min 20 al 50) para sacar clips en segundos."
    )

    max_clips = st.slider("Cantidad máxima de clips a generar:", min_value=1, max_value=10, value=5)

    st.info(
        "🎯 **¿Qué formato elegir?**\n\n"
        "• **Zoom centrado (9:16):** Ideal para podcasts y charlas."
    )

# ==========================================
# INTERFAZ PRINCIPAL
# ==========================================
st.title("🎬 Cliperos AI: Convierte videos largos en Clips Virales")
st.caption("Extrae automáticamente los mejores momentos en formato vertical (9:16) listos para TikTok, Reels y Shorts.")

c1, c2, c3 = st.columns(3)
with c1:
    st.info("**Paso 1: Elige tu fuente**\n\nPega un enlace de Kick, Twitch o YouTube, o sube un MP4.")
with c2:
    st.info("**Paso 2: Encuadre y tramo**\n\nElige zoom o fondo negro (y filtra minutos si es un stream largo).")
with c3:
    st.info("**Paso 3: Descarga y sube**\n\nRevisa el puntaje viral de cada clip y descárgalo en MP4 o ZIP.")

st.markdown("---")

tab_url, tab_mp4 = st.tabs(["🔗 Pegar Enlace (Kick / Twitch / YouTube)", "📁 Subir Video MP4 Directo"])

url_video = ""
archivo_subido = None

with tab_url:
    url_video = st.text_input(
        "Pega aquí el enlace del video o directo:",
        placeholder="https://kick.com/lacobraaa/videos/...",
    )
    usar_tramo = st.checkbox(
        "⏱️ Analizar solo un tramo específico por minutos (Recomendado para directos de +1 hora)",
        value=True,
    )
    col_ini, col_fin = st.columns(2)
    with col_ini:
        min_inicio = st.number_input("Minuto de inicio:", min_value=0, max_value=1440, value=45, disabled=not usar_tramo)
    with col_fin:
        min_fin = st.number_input("Minuto de fin:", min_value=1, max_value=1440, value=90, disabled=not usar_tramo)

with tab_mp4:
    archivo_subido = st.file_uploader("Sube tu archivo de video (MP4, MOV, MKV):", type=["mp4", "mov", "mkv"])

estilo_encuadre = st.radio(
    "📐 Elige el estilo de encuadre vertical:",
    options=[
        "Zoom centrado (9:16 — Ideal para podcasts y charlas)",
        "Fondo negro (Pantalla completa — Ideal para gameplays y streams)",
    ],
    index=1,
    horizontal=True,
)

# ==========================================
# EJECUCIÓN DEL PIPELINE
# ==========================================
if st.button("🚀 Generar Clips Virales Ahora", type="primary", use_container_width=True):
    api_key = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
    cookies_yt = st.secrets.get("YOUTUBE_COOKIES", "")

    if not api_key:
        st.error("❌ Falta configurar `GEMINI_API_KEY` en `.streamlit/secrets.toml`.")
        st.stop()

    if not url_video.strip() and archivo_subido is None:
        st.warning("⚠️ Pega un enlace de video o sube un archivo MP4 antes de continuar.")
        st.stop()

    if url_video.strip() and usar_tramo and min_fin <= min_inicio:
        st.warning("⚠️ El minuto de fin debe ser mayor que el minuto de inicio.")
        st.stop()

    # Limpiar resultados anteriores antes de iniciar una nueva generación
    st.session_state["clips_generados"] = []
    st.session_state["zip_bytes"] = None

    barra = st.progress(0, text="⏳ Iniciando entorno de trabajo...")
    carpeta_temp = tempfile.mkdtemp(prefix="cliperos_")
    carpeta_persistente = os.path.join(tempfile.gettempdir(), "cliperos_resultados")
    shutil.rmtree(carpeta_persistente, ignore_errors=True)
    os.makedirs(carpeta_persistente, exist_ok=True)

    try:
        # PASO 1: Obtener video y extraer audio ultraligero
        barra.progress(12, text="📥 Paso 1/3: Descargando video y extrayendo audio ultraligero...")
        if archivo_subido is not None:
            ruta_video = os.path.join(carpeta_temp, "subido.mp4")
            with open(ruta_video, "wb") as f:
                f.write(archivo_subido.read())
        else:
            ruta_video = descargar_video_tramo(
                url=url_video,
                carpeta_trabajo=carpeta_temp,
                usar_tramo=usar_tramo,
                min_inicio=int(min_inicio),
                min_fin=int(min_fin),
                cookies_txt=cookies_yt,
            )

        ruta_audio = extraer_audio_ligero(ruta_video, carpeta_temp)

        # PASO 2: Transcripción en GPU + Análisis con Gemini
        barra.progress(45, text="🎙️ Paso 2/3: Transcribiendo audio con Whisper en GPU...")
        resultado_whisper = modelo_whisper.transcribe(
            ruta_audio,
            language="es",
            fp16=(dispositivo_whisper == "cuda"),
        )
        segmentos = resultado_whisper.get("segments", [])
        if not segmentos:
            raise RuntimeError("No se detectó voz clara en el tramo seleccionado.")

        barra.progress(68, text="🧠 Paso 2/3: Gemini analizando texto y detectando momentos virales...")
        momentos = detectar_momentos_virales(api_key=api_key, segmentos=segmentos, max_clips=max_clips)

        # PASO 3: Renderizado vertical 9:16 con NVENC y empaquetado ZIP
        clips_listos = []
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            total = max(1, len(momentos))
            for idx, m in enumerate(momentos, start=1):
                progreso = 70 + int((idx / total) * 28)
                titulo = str(m.get("titulo", f"Clip Viral {idx}"))
                inicio = float(m.get("inicio", 0))
                fin = float(m.get("fin", inicio + 30))
                puntaje = int(m.get("puntaje_viral", 90))
                razon = str(m.get("razon", "Momento destacado de alta retención."))

                barra.progress(min(98, progreso), text=f"🎬 Paso 3/3: Renderizando clip {idx}/{total} en vertical (9:16)...")
                nombre_archivo = f"clip_{idx:02d}_{limpiar_nombre(titulo)}.mp4"
                ruta_clip_final = os.path.join(carpeta_persistente, nombre_archivo)

                renderizar_clip_vertical(
                    ruta_video=ruta_video,
                    inicio=inicio,
                    fin=fin,
                    estilo=estilo_encuadre,
                    ruta_salida=ruta_clip_final,
                    usar_gpu=gpu_nvenc_activa,
                )

                with open(ruta_clip_final, "rb") as f_clip:
                    bytes_clip = f_clip.read()

                zf.writestr(nombre_archivo, bytes_clip)
                clips_listos.append(
                    {
                        "numero": idx,
                        "titulo": titulo,
                        "inicio": inicio,
                        "fin": fin,
                        "puntaje": puntaje,
                        "razon": razon,
                        "nombre_archivo": nombre_archivo,
                        "ruta": ruta_clip_final,
                        "bytes": bytes_clip,
                    }
                )

        zip_buffer.seek(0)
        st.session_state["clips_generados"] = clips_listos
        st.session_state["zip_bytes"] = zip_buffer.getvalue()
        barra.progress(100, text="✅ ¡Proceso completado! Tus clips están listos abajo.")

    except Exception as e:
        st.error(f"❌ Ocurrió un error durante el proceso:\n\n{e}")
    finally:
        shutil.rmtree(carpeta_temp, ignore_errors=True)

# ==========================================
# SECCIÓN PERSISTENTE DE RESULTADOS Y DESCARGA MÚLTIPLE
# (Fuera del botón principal para que no se borre al descargar)
# ==========================================
if st.session_state["clips_generados"]:
    clips = st.session_state["clips_generados"]
    st.markdown("---")
    st.subheader(f"🔥 {len(clips)} Clips Virales Listos para Descargar")

    col_zip, col_limpiar = st.columns([4, 1])
    with col_zip:
        if st.session_state["zip_bytes"]:
            st.download_button(
                label=f"📦 Descargar TODOS los clips juntos ({len(clips)} videos en .ZIP)",
                data=st.session_state["zip_bytes"],
                file_name="cliperos_ai_pack_viral.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
                key="btn_descargar_zip_completo",
            )
    with col_limpiar:
        if st.button("🗑️ Limpiar resultados", use_container_width=True, key="btn_limpiar_clips"):
            st.session_state["clips_generados"] = []
            st.session_state["zip_bytes"] = None
            st.rerun()

    st.markdown("")

    # Mostrar clips en cuadrícula de 2 columnas con descarga individual sin recargar estado
    cols = st.columns(2)
    for idx, clip in enumerate(clips):
        with cols[idx % 2]:
            with st.container(border=True):
                st.markdown(f"### #{clip['numero']} · {clip['titulo']}")
                st.caption(
                    f"🔥 Puntaje viral: **{clip['puntaje']}/100** · "
                    f"⏱️ Tramo: `{segundos_a_hhmmss(clip['inicio'])}` → `{segundos_a_hhmmss(clip['fin'])}`"
                )
                st.write(clip["razon"])
                st.video(clip["bytes"])
                st.download_button(
                    label=f"⬇️ Descargar Clip #{clip['numero']} (MP4)",
                    data=clip["bytes"],
                    file_name=clip["nombre_archivo"],
                    mime="video/mp4",
                    use_container_width=True,
                    key=f"btn_dl_individual_{clip['numero']}",
                )
