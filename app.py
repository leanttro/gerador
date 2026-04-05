import os
import time
import uuid
import json
import requests
import urllib.parse
from flask import Flask, request, jsonify, render_template, send_from_directory, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
HTML_TEMPLATES_FOLDER = os.path.join(BASE_DIR, 'html_templates')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm', 'svg', 'avif'}
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', uuid.uuid4().hex)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=['1000 per day', '200 per hour']
)

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
PIXABAY_API_KEY = os.environ.get('PIXABAY_API_KEY')

BEST_FREE_MODEL = 'llama-3.3-70b-versatile'

if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)
else:
    groq_client = None

version_history: dict[str, list[dict]] = {}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

if not os.path.exists(HTML_TEMPLATES_FOLDER):
    os.makedirs(HTML_TEMPLATES_FOLDER)


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_media_type(filename: str) -> str:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return 'video' if ext in {'mp4', 'webm'} else 'image'


def cleanup_old_uploads(max_age_hours: int = 24):
    try:
        now = time.time()
        for fname in os.listdir(UPLOAD_FOLDER):
            fpath = os.path.join(UPLOAD_FOLDER, fname)
            if os.path.isfile(fpath):
                age_hours = (now - os.path.getmtime(fpath)) / 3600
                if age_hours > max_age_hours:
                    os.remove(fpath)
    except Exception as e:
        print(f'[cleanup] Erro: {e}')


def get_session_id(req) -> str:
    return req.headers.get('X-Session-ID', 'default')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/media/<path:filename>')
