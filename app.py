import os
import json
import time
import requests
import threading
import glob
import uuid
import concurrent.futures
from flask import Flask, request, jsonify, render_template, send_from_directory
from groq import Groq
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip, TextClip
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configurações
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

groq_client = Groq(api_key=GROQ_API_KEY)

# Gerenciamento de sessões (para edição de frases)
sessions = {}  # session_id -> {niche, visualStyle, customText, mode, provider, ratio, logo_path, script, prompts, phrases}

# Trava para processamento paralelo (usaremos threads)
process_lock = threading.Lock()

# Limpeza de arquivos antigos
def cleanup_old_files():
    patterns = ['*.mp3', '*.jpg', '*.png', 'final_video.mp4']
    for pattern in patterns:
        files = glob.glob(pattern)
        for f in files:
            try:
                os.remove(f)
            except:
                pass
    # Limpa uploads antigos (mais de 1 hora)
    for f in glob.glob(os.path.join(UPLOAD_FOLDER, "*")):
        if time.time() - os.path.getmtime(f) > 3600:
            try:
                os.remove(f)
            except:
                pass

def get_dimensions(ratio):
    ratios = {
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "1:1": (1024, 1024),
        "4:5": (1080, 1350)
    }
    return ratios.get(ratio, (1024, 1024))

def apply_logo(image_path, logo_path):
    if not logo_path or not os.path.exists(logo_path):
        return image_path
    try:
        base = Image.open(image_path).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
        base_w, base_h = base.size
        logo_w, logo_h = logo.size
        new_logo_w = int(base_w * 0.15)
        new_logo_h = int(logo_h * (new_logo_w / logo_w))
        logo = logo.resize((new_logo_w, new_logo_h), Image.Resampling.LANCZOS)
        pos = (base_w - new_logo_w - 20, base_h - new_logo_h - 20)
        base.paste(logo, pos, logo)
        base.convert("RGB").save(image_path)
        return image_path
    except Exception as e:
        print(f"Erro ao aplicar logo: {e}")
        return image_path

def add_text_to_image(image_path, text, position='bottom', font_size=40, color='white', stroke_width=2):
    """Adiciona texto a uma imagem usando PIL"""
    try:
        img = Image.open(image_path).convert("RGBA")
        draw = ImageDraw.Draw(img)
        # Usar fonte padrão (se disponível, tenta uma TrueType)
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except:
            font = ImageFont.load_default()
        
        # Medir texto
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        
        # Posicionar
        img_w, img_h = img.size
        if position == 'bottom':
            x = (img_w - text_w) // 2
            y = img_h - text_h - 30
        elif position == 'top':
            x = (img_w - text_w) // 2
            y = 30
        else:  # center
            x = (img_w - text_w) // 2
            y = (img_h - text_h) // 2
        
        # Desenhar contorno
        draw.text((x-2, y-2), text, font=font, fill='black')
        draw.text((x+2, y-2), text, font=font, fill='black')
        draw.text((x-2, y+2), text, font=font, fill='black')
        draw.text((x+2, y+2), text, font=font, fill='black')
        draw.text((x, y), text, font=font, fill=color)
        
        img.save(image_path)
    except Exception as e:
        print(f"Erro ao adicionar texto: {e}")

def generate_script_and_prompts(niche, visual_style, custom_text):
    """Gera roteiro e frases curtas para cada imagem"""
    prompt = f"""
    Você é um roteirista profissional. Crie um roteiro curto de 30 segundos sobre "{niche}" no estilo visual "{visual_style}".
    O roteiro deve conter um texto falado (script) e uma lista de 4 frases curtas e impactantes (uma para cada imagem) que complementem o roteiro.
    Se o usuário forneceu o texto adicional "{custom_text}", incorpore-o de forma natural nas frases ou no roteiro.
    Retorne estritamente um JSON com as chaves: "script" (string), "phrases" (lista de 4 strings).
    Exemplo:
    {{
        "script": "Bem-vindo ao mundo das curiosidades históricas. Você sabia que...",
        "phrases": ["Curiosidade 1", "Fato incrível 2", "Você não vai acreditar", "Compartilhe com um amigo"]
    }}
    """
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
            data = json.loads(response.choices[0].message.content)
            if "script" in data and "phrases" in data and len(data["phrases"]) >= 4:
                return data
        except Exception as e:
            print(f"Falha no modelo {model}: {e}")
            continue
    raise Exception("Não foi possível gerar roteiro com os modelos disponíveis.")

