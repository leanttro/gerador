import os
import json
import time
import requests
import threading
import glob
from flask import Flask, request, jsonify, render_template, send_from_directory
from groq import Groq
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
from PIL import Image, ImageDraw, ImageFont, ImageResampling
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

def load_system_font(size):
    """Tenta carregar uma fonte TrueType do sistema"""
    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\Arial.ttf", # Para Windows local
        "/System/Library/Fonts/Helvetica.ttc", # Para macOS local
    ]
    for font_path in possible_fonts:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except:
                pass
    return ImageFont.load_default()

def apply_text_visual(image_path, text, font_path=None):
    """Aplica o texto visualmente sobre a imagem gerada utilizando Pillow com fundo semi-transparente"""
    if not text:
        return image_path
    
    try:
        base_image = Image.open(image_path).convert("RGBA")
        base_w, base_h = base_image.size
        draw = ImageDraw.Draw(base_image)
        
        # Define tamanho da fonte (aprox 4% da altura)
        font_size = int(base_h * 0.04)
        font = load_system_font(font_size)
        
        # Quebra de texto manual simples (aprox 25 caracteres por linha)
        chars_per_line = 30
        lines = [text[i:i+chars_per_line] for i in range(0, len(text), chars_per_line)]
        
        # Calcula tamanho total do bloco de texto
        max_line_w = 0
        total_text_h = 0
        for line in lines:
            line_w, line_h = draw.textsize(line, font=font)
            max_line_w = max(max_line_w, line_w)
            total_text_h += line_h
            
        # Define margens e tamanho do retângulo de fundo
        margin_x = 20
        margin_y = 20
        rect_padding = 10
        rect_w = max_line_w + (rect_padding * 2)
        rect_h = total_text_h + (rect_padding * 2)
        
        # Posição centralizada no rodapé (acima do logo, se possível)
        pos_x = (base_w - rect_w) // 2
        pos_y = base_h - rect_h - (margin_y + 40) # 40px extras de segurança
        
        # Cria retângulo semi-transparente
        rect_image = Image.new('RGBA', (base_w, base_h), (0,0,0,0))
        rect_draw = ImageDraw.Draw(rect_image)
        rect_draw.rectangle([(pos_x, pos_y), (pos_x + rect_w, pos_y + rect_h)], fill=(0,0,0,160)) # Fundo preto semi-transparente
        
        # Desenha o texto branco sobre o retângulo
        current_y = pos_y + rect_padding
        for line in lines:
            line_w, line_h = draw.textsize(line, font=font)
            text_x = pos_x + rect_padding + (max_line_w - line_w) // 2 # Centraliza cada linha dentro do bloco
            rect_draw.text((text_x, current_y), line, font=font, fill="white")
            current_y += line_h

        # Aplica o retângulo com texto na imagem base
        final_image = Image.alpha_composite(base_image, rect_image)
        rgb_image = final_image.convert("RGB")
        rgb_image.save(image_path)
        return image_path
    except Exception as e:
        print(f"Erro ao aplicar texto visual: {e}")
        return image_path

def apply_logo(image_path, logo_path):
    """Aplica o logo sobre a imagem gerada utilizando Pillow"""
    if not logo_path or not os.path.exists(logo_path):
        return image_path
    
    try:
        base_image = Image.open(image_path).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
        
        base_w, base_h = base_image.size
        logo_w, logo_h = logo.size
        
        # Redimensiona o logo para 12% da largura da imagem
        new_logo_w = int(base_w * 0.12)
        new_logo_h = int(logo_h * (new_logo_w / logo_w))
        logo = logo.resize((new_logo_w, new_logo_h), ImageResampling.LANCZOS)
        
        # Posiciona no canto inferior direito (margem de 15px)
        pos_x = base_w - new_logo_w - 15
        pos_y = base_h - new_logo_h - 15
        
        base_image.paste(logo, (pos_x, pos_y), logo)
        rgb_image = base_image.convert("RGB")
        rgb_image.save(image_path)
        return image_path
    except Exception as e:
        print(f"Erro ao aplicar logo: {e}")
        return image_path