def serve_media(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/html_templates/<path:filename>')
def serve_html_templates(filename):
    return send_from_directory(HTML_TEMPLATES_FOLDER, filename)


@app.route('/api/upload', methods=['POST'])
@limiter.limit('60 per minute')
def api_upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Nenhum arquivo enviado'}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({'success': False, 'error': 'Nome de arquivo vazio'}), 400

    if not allowed_file(file.filename):
        return jsonify({
            'success': False,
            'error': f'Tipo não permitido. Use: {", ".join(sorted(ALLOWED_EXTENSIONS))}'
        }), 400

    try:
        safe_name = secure_filename(file.filename)
        unique_name = f'{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_name}'
        filepath = os.path.join(UPLOAD_FOLDER, unique_name)
        file.save(filepath)

        file_size = os.path.getsize(filepath)
        if file_size > MAX_FILE_SIZE:
            os.remove(filepath)
            return jsonify({
                'success': False,
                'error': f'Arquivo muito grande. Máximo: {MAX_FILE_SIZE_MB}MB'
            }), 400

        cleanup_old_uploads()

        media_type = get_media_type(unique_name)
        file_url = f'/media/{unique_name}'

        return jsonify({
            'success': True,
            'url': file_url,
            'filename': unique_name,
            'type': media_type,
            'size_kb': round(file_size / 1024, 1)
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pixabay', methods=['POST'])
@limiter.limit('30 per minute')
def api_pixabay():
    if not PIXABAY_API_KEY:
        return jsonify({'success': False, 'error': 'PIXABAY_API_KEY não configurada no servidor.'}), 400
    
    data = request.json or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'Busca vazia'}), 400
        
    try:
        url = f'https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={urllib.parse.quote(query)}&image_type=photo&orientation=vertical&per_page=8'
        response = requests.get(url, timeout=10)
        res_data = response.json()
        
        images = []
        for hit in res_data.get('hits', []):
            images.append({
                'url': hit['largeImageURL'],
                'preview': hit['previewURL'],
                'tags': hit['tags']
            })
            
        return jsonify({'success': True, 'images': images})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/generate', methods=['POST'])
@limiter.limit('25 per minute')
def api_generate():
    data = request.json or {}
    prompt = data.get('prompt', '').strip()
    assets = data.get('assets', [])
    previous_code = data.get('previous_code', None)
    style_preset = data.get('style_preset', 'dark')
    format_ratio = data.get('format_ratio', '9:16')
    ai_engine = data.get('ai_engine', 'groq')

    if not prompt:
        return jsonify({'success': False, 'error': 'Prompt vazio'}), 400

    if not previous_code:
        return jsonify({'success': False, 'error': 'Selecione um template base no painel'}), 400

    style_guides = {
        'dark':      'Cores HEX para paleta escura premium. Pretos profundos, cinzas e acentos neon.',
        'neon':      'Cores HEX para paleta neon vibrante. Roxo, rosa, azul elétrico em fundo preto absoluto.',
        'minimal':   'Cores HEX para design minimalista. Tons sólidos, sóbrios e muito espaço branco.',
        'gold':      'Cores HEX para paleta luxo. Dourado, preto fosco, champagne.',
        'gradient':  'Cores HEX para paleta moderna e vibrante.',
        'corporate': 'Cores HEX para estilo corporativo. Azul petróleo, branco, cinza.',
    }
    style_guide = style_guides.get(style_preset, style_guides['dark'])

    system_prompt = f'''Você é um motor de processamento de dados JSON estruturado.
A sua única função é ler o código HTML do template fornecido, identificar todas as variáveis em maiúsculo que começam com CHAVE_ e preenchê-las com base na instrução do usuário.

Regras de processamento
* Retorne APENAS um objeto JSON válido, sem crases, sem markdown, sem explicações.
* As chaves do JSON devem ser as variáveis encontradas no código, como CHAVE_TITULO, CHAVE_SUBTITULO, CHAVE_IMG_1, CHAVE_COR_PRIMARIA, etc.
* Extraia a intenção criativa do usuário para gerar os textos corretos.
* Use as URLs dos ASSETS fornecidos para substituir as chaves de imagem/vídeo.
* Para chaves de cor, gere códigos HEX reais com base na seguinte diretriz de estilo: {style_guide}
* Não altere o nome das chaves. Copie exatamente como estão no HTML fornecido.'''

    user_content = f'INSTRUÇÃO CRIATIVA: {prompt}\n\n'

    if assets:
        user_content += 'ASSETS DISPONÍVEIS:\n' + '\n'.join(assets) + '\n\n'

    user_content += f'CÓDIGO TEMPLATE PARA MAPEAMENTO DE CHAVES:\n{previous_code}'

    try:
        generated_json_str = ''
        tokens_used = 0

        if ai_engine == 'gemini':
            if not GEMINI_API_KEY:
                return jsonify({'success': False, 'error': 'Chave ausente'}), 400

            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}'
            payload = {
                'systemInstruction': {'parts': [{'text': system_prompt}]},
                'contents': [{'parts': [{'text': user_content}]}],
                'generationConfig': {'temperature': 0.1, 'responseMimeType': 'application/json'}
            }
            res = requests.post(url, json=payload).json()
            generated_json_str = res['candidates'][0]['content']['parts'][0]['text']

        elif ai_engine == 'openrouter':
            if not OPENROUTER_API_KEY:
                return jsonify({'success': False, 'error': 'Chave ausente'}), 400

            url = 'https://openrouter.ai/api/v1/chat/completions'
            headers = {'Authorization': f'Bearer {OPENROUTER_API_KEY}'}
            payload = {
                'model': 'google/gemini-2.0-flash-001',
                'response_format': {'type': 'json_object'},
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_content}
                ],
                'temperature': 0.1
            }
            res = requests.post(url, headers=headers, json=payload).json()
            generated_json_str = res['choices'][0]['message']['content']
            tokens_used = res.get('usage', {}).get('total_tokens', 0)

        else:
            if not groq_client:
                return jsonify({'success': False, 'error': 'Chave ausente'}), 400

            response = groq_client.chat.completions.create(
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_content}
                ],
                model=BEST_FREE_MODEL,
                temperature=0.1,
                response_format={'type': 'json_object'},
            )
            generated_json_str = response.choices[0].message.content
            tokens_used = getattr(getattr(response, 'usage', None), 'total_tokens', 0)

        import json
        substituicoes = json.loads(generated_json_str.strip())
        
        html_final = previous_code
        for chave, valor in substituicoes.items():
            html_final = html_final.replace(str(chave), str(valor))

        session_id = get_session_id(request)
        if session_id not in version_history:
            version_history[session_id] = []

        version_entry = {
            'id': str(uuid.uuid4()),
            'timestamp': int(time.time()),
            'prompt': prompt,
            'style': style_preset,
            'format': format_ratio,
            'html': html_final,
            'assets': assets,
        }
        version_history[session_id].append(version_entry)

        if len(version_history[session_id]) > 15:
            version_history[session_id] = version_history[session_id][-15:]

        return jsonify({
            'success': True,
            'html': html_final,
            'version_id': version_entry['id'],
            'version_count': len(version_history[session_id]),
            'tokens_used': tokens_used,
            'model': ai_engine,
        })

    except Exception as e:
        error_msg = str(e)
        if '429' in error_msg or 'limit' in error_msg.lower():
            msg = 'Cota excedida. Aguarde ou troque de motor.'
        else:
            msg = f'Erro no processamento: {error_msg}'
        return jsonify({'success': False, 'error': msg}), 500