def generate_images_parallel(prompts, width, height, provider, logo_path, phrases, custom_text, session_id):
    """Gera imagens em paralelo usando ThreadPoolExecutor"""
    def generate_one(index, prompt):
        # Gera imagem
        if provider == "pollinations":
            encoded = requests.utils.quote(prompt)
            url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&nologo=true&model=flux"
            img_data = requests.get(url).content
        else:  # leonardo
            # (código simplificado, assumindo que temos a API key)
            headers = {"authorization": f"Bearer {LEONARDO_API_KEY}"}
            payload = {"height": height, "width": width, "prompt": prompt, "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3", "num_images": 1}
            create_res = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
            gen_id = create_res.json().get("sdGenerationJob", {}).get("generationId")
            if not gen_id:
                return None
            # Polling
            for _ in range(30):  # timeout 90s
                time.sleep(3)
                get_res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers)
                data = get_res.json()
                if data.get("generations_by_pk", {}).get("status") == "COMPLETE":
                    img_url = data["generations_by_pk"]["generated_images"][0]["url"]
                    img_data = requests.get(img_url).content
                    break
            else:
                return None
        
        # Salvar
        img_path = f"image_{session_id}_{index}.jpg"
        with open(img_path, "wb") as f:
            f.write(img_data)
        
        # Aplicar logo e texto
        if logo_path:
            apply_logo(img_path, logo_path)
        if phrases and index < len(phrases):
            add_text_to_image(img_path, phrases[index])
        if custom_text:
            # Adiciona um pequeno rodapé com o texto customizado
            add_text_to_image(img_path, custom_text, position='bottom', font_size=24, color='#cccccc')
        return img_path
    
    # Paralelização
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(generate_one, i, prompts[i]): i for i in range(len(prompts))}
        results = []
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return [r for r in results if r is not None]

def generate_audio(text, filename="output_audio.mp3"):
    safe_text = text.replace('"', '').replace("'", "")
    command = f'edge-tts --text "{safe_text}" --voice pt-BR-AntonioNeural --write-media {filename}'
    os.system(command)
    return filename

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate_script', methods=['POST'])
def generate_script():
    """Primeira etapa: gera roteiro e frases, salva na sessão"""
    niche = request.form.get('niche')
    visual_style = request.form.get('visualStyle')
    custom_text = request.form.get('customText')
    mode = request.form.get('mode')
    provider = request.form.get('provider')
    ratio = request.form.get('ratio')
    
    if not niche:
        return jsonify({"success": False, "error": "Nicho ausente"}), 400
    
    # Salvar logo se houver
    logo_path = None
    if 'logo' in request.files:
        logo_file = request.files['logo']
        if logo_file.filename:
            filename = secure_filename(logo_file.filename)
            logo_path = os.path.join(UPLOAD_FOLDER, filename)
            logo_file.save(logo_path)
    
    try:
        content = generate_script_and_prompts(niche, visual_style, custom_text)
        script = content["script"]
        phrases = content["phrases"]
        
        # Gerar prompts para imagens (baseado no roteiro e estilo)
        # Cada prompt será uma frase da imagem + estilo + pessoas reais se solicitado
        image_prompts = []
        for phrase in phrases:
            prompt = f"{phrase} - Estilo: {visual_style}. Inclua pessoas reais em um ambiente natural, fotografia realista."
            image_prompts.append(prompt)
        
        # Criar ID de sessão
        session_id = str(uuid.uuid4())
        sessions[session_id] = {
            "niche": niche,
            "visual_style": visual_style,
            "custom_text": custom_text,
            "mode": mode,
            "provider": provider,
            "ratio": ratio,
            "logo_path": logo_path,
            "script": script,
            "prompts": image_prompts,
            "phrases": phrases
        }
        
        return jsonify({
            "success": True,
            "session_id": session_id,
            "phrases": phrases,
            "script": script
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/generate_final', methods=['POST'])
def generate_final():
    """Segunda etapa: gera imagens/vídeo com as frases editadas"""
    data = request.json
    session_id = data.get('session_id')
    edited_phrases = data.get('phrases')
    
    if session_id not in sessions:
        return jsonify({"success": False, "error": "Sessão inválida"}), 400
    
    session = sessions[session_id]
    # Atualizar frases editadas
    session["phrases"] = edited_phrases
    
    mode = session["mode"]
    provider = session["provider"]
    ratio = session["ratio"]
    logo_path = session["logo_path"]
    script = session["script"]
    prompts = session["prompts"]
    phrases = session["phrases"]
    custom_text = session["custom_text"]
    
    # Obter dimensões
    width, height = get_dimensions(ratio)
    
    # Geração paralela de imagens
    image_paths = generate_images_parallel(
        prompts, width, height, provider, logo_path, phrases, custom_text, session_id
    )
    
    if not image_paths:
        return jsonify({"success": False, "error": "Falha na geração das imagens"}), 500
    
    if mode == "video":
        # Gerar áudio
        audio_path = generate_audio(script)
        audio = AudioFileClip(audio_path)
        duration_per_image = audio.duration / len(image_paths)
        clips = [ImageClip(img).set_duration(duration_per_image) for img in image_paths]
        video = concatenate_videoclips(clips, method="compose").set_audio(audio)
        output_file = f"final_video_{session_id}.mp4"
        video.write_videofile(output_file, fps=24, codec="libx264", audio_codec="aac", threads=1, verbose=False, logger=None)
        return jsonify({"success": True, "file": output_file, "type": "video"})
    
    elif mode == "carousel":
        # Retornar lista de imagens
        return jsonify({"success": True, "files": image_paths, "type": "carousel"})
    
    else:  # mode == "image"
        # Retornar apenas a primeira imagem (ou todas, mas simplificamos)
        return jsonify({"success": True, "file": image_paths[0], "type": "image"})

@app.route('/status/<session_id>')
def get_status(session_id):
    # (opcional: implementar um tracker de progresso)
    return jsonify({"step": "Processando...", "remaining": 0})

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(os.getcwd(), filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
