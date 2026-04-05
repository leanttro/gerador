import os
import time
import uuid
import json
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
# GROQ — MELHOR MODELO GRATUITO DISPONÍVEL
# Troque por "gemma2-9b-it" se quiser mais velocidade
# ─────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    print("⚠️  AVISO: GROQ_API_KEY não encontrada! Configure a variável de ambiente.")

BEST_FREE_MODEL = "llama-3.3-70b-versatile"   # Melhor modelo free do Groq em 2025/2026

groq_client = Groq(api_key=GROQ_API_KEY)

# Histórico de versões em memória (por session_id)
version_history: dict[str, list[dict]] = {}

# ─────────────────────────────────────────────
# UTILITÁRIOS
# ─────────────────────────────────────────────
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_media_type(filename: str) -> str:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return 'video' if ext in {'mp4', 'webm'} else 'image'


def cleanup_old_uploads(max_age_hours: int = 24):
    """Remove uploads mais antigos que max_age_hours para liberar espaço."""
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
    """Retorna session_id vindo do header X-Session-ID ou 'default'."""
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

    if not prompt:
        return jsonify({"success": False, "error": "Prompt não pode estar vazio"}), 400

    if len(prompt) > 3000:
        return jsonify({"success": False, "error": "Prompt muito longo. Máximo: 3000 caracteres"}), 400

    # ── Guias de estilo ──────────────────────────────────────
    style_guides = {
        'dark':      'Paleta escura premium (pretos profundos, cinzas, acentos neon sutis), estilo Motor Dark Studio',
        'neon':      'Paleta neon vibrante (roxo, rosa, azul elétrico, verde limão) em fundo preto absoluto, estética cyberpunk',
        'minimal':   'Design minimalista ultra-clean, muito espaço branco, tipografia grande e forte, cores sólidas e sóbrias',
        'gold':      'Paleta luxo e premium (dourado rico, preto fosco, champagne, branco pérola), elegante e sofisticado',
        'gradient':  'Gradientes ricos e fluídos (roxo→azul, laranja→rosa, etc.), moderno, colorido e extremamente chamativo',
        'corporate': 'Corporativo profissional confiável (azul petróleo, branco, cinza), clean e sério',
    }

    # ── Guias de formato ─────────────────────────────────────
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
1. MOTOR DE CENAS: Use um sistema de Cenas (divs com class="scene") que aparecem e desaparecem. Controle a visibilidade usando opacity, transform e transition. NUNCA use display: none para transições de cena.
2. TIMING (JS): Use um script JS no final com requestAnimationFrame, setTimeout ou Promises para controlar o tempo exato de entrada e saída de cada cena, criando um fluxo narrativo perfeito e sincronizado.
3. IMAGENS PERFEITAS (ANTI-CORTE): Imagens e Logos DEVEM OBRIGATORIAMENTE usar regras de CSS de contenção: `max-width: 100%; max-height: 100%; object-fit: contain;` para NUNCA serem cortadas ou distorcidas.
4. EFEITOS CANVAS (Obrigatório): Adicione um efeito de partículas ou elementos dinâmicos no fundo usando HTML5 <canvas> (ex: brilhos, pétalas, poeira, estrelas, formas flutuantes).
5. TIPOGRAFIA CINEMATOGRÁFICA: Importe fontes elegantes do Google Fonts (ex: Cormorant Garamond, DM Sans, Inter, Playfair Display) e use tamanhos responsivos dinâmicos com clamp() (ex: clamp(2rem, 5vw, 5rem)).
6. TRANSIÇÕES SUAVES: Crie transições (ease-in-out ou cubic-bezier) longas e suaves de pelo menos 1.2 segundos entre os elementos e camadas.
7. PROFUNDIDADE E LAYERS: Use box-shadow, text-shadow, drop-shadow (em SVGs ou PNGs), e backdrop-filter: blur() para criar camadas ricas e texturas sofisticadas. 

