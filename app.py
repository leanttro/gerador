import os
import time
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["500 per day", "100 per hour"]
)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

groq_client = Groq(api_key=GROQ_API_KEY)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/media/<path:filename>')
def serve_media(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/api/upload', methods=['POST'])
@limiter.limit("50 per minute")
def api_upload():
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "Nenhum arquivo enviado"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "Nome de arquivo vazio"}), 400
        
    try:
        filename = secure_filename(f"{int(time.time())}_{file.filename}")
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        
        file_url = f"/media/{filename}"
        return jsonify({"success": True, "url": file_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/generate', methods=['POST'])
@limiter.limit("20 per minute")
def api_generate():
    data = request.json
    prompt = data.get('prompt')
    assets = data.get('assets', [])
    previous_code = data.get('previous_code', None)
    
    system_prompt = """Você é um desenvolvedor front-end especialista em animações e design voltado para conversão e redes sociais (Motor Dark Studio).
Sua missão é gerar um código HTML completo, único e contido em um único arquivo, com CSS embutido e JavaScript, criando uma experiência visual impecável e animada.
O código deve ser responsivo e otimizado para o formato 9:16 (vertical).
Use tipografia elegante, paleta de cores escura (Motor Dark) ou baseada na instrução, transições suaves e elementos em DOM ou Canvas.
Se o usuário forneceu URLs de assets (logos, imagens, vídeos), você DEVE integrá-los no design de forma inteligente.
Retorne APENAS o código HTML cru e válido, começando com <!DOCTYPE html> e terminando com </html>. Nenhuma formatação markdown, nenhuma explicação adicional."""

    user_content = f"Instruções do usuário: {prompt}\n\n"
    
    if assets:
        user_content += "URLs dos Assets enviados pelo usuário que DEVEM ser usados no código:\n"
        for asset in assets:
            user_content += f"- {asset}\n"
            
    if previous_code:
        user_content += f"\n\nCódigo HTML anterior que deve ser modificado com base na instrução atual:\n{previous_code}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]
    
    try:
        response = groq_client.chat.completions.create(
            messages=messages,
            model="llama3-70b-8192",
            temperature=0.4,
            max_tokens=6000
        )
        
        generated_html = response.choices[0].message.content.strip()
        
        if generated_html.startswith("```html"):
            generated_html = generated_html[7:]
        if generated_html.endswith("```"):
            generated_html = generated_html[:-3]
            
        return jsonify({"success": True, "html": generated_html.strip()})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