@app.route('/api/history', methods=['GET'])
def api_history():
    session_id = get_session_id(request)
    history = version_history.get(session_id, [])
    summary = [
        {
            'id':        v['id'],
            'timestamp': v['timestamp'],
            'prompt':    v['prompt'][:80] + '…' if len(v['prompt']) > 80 else v['prompt'],
            'style':     v.get('style', 'dark'),
            'format':    v.get('format', '9:16'),
        }
        for v in history
    ]
    return jsonify({'success': True, 'history': list(reversed(summary)), 'count': len(summary)})


@app.route('/api/history/<version_id>', methods=['GET'])
def api_get_version(version_id):
    session_id = get_session_id(request)
    history = version_history.get(session_id, [])
    version = next((v for v in history if v['id'] == version_id), None)
    if not version:
        return jsonify({'success': False, 'error': 'Versão não encontrada'}), 404
    return jsonify({'success': True, 'version': version})


@app.route('/api/history', methods=['DELETE'])
def api_clear_history():
    session_id = get_session_id(request)
    version_history[session_id] = []
    return jsonify({'success': True, 'message': 'Histórico limpo com sucesso'})


@app.route('/api/templates', methods=['GET'])
def api_templates():
    templates = []
    try:
        if os.path.exists(HTML_TEMPLATES_FOLDER):
            for fname in os.listdir(HTML_TEMPLATES_FOLDER):
                if fname.endswith('.html'):
                    file_path = os.path.join(HTML_TEMPLATES_FOLDER, fname)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    clean_name = fname.replace('.html', '').replace('_', ' ').title()
                    
                    templates.append({
                        'id': fname,
                        'name': clean_name,
                        'prompt': 'Adapte este template alterando os textos, imagens e cores de acordo com a seguinte instrução: ',
                        'style': 'dark',
                        'url': f'/html_templates/{fname}',
                        'html': content
                    })
    except Exception as e:
        print(f'Erro ao ler templates: {e}')

    if not templates:
        templates = [
            {
                'id': 'exemplo_vazio',
                'name': 'Nenhum Template Encontrado',
                'prompt': 'Adicione arquivos na pasta html_templates',
                'style': 'dark',
                'url': '',
                'html': ''
            }
        ]

    return jsonify({'success': True, 'templates': templates})


@app.route('/api/status', methods=['GET'])
def api_status():
    total_versions = sum(len(v) for v in version_history.values())
    try:
        uploads_count = len(os.listdir(UPLOAD_FOLDER))
    except Exception:
        uploads_count = 0

    return jsonify({
        'success':        True,
        'model':          BEST_FREE_MODEL,
        'api_configured': True,
        'total_sessions': len(version_history),
        'total_versions': total_versions,
        'uploads_count':  uploads_count,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    print(f' Motor Dark Studio rodando na porta {port}')
    app.run(host='0.0.0.0', port=port, debug=debug)
