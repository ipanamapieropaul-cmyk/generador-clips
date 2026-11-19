import streamlit as st
import yt_dlp
import json
import os
import glob
import subprocess
from concurrent.futures import ThreadPoolExecutor
from faster_whisper import WhisperModel
from google import genai

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
    1. **Analiza el audio** del video o tramo elegido.
    2. **Transcribe cada palabra** con tiempos exactos.
    3. **Detecta los momentos más virales** con IA.
    4. **Renderiza en vertical (9:16)** listo para subir.
    """)
    st.divider()
    st.subheader("⏱️ Directos largos (Kick / Twitch)")
    st.info(
        "Si pegas un directo de **2 a 8 horas**, activa la opción **'Analizar solo un tramo por minutos'** para procesar bloques de 45–60 minutos en menos de 2 minutos."
    )
    st.subheader("🎯 ¿Qué formato elegir?")
    st.info(
        "**• Zoom centrado (9:16):** Ideal para podcasts, charlas o reacciones.\n\n"
        "**• Fondo borroso:** Ideal para gameplays, fútbol o streams completos."
    )

# --- CABECERA VISUAL ---
st.title("🎬 Cliperos AI: Convierte videos largos en Clips Virales")
st.caption("Extrae automáticamente los mejores momentos en formato vertical (9:16) listos para TikTok, Reels y Shorts.")

col1, col2, col3 = st.columns(3)
with col1:
    st.info("**Paso 1: Elige tu fuente**\n\nPega un enlace de YouTube, Twitch o Kick, o sube un MP4.")
with col2:
    st.info("**Paso 2: Encuadre y tramo**\n\nElige zoom o fondo borroso (y filtra minutos si es un directo largo).")
with col3:
    st.info("**Paso 3: Descarga y sube**\n\nRevisa el puntaje viral de cada clip y descárgalo en MP4.")

st.divider()

api_key = st.secrets["GEMINI_API_KEY"]

tab_enlace, tab_archivo = st.tabs(["🔗 Pegar Enlace (YouTube / Twitch / Kick)", "📁 Subir Video MP4 Directo"])

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

# Selector de tramo para directos largos
usar_tramo = st.checkbox("⏱️ Analizar solo un tramo específico por minutos (Recomendado para directos de +1 hora)")
min_inicio = 0
min_fin = 60
if usar_tramo:
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        min_inicio = st.number_input("Minuto de inicio:", min_value=0, max_value=1440, value=0, step=5)
    with col_t2:
        min_fin = st.number_input("Minuto de fin:", min_value=1, max_value=1440, value=60, step=5)

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
        if archivo.startswith(("audio_temp", "fragmento_", "video_local_temp")):
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
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        ]
    else:
        filtro_ffmpeg = ["-vf", "crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0"]

    recorte_tiempo = ["-ss", str(inicio), "-to", str(fin)] if (inicio is not None and fin is not None) else []

    comando_gpu = [
        ffmpeg_bin, "-y", *recorte_tiempo, "-i", entrada_mp4,
        *filtro_ffmpeg,
        "-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23",
        "-c:a", "aac", "-b:a", "128k",
        ruta_final
    ]
    res = subprocess.run(comando_gpu, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        comando_cpu = [
            ffmpeg_bin, "-y", *recorte_tiempo, "-i", entrada_mp4,
            *filtro_ffmpeg,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
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
            'format': 'best[height<=1080][ext=mp4]/best[height<=1080]/best',
            'outtmpl': temp_fragmento,
            'download_ranges': yt_dlp.utils.download_range_func(None, [(inicio, fin)]),
        }
        with yt_dlp.YoutubeDL(ydl_clip_opts) as ydl:
            ydl.download([fuente])

        if os.path.exists(temp_fragmento):
            renderizar_clip_ffmpeg(temp_fragmento, ruta_final, usar_fondo_borroso)
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

        # Paso 1: Obtener audio (completo o solo el tramo elegido)
        barra.progress(15, text="📥 Paso 1/4: Descargando y preparando pista de audio...")
        if es_archivo_local:
            ruta_video_local = "video_local_temp.mp4"
            with open(ruta_video_local, "wb") as f:
                f.write(archivo_subido.getbuffer())
            archivo_audio = "audio_temp.mp3"
            recorte_audio = ["-ss", str(min_inicio * 60), "-to", str(min_fin * 60)] if usar_tramo else []
            subprocess.run(
                [ffmpeg_bin, "-y", *recorte_audio, "-i", ruta_video_local, "-vn", "-acodec", "libmp3lame", "-q:a", "4", archivo_audio],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            fuente_clips = ruta_video_local
        else:
            ydl_opts = {
                **OPCIONES_YTDLP_BASE,
                'format': 'bestaudio/best',
                'outtmpl': 'audio_temp.%(ext)s',
            }
            if usar_tramo:
                ydl_opts['download_ranges'] = yt_dlp.utils.download_range_func(None, [(min_inicio * 60, min_fin * 60)])

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    archivo_audio = ydl.prepare_filename(info)
                fuente_clips = url
            except Exception as e:
                barra.empty()
                st.error(f"⚠️ No se pudo descargar el enlace ({str(e)[:140]}). Si las cookies expiraron, renuévalas o usa la pestaña 'Subir Video MP4 Directo'.")
                st.stop()

        # Paso 2: Transcribir
        barra.progress(40, text="🎙️ Paso 2/4: Transcribiendo voz a texto con IA...")
        try:
            model = WhisperModel("base", device="cuda", compute_type="float16")
        except Exception:
            model = WhisperModel("tiny", device="cpu", compute_type="int8")

        segments, _ = model.transcribe(archivo_audio, beam_size=1, vad_filter=True)
        # Sumamos el desfase_segundos para conservar el tiempo real dentro del directo original
        lineas_transcripcion = [
            f"[{(s.start + desfase_segundos):.1f}s - {(s.end + desfase_segundos):.1f}s]: {s.text}"
            for s in segments
        ]

        # Paso 3: Analizar con Gemini
        barra.progress(65, text="🧠 Paso 3/4: Detectando los mejores ganchos virales...")
        texto_completo = "\n".join(lineas_transcripcion)
        tamano_bloque = 30000
        bloques = [texto_completo[i:i + tamano_bloque] for i in range(0, len(texto_completo), tamano_bloque)]
        
        todos_los_clips = []
        for idx, bloque in enumerate(bloques):
            prompt = f"""
            Eres un editor experto en retención viral para TikTok. Analiza este fragmento de transcripción
            (Parte {idx+1} de {len(bloques)}) con tiempos en segundos y extrae los 2 MEJORES clips virales.
            Reglas:
            - Cada clip debe durar entre 25 y 60 segundos (fin - inicio).
            - Usa exactamente los tiempos en segundos que aparecen entre corchetes.
            - Busca ganchos fuertes al inicio, historias completas, risas, polémica o datos clave.
            
            Responde ÚNICAMENTE con un JSON válido:
            [
              {{"titulo": "Gancho atractivo", "inicio": 120.5, "fin": 165.0, "puntaje": 94, "razon": "Por qué retiene audiencia"}}
            ]

            Transcripción:
            {bloque}
            """
            respuesta = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt
            )
            texto_json = respuesta.text.replace("```json", "").replace("```", "").strip()
            try:
                todos_los_clips.extend(json.loads(texto_json))
            except:
                continue

        todos_los_clips = sorted(todos_los_clips, key=lambda x: x.get('puntaje', 0), reverse=True)[:4]

        # Paso 4: Renderizar clips
        barra.progress(85, text="✂️ Paso 4/4: Renderizando tus clips con el encuadre elegido...")
        os.makedirs("clips_videos", exist_ok=True)
        tareas = [(i, clip, fuente_clips, es_archivo_local, "clips_videos", usar_fondo_borroso) for i, clip in enumerate(todos_los_clips)]
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            resultados = list(executor.map(procesar_clip_tarea, tareas))

        barra.progress(100, text="✅ ¡Proceso completado!")
        st.balloons()

        for res in resultados:
            if res:
                i, clip, ruta_final = res
                st.divider()
                col_vid, col_info = st.columns([1, 1.5])
                with col_vid:
                    st.video(ruta_final)
                with col_info:
                    st.subheader(f"🔥 Clip #{i+1}: {clip['titulo']}")
                    st.metric(label="Potencial Viral estimado", value=f"{clip['puntaje']} / 100")
                    st.write(f"**¿Por qué retiene audiencia?** {clip['razon']}")
                    st.caption(f"⏱️ Marca en el directo: {formatear_tiempo(clip['inicio'])} al {formatear_tiempo(clip['fin'])}")
                    
                    with open(ruta_final, "rb") as file_vid:
                        st.download_button(
                            label=f"⬇️ Descargar Clip #{i+1} (MP4)",
                            data=file_vid,
                            file_name=f"clip_viral_{i+1}.mp4",
                            mime="video/mp4",
                            use_container_width=True
                        )

        limpiar_temporales()
