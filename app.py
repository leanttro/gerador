import os
import json
import time
import requests
import threading
import glob
from flask import Flask, request, jsonify, render_template, send_from_directory
from groq import Groq
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip

app = Flask(__name__)

# Sem economia de tokens: Variáveis de Ambiente
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

# TRAVA DE SEGURANÇA: Só permite um render por vez
process_lock = threading.Lock()

def cleanup_old_files():
    """Deleta arquivos temporários para economizar espaço no Dokploy"""
    patterns = ['*.mp3', '*.jpg', '*.png', 'final_video.mp4']
    for pattern in patterns:
        files = glob.glob(pattern)
        for f in files:
            try:
                os.remove(f)
                print(f"Limpeza: {f} removido.")
            except:
                pass

def generate_script_and_prompts(niche):
    prompt = f"Crie um roteiro curto de 30 segundos sobre {niche}. Retorne estritamente um JSON com a chave script contendo o texto falado e a chave prompts contendo uma lista de 4 prompts em ingles detalhados para gerar imagens em IA."
    
    models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "gemma2-9b-it"
    ]
    
    for model in models:
        try:
            response = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                response_format={"type": "json_object"}
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"Falha no modelo {model}: {e}")
            continue
            
    raise Exception("Todos os modelos do Groq falharam.")

def generate_audio(text, filename="output_audio.mp3"):
    safe_text = text.replace('"', '').replace("'", "")
    # Uso do edge-tts (Voz da Microsoft gratuita)
    command = f'edge-tts --text "{safe_text}" --voice pt-BR-AntonioNeural --write-media {filename}'
    os.system(command)
    return filename

def generate_images_leonardo(prompts):
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {LEONARDO_API_KEY}"
    }
    
    downloaded_files = []
    
    for index, prompt in enumerate(prompts):
        payload = {
            "height": 1024,
            "width": 576,
            "prompt": prompt,
            "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3", # Leonardo Vision XL
            "num_images": 1
        }
        
        create_res = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        generation_id = create_res.json().get("sdGenerationJob", {}).get("generationId")
        
        if not generation_id:
            continue
            
        status = "PENDING"
        while status != "COMPLETE":
            time.sleep(3)
            get_res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{generation_id}", headers=headers)
            data = get_res.json()
            status = data.get("generations_by_pk", {}).get("status")
            
        image_url = data["generations_by_pk"]["generated_images"][0]["url"]
        img_data = requests.get(image_url).content
        img_filename = f"image_{index}.jpg"
        
        with open(img_filename, "wb") as handler:
            handler.write(img_data)
            
        downloaded_files.append(img_filename)
        
    return downloaded_files

def compile_video(audio_file, image_files, output_filename="final_video.mp4"):
    audio = AudioFileClip(audio_file)
    duration_per_image = audio.duration / len(image_files)
    
    clips = []
    for img in image_files:
        clip = ImageClip(img).set_duration(duration_per_image)
        clips.append(clip)
        
    video = concatenate_videoclips(clips, method="compose")
    video = video.set_audio(audio)
    
    # LIMITADOR DE CPU: threads=1 evita que o VPS trave 100%
    video.write_videofile(output_filename, fps=24, codec="libx264", audio_codec="aac", threads=1)
    
    return output_filename

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(os.getcwd(), filename)

@app.route('/generate', methods=['POST'])
def generate_video():
    # Tenta adquirir a trava. Se não conseguir, retorna erro 429 (Too Many Requests)
    if not process_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "O motor já está processando um vídeo. Aguarde alguns minutos."}), 429
    
    try:
        data = request.json
        niche = data.get('niche')
        
        if not niche:
            return jsonify({"success": False, "error": "Nicho ausente"}), 400

        # FAXINA ANTES DE COMEÇAR
        cleanup_old_files()
            
        content = generate_script_and_prompts(niche)
        script = content.get("script")
        prompts = content.get("prompts")
        
        audio_path = generate_audio(script)
        image_paths = generate_images_leonardo(prompts)
        video_path = compile_video(audio_path, image_paths)
        
        return jsonify({"success": True, "file": video_path})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        # LIBERA A TRAVA para o próximo uso
        process_lock.release()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