def generate_script_and_prompts(niche, mode, background_style, use_real_people, context):
    """Gera o roteiro (para vídeo) e 4 prompts de imagem detalhados baseados nos inputs do usuário."""
    
    base_instruction = f"Crie um roteiro curto de 30 segundos sobre {niche}. Retorne estritamente um JSON com a chave script contendo o texto falado."
    
    # Adiciona diretrizes de estilo ao prompt do Groq
    image_style_instruction = f"Inclua também a chave prompts contendo uma lista de 4 prompts em inglês DETALHADOS para gerar imagens em IA profissionais. As imagens devem ter um estilo profissional 'Canva-like', com um fundo {background_style}."
    if use_real_people:
        image_style_instruction += " Devem incluir pessoas reais em poses profissionais."
    if context:
        image_style_instruction += f" Adicione visualmente uma área discreta com o texto: {context}."
    if mode == "carrossel":
        image_style_instruction += " Os prompts devem descrever cenas que se conectam visualmente, criando uma transição suave entre elas."
        
    prompt = f"{base_instruction} {image_style_instruction}"
    
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
    """Gera áudio em português usando Edge-TTS."""
    safe_text = text.replace('"', '').replace("'", "")
    command = f'edge-tts --text "{safe_text}" --voice pt-BR-AntonioNeural --write-media {filename}'
    os.system(command)
    return filename

def generate_images_pollinations(prompts, width, height, logo_path, phrases):
    """Gera imagens usando o Pollinations (Grátis), aplica logo e texto."""
    downloaded_files = []
    for index, prompt in enumerate(prompts):
        global progress_status
        progress_status["remaining"] = 30 - (index * 5)
        
        encoded_prompt = requests.utils.quote(prompt)
        # Use modelo Flux e força seed diferente para variação
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&nologo=true&model=flux&seed={int(time.time()) + index}"
        
        try:
            img_data = requests.get(image_url).content
            img_filename = f"image_{index}.jpg"
            
            with open(img_filename, "wb") as handler:
                handler.write(img_data)
            
            # Aplica o texto visual (frase) e depois o logo
            apply_text_visual(img_filename, phrases[index])
            apply_logo(img_filename, logo_path)
            downloaded_files.append(img_filename)
        except Exception as e:
            print(f"Erro ao gerar imagem no Pollinations: {e}")
        
    return downloaded_files

def generate_images_leonardo(prompts, width, height, logo_path, phrases):
    """Gera imagens usando o Leonardo.ai (Pago), aplica logo e texto."""
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
            "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3", # Ex: Leonardo Vision XL
            "num_images": 1
        }
        
        try:
            create_res = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
            generation_id = create_res.json().get("sdGenerationJob", {}).get("generationId")
            
            if not generation_id:
                continue
                
            status = "PENDING"
            while status != "COMPLETE":
                time.sleep(3) # Aguarda processamento
                get_res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{generation_id}", headers=headers)
                data = get_res.json()
                status = data.get("generations_by_pk", {}).get("status")
                
            image_url = data["generations_by_pk"]["generated_images"][0]["url"]
            img_data = requests.get(image_url).content
            img_filename = f"image_{index}.jpg"
            
            with open(img_filename, "wb") as handler:
                handler.write(img_data)
                
            # Aplica o texto visual (frase) e depois o logo
            apply_text_visual(img_filename, phrases[index])
            apply_logo(img_filename, logo_path)
            downloaded_files.append(img_filename)
        except Exception as e:
            print(f"Erro ao gerar imagem no Leonardo: {e}")
            
    return downloaded_files

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/status')
def get_status():
    return jsonify(progress_status)

