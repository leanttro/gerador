import os
import json
import time
import requests
import threading
import glob
import re
import urllib.parse
import asyncio
import edge_tts
from flask import Flask, request, jsonify, render_template, send_from_directory
from groq import Groq
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

groq_client = Groq(api_key=GROQ_API_KEY)

process_lock = threading.Lock()

progress_status = {"step": "Ocioso", "remaining": 0}

def cleanup_old_files():
    patterns = ['*.mp3', '*.jpg', '*.png', 'final_video.mp4', 'raw_*.jpg']
    for pattern in patterns:
        files = glob.glob(os.path.join(BASE_DIR, pattern))
        for f in files:
            try:
                os.remove(f)
            except Exception as e:
                pass
    
    logos = glob.glob(os.path.join(UPLOAD_FOLDER, "*"))
    for l in logos:
        try:
            os.remove(l)
        except Exception as e:
            pass

def get_dimensions(ratio):
    if ratio == "16:9":
        return (1024, 576)
    elif ratio == "9:16":
        return (576, 1024)
    elif ratio == "1:1":
        return (1024, 1024)
    elif ratio == "4:5":
        return (768, 960)
    else:
        return (1024, 1024)

def load_system_font(size, font_name="Arial"):
    possible_fonts = [
        f"C:\\Windows\\Fonts\\{font_name}.ttf",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        f"/System/Library/Fonts/Helvetica.ttc",
        f"/System/Library/Fonts/Supplemental/{font_name}.ttf"
    ]
    for font_path in possible_fonts:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception as e:
                pass
    return ImageFont.load_default()

