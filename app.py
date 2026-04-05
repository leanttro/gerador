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

# ─────────────────────────────────────────────
# CONFIGURAÇÕES BASE
# ─────────────────────────────────────────────
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
HTML_TEMPLATES_FOLDER = os.path.join(BASE_DIR, 'html_templates')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm', 'svg', 'avif'}
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", uuid.uuid4().hex)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "200 per hour"]
)

# ─────────────────────────────────────────────
# CHAVES DE API
# ─────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")

BEST_FREE_MODEL = "llama-3.3-70b-versatile"

if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)
else:
    groq_client = None

# Histórico de versões em memória (por session_id)
version_history: dict[str, list[dict]] = {}

# ─────────────────────────────────────────────
# UTILITÁRIOS
# ─────────────────────────────────────────────
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
        print(f"[cleanup] Erro: {e}")


def get_session_id(req) -> str:
    return req.headers.get('X-Session-ID', 'default')


# ─────────────────────────────────────────────
# ROTAS PRINCIPAIS
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/media/<path:filename>')
def serve_media(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/html_templates/<path:filename>')
def serve_html_templates(filename):
    return send_from_directory(HTML_TEMPLATES_FOLDER, filename)


# ─────────────────────────────────────────────
# UPLOAD DE ASSETS
# ─────────────────────────────────────────────
@app.route('/api/upload', methods=['POST'])
@limiter.limit("60 per minute")
def api_upload():
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "Nenhum arquivo enviado"}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({"success": False, "error": "Nome de arquivo vazio"}), 400

    if not allowed_file(file.filename):
        return jsonify({
            "success": False,
            "error": f"Tipo não permitido. Use: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        }), 400

    try:
        safe_name = secure_filename(file.filename)
        unique_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_name}"
        filepath = os.path.join(UPLOAD_FOLDER, unique_name)
        file.save(filepath)

        file_size = os.path.getsize(filepath)
        if file_size > MAX_FILE_SIZE:
            os.remove(filepath)
            return jsonify({
                "success": False,
                "error": f"Arquivo muito grande. Máximo: {MAX_FILE_SIZE_MB}MB"
            }), 400

        cleanup_old_uploads()

        media_type = get_media_type(unique_name)
        file_url = f"/media/{unique_name}"

        return jsonify({
            "success": True,
            "url": file_url,
            "filename": unique_name,
            "type": media_type,
            "size_kb": round(file_size / 1024, 1)
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# BUSCA NO PIXABAY
# ─────────────────────────────────────────────
@app.route('/api/pixabay', methods=['POST'])
@limiter.limit("30 per minute")
def api_pixabay():
    if not PIXABAY_API_KEY:
        return jsonify({"success": False, "error": "PIXABAY_API_KEY não configurada no servidor."}), 400
    
    data = request.json or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({"success": False, "error": "Busca vazia"}), 400
        
    try:
        url = f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={urllib.parse.quote(query)}&image_type=photo&orientation=vertical&per_page=8"
        response = requests.get(url, timeout=10)
        res_data = response.json()
        
        images = []
        for hit in res_data.get("hits", []):
            images.append({
                "url": hit["largeImageURL"],
                "preview": hit["previewURL"],
                "tags": hit["tags"]
            })
            
        return jsonify({"success": True, "images": images})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# GERAÇÃO DE HTML VIA IA
# ─────────────────────────────────────────────
@app.route('/api/generate', methods=['POST'])
@limiter.limit("25 per minute")
def api_generate():
    data = request.json or {}
    prompt        = data.get('prompt', '').strip()
    assets        = data.get('assets', [])
    previous_code = data.get('previous_code', None)
    style_preset  = data.get('style_preset', 'dark')
    format_ratio  = data.get('format_ratio', '9:16')
    ai_engine     = data.get('ai_engine', 'groq')

    if not prompt:
        return jsonify({"success": False, "error": "Prompt não pode estar vazio"}), 400

    if len(prompt) > 3000:
        return jsonify({"success": False, "error": "Prompt muito longo. Máximo: 3000 caracteres"}), 400

    style_guides = {
        'dark':      'Paleta escura premium (pretos profundos, cinzas, acentos neon sutis), estilo Motor Dark Studio',
        'neon':      'Paleta neon vibrante (roxo, rosa, azul elétrico, verde limão) em fundo preto absoluto, estética cyberpunk',
        'minimal':   'Design minimalista ultra-clean, muito espaço branco, tipografia grande e forte, cores sólidas e sóbrias',
        'gold':      'Paleta luxo e premium (dourado rico, preto fosco, champagne, branco pérola), elegante e sofisticado',
        'gradient':  'Gradientes ricos e fluídos (roxo→azul, laranja→rosa, etc.), moderno, colorido e extremamente chamativo',
        'corporate': 'Corporativo profissional confiável (azul petróleo, branco, cinza), clean e sério',
    }

    format_guides = {
        '9:16':  'VERTICAL 9:16 — largura 100vw, altura 177.78vw (ou usar unidades fixas 1080×1920px emulado via viewport). Ideal para Stories, Reels, TikTok.',
        '1:1':   'QUADRADO 1:1 — largura e altura iguais em 100vmin. Ideal para feed do Instagram e LinkedIn.',
        '16:9':  'HORIZONTAL 16:9 — largura 100vw, altura 56.25vw. Ideal para YouTube, apresentações e LinkedIn.',
        '4:5':   'PORTRAIT 4:5 — largura 100vw, altura 125vw. Ideal para feed do Instagram com mais área vertical.',
    }

    style_guide  = style_guides.get(style_preset,  style_guides['dark'])
    format_guide = format_guides.get(format_ratio, format_guides['9:16'])

    system_prompt = f"""Você é um Desenvolvedor Front-End ELITE, Diretor de Arte e Especialista em Motion Design (Motor Dark Studio).

═══════════════════════════════════════════
MISSÃO ABSOLUTA
═══════════════════════════════════════════
Gerar um arquivo HTML 100% autocontido (CSS + JS embutidos), criando uma animação cinematográfica de altíssimo nível, digna de uma agência top de mercado. O código deve criar um motor de renderização fluído na própria página.

═══════════════════════════════════════════
ESPECIFICAÇÕES DE FORMATO
═══════════════════════════════════════════
{format_guide}
O container principal deve usar: width: 100%; height: 100%; overflow: hidden; sem scrollbar.

═══════════════════════════════════════════
IDENTIDADE VISUAL
═══════════════════════════════════════════
{style_guide}
Use variáveis CSS (:root) para todas as cores e tamanhos. Crie uma paleta coesa com pelo menos 5 variáveis de cor.

═══════════════════════════════════════════
REGRAS OBRIGATÓRIAS E TÉCNICAS (O SEGREDO DO SUCESSO)
═══════════════════════════════════════════
1. MOTOR DE CENAS: Use um sistema de Cenas (divs com class=scene) que aparecem e desaparecem. Controle a visibilidade usando opacity, transform e transition. NUNCA use display: none para transições de cena.
2. TIMING (JS): Use um script JS no final com requestAnimationFrame, setTimeout ou Promises para controlar o tempo exato de entrada e saída de cada cena, criando um fluxo narrativo perfeito e sincronizado.
3. IMAGENS PERFEITAS (ANTI-CORTE): Imagens e Logos DEVEM OBRIGATORIAMENTE usar regras de CSS de contenção: max-width: 100%; max-height: 100%; object-fit: contain; para NUNCA serem cortadas ou distorcidas.
4. EFEITOS CANVAS (Obrigatório): Adicione um efeito de partículas ou elementos dinâmicos no fundo usando HTML5 canvas (ex: brilhos, pétalas, poeira, estrelas, formas flutuantes).
5. TIPOGRAFIA CINEMATOGRÁFICA: Importe fontes elegantes do Google Fonts (ex: Cormorant Garamond, DM Sans, Inter, Playfair Display) e use tamanhos responsivos dinâmicos com clamp() (ex: clamp(2rem, 5vw, 5rem)).
6. TRANSIÇÕES SUAVES: Crie transições (ease-in-out ou cubic-bezier) longas e suaves de pelo menos 1.2 segundos entre os elementos e camadas.
7. PROFUNDIDADE E LAYERS: Use box-shadow, text-shadow, drop-shadow (em SVGs ou PNGs), e backdrop-filter: blur() para criar camadas ricas e texturas sofisticadas. 
8. CARROSSEL / POST ESTÁTICO (CRÍTICO): Se o usuário pedir um carrossel de fotos para postar, ou uma imagem estática, NUNCA USE auto-play rápido. Crie botões visíveis de navegação (Próximo/Anterior) ou permita troca de cena por clique, para que o usuário possa tirar print de cada tela com calma. Pare o temporizador automático!

═══════════════════════════════════════════
REGRAS DE CONTEÚDO E ASSETS
═══════════════════════════════════════════
- NUNCA use Lorem ipsum — crie conteúdo coerente e persuasivo com a instrução.
- NUNCA deixe regiões vazias sem intenção visual.
- SEMPRE crie hierarquia visual (hero element, suporte, background animado).
- Se houver assets do usuário (logos, fotos, vídeos): ELES SÃO O ELEMENTO CENTRAL DO DESIGN.
- Aplique as imagens/logos do usuário com destaque absoluto, centralizado ou no topo das cenas aplicáveis. NUNCA distorça essas imagens.
- Vídeos do usuário: use video autoplay muted loop playsinline style=object-fit: contain; max-width: 100%; max-height: 100%;.

═══════════════════════════════════════════
SAÍDA ESPERADA
═══════════════════════════════════════════
Retorne APENAS o código HTML bruto e válido. NENHUMA formatação markdown. ZERO texto antes ou depois do código. ZERO explicações. Comece com <!DOCTYPE html> e termine com </html>."""

    user_content = f"INSTRUÇÃO CRIATIVA: {prompt}\n\n"

    if assets:
        user_content += "═══ ASSETS DO USUÁRIO (INTEGRE OBRIGATORIAMENTE) ═══\n"
        for i, asset_url in enumerate(assets, 1):
            mtype = 'VÍDEO' if any(asset_url.lower().endswith(v) for v in ['.mp4', '.webm']) else 'IMAGEM'
            user_content += f"  [{i}] {mtype}: {asset_url}\n"
        user_content += "\nTodos os assets acima DEVEM aparecer no HTML final como elementos visuais centrais. NÃO corte-os e mantenha a proporção natural (object-fit: contain).\n\n"

    if previous_code:
        user_content += (
            "═══ CÓDIGO HTML EXISTENTE (TEMPLATE BASE) ═══\n"
            "O código abaixo é um template funcional de alta qualidade.\n"
            "PRESERVE estritamente a física do canvas, os temporizadores (timers/JS), os z-indexes e o motor de cenas.\n"
            "Sua tarefa é SUBSTITUIR APENAS textos, URLs de imagens, cores (mantendo a harmonia) e tipografia para se adaptar perfeitamente à INSTRUÇÃO CRIATIVA e aos ASSETS DO USUÁRIO.\n\n"
            f"{previous_code}"
        )
    else:
        user_content += "Crie do zero com base na instrução acima."

    try:
        generated_html = ""
        tokens_used = 0

        if ai_engine == 'gemini':
            if not GEMINI_API_KEY:
                return jsonify({"success": False, "error": "GEMINI_API_KEY ausente no servidor"}), 400
            
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": [{"text": user_content}]}],
                "generationConfig": {"temperature": 0.45, "maxOutputTokens": 8000}
            }
            res = requests.post(url, json=payload).json()
            try:
                generated_html = res['candidates'][0]['content']['parts'][0]['text']
            except KeyError:
                return jsonify({"success": False, "error": f"Erro Gemini: {res}"}), 500

        elif ai_engine == 'openrouter':
            if not OPENROUTER_API_KEY:
                return jsonify({"success": False, "error": "OPENROUTER_API_KEY ausente no servidor"}), 400
            
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
            payload = {
                "model": "meta-llama/llama-3.1-8b-instruct:free",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "temperature": 0.45
            }
            res = requests.post(url, headers=headers, json=payload).json()
            try:
                generated_html = res['choices'][0]['message']['content']
                tokens_used = res.get('usage', {}).get('total_tokens', 0)
            except KeyError:
                return jsonify({"success": False, "error": f"Erro OpenRouter: {res}"}), 500

        else:
            if not groq_client:
                return jsonify({"success": False, "error": "GROQ_API_KEY ausente no servidor"}), 400
                
            response = groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                model=BEST_FREE_MODEL,
                temperature=0.45,
                max_tokens=8000,
                top_p=0.92,
            )
            generated_html = response.choices[0].message.content
            tokens_used = getattr(getattr(response, 'usage', None), 'total_tokens', 0)

        generated_html = generated_html.strip()
        if generated_html.startswith("```html"):
            generated_html = generated_html[7:]
        elif generated_html.startswith("```"):
            generated_html = generated_html[3:]
        if generated_html.endswith("```"):
            generated_html = generated_html[:-3]
        generated_html = generated_html.strip()

        lower = generated_html.lower()
        if not (lower.startswith('<!doctype') or lower.startswith('<html')):
            return jsonify({
                "success": False,
                "error": "O modelo não retornou HTML válido. Tente reformular a instrução."
            }), 500

        session_id = get_session_id(request)
        if session_id not in version_history:
            version_history[session_id] = []

        version_entry = {
            "id":        str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "prompt":    prompt,
            "style":     style_preset,
            "format":    format_ratio,
            "html":      generated_html,
            "assets":    assets,
        }
        version_history[session_id].append(version_entry)

        if len(version_history[session_id]) > 15:
            version_history[session_id] = version_history[session_id][-15:]

        return jsonify({
            "success":       True,
            "html":          generated_html,
            "version_id":    version_entry["id"],
            "version_count": len(version_history[session_id]),
            "tokens_used":   tokens_used,
            "model":         ai_engine,
        })

    except Exception as e:
        error_msg = str(e)
        if "rate_limit" in error_msg.lower() or "429" in error_msg.lower():
            msg = "Limite de requisições atingido. Troque de motor no painel ou aguarde."
        elif "api_key" in error_msg.lower() or "auth" in error_msg.lower():
            msg = "Erro de autenticação. Verifique sua chave de API."
        elif "context_length" in error_msg.lower() or "tokens" in error_msg.lower():
            msg = "Código anterior muito longo. Tente começar uma nova geração do zero."
        else:
            msg = f"Erro na geração: {error_msg}"
        return jsonify({"success": False, "error": msg}), 500


# ─────────────────────────────────────────────
# HISTÓRICO DE VERSÕES
# ─────────────────────────────────────────────
@app.route('/api/history', methods=['GET'])
def api_history():
    session_id = get_session_id(request)
    history = version_history.get(session_id, [])
    summary = [
        {
            "id":        v["id"],
            "timestamp": v["timestamp"],
            "prompt":    v["prompt"][:80] + "…" if len(v["prompt"]) > 80 else v["prompt"],
            "style":     v.get("style", "dark"),
            "format":    v.get("format", "9:16"),
        }
        for v in history
    ]
    return jsonify({"success": True, "history": list(reversed(summary)), "count": len(summary)})


@app.route('/api/history/<version_id>', methods=['GET'])
def api_get_version(version_id):
    session_id = get_session_id(request)
    history = version_history.get(session_id, [])
    version = next((v for v in history if v["id"] == version_id), None)
    if not version:
        return jsonify({"success": False, "error": "Versão não encontrada"}), 404
    return jsonify({"success": True, "version": version})


@app.route('/api/history', methods=['DELETE'])
def api_clear_history():
    session_id = get_session_id(request)
    version_history[session_id] = []
    return jsonify({"success": True, "message": "Histórico limpo com sucesso"})


# ─────────────────────────────────────────────
# TEMPLATES REAIS (LENDO DA PASTA html_templates)
# ─────────────────────────────────────────────
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
                        "id": fname,
                        "name": clean_name,
                        "prompt": f"Mantenha a animação, canvas e estrutura deste template, mas altere os textos e imagens para a seguinte instrução: ",
                        "style": "dark",
                        "url": f"/html_templates/{fname}",
                        "html": content
                    })
    except Exception as e:
        print(f"Erro ao ler templates: {e}")

    if not templates:
        templates = [
            {
                "id": "exemplo_vazio",
                "name": "Nenhum Template Encontrado",
                "prompt": "Crie uma animação do zero pois não há templates na pasta html_templates...",
                "style": "dark",
                "url": "",
                "html": ""
            }
        ]

    return jsonify({"success": True, "templates": templates})


# ─────────────────────────────────────────────
# STATUS DA API
# ─────────────────────────────────────────────
@app.route('/api/status', methods=['GET'])
def api_status():
    total_versions = sum(len(v) for v in version_history.values())
    try:
        uploads_count = len(os.listdir(UPLOAD_FOLDER))
    except Exception:
        uploads_count = 0

    return jsonify({
        "success":        True,
        "model":          BEST_FREE_MODEL,
        "api_configured": True,
        "total_sessions": len(version_history),
        "total_versions": total_versions,
        "uploads_count":  uploads_count,
    })


# ─────────────────────────────────────────────
# INICIALIZAÇÃO
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"🚀 Motor Dark Studio — rodando na porta {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