@app.route('/suggest-phrases', methods=['POST'])
def suggest_phrases():
    """Rota para buscar frases sugeridas da IA antes de gerar a mídia final."""
    global progress_status
    if not process_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "O motor já está processando um ativo."}), 429
    
    data = request.json
    niche = data.get('niche')
    
    if not niche:
        return jsonify({"success": False, "error": "Nicho ausente"}), 400

    try:
        progress_status = {"step": "Buscando Frases Criativas...", "remaining": 10}
        prompt = f"Crie 4 frases curtas e impactantes sobre {niche} para posts de redes sociais (aprox 25 caracteres cada). Retorne estritamente um JSON com a chave phrases contendo uma lista de 4 strings."
        
        # Chama Groq para frases
        response = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", # Modelo rápido para esta tarefa
            response_format={"type": "json_object"}
        )
        phrases_data = json.loads(response.choices[0].message.content)
        phrases = phrases_data.get("phrases")
        
        return jsonify({"success": True, "phrases": phrases})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        process_lock.release()

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(os.getcwd(), filename)

@app.route('/generate', methods=['POST'])
def generate_video():
    """Rota principal para gerar a mídia final (vídeo, carrossel ou imagens)."""
    global progress_status
    if not process_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "O motor já está processando um ativo."}), 429
    
    try:
        # Pega dados do Form (necessário para Upload de arquivo)
        niche = request.form.get('niche')
        mode = request.form.get('mode', 'video')
        provider = request.form.get('provider', 'pollinations')
        ratio = request.form.get('ratio', '9:16')
        background_style = request.form.get('background_style')
        use_real_people = request.form.get('use_real_people') == 'true'
        context = request.form.get('context')
        
        # Pega as frases editadas do usuário
        phrases = []
        try:
            phrases_raw = request.form.get('phrases')
            if phrases_raw:
                phrases = json.loads(phrases_raw)
        except:
            pass
        
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
        content = generate_script_and_prompts(niche, mode, background_style, use_real_people, context)
        script = content.get("script")
        prompts = content.get("prompts")
        
        # Se o usuário não forneceu frases, usa as geradas implicitamente (fallback)
        if not phrases or len(phrases) < 4:
            phrases = ["Aproveite!", "Saiba Mais", "Descubra", "Increva-se"]
        
        if provider == "leonardo":
            progress_status = {"step": "Gerando Imagens e Texto (Leonardo)...", "remaining": 45}
            image_paths = generate_images_leonardo(prompts, width, height, logo_path, phrases)
        else:
            progress_status = {"step": "Gerando Imagens e Texto (Pollinations)...", "remaining": 35}
            image_paths = generate_images_pollinations(prompts, width, height, logo_path, phrases)
        
        if mode == "video":
            progress_status = {"step": "Gerando Áudio e Compilando Vídeo Final...", "remaining": 20}
            audio_path = generate_audio(script)
            
            # Compilação de vídeo
            audio = AudioFileClip(audio_path)
            duration_per_image = audio.duration / len(image_paths)
            
            clips = []
            for img in image_paths:
                clip = ImageClip(img).set_duration(duration_per_image)
                clips.append(clip)
                
            video = concatenate_videoclips(clips, method="compose")
            video = video.set_audio(audio)
            
            # Otimização de threads para velocidade de renderização
            threads = os.cpu_count() or 4
            video.write_videofile("final_video.mp4", fps=24, codec="libx264", audio_codec="aac", threads=threads)
            
            progress_status = {"step": "Concluído", "remaining": 0}
            return jsonify({"success": True, "file": "final_video.mp4", "type": "video"})
        elif mode == "carrossel":
            progress_status = {"step": "Concluído", "remaining": 0}
            return jsonify({"success": True, "files": image_paths, "type": "carrossel"})
        else:
            progress_status = {"step": "Concluído", "remaining": 0}
            # Se for apenas imagem, retorna a lista completa de imagens
            return jsonify({"success": True, "files": image_paths, "type": "image"})
        
    except Exception as e:
        progress_status = {"step": "Erro fatal", "remaining": 0}
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        process_lock.release()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