═══════════════════════════════════════════
REGRAS DE CONTEÚDO E ASSETS
═══════════════════════════════════════════
- NUNCA use "Lorem ipsum" — crie conteúdo coerente e persuasivo com a instrução.
- NUNCA deixe regiões vazias sem intenção visual.
- SEMPRE crie hierarquia visual (hero element, suporte, background animado).
- Se houver assets do usuário (logos, fotos, vídeos): ELES SÃO O ELEMENTO CENTRAL DO DESIGN.
- Aplique as imagens/logos do usuário com destaque absoluto, centralizado ou no topo das cenas aplicáveis. NUNCA distorça essas imagens.
- Vídeos do usuário: use <video autoplay muted loop playsinline style="object-fit: contain; max-width: 100%; max-height: 100%;">.

═══════════════════════════════════════════
SAÍDA ESPERADA
═══════════════════════════════════════════
Retorne APENAS o código HTML bruto e válido. NENHUMA formatação markdown (sem marcações como ```html). ZERO texto antes ou depois do código. ZERO explicações. Comece com <!DOCTYPE html> e termine com </html>."""

    # ── Montagem do prompt do usuário ─────────────────────────
    user_content = f"INSTRUÇÃO CRIATIVA: {prompt}\n\n"

    if assets:
        user_content += "═══ ASSETS DO USUÁRIO (INTEGRE OBRIGATORIAMENTE) ═══\n"
        for i, asset_url in enumerate(assets, 1):
            mtype = 'VÍDEO' if any(asset_url.lower().endswith(v) for v in ['.mp4', '.webm']) else 'IMAGEM'
            user_content += f"  [{i}] {mtype}: {asset_url}\n"
        user_content += "\nTodos os assets acima DEVEM aparecer no HTML final como elementos visuais centrais. NÃO corte-os e mantenha a proporção natural (object-fit: contain).\n\n"

    if previous_code:
        user_content += (
            "═══ CÓDIGO HTML EXISTENTE ═══\n"
            "Modifique/melhore o HTML abaixo com base na nova instrução. Preserve o que estava bom e garanta a arquitetura nova solicitada.\n\n"
            f"{previous_code}"
        )
    else:
        user_content += "Crie do zero com base na instrução acima."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content}
    ]

    try:
        response = groq_client.chat.completions.create(
            messages=messages,
            model=BEST_FREE_MODEL,
            temperature=0.55,
            max_tokens=8000,
            top_p=0.92,
        )

        generated_html = response.choices[0].message.content.strip()

        # Limpa fences markdown caso o modelo insista
        if generated_html.startswith("```html"):
            generated_html = generated_html[7:]
        elif generated_html.startswith("```"):
            generated_html = generated_html[3:]
        if generated_html.endswith("```"):
            generated_html = generated_html[:-3]
        generated_html = generated_html.strip()

        # Validação básica
        lower = generated_html.lower()
        if not (lower.startswith('<!doctype') or lower.startswith('<html')):
            return jsonify({
                "success": False,
                "error": "O modelo não retornou HTML válido. Tente reformular a instrução."
            }), 500

        # ── Salva no histórico de versões ──────────────────────
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

        # Mantém só as últimas 15 versões por sessão
        if len(version_history[session_id]) > 15:
            version_history[session_id] = version_history[session_id][-15:]

        tokens_used = getattr(getattr(response, 'usage', None), 'total_tokens', 0)

        return jsonify({
            "success":       True,
            "html":          generated_html,
            "version_id":    version_entry["id"],
            "version_count": len(version_history[session_id]),
            "tokens_used":   tokens_used,
            "model":         BEST_FREE_MODEL,
        })

    except Exception as e:
        error_msg = str(e)
        if "rate_limit" in error_msg.lower():
            msg = "Limite de requisições atingido. Aguarde um momento e tente novamente."
        elif "api_key" in error_msg.lower() or "auth" in error_msg.lower():
            msg = "Erro de autenticação. Verifique sua GROQ_API_KEY."
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
# TEMPLATES PRONTOS
# ─────────────────────────────────────────────
@app.route('/api/templates', methods=['GET'])
def api_templates():
    templates = [
        {
            "id":     "brand_intro",
            "name":   "✨ Intro de Marca",
            "prompt": "Crie uma intro animada de marca com logo centralizado rodeado por partículas brilhantes, tagline com efeito de digitação letra por letra, fundo com gradiente dark animado e fade out suave no final",
            "style":  "dark",
        },
        {
            "id":     "product_launch",
            "name":   "🚀 Lançamento de Produto",
            "prompt": "Crie uma animação de lançamento de produto premium: título com efeito de explosão de letras, subtítulo, preço em destaque com glow dourado, botão de compra pulsante e fundo com efeito de luz dinâmica",
            "style":  "dark",
        },
        {
            "id":     "countdown",
            "name":   "⏱️ Contagem Regressiva",
            "prompt": "Crie uma contagem regressiva animada para evento especial com JavaScript calculando dias, horas, minutos e segundos em tempo real. Cada número em card separado com flip animation, título do evento e data acima",
            "style":  "neon",
        },
        {
            "id":     "social_promo",
            "name":   "🎉 Promoção Relâmpago",
            "prompt": "Crie um post animado de promoção relâmpago: badge 'OFERTA' piscando, porcentagem de desconto gigante com efeito de zoom, preço riscado e preço novo, confete caindo animado, urgência com texto 'Só hoje!'",
            "style":  "gradient",
        },
        {
            "id":     "testimonial",
            "name":   "⭐ Depoimento de Cliente",
            "prompt": "Crie um card animado de depoimento: 5 estrelas douradas que aparecem uma a uma, foto de perfil circular com borda animada, texto do depoimento com digitação progressiva, nome e cargo do cliente",
            "style":  "minimal",
        },
        {
            "id":     "stats_showcase",
            "name":   "📊 Showcase de Números",
            "prompt": "Crie uma animação de estatísticas impactantes com 3 números grandes que contam do zero até o valor final (ex: 10.000 clientes, 98% satisfação, R$2M faturado), ícones e labels, cada stat em card glassmorphism",
            "style":  "dark",
        },
        {
            "id":     "event_invite",
            "name":   "🎟️ Convite para Evento",
            "prompt": "Crie um convite animado para evento premium com data, horário, local, speaker em destaque com foto placeholder circular, botão RSVP com efeito shimmer e fundo com bokeh animado",
            "style":  "gold",
        },
        {
            "id":     "services_carousel",
            "name":   "🛠️ Carrossel de Serviços",
            "prompt": "Crie um carrossel automático (auto-play com JavaScript, 3s por slide) mostrando 4 serviços diferentes, cada slide com ícone emoji grande, título, descrição curta, número do slide e barra de progresso animada",
            "style":  "dark",
        },
        {
            "id":     "music_visualizer",
            "name":   "🎵 Visualizador Musical",
            "prompt": "Crie um visualizador de música animado com barras de equalizer pulsando em CSS animation, nome da música, artista, disco girando animado, controles de play/pause decorativos e fundo dark com glow neon",
            "style":  "neon",
        },
        {
            "id":     "before_after",
            "name":   "🔄 Antes e Depois",
            "prompt": "Crie uma animação de comparação antes/depois com slider central que se move automaticamente revelando o lado 'depois'. Labels ANTES e DEPOIS animados, resultado final destacado com check verde e título de transformação",
            "style":  "minimal",
        },
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
        "api_configured": bool(GROQ_API_KEY),
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
    print(f"🤖 Modelo IA: {BEST_FREE_MODEL}")
    print(f"🔑 API Key: {'✅ configurada' if GROQ_API_KEY else '❌ NÃO configurada'}")
    app.run(host='0.0.0.0', port=port, debug=debug)
