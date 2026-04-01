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

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

groq_client = Groq(api_key=GROQ_API_KEY)

# TRAVA DE SEGURANÇA: Só permite um processamento por vez
process_lock = threading.Lock()

# Estado global para o progresso
progress_status = {"step": "Ocioso", "remaining": 0}

def cleanup_old_files():
    """Deleta arquivos temporários para economizar espaço no servidor"""
    patterns = ['*.mp3', '*.jpg', '*.png', 'final_video.mp4']
    for pattern in patterns:
        files = glob.glob(pattern)
        for f in files:
            try:
                os.remove(f)
                print(f"Limpeza: {f} removido.")
            except Exception as e:
                print(f"Erro na limpeza: {e}")
    
    # Limpa pasta de uploads (logos)
    logos = glob.glob(os.path.join(UPLOAD_FOLDER, "*"))
    for l in logos:
        try:
            os.remove(l)
        except:
            pass

def get_dimensions(ratio):
    """Retorna as dimensões baseadas no formato escolhido"""
    if ratio == "16:9":
        return (1280, 720)
    elif ratio == "9:16":
        return (720, 1280)
    elif ratio == "1:1":
        return (1024, 1024)
    elif ratio == "4:5":
        return (1080, 1350)
    else:
        return (1024, 1024)

def apply_logo(image_path, logo_path):
    """Aplica o logo sobre a imagem gerada utilizando Pillow"""
    if not logo_path or not os.path.exists(logo_path):
        return image_path
    
    try:
        base_image = Image.open(image_path).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
        
        base_w, base_h = base_image.size
        logo_w, logo_h = logo.size
        
        # Redimensiona o logo para 15% da largura da imagem
        new_logo_w = int(base_w * 0.15)
        new_logo_h = int(logo_h * (new_logo_w / logo_w))
        logo = logo.resize((new_logo_w, new_logo_h), Image.Resampling.LANCZOS)
        
        # Posiciona no canto inferior direito (margem de 20px)
        pos_x = base_w - new_logo_w - 20
        pos_y = base_h - new_logo_h - 20
        
        base_image.paste(logo, (pos_x, pos_y), logo)
        rgb_image = base_image.convert("RGB")
        rgb_image.save(image_path)
        return image_path
    except Exception as e:
        print(f"Erro ao aplicar logo: {e}")
        return image_path

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
    command = f'edge-tts --text "{safe_text}" --voice pt-BR-AntonioNeural --write-media {filename}'
    os.system(command)
    return filename

def generate_images_pollinations(prompts, width, height, logo_path):
    downloaded_files = []
    for index, prompt in enumerate(prompts):
        global progress_status
        progress_status["remaining"] = 30 - (index * 5)
        
        encoded_prompt = requests.utils.quote(prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&nologo=true&model=flux&seed={int(time.time()) + index}"
        
        img_data = requests.get(image_url).content
        img_filename = f"image_{index}.jpg"
        
        with open(img_filename, "wb") as handler:
            handler.write(img_data)
        
        apply_logo(img_filename, logo_path)
        downloaded_files.append(img_filename)
        
    return downloaded_files

def generate_images_leonardo(prompts, width, height, logo_path):
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {LEONARDO_API_KEY}"
    }
    
    downloaded_files = []
    for index, prompt in enumerate(prompts):
        global progress_status
        progress_status["remaining"] = 40 - (index * 8)
        
        payload = {
            "height": height,
            "width": width,
            "prompt": prompt,
            "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3",
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
            
        apply_logo(img_filename, logo_path)
        downloaded_files.append(img_filename)
        
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
def generate_video():
    global progress_status
    if not process_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "O motor já está processando um ativo."}), 429
    
    try:
        # Pega dados do Form (necessário para Upload de arquivo)
        niche = request.form.get('niche')
        mode = request.form.get('mode', 'video')
        provider = request.form.get('provider', 'pollinations')
        ratio = request.form.get('ratio', '9:16')
        
        if not niche:
            return jsonify({"success": False, "error": "Nicho ausente"}), 400

        cleanup_old_files()
        
        # Salva logo se houver
        logo_path = None
        if 'logo' in request.files:
            logo_file = request.files['logo']
            if logo_file.filename != '':
                filename = secure_filename(logo_file.filename)
                logo_path = os.path.join(UPLOAD_FOLDER, filename)
                logo_file.save(logo_path)

        width, height = get_dimensions(ratio)
            
        progress_status = {"step": "Criando Roteiro e Prompts...", "remaining": 50}
        content = generate_script_and_prompts(niche)
        script = content.get("script")
        prompts = content.get("prompts")
        
        if provider == "leonardo":
            progress_status = {"step": "Gerando Imagens (Leonardo)...", "remaining": 45}
            image_paths = generate_images_leonardo(prompts, width, height, logo_path)
        else:
            progress_status = {"step": "Gerando Imagens (Pollinations)...", "remaining": 35}
            image_paths = generate_images_pollinations(prompts, width, height, logo_path)
        
        if mode == "video":
            progress_status = {"step": "Gerando Áudio...", "remaining": 20}
            audio_path = generate_audio(script)
            
            progress_status = {"step": "Compilando Vídeo Final...", "remaining": 15}
            audio = AudioFileClip(audio_path)
            duration_per_image = audio.duration / len(image_paths)
            
            clips = []
            for img in image_paths:
                clip = ImageClip(img).set_duration(duration_per_image)
                clips.append(clip)
                
            video = concatenate_videoclips(clips, method="compose")
            video = video.set_audio(audio)
            video.write_videofile("final_video.mp4", fps=24, codec="libx264", audio_codec="aac", threads=1)
            
            progress_status = {"step": "Concluído", "remaining": 0}
            return jsonify({"success": True, "file": "final_video.mp4", "type": "video"})
        else:
            progress_status = {"step": "Concluído", "remaining": 0}
            # Se for apenas imagem, retorna a primeira ou zip (aqui retorna a primeira para simplificar)
            return jsonify({"success": True, "file": image_paths[0], "type": "image"})
        
    except Exception as e:
        progress_status = {"step": "Erro fatal", "remaining": 0}
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        process_lock.release()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
