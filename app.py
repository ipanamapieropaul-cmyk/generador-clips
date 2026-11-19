import streamlit as st
import yt_dlp
import json
import os
import glob
import subprocess
from concurrent.futures import ThreadPoolExecutor
from faster_whisper import WhisperModel
from google import genai

# Buscar FFmpeg instalado por winget
rutas_winget = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\**\bin"), recursive=True)
carpeta_ffmpeg = rutas_winget[0] if rutas_winget else None
ffmpeg_bin = os.path.join(carpeta_ffmpeg, "ffmpeg.exe") if carpeta_ffmpeg else "ffmpeg"
if carpeta_ffmpeg and carpeta_ffmpeg not in os.environ["PATH"]:
    os.environ["PATH"] = carpeta_ffmpeg + os.pathsep + os.environ["PATH"]

st.set_page_config(page_title="AI Viral Clipper", page_icon="⚡", layout="wide")
st.title("⚡ Generador de Clips Virales (Descarga Paralela)")
st.markdown("Optimizado para **YouTube, Twitch y Kick** con descargas concurrentes y corte directo por GPU.")

# Leer la API Key automáticamente desde .streamlit/secrets.toml
api_key = st.secrets["GEMINI_API_KEY"]
url = st.text_input("Pega el enlace del video o directo (YouTube, Twitch, Kick):")

def limpiar_temporales():
    for archivo in os.listdir():
        if archivo.startswith("audio_temp") or archivo.startswith("fragmento_"):
            try:
                os.remove(archivo)
            except:
                pass

def descargar_y_recortar_clip(datos):
    i, clip, url_video, folder_clips = datos
    inicio = float(clip['inicio'])
    fin = float(clip['fin'])
    temp_fragmento = f"fragmento_{i}.mp4"
    ruta_final = os.path.join(folder_clips, f"viral_{i+1}.mp4")

    ydl_clip_opts = {
        'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
        'outtmpl': temp_fragmento,
        'download_ranges': yt_dlp.utils.download_range_func(None, [(inicio, fin)]),
        'concurrent_fragment_downloads': 8,
        'quiet': True
    }
    if carpeta_ffmpeg:
        ydl_clip_opts['ffmpeg_location'] = carpeta_ffmpeg

    with yt_dlp.YoutubeDL(ydl_clip_opts) as ydl:
        ydl.download([url_video])

    if os.path.exists(temp_fragmento):
        comando_gpu = [
            ffmpeg_bin, "-y", "-i", temp_fragmento,
            "-vf", "crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0",
            "-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23",
            "-c:a", "aac", "-b:a", "128k",
            ruta_final
        ]
        resultado = subprocess.run(comando_gpu, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if resultado.returncode != 0:
            comando_cpu = [
                ffmpeg_bin, "-y", "-i", temp_fragmento,
                "-vf", "crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                ruta_final
            ]
            subprocess.run(comando_cpu, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return (i, clip, ruta_final)
    return None

if st.button("⚡ Buscar y Crear Clips Virales") and url:
    limpiar_temporales()
    client = genai.Client(api_key=api_key)

    with st.spinner("Paso 1/4: Descargando audio ligero a máxima velocidad..."):
        ydl_opts = {
            'format': 'worstaudio/bestaudio',
            'outtmpl': 'audio_temp.%(ext)s',
            'concurrent_fragment_downloads': 8,
            'quiet': True
        }
        if carpeta_ffmpeg:
            ydl_opts['ffmpeg_location'] = carpeta_ffmpeg

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            archivo_audio = ydl.prepare_filename(info)

    with st.spinner("Paso 2/4: Transcribiendo audio con IA..."):
        try:
            model = WhisperModel("base", device="cuda", compute_type="float16")
        except Exception:
            model = WhisperModel("tiny", device="cpu", compute_type="int8")

        segments, _ = model.transcribe(archivo_audio, beam_size=1, vad_filter=True)
        lineas_transcripcion = [f"[{s.start:.1f}s - {s.end:.1f}s]: {s.text}" for s in segments]

    with st.spinner("Paso 3/4: Detectando mejores momentos para TikTok..."):
        texto_completo = "\n".join(lineas_transcripcion)
        tamano_bloque = 30000
        bloques = [texto_completo[i:i + tamano_bloque] for i in range(0, len(texto_completo), tamano_bloque)]
        
        todos_los_clips = []
        for idx, bloque in enumerate(bloques):
            prompt = f"""
            Eres un editor experto en retención viral para TikTok. Analiza este fragmento de transcripción
            (Parte {idx+1} de {len(bloques)}) con tiempos en segundos y extrae los 2 MEJORES clips virales.
            Reglas:
            - Cada clip debe durar entre 30 y 60 segundos (fin - inicio).
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

    with st.spinner("Paso 4/4: Descargando y recortando los 4 clips al mismo tiempo..."):
        os.makedirs("clips_videos", exist_ok=True)
        tareas = [(i, clip, url, "clips_videos") for i, clip in enumerate(todos_los_clips)]
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            resultados = list(executor.map(descargar_y_recortar_clip, tareas))

        for res in resultados:
            if res:
                i, clip, ruta_final = res
                st.divider()
                st.subheader(f"🔥 Clip #{i+1}: {clip['titulo']} — Puntaje Viral: {clip['puntaje']}/100")
                st.write(f"**Por qué funciona en TikTok:** {clip['razon']}")
                st.video(ruta_final)

    limpiar_temporales()
    st.success("¡Listo! Descarga y recorte completados en paralelo.")
    