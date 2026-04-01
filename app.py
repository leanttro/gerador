import os
import json
import time
import requests
import threading
import glob
from flask import Flask, request, jsonify, render_template, send_from_directory
from groq import Groq
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
from PIL import Image
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configurações de Ambiente
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

groq_client = Groq(api_key=GROQ_API_KEY)

# TRAVA DE SEGURANÇA: Só permite um processamento por vez
process_lock = threading.Lock()

# Estado global para o progresso visível no frontend
progress_status = {"step": "Ocioso", "remaining": 0}

def cleanup_old_files():
    """Deleta arquivos temporários para economizar espaço no servidor"""
    patterns = ['*.mp3', '*.jpg', '*.png', 'final_video.mp4', 'uploads/*']
    for pattern in patterns:
        files = glob.glob(pattern)
        for f in files:
            try:
                if os.path.isfile(f):
                    os.remove(f)
                    print(f"Limpeza: {f} removido.")
            except Exception as e:
                print(f"Erro ao limpar {f}: {e}")

def get_dimensions(ratio):
    """Retorna as dimensões baseadas no formato escolhido"""
    ratios = {
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "1:1": (1024, 1024),
        "4:5": (1080, 1350)
    }
    return ratios.get(ratio, (1024, 1024))

def apply_logo(image_path, logo_path, position=("right", "bottom")):
    """Aplica o logo sobre a imagem gerada utilizando Pillow"""
    if not logo_path or not os.path.exists(logo_path):
        return image_path
    
    base_image = Image.open(image_path).convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")
    
    # Redimensiona o logo para ocupar 15% da largura da imagem base
    base_w, base_h = base_image.size
    logo_w, logo_h = logo.size
    new_logo_w = int(base_w * 0.15)
    new_logo_h = int(logo_h * (new_logo_w / logo_w))
    logo = logo.resize((new_logo_w, new_logo_h), Image.Resampling.LANCZOS)
    
    # Calcula posição (margem de 20px)
    if position == ("right", "bottom"):
        pos_x = base_w - new_logo_w - 20
        pos_y = base_h - new_logo_h - 20
    
    base_image.paste(logo, (pos_x, pos_y), logo)
    rgb_image = base_image.convert("RGB")
    rgb_image.save(image_path)
    return image_path

def generate_script_and_prompts(niche):
    """Gera roteiro e prompts via Groq"""
    prompt = f"Crie um roteiro curto de 30 segundos sobre {niche}. Retorne estritamente um JSON com a chave script contendo o texto falado e a chave prompts contendo uma lista de 4 prompts em ingles detalhados para gerar imagens em IA."
    
    models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"]
    
    for model in models:
        try:
            response = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                response_format={"type": "json_object"}
            )
            return json.loads(response.choices[0].message.content)
        except Exception:
            continue
    raise Exception("Modelos Groq falharam.")

def generate_audio(text, filename="output_audio.mp3"):
    """Gera voz via Microsoft Edge TTS"""
    safe_text = text.replace('"', '').replace("'", "")
    command = f'edge-tts --text "{safe_text}" --voice pt-BR-AntonioNeural --write-media {filename}'
    os.system(command)
    return filename

def generate_images_pollinations(prompts, width, height, logo_path):
    """Gera imagens via Pollinations (Grátis)"""
    downloaded_files = []
    for index, p in enumerate(prompts):
        global progress_status
        progress_status["remaining"] = 30 - (index * 5)
        encoded_prompt = requests.utils.quote(p)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&nologo=true&model=flux&seed={int(time.time())+index}"
        img_data = requests.get(url).content
        filename = f"image_{index}.jpg"
        with open(filename, "wb") as f:
            f.write(img_data)
        apply_logo(filename, logo_path)
        downloaded_files.append(filename)
    return downloaded_files

def generate_images_leonardo(prompts, width, height, logo_path):
    """Gera imagens via Leonardo.ai (Pago)"""
    headers = {"accept": "application/json", "content-type": "application/json", "authorization": f"Bearer {LEONARDO_API_KEY}"}
    downloaded_files = []
    for index, p in enumerate(prompts):
        payload = {"height": height, "width": width, "prompt": p, "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3", "num_images": 1}
        res = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        gen_id = res.json().get("sdGenerationJob", {}).get("generationId")
        if not gen_id: continue
        
        status = "PENDING"
        while status != "COMPLETE":
            time.sleep(3)
            data = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
            status = data.get("generations_by_pk", {}).get("status")
            
        url = data["generations_by_pk"]["generated_images"][0]["url"]
        img_data = requests.get(url).content
        filename = f"image_{index}.jpg"
        with open(filename, "wb") as f:
            f.write(img_data)
        apply_logo(filename, logo_path)
        downloaded_files.append(filename)
    return downloaded_files

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/status')
def get_status():
    return jsonify(progress_status)

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(os.getcwd(), filename)

@app.route('/generate', methods=['POST'])
def generate():
    global progress_status
    if not process_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "Motor ocupado."}), 429
    
    try:
        cleanup_old_files()
        
        # Recebendo dados do FormData
        niche = request.form.get('niche')
        mode = request.form.get('mode', 'video')
        provider = request.form.get('provider', 'pollinations')
        ratio = request.form.get('ratio', '9:16')
        
        logo_path = None
        if 'logo' in request.files:
            file = request.files['logo']
            if file.filename != '':
                logo_path = os.path.join(UPLOAD_FOLDER, secure_filename(file.filename))
                file.save(logo_path)

        width, height = get_dimensions(ratio)
        
        progress_status = {"step": "Criando Ativos...", "remaining": 50}
        content = generate_script_and_prompts(niche)
        prompts = content.get("prompts")
        
        if provider == "leonardo":
            img_paths = generate_images_leonardo(prompts, width, height, logo_path)
        else:
            img_paths = generate_images_pollinations(prompts, width, height, logo_path)
            
        if mode == "video":
            progress_status = {"step": "Compilando Vídeo...", "remaining": 20}
            audio_path = generate_audio(content.get("script"))
            
            audio = AudioFileClip(audio_path)
            clips = [ImageClip(m).set_duration(audio.duration/len(img_paths)) for m in img_paths]
            video = concatenate_videoclips(clips, method="compose").set_audio(audio)
            video.write_videofile("final_video.mp4", fps=24, codec="libx264", audio_codec="aac", threads=1)
            
            progress_status = {"step": "Concluído", "remaining": 0}
            return jsonify({"success": True, "file": "final_video.mp4", "type": "video"})
        else:
            progress_status = {"step": "Concluído", "remaining": 0}
            # Retorna a primeira imagem como exemplo para download se for modo imagem
            return jsonify({"success": True, "file": img_paths[0], "type": "image"})
            
    except Exception as e:
        progress_status = {"step": "Erro", "remaining": 0}
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        process_lock.release()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