def apply_text_custom(image_path, text, color_hex, font_name, pct_x, pct_y):
    if not text:
        return image_path
    
    try:
        base_image = Image.open(image_path).convert("RGBA")
        base_w, base_h = base_image.size
        draw = ImageDraw.Draw(base_image)
        
        font_size = int(base_h * 0.04)
        font = load_system_font(font_size, font_name)
        
        chars_per_line = 30
        lines = [text[i:i+chars_per_line] for i in range(0, len(text), chars_per_line)]
        
        max_line_w = 0
        total_text_h = 0
        
        for line in lines:
            try:
                line_w, line_h = draw.textsize(line, font=font)
            except AttributeError:
                bbox = draw.textbbox((0, 0), line, font=font)
                line_w = bbox[2] - bbox[0]
                line_h = bbox[3] - bbox[1]
            max_line_w = max(max_line_w, line_w)
            total_text_h += line_h
            
        rect_padding = 10
        rect_w = max_line_w + (rect_padding * 2)
        rect_h = total_text_h + (rect_padding * 2)
        
        pos_x = int(pct_x * (base_w - rect_w))
        pos_y = int(pct_y * (base_h - rect_h))
        
        pos_x = max(0, min(pos_x, base_w - rect_w))
        pos_y = max(0, min(pos_y, base_h - rect_h))
        
        rect_image = Image.new('RGBA', (base_w, base_h), (0,0,0,0))
        rect_draw = ImageDraw.Draw(rect_image)
        rect_draw.rectangle([(pos_x, pos_y), (pos_x + rect_w, pos_y + rect_h)], fill=(0,0,0,180))
        
        current_y = pos_y + rect_padding
        color_tuple = tuple(int(color_hex.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        
        for line in lines:
            try:
                line_w, line_h = draw.textsize(line, font=font)
            except AttributeError:
                bbox = draw.textbbox((0, 0), line, font=font)
                line_w = bbox[2] - bbox[0]
                line_h = bbox[3] - bbox[1]
            text_x = pos_x + rect_padding + (max_line_w - line_w) // 2
            rect_draw.text((text_x, current_y), line, font=font, fill=color_tuple)
            current_y += line_h

        final_image = Image.alpha_composite(base_image, rect_image)
        rgb_image = final_image.convert("RGB")
        rgb_image.save(image_path)
        
        return image_path
    
    except Exception as e:
        print(f"Erro ao aplicar texto: {e}")
        return image_path

def apply_logo_custom(image_path, logo_path, pct_x, pct_y):
    if not logo_path or not os.path.exists(logo_path):
        return image_path
    
    try:
        base_image = Image.open(image_path).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
        
        base_w, base_h = base_image.size
        logo_w, logo_h = logo.size
        
        new_logo_w = int(base_w * 0.15)
        new_logo_h = int(logo_h * (new_logo_w / logo_w))
        logo = logo.resize((new_logo_w, new_logo_h), Image.LANCZOS)
        
        pos_x = int(pct_x * (base_w - new_logo_w))
        pos_y = int(pct_y * (base_h - new_logo_h))
        
        pos_x = max(0, min(pos_x, base_w - new_logo_w))
        pos_y = max(0, min(pos_y, base_h - new_logo_h))
        
        base_image.paste(logo, (pos_x, pos_y), logo)
        rgb_image = base_image.convert("RGB")
        rgb_image.save(image_path)
        
        return image_path
    
    except Exception as e:
        print(f"Erro ao aplicar logo: {e}")
        return image_path

def generate_audio(text, filename="output_audio.mp3"):
    filepath = os.path.join(BASE_DIR, filename)
    
    async def create_audio():
        communicate = edge_tts.Communicate(text, "pt-BR-AntonioNeural")
        await communicate.save(filepath)
        
    asyncio.run(create_audio())
    return filepath

def generate_images_pollinations(prompts, width, height, background_style):
    downloaded_files = []
    base_seed = int(time.time())
    style_suffix = f", {background_style}, highly detailed masterpiece"
    
    for index, prompt in enumerate(prompts):
        global progress_status
        progress_status["remaining"] = 30 - (index * 5)
        
        prompt_str = str(prompt)
        prompt_str = re.sub(r'[^a-zA-Z0-9\s,]', '', prompt_str)
        prompt_str = prompt_str[:80].strip() + style_suffix
        
        encoded_prompt = urllib.parse.quote(prompt_str)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&seed={base_seed}&nologo=true"
        
        for attempt in range(3):
            try:
                response = requests.get(image_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
                if len(response.content) > 5000:
                    img_filename = f"raw_image_{index}.jpg"
                    img_filepath = os.path.join(BASE_DIR, img_filename)
                    
                    with open(img_filepath, "wb") as h:
                        h.write(response.content)
                        
                    downloaded_files.append(img_filename)
                    break
            except Exception as e:
                time.sleep(3)
                
    return downloaded_files

def generate_images_leonardo(prompts, width, height, background_style):
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {LEONARDO_API_KEY}"
    }
    
    downloaded_files = []
    style_suffix = f", {background_style}, highly detailed masterpiece"

    for index, prompt in enumerate(prompts):
        global progress_status
        progress_status["remaining"] = 40 - (index * 8)
        
        prompt_str = str(prompt)[:80].strip() + style_suffix

        payload = {
            "height": height,
            "width": width,
            "prompt": prompt_str,
            "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3",
            "num_images": 1
        }
        
        try:
            create_res = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
            generation_id = create_res.json().get("sdGenerationJob", {}).get("generationId")
            
            status = "PENDING"
            while status != "COMPLETE":
                time.sleep(3)
                get_res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{generation_id}", headers=headers)
                data = get_res.json()
                status = data.get("generations_by_pk", {}).get("status")
                
                if status == "FAILED":
                    raise Exception("Falha Leonardo")
                
            image_url = data["generations_by_pk"]["generated_images"][0]["url"]
            img_data = requests.get(image_url).content
            
            img_filename = f"raw_image_{index}.jpg"
            img_filepath = os.path.join(BASE_DIR, img_filename)
            
            with open(img_filepath, "wb") as h:
                h.write(img_data)
                
            downloaded_files.append(img_filename)
            
        except Exception as e:
            print(e)
            
    return downloaded_files

def generate_images_pixabay(prompts, width, height):
    downloaded_files = []
    orientation = "vertical" if height > width else "horizontal"
    if width == height:
        orientation = "horizontal"

    for index, prompt in enumerate(prompts):
        global progress_status
        progress_status["remaining"] = 20 - (index * 4)
        
        prompt_str = str(prompt)
        prompt_str = re.sub(r'[^a-zA-Z0-9\s]', '', prompt_str).strip()
        search_term = urllib.parse.quote(prompt_str[:50])
        
        url = f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={search_term}&image_type=photo&orientation={orientation}&per_page=3"
        
        try:
            response = requests.get(url).json()
            
            if response.get("totalHits", 0) > 0:
                image_url = response["hits"][0]["largeImageURL"]
            else:
                image_url = "https://cdn.pixabay.com/photo/2015/04/23/22/00/tree-736885_1280.jpg"
                
            img_data = requests.get(image_url).content
            img_filename = f"raw_image_{index}.jpg"
            img_filepath = os.path.join(BASE_DIR, img_filename)
            
            with open(img_filepath, "wb") as h:
                h.write(img_data)
                
            downloaded_files.append(img_filename)
            
        except Exception as e:
            print(e)
            
    return downloaded_files

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/status')
def get_status():
    return jsonify(progress_status)

@app.route('/media/<path:filename>')
def serve_media(filename):
    return send_from_directory(BASE_DIR, filename)

@app.route('/api/search', methods=['POST'])
def api_search():
    data = request.json
    niche = data.get('niche')
    
    url = 'https://google.serper.dev/search'
    query = f'{niche} (site:tiktok.com OR site:instagram.com/reels OR site:youtube.com/shorts)'
    
    payload = {
        'q': query,
        'num': 5,
        'gl': 'br',
        'hl': 'pt-br',
        'tbs': 'qdr:w'
    }
    
    headers = {
        'X-API-KEY': SERPER_API_KEY,
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        results = response.json().get('organic', [])
        return jsonify({"success": True, "data": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    data = request.json
    content = data.get('content')
    
    prompt = f"Analise este post viral: '{content}'. Retorne estritamente um JSON com: 'script' (texto narrado 30s), 'prompts' (lista 4 prompts imagem em ingles), 'engine' (pixabay ou pollinations), 'color' (cor sugerida hex), 'font' (Arial ou Verdana)."
    
    try:
        response = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        return jsonify({"success": True, "data": result})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/generate-raw', methods=['POST'])
def api_generate_raw():
    global progress_status
    if not process_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "O motor já está em uso"}), 429
    
    try:
        cleanup_old_files()
        
        provider = request.form.get('provider', 'pollinations')
        ratio = request.form.get('ratio', '9:16')
        background_style = request.form.get('background_style', 'modern')
        
        prompts = []
        try:
            prompts = json.loads(request.form.get('prompts', '[]'))
        except Exception as e:
            pass
            
        width, height = get_dimensions(ratio)
        progress_status = {"step": f"Gerando imagens via {provider}...", "remaining": 30}
        
        if provider == "leonardo":
            images = generate_images_leonardo(prompts, width, height, background_style)
        elif provider == "pixabay":
            images = generate_images_pixabay(prompts, width, height)
        else:
            images = generate_images_pollinations(prompts, width, height, background_style)
            
        progress_status = {"step": "Ocioso", "remaining": 0}
        return jsonify({"success": True, "images": images})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
        
    finally:
        process_lock.release()

@app.route('/api/render-final', methods=['POST'])
def api_render_final():
    global progress_status
    if not process_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "O motor já está em uso"}), 429
    
    try:
        mode = request.form.get('mode', 'video')
        script = request.form.get('script', '')
        text_pct_x = float(request.form.get('text_x', 0.5))
        text_pct_y = float(request.form.get('text_y', 0.8))
        color = request.form.get('color', '#FFFFFF')
        font = request.form.get('font', 'Arial')
        logo_pct_x = float(request.form.get('logo_x', 0.1))
        logo_pct_y = float(request.form.get('logo_y', 0.1))
        
        images = []
        try:
            images = json.loads(request.form.get('images', '[]'))
        except Exception as e:
            pass
            
        phrases = []
        try:
            phrases = json.loads(request.form.get('phrases', '[]'))
        except Exception as e:
            pass
            
        logo_path = None
        if 'logo' in request.files:
            logo_file = request.files['logo']
            if logo_file.filename != '':
                filename = secure_filename(logo_file.filename)
                logo_path = os.path.join(UPLOAD_FOLDER, filename)
                logo_file.save(logo_path)

        progress_status = {"step": "Aplicando textos e logos nas imagens...", "remaining": 20}
        final_images = []
        
        for i, img_name in enumerate(images):
            filepath = os.path.join(BASE_DIR, img_name)
            if i < len(phrases) and phrases[i].strip():
                apply_text_custom(filepath, phrases[i], color, font, text_pct_x, text_pct_y)
            if logo_path:
                apply_logo_custom(filepath, logo_path, logo_pct_x, logo_pct_y)
            final_images.append(img_name)

        if mode == "video":
            progress_status = {"step": "Renderizando Vídeo com Audio...", "remaining": 10}
            
            audio_path = generate_audio(script)
            audio = AudioFileClip(audio_path)
            total_duration = audio.duration
            num_images = len(final_images)
            overlap = 0.5
            duration_per_image = (total_duration + (num_images - 1) * overlap) / num_images
            
            clips = []
            for i, img in enumerate(final_images):
                clip = ImageClip(os.path.join(BASE_DIR, img)).set_duration(duration_per_image)
                clip = clip.resize(lambda t: 1.0 + 0.04 * (t / duration_per_image))
                if i > 0:
                    clip = clip.crossfadein(overlap)
                clips.append(clip)
                
            video = concatenate_videoclips(clips, padding=-overlap, method="compose").set_audio(audio)
            video_filepath = os.path.join(BASE_DIR, "final_video.mp4")
            
            threads = os.cpu_count() or 4
            video.write_videofile(video_filepath, fps=24, codec="libx264", audio_codec="aac", threads=threads, preset="ultrafast")
            
            progress_status = {"step": "Concluído", "remaining": 0}
            return jsonify({"success": True, "type": "video", "file": "final_video.mp4"})
            
        else:
            progress_status = {"step": "Concluído", "remaining": 0}
            return jsonify({"success": True, "type": mode, "files": final_images})
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
        
    finally:
        process_lock.release()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
