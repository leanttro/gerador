import os
import time
import uuid
import json
import random
import threading
import smtplib
import requests
import urllib.parse
import re
import csv
import io
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from flask import Flask, request, jsonify, render_template, send_from_directory, session, Response, redirect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq
from werkzeug.utils import secure_filename
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

try:
    import imgkit
except ImportError:
    imgkit = None

try:
    import weasyprint
except ImportError:
    weasyprint = None

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
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")

BEST_FREE_MODEL = "llama-3.3-70b-versatile"

if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)
else:
    groq_client = None

# Histórico de versões em memória (por session_id)
version_history: dict[str, list[dict]] = {}

# ─────────────────────────────────────────────
# CONFIGURAÇÃO DO BAILEYS (WhatsApp)
# ─────────────────────────────────────────────
BAILEYS_URL = os.environ.get("BAILEYS_URL", "http://213.199.56.207:3000")
# ─────────────────────────────────────────────
# VARIÁVEIS DIRECTUS E GOOGLE (extras)
# ─────────────────────────────────────────────
DIRECTUS_URL = os.environ.get("DIRECTUS_URL", "")
DIRECTUS_TOKEN = os.environ.get("DIRECTUS_TOKEN", "")
DIRECTUS_TABLE = os.environ.get("DIRECTUS_TABLE", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "https://seuapp.com/api/metricas/oauth/callback")
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "")

def get_directus_headers():
    return {
        "Authorization": f"Bearer {DIRECTUS_TOKEN}",
        "Content-Type": "application/json"
    }


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

@app.route('/home')
def home():
    return render_template('home.html',
        directus_url=os.environ.get("DIRECTUS_URL", ""),
        directus_token=os.environ.get("DIRECTUS_TOKEN", ""),
        directus_table=os.environ.get("DIRECTUS_TABLE", "")
    )
@app.route('/conteudo')
def conteudo():
    return render_template('conteudo.html')

@app.route('/criacao')
def criacao():
    return render_template('criacao.html')

@app.route('/prospeccao')
def prospeccao():
    return render_template('prospeccao.html', 
        directus_url=os.environ.get("DIRECTUS_URL", ""),
        directus_token=os.environ.get("DIRECTUS_TOKEN", ""),
        directus_table=os.environ.get("DIRECTUS_TABLE", ""),
        serper_api_key=os.environ.get("SERPER_API_KEY", "")
    )

@app.route('/whatsapp')
def whatsapp():
    return render_template('whatsapp.html',
        directus_url=os.environ.get("DIRECTUS_URL", ""),
        directus_token=os.environ.get("DIRECTUS_TOKEN", ""),
        directus_table=os.environ.get("DIRECTUS_TABLE", "")
    )

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
# NOVA ROTA: INSPIRAÇÕES VIA SERPER
# ─────────────────────────────────────────────
@app.route('/api/inspirations', methods=['POST'])
@limiter.limit("20 per minute")
def api_inspirations():
    if not SERPER_API_KEY:
        return jsonify({"success": False, "error": "SERPER_API_KEY não configurada no servidor."}), 400

    data = request.json or {}
    niche = data.get('niche', '').strip()
    if not niche:
        return jsonify({"success": False, "error": "Nicho não informado."}), 400

    # Construção da query para buscar ideias de posts orientadas ao criador/negócio
    query = f"ideias de posts para negócio de {niche} criador de conteúdo instagram dicas simples"
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "q": query,
        "num": 10
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        results = resp.json()
        organic = results.get("organic", [])

        inspos = []
        for item in organic[:8]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            link = item.get("link", "")
            # Classificação simples pelo título/snippet
            lower_text = (title + " " + snippet).lower()
            if "reels" in lower_text or " reel " in lower_text:
                post_type = "Reels"
            elif "carrossel" in lower_text or "carousel" in lower_text:
                post_type = "Carrossel"
            else:
                post_type = "Postagem Estática"

            inspos.append({
                "type": post_type,
                "title": title[:120],
                "snippet": snippet[:180],
                "link": link
            })

        # Camada de IA: reformula os snippets brutos como ideias práticas para o criador/empresário
        if inspos and groq_client:
            raw_snippets = "\n".join([f"- {i['title']}: {i['snippet']}" for i in inspos])
            try:
                ai_resp = groq_client.chat.completions.create(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Você é um estrategista de conteúdo para pequenos negócios. "
                                "A partir dos resultados abaixo, extraia ideias SIMPLES e PRÁTICAS de posts "
                                "que um empresário ou IA pode criar rapidamente: ex. 'Mostre os bastidores da montagem', "
                                "'Poste uma comparação antes/depois do prato', 'Faça um carrossel com os ingredientes do dia'. "
                                "Seja direto, sem enrolação. Retorne APENAS um JSON: "
                                "{\"ideias\": [{\"tipo\": \"Reels|Carrossel|Estática\", \"titulo\": \"...\", \"descricao\": \"...\"}]}"
                            )
                        },
                        {"role": "user", "content": f"Nicho: {niche}\n\nResultados:\n{raw_snippets}"}
                    ],
                    model=BEST_FREE_MODEL,
                    temperature=0.5,
                    response_format={"type": "json_object"},
                )
                parsed = json.loads(ai_resp.choices[0].message.content)
                ideias = parsed.get("ideias", [])
                inspos = [
                    {
                        "type": i.get("tipo", "Postagem Estática"),
                        "title": i.get("titulo", ""),
                        "snippet": i.get("descricao", ""),
                        "link": ""
                    }
                    for i in ideias
                ]
            except Exception:
                pass  # fallback: mantém os inspos originais do Serper

        return jsonify({"success": True, "inspirations": inspos})
    except Exception as e:
        return jsonify({"success": False, "error": f"Erro na busca: {str(e)}"}), 500


# ─────────────────────────────────────────────
# HELPERS DE IA COM FALLBACK AUTOMÁTICO
# Ordem: OpenRouter → Groq → Gemini
# ─────────────────────────────────────────────

def _openrouter_call(messages, model="google/gemini-2.0-flash-001",
                     temperature=0.45, max_tokens=8192, json_mode=False):
    """Chama OpenRouter. Lança exceção em caso de falha."""
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY ausente")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    res = requests.post(url, headers=headers, json=payload, timeout=60).json()
    if 'error' in res:
        err = res['error']
        msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
        raise Exception(f"OpenRouter: {msg}")
    text = res['choices'][0]['message']['content']
    tokens = res.get('usage', {}).get('total_tokens', 0)
    return text, tokens


def _groq_call(messages, temperature=0.45, max_tokens=8000, json_mode=False):
    """Chama Groq. Lança exceção em caso de falha."""
    if not groq_client:
        raise Exception("GROQ_API_KEY ausente")
    kwargs = dict(
        messages=messages,
        model=BEST_FREE_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=0.92,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = groq_client.chat.completions.create(**kwargs)
    text = response.choices[0].message.content
    tokens = getattr(getattr(response, 'usage', None), 'total_tokens', 0)
    return text, tokens


def _gemini_call(system_prompt, user_content,
                 temperature=0.45, max_tokens=8192, json_mode=False):
    """Chama Gemini diretamente. Lança exceção em caso de falha."""
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY ausente")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    gen_config = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gen_config["responseMimeType"] = "application/json"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_content}]}],
        "generationConfig": gen_config,
    }
    res = requests.post(url, json=payload, timeout=60).json()
    if 'error' in res:
        raise Exception(f"Gemini: {res['error']['message']}")
    text = res['candidates'][0]['content']['parts'][0]['text']
    return text, 0


def _ai_with_fallback(system_prompt, user_content,
                      temperature=0.45, max_tokens=8192,
                      json_mode=False, preferred_engine='openrouter'):
    """
    Tenta provedores em cascata: preferred_engine → os demais.
    Ordem padrão: openrouter → groq → gemini.
    Retorna (text, tokens, engine_usado).
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]
    all_providers = ['openrouter', 'groq', 'gemini']
    if preferred_engine in all_providers:
        order = [preferred_engine] + [p for p in all_providers if p != preferred_engine]
    else:
        order = all_providers

    errors = []
    for provider in order:
        try:
            print(f"[ai_fallback] Tentando provider: {provider}")
            if provider == 'openrouter':
                text, tokens = _openrouter_call(
                    messages, temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode)
            elif provider == 'groq':
                text, tokens = _groq_call(
                    messages, temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode)
            elif provider == 'gemini':
                text, tokens = _gemini_call(
                    system_prompt, user_content,
                    temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode)
            else:
                continue
            print(f"[ai_fallback] Sucesso com: {provider}")
            return text, tokens, provider
        except Exception as e:
            errors.append(f"{provider}: {str(e)}")
            print(f"[ai_fallback] {provider} falhou → {e}. Tentando próximo...")
            continue

    raise Exception("Todos os provedores falharam → " + " | ".join(errors))


# ─────────────────────────────────────────────
# GERAÇÃO DE HTML VIA IA (UNIFICADA)
# ─────────────────────────────────────────────
@app.route('/api/generate', methods=['POST'])
@limiter.limit("25 per minute")
def api_generate():
    data = request.json or {}
    prompt           = data.get('prompt', '').strip()
    assets           = data.get('assets', [])
    previous_code    = data.get('previous_code', None)
    style_preset     = data.get('style_preset', 'dark')
    format_ratio     = data.get('format_ratio', '9:16')
    ai_engine        = data.get('ai_engine', 'groq')
    generation_mode  = data.get('generation_mode', 'generate')   # 'generate' ou 'replace'

    if not prompt:
        return jsonify({"success": False, "error": "Prompt não pode estar vazio"}), 400

    if len(prompt) > 3000:
        return jsonify({"success": False, "error": "Prompt muito longo. Máximo: 3000 caracteres"}), 400

    # ─────────────────────────────────────────────────────────────────
    # MODO REPLACE (substituição exata de variáveis CHAVE_)
    # ─────────────────────────────────────────────────────────────────
    if generation_mode == "replace":
        if not previous_code:
            return jsonify({"success": False, "error": "Modo 'replace' exige um template base (previous_code)."}), 400

        # Mapeamento de estilos para guia de cores (usado pela IA para gerar JSON)
        style_guides = {
            'dark':      'Cores HEX para paleta escura premium. Pretos profundos, cinzas e acentos neon.',
            'neon':      'Cores HEX para paleta neon vibrante. Roxo, rosa, azul elétrico em fundo preto absoluto.',
            'minimal':   'Cores HEX para design minimalista. Tons sólidos, sóbrios e muito espaço branco.',
            'gold':      'Cores HEX para paleta luxo. Dourado, preto fosco, champagne.',
            'gradient':  'Cores HEX para paleta moderna e vibrante.',
            'corporate': 'Cores HEX para estilo corporativo. Azul petróleo, branco, cinza.',
        }
        style_guide = style_guides.get(style_preset, style_guides['dark'])

        system_prompt_replace = f"""Você é um motor de processamento de dados JSON estruturado.
A sua única função é ler o código HTML do template fornecido, identificar todas as variáveis em maiúsculo que começam com CHAVE_ e preenchê-las com base na instrução do usuário.

Regras de processamento
* Retorne APENAS um objeto JSON válido, sem crases, sem markdown, sem explicações.
* As chaves do JSON devem ser as variáveis encontradas no código, como CHAVE_TITULO, CHAVE_SUBTITULO, CHAVE_IMG_1, CHAVE_COR_PRIMARIA, etc.
* Extraia a intenção criativa do usuário para gerar os textos corretos.
* Use as URLs dos ASSETS fornecidos para substituir as chaves de imagem/vídeo.
* Para chaves de cor, gere códigos HEX reais com base na seguinte diretriz de estilo: {style_guide}
* Não altere o nome das chaves. Copie exatamente como estão no HTML fornecido."""

        user_content_replace = f"INSTRUÇÃO CRIATIVA: {prompt}\n\n"

        if assets:
            user_content_replace += "ASSETS DISPONÍVEIS:\n" + '\n'.join(assets) + '\n\n'

        user_content_replace += f"CÓDIGO TEMPLATE PARA MAPEAMENTO DE CHAVES:\n{previous_code}"

        try:
            generated_json_str = ""
            tokens_used = 0
            engine_used = ai_engine

            # ── Fallback automático: tenta preferred → openrouter → groq → gemini ──
            generated_json_str, tokens_used, engine_used = _ai_with_fallback(
                system_prompt_replace,
                user_content_replace,
                temperature=0.1,
                max_tokens=4096,
                json_mode=True,
                preferred_engine=ai_engine,
            )

            # Limpeza rigorosa para evitar falha de parse JSON
            generated_json_str = generated_json_str.strip()
            if generated_json_str.startswith("```json"):
                generated_json_str = generated_json_str[7:]
            elif generated_json_str.startswith("```"):
                generated_json_str = generated_json_str[3:]
            if generated_json_str.endswith("```"):
                generated_json_str = generated_json_str[:-3]
            generated_json_str = generated_json_str.strip()

            try:
                substituicoes = json.loads(generated_json_str)
            except json.JSONDecodeError as e:
                raise Exception(f"A IA não retornou um JSON válido. Erro: {str(e)} Retorno: {generated_json_str[:100]}")

            html_final = previous_code
            for chave, valor in substituicoes.items():
                html_final = html_final.replace(str(chave), str(valor))

            # Garante substituição de CHAVE_PROPORCAO com o formato selecionado
            if 'CHAVE_PROPORCAO' in html_final:
                html_final = html_final.replace('CHAVE_PROPORCAO', format_ratio)

            # Salvar no histórico
            session_id = get_session_id(request)
            if session_id not in version_history:
                version_history[session_id] = []

            version_entry = {
                "id":        str(uuid.uuid4()),
                "timestamp": int(time.time()),
                "prompt":    prompt,
                "style":     style_preset,
                "format":    format_ratio,
                "html":      html_final,
                "assets":    assets,
                "mode":      "replace"
            }
            version_history[session_id].append(version_entry)
            if len(version_history[session_id]) > 15:
                version_history[session_id] = version_history[session_id][-15:]

            return jsonify({
                "success":       True,
                "html":          html_final,
                "version_id":    version_entry["id"],
                "version_count": len(version_history[session_id]),
                "tokens_used":   tokens_used,
                "model":         engine_used,
                "mode":          "replace"
            })

        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "limit" in error_msg.lower():
                msg = "Cota de IA excedida (429). Aguarde alguns segundos ou troque de motor."
            elif "api_key" in error_msg.lower() or "auth" in error_msg.lower():
                msg = "Erro de autenticação nas chaves de API."
            else:
                msg = f"Erro no replace: {error_msg}"
            return jsonify({"success": False, "error": msg}), 500

    # ─────────────────────────────────────────────────────────────────
    # MODO GENERATE (criação livre, igual ao original)
    # ─────────────────────────────────────────────────────────────────
    else:
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

        system_prompt_generate = f"""Você é um Desenvolvedor Front-End ELITE, Diretor de Arte e Especialista em Motion Design (Motor Dark Studio).

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
8. CARROSSEL / POST ESTÁTICO (CRÍTICO): Se o usuário pedir um carrossel de fotos para postar, ou uma imagem estática, NUNCA USE auto-play rápido. Crie botões visíveis de navegação (Próximo/Anterior) ou permita troca de cena por clique, para que o usuário possa tirar print de cada tela com calma. Pare o temporizador automático!

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
Retorne APENAS o código HTML bruto e válido. NENHUMA formatação markdown. ZERO texto antes ou depois do código. ZERO explicações. Comece com <!DOCTYPE html> e termine com </html>."""

        user_content_generate = f"INSTRUÇÃO CRIATIVA: {prompt}\n\n"

        if assets:
            user_content_generate += "═══ ASSETS DO USUÁRIO (INTEGRE OBRIGATORIAMENTE) ═══\n"
            for i, asset_url in enumerate(assets, 1):
                mtype = 'VÍDEO' if any(asset_url.lower().endswith(v) for v in ['.mp4', '.webm']) else 'IMAGEM'
                user_content_generate += f"  [{i}] {mtype}: {asset_url}\n"
            user_content_generate += "\nTodos os assets acima DEVEM aparecer no HTML final como elementos visuais centrais. NÃO corte-os e mantenha a proporção natural (object-fit: contain).\n\n"

        if previous_code:
            user_content_generate += (
                "═══ CÓDIGO HTML EXISTENTE (TEMPLATE BASE) ═══\n"
                "O código abaixo é um template funcional de alta qualidade.\n"
                "PRESERVE estritamente a física do canvas, os temporizadores (timers/JS), os z-indexes e o motor de cenas.\n"
                "Sua tarefa é SUBSTITUIR APENAS textos, URLs de imagens, cores (mantendo a harmonia) e tipografia para se adaptar perfeitamente à INSTRUÇÃO CRIATIVA e aos ASSETS DO USUÁRIO.\n\n"
                f"{previous_code}"
            )
        else:
            user_content_generate += "Crie do zero com base na instrução acima."

        try:
            generated_html = ""
            tokens_used = 0
            engine_used = ai_engine

            # ── Fallback automático: tenta preferred → openrouter → groq → gemini ──
            generated_html, tokens_used, engine_used = _ai_with_fallback(
                system_prompt_generate,
                user_content_generate,
                temperature=0.45,
                max_tokens=8192,
                json_mode=False,
                preferred_engine=ai_engine,
            )

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
                "mode":      "generate"
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
                "model":         engine_used,
                "mode":          "generate"
            })

        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "limit" in error_msg.lower():
                msg = "Cota de IA excedida (429). Aguarde alguns segundos ou troque de motor."
            elif "api_key" in error_msg.lower() or "auth" in error_msg.lower():
                msg = "Erro de autenticação nas chaves de API."
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
            "mode":      v.get("mode", "generate")
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
# FORMULÁRIO DO CLIENTE
# ─────────────────────────────────────────────
@app.route('/formulario')
def formulario():
    return render_template('formulario.html')


# ─────────────────────────────────────────────
# SUGESTÃO DE TEXTO VIA IA (para o formulário e Kanban)
# ─────────────────────────────────────────────
@app.route('/api/sugestao-texto', methods=['POST'])
@limiter.limit("60 per minute")
def api_sugestao_texto():
    data = request.json or {}
    prompt = data.get('prompt', '').strip()

    if not prompt:
        return jsonify({"success": False, "error": "Prompt vazio"}), 400

    if not groq_client:
        return jsonify({"success": False, "error": "GROQ_API_KEY não configurada"}), 400

    try:
        response = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é um assistente especialista em marketing digital e diretor de arte. "
                        "Siga rigorosamente as instruções do usuário. "
                        "Se o usuário pedir um retorno em JSON, retorne APENAS o JSON válido e estrito, "
                        "sem blocos de código markdown (```json) e sem nenhum texto adicional."
                    )
                },
                {"role": "user", "content": prompt}
            ],
            model=BEST_FREE_MODEL,
            temperature=0.5,
            max_tokens=3000,
        )
        text = response.choices[0].message.content.strip()
        return jsonify({"success": True, "text": text})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# RECEBER PEDIDO DO FORMULÁRIO
# Salva o pedido em JSON local (sem banco de dados)
# ─────────────────────────────────────────────
PEDIDOS_FOLDER = os.path.join(BASE_DIR, 'pedidos')
os.makedirs(PEDIDOS_FOLDER, exist_ok=True)

@app.route('/painel')
def painel():
    return render_template('painel.html')

@app.route('/api/form-pedido', methods=['POST'])
@limiter.limit("10 per minute")
def api_form_pedido():
    data = request.json or {}

    pedido_id = str(uuid.uuid4())[:8].upper()
    timestamp = int(time.time())

    pedido = {
        "id":           pedido_id,
        "timestamp":    timestamp,
        "template_id":  data.get("template_id", ""),
        "prompt":       data.get("prompt", ""),
        "assets":       data.get("assets", []),
        "colors":       data.get("colors", {}),
        "fields":       data.get("fields", {}),
        "status":       "pendente",
    }

    # Salva em arquivo JSON na pasta pedidos/
    pedido_path = os.path.join(PEDIDOS_FOLDER, f"pedido_{pedido_id}.json")
    try:
        with open(pedido_path, 'w', encoding='utf-8') as f:
            json.dump(pedido, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({"success": False, "error": f"Erro ao salvar pedido: {str(e)}"}), 500

    print(f"[PEDIDO] Novo pedido recebido: {pedido_id} | Template: {pedido['template_id']} | Cliente: {pedido['fields'].get('cliente','')}")

    return jsonify({
        "success":   True,
        "pedido_id": pedido_id,
        "message":   "Pedido recebido com sucesso!"
    })


# ─────────────────────────────────────────────
# LISTAR PEDIDOS (para você ver no painel)
# ─────────────────────────────────────────────
@app.route('/api/pedidos', methods=['GET'])
def api_pedidos():
    pedidos = []
    try:
        for fname in sorted(os.listdir(PEDIDOS_FOLDER), reverse=True):
            if fname.endswith('.json'):
                fpath = os.path.join(PEDIDOS_FOLDER, fname)
                with open(fpath, 'r', encoding='utf-8') as f:
                    pedidos.append(json.load(f))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True, "pedidos": pedidos, "total": len(pedidos)})


# ─────────────────────────────────────────────
# CRM — ARMAZENAMENTO LOCAL (JSON)
# ─────────────────────────────────────────────
CONTACTS_FILE = os.path.join(BASE_DIR, 'contacts.json')

def load_contacts():
    try:
        if os.path.exists(CONTACTS_FILE):
            with open(CONTACTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[CRM] Erro ao carregar contacts.json: {e}")
    return []

def save_contacts(contacts):
    try:
        with open(CONTACTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(contacts, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[CRM] Erro ao salvar contacts.json: {e}")
        return False

@app.route('/api/crm/contacts', methods=['GET'])
def crm_list():
    contacts = load_contacts()
    return jsonify({"success": True, "contacts": contacts, "total": len(contacts)})

@app.route('/api/crm/contacts', methods=['POST'])
@limiter.limit("120 per minute")
def crm_create():
    data = request.json or {}
    contacts = load_contacts()
    contact = {
        "id":         str(uuid.uuid4()),
        "created_at": int(time.time()),
        "nome":       data.get("nome", "").strip(),
        "empresa":    data.get("empresa", "").strip(),
        "whatsapp":   data.get("whatsapp", "").strip(),
        "email":      data.get("email", "").strip(),
        "origem":     data.get("origem", "manual"),
        "status":     data.get("status", "novo"),
        "tags":       data.get("tags", []),
        "obs":        data.get("obs", "").strip(),
    }
    if not contact["nome"] and not contact["empresa"] and not contact["whatsapp"]:
        return jsonify({"success": False, "error": "Preencha nome, empresa ou WhatsApp."}), 400
    contacts.append(contact)
    save_contacts(contacts)
    return jsonify({"success": True, "contact": contact})

@app.route('/api/crm/contacts/<contact_id>', methods=['GET'])
def crm_get(contact_id):
    contacts = load_contacts()
    contact  = next((c for c in contacts if c["id"] == contact_id), None)
    if not contact:
        return jsonify({"success": False, "error": "Contato não encontrado."}), 404
    return jsonify({"success": True, "contact": contact})

@app.route('/api/crm/contacts/<contact_id>', methods=['PATCH'])
@limiter.limit("120 per minute")
def crm_update(contact_id):
    data     = request.json or {}
    contacts = load_contacts()
    idx      = next((i for i, c in enumerate(contacts) if c["id"] == contact_id), None)
    if idx is None:
        return jsonify({"success": False, "error": "Contato não encontrado."}), 404
    allowed = ["nome","empresa","whatsapp","email","origem","status","tags","obs"]
    for field in allowed:
        if field in data:
            contacts[idx][field] = data[field]
    contacts[idx]["updated_at"] = int(time.time())
    save_contacts(contacts)
    return jsonify({"success": True, "contact": contacts[idx]})

@app.route('/api/crm/contacts/<contact_id>', methods=['DELETE'])
@limiter.limit("60 per minute")
def crm_delete(contact_id):
    contacts = load_contacts()
    original = len(contacts)
    contacts = [c for c in contacts if c["id"] != contact_id]
    if len(contacts) == original:
        return jsonify({"success": False, "error": "Contato não encontrado."}), 404
    save_contacts(contacts)
    return jsonify({"success": True, "message": "Contato excluído."})

@app.route('/api/crm/export', methods=['GET'])
def crm_export():
    import csv, io
    contacts = load_contacts()
    output   = io.StringIO()
    writer   = csv.writer(output)
    headers  = ["nome","empresa","whatsapp","email","origem","status","tags","obs","created_at"]
    writer.writerow(headers)
    for c in contacts:
        writer.writerow([
            c.get("nome",""), c.get("empresa",""), c.get("whatsapp",""),
            c.get("email",""), c.get("origem",""), c.get("status",""),
            ";".join(c.get("tags",[])), c.get("obs",""),
            c.get("created_at","")
        ])
    output.seek(0)
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=crm_export_{int(time.time())}.csv"}
    )

# ─────────────────────────────────────────────
# MINERADOR — GOOGLE MAPS, INSTAGRAM, LINKEDIN
# ─────────────────────────────────────────────
def _limpar_telefone(txt):
    nums = re.sub(r'\D', '', str(txt))
    if len(nums) >= 10:
        return f"55{nums}" if not nums.startswith('55') else nums
    return None

def _extrair_email_txt(txt):
    m = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', str(txt))
    return m.group(0) if m else None

def _extrair_whatsapp_txt(txt):
    padrao = r'(?:(?:\+|00)?55\s?)?(?:\(?([1-9][0-9])\)?\s?)?(?:((?:9\d|[2-9])\d{3})\-?(\d{4}))'
    match  = re.search(padrao, str(txt))
    if match:
        ddd, p1, p2 = match.groups()
        if not ddd: ddd = "11"
        return f"55{ddd}{p1}{p2}".replace(" ","").replace("-","")
    return None

def _serper_places(query, num=20):
    if not SERPER_API_KEY:
        return []
    try:
        res = requests.post(
            "https://google.serper.dev/places",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num, "gl": "br", "hl": "pt-br"},
            timeout=15
        )
        return res.json().get("places", [])
    except Exception as e:
        print(f"[Minerador] Serper places error: {e}")
        return []

def _serper_search(query, num=20):
    if not SERPER_API_KEY:
        return []
    try:
        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num, "gl": "br", "hl": "pt-br"},
            timeout=15
        )
        return res.json().get("organic", [])
    except Exception as e:
        print(f"[Minerador] Serper search error: {e}")
        return []

@app.route('/api/minerador', methods=['POST'])
@limiter.limit("30 per minute")
def api_minerador():
    data    = request.json or {}
    fonte   = data.get("fonte", "maps")
    nicho   = data.get("nicho", "").strip()
    cidade  = data.get("cidade", "").strip()
    bairros = data.get("bairros", "").strip()
    quantidade = int(data.get("quantidade", 20))
    results = []

    if fonte == "maps":
        lista_bairros = [b.strip() for b in bairros.split(',') if b.strip()] if bairros else [""]
        for bairro in lista_bairros:
            query = f"{nicho} em {bairro} {cidade}".strip() if bairro else f"{nicho} {cidade}".strip()
            places = _serper_places(query, quantidade)
            for p in places:
                nome    = p.get("title","")
                tel_raw = p.get("phoneNumber","")
                tel     = _limpar_telefone(tel_raw) if tel_raw else None
                results.append({
                    "nome":     "", "empresa": nome,
                    "whatsapp": tel or "",
                    "email":    "",
                    "endereco": p.get("address",""),
                    "info":     p.get("address",""),
                    "fonte":    "maps",
                    "link":     p.get("website",""),
                })
            time.sleep(0.3)

    elif fonte == "instagram":
        queries = [
            f'site:instagram.com "{nicho}" "{cidade}"',
            f'site:instagram.com "{nicho}" {cidade} whatsapp',
        ]
        seen = set()
        for q in queries:
            items = _serper_search(q, quantidade)
            for item in items:
                txt      = (item.get("title","") + " " + item.get("snippet",""))
                wpp      = _extrair_whatsapp_txt(txt)
                email    = _extrair_email_txt(txt)
                empresa  = item.get("title","").split("|")[0].split("•")[0].strip()[:60]
                key      = wpp or email or empresa
                if key and key not in seen:
                    seen.add(key)
                    results.append({
                        "nome": "", "empresa": empresa,
                        "whatsapp": wpp or "",
                        "email":    email or "",
                        "endereco": "", "info": item.get("snippet","")[:100],
                        "fonte":    "instagram",
                        "link":     item.get("link",""),
                    })

    elif fonte == "linkedin":
        queries = [
            f'site:linkedin.com/company "{nicho}" "{cidade}"',
            f'site:linkedin.com/in "{nicho}" "{cidade}"',
        ]
        seen = set()
        for q in queries:
            items = _serper_search(q, quantidade)
            for item in items:
                txt    = (item.get("title","") + " " + item.get("snippet",""))
                wpp    = _extrair_whatsapp_txt(txt)
                email  = _extrair_email_txt(txt)
                emp    = item.get("title","").split("|")[0].split("-")[0].strip()[:60]
                key    = wpp or email or emp
                if key and key not in seen:
                    seen.add(key)
                    results.append({
                        "nome": "", "empresa": emp,
                        "whatsapp": wpp or "",
                        "email":    email or "",
                        "endereco": "", "info": item.get("snippet","")[:100],
                        "fonte":    "linkedin",
                        "link":     item.get("link",""),
                    })

    if not results:
        return jsonify({"success": True, "results": [], "message": "Nenhum contato encontrado. Tente outro nicho ou cidade."})

    return jsonify({"success": True, "results": results, "total": len(results)})

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@app.route('/api/scrape-email', methods=['POST'])
def api_scrape_email():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url: 
        return jsonify({"email": ""})
    
    if not url.startswith('http'): 
        url = 'http://' + url
        
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        res = requests.get(url, headers=headers, timeout=5, verify=False)
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', res.text)
        validos = [e for e in set(emails) if not e.endswith(('png','jpg','jpeg','gif','webp', 'css', 'js')) and 'sentry' not in e and 'wix' not in e]
        
        if validos:
            return jsonify({"email": validos[0].lower()})
    except:
        pass
        
    return jsonify({"email": ""})


# ─────────────────────────────────────────────
# WHATSAPP — STATUS DO CHIP (Baileys)
# ─────────────────────────────────────────────
@app.route('/api/wpp/status', methods=['GET'])
def wpp_status():
    try:
        r = requests.get(f"{BAILEYS_URL}/status", timeout=5)
        data = r.json()
        return jsonify({
            "success":   True,
            "connected": data.get("connected", False),
            "number":    data.get("number", "")
        })
    except Exception as e:
        return jsonify({"success": True, "connected": False, "number": "", "error": str(e)})


# ─────────────────────────────────────────────
# WHATSAPP — ENVIAR MENSAGEM (via Baileys)
# ─────────────────────────────────────────────
@app.route('/api/wpp/send', methods=['POST'])
@limiter.limit("120 per minute")
def wpp_send():
    data      = request.json or {}
    number    = data.get("number", "").strip()
    message   = data.get("message", "").strip()
    image     = data.get("image", None)
    video_url = data.get("videoUrl", "").strip()

    if not number:
        return jsonify({"success": False, "error": "Número não informado."}), 400
    if not message:
        return jsonify({"success": False, "error": "Mensagem não pode estar vazia."}), 400

    # Normaliza o número: garante formato 55DDDNUMERO
    number_clean = re.sub(r'\D', '', number)
    if not number_clean.startswith('55') and len(number_clean) >= 10:
        number_clean = '55' + number_clean

    payload = {
        "number":  number_clean,
        "message": message
    }
    if image:
        payload["image"] = image
    if video_url:
        payload["videoUrl"] = video_url

    try:
        r = requests.post(
            f"{BAILEYS_URL}/disparar",
            json=payload,
            timeout=20
        )
        if r.status_code == 200:
            return jsonify({"success": True, "number": number_clean})
        else:
            return jsonify({"success": False, "error": f"API Baileys retornou {r.status_code}: {r.text[:200]}"}), 500
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "error": "API do WhatsApp offline. Verifique o servidor Baileys."}), 503
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# WHATSAPP — GERAR COPY COM IA (Groq)
# ─────────────────────────────────────────────
@app.route('/api/wpp/generate-copy', methods=['POST'])
@limiter.limit("20 per minute")
def wpp_generate_copy():
    if not groq_client:
        return jsonify({"success": False, "error": "GROQ_API_KEY não configurada no servidor."}), 400

    data      = request.json or {}
    nicho     = data.get("nicho", "negócios B2B").strip()
    empresa   = data.get("empresa", "").strip()
    descricao = data.get("descricao", "").strip()

    contexto = ""
    if empresa:   contexto += f" Minha empresa se chama {empresa}."
    if descricao: contexto += f" O que vendemos: {descricao}."

    prompt = (
        f"Você é um especialista em prospecção via WhatsApp. "
        f"Escreva UMA mensagem de abordagem fria, MUITO CURTA (máximo 2 frases), "
        f"informal e direta, para prospectar clientes do nicho: {nicho}.{contexto} "
        f"Use as variáveis {{nome}} para o nome do contato e {{empresa}} para a empresa dele. "
        f"Tom: vizinho simpático mas profissional. Sem cara de robô. Sem emojis em excesso. "
        f"Retorne apenas o texto da mensagem, sem aspas, sem explicações."
    )

    try:
        response = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=BEST_FREE_MODEL,
            temperature=0.7,
            max_tokens=150
        )
        copy = response.choices[0].message.content.strip()
        return jsonify({"success": True, "copy": copy})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# E-MAIL — ROTA DE PÁGINA
# ─────────────────────────────────────────────
@app.route('/email')
def email_page():
    return render_template('email.html',
        directus_url=os.environ.get("DIRECTUS_URL", ""),
        directus_token=os.environ.get("DIRECTUS_TOKEN", ""),
        directus_table=os.environ.get("DIRECTUS_TABLE", "")
    )


# ─────────────────────────────────────────────
# E-MAIL — ARQUIVOS DE DADOS
# ─────────────────────────────────────────────
SMTP_CONFIG_FILE   = os.path.join(BASE_DIR, 'smtp_config.json')
EMAIL_HISTORY_FILE = os.path.join(BASE_DIR, 'email_history.json')

# Jobs de disparo em memória: { job_id: { status, progress, log, total, sent, errors } }
email_jobs: dict = {}


def _directus_smtp_table():
    """Retorna o nome da coleção SMTP no Directus, testando os dois formatos possíveis."""
    candidates = ["config_smtp", "Config_SMTP", "Config SMTP", "Config_Smtp"]
    for name in candidates:
        try:
            import urllib.parse as _up
            url = f"{DIRECTUS_URL}/items/{_up.quote(name, safe='')}?limit=1"
            r = requests.get(url, headers=get_directus_headers(), timeout=5)
            if r.status_code == 200:
                return name
        except Exception:
            pass
    return None


def load_smtp_config() -> dict:
    """Busca a config SMTP do Directus. Fallback para smtp_config.json local."""
    if DIRECTUS_URL and DIRECTUS_TOKEN:
        try:
            table = _directus_smtp_table()
            if table:
                import urllib.parse as _up
                url = f"{DIRECTUS_URL}/items/{_up.quote(table, safe='')}?limit=1"
                r = requests.get(url, headers=get_directus_headers(), timeout=5)
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    if data:
                        row = data[0]
                        cfg = {
                            "host": row.get("SMTP_Host") or row.get("smtp_host") or "",
                            "port": int(row.get("SMTP_Port") or row.get("smtp_port") or 587),
                            "user": row.get("SMTP_User") or row.get("smtp_user") or "",
                            "pass": row.get("SMTP_Pass") or row.get("smtp_pass") or "",
                            "_directus_id": row.get("id"),
                            "_directus_table": table,
                        }
                        if cfg["host"] and cfg["user"] and cfg["pass"]:
                            return cfg
        except Exception as e:
            print(f"[Email] Erro ao buscar SMTP do Directus: {e}")
    # fallback: arquivo local
    try:
        if os.path.exists(SMTP_CONFIG_FILE):
            with open(SMTP_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[Email] Erro ao carregar smtp_config.json: {e}")
    return {}


def save_smtp_config(cfg: dict) -> bool:
    """Salva a config SMTP no Directus (se disponível) e também localmente como backup."""
    # Salva local sempre (backup)
    try:
        local_cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
        with open(SMTP_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(local_cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Email] Erro ao salvar smtp_config.json local: {e}")

    # Salva no Directus se configurado
    if DIRECTUS_URL and DIRECTUS_TOKEN:
        try:
            import urllib.parse as _up
            table = cfg.get("_directus_table") or _directus_smtp_table()
            if not table:
                table = "Config_SMTP"
            payload = {
                "SMTP_Host": cfg.get("host", ""),
                "SMTP_Port": cfg.get("port", 587),
                "SMTP_User": cfg.get("user", ""),
                "SMTP_Pass": cfg.get("pass", ""),
            }
            directus_id = cfg.get("_directus_id")
            if directus_id:
                r = requests.patch(
                    f"{DIRECTUS_URL}/items/{_up.quote(table, safe='')}/{directus_id}",
                    headers=get_directus_headers(), json=payload, timeout=10
                )
            else:
                r = requests.post(
                    f"{DIRECTUS_URL}/items/{_up.quote(table, safe='')}",
                    headers=get_directus_headers(), json=payload, timeout=10
                )
            if r.status_code in (200, 201, 204):
                print(f"[Email] SMTP salvo no Directus OK")
                return True
            else:
                print(f"[Email] Erro ao salvar SMTP no Directus: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[Email] Erro ao salvar SMTP no Directus: {e}")
    return True  # retorna True se ao menos o local foi salvo


def load_email_history() -> list:
    try:
        if os.path.exists(EMAIL_HISTORY_FILE):
            with open(EMAIL_HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[Email] Erro ao carregar email_history.json: {e}")
    return []


def save_email_history(history: list) -> bool:
    try:
        # Mantém apenas os últimos 500 registros
        history = history[-500:]
        with open(EMAIL_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[Email] Erro ao salvar email_history.json: {e}")
        return False


def _enviar_email_smtp(smtp_cfg: dict, to: str, subject: str, body: str,
                       anexo_bytes=None, anexo_nome=None, anexo_mime=None) -> tuple:
    """Envia um e-mail via SMTP. Retorna (True, 'OK') ou (False, 'mensagem de erro')."""
    try:
        to      = str(to).strip()
        subject = str(subject).strip()
        body    = str(body).replace('\n', '<br>')

        # Detecta imagem inline via {{imagem}}
        usar_inline = (
            anexo_bytes is not None
            and anexo_mime is not None
            and "image" in anexo_mime
            and "{{imagem}}" in body.lower()
        )

        if usar_inline:
            msg = MIMEMultipart('related')
            msg['From']    = smtp_cfg['user']
            msg['To']      = to
            msg['Subject'] = subject
            alt = MIMEMultipart('alternative')
            msg.attach(alt)
            body_upd = body.replace("{{imagem}}", '<br><img src="cid:imagem_corpo" style="max-width:100%;height:auto;"><br>')
            alt.attach(MIMEText(body_upd, 'html', 'utf-8'))
            img = MIMEImage(anexo_bytes)
            img.add_header('Content-ID', '<imagem_corpo>')
            img.add_header('Content-Disposition', 'inline', filename=anexo_nome or 'imagem.jpg')
            msg.attach(img)
        else:
            msg = MIMEMultipart()
            msg['From']    = smtp_cfg['user']
            msg['To']      = to
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'html', 'utf-8'))
            if anexo_bytes is not None and not usar_inline:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(anexo_bytes)
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{anexo_nome or "anexo"}"')
                msg.attach(part)

        server = smtplib.SMTP(smtp_cfg['host'], int(smtp_cfg['port']))
        server.starttls()
        server.login(smtp_cfg['user'], smtp_cfg['pass'])
        server.sendmail(smtp_cfg['user'], to, msg.as_string())
        server.quit()
        return True, "OK"
    except Exception as e:
        return False, str(e)


def _disparo_email_worker(job_id: str, contacts: list, assunto: str, corpo: str,
                           smtp_cfg: dict, delay_min: int, delay_max: int,
                           anexo_bytes=None, anexo_nome=None, anexo_mime=None,
                           agendamento_ts: int = None, base_url: str = ''):
    """Thread worker para disparo de e-mails em lote, com agendamento e tracking."""
    job     = email_jobs[job_id]
    history = load_email_history()
    total   = len(contacts)
    job['total'] = total

    # ── Agendamento: aguarda até o horário definido ──────────────────────────
    if agendamento_ts and agendamento_ts > int(time.time()):
        job['status'] = 'agendado'
        job['log'].append({'type': 'info', 'text': f'⏳ Aguardando horário agendado: {time.strftime("%d/%m/%Y %H:%M", time.localtime(agendamento_ts))}'})
        while int(time.time()) < agendamento_ts:
            if job.get('cancel'):
                job['status'] = 'cancelled'
                return
            time.sleep(5)

    job['status'] = 'running'

    for i, contact in enumerate(contacts):
        if job.get('cancel'):
            job['status'] = 'cancelled'
            break

        nome    = str(contact.get('nome', '') or contact.get('empresa', '') or 'Prezado(a)')
        empresa = str(contact.get('empresa', '') or '')
        email   = str(contact.get('email', '')).strip()

        if not email or '@' not in email:
            job['log'].append({'type': 'warning', 'text': f'Sem e-mail válido: {nome}'})
            continue

        assunto_final = assunto.replace('{nome}', nome).replace('{empresa}', empresa)
        corpo_final   = corpo.replace('{nome}', nome).replace('{empresa}', empresa)

        # Gera ID único para este envio e injeta tracking
        history_id = str(uuid.uuid4())[:12]
        if base_url:
            corpo_final = _inject_tracking(corpo_final, history_id, base_url)

        ok, msg = _enviar_email_smtp(smtp_cfg, email, assunto_final, corpo_final,
                                     anexo_bytes, anexo_nome, anexo_mime)

        ts = int(time.time())
        if ok:
            job['sent']   += 1
            job['log'].append({'type': 'success', 'text': f'✓ {email}'})
            history.append({'id': history_id, 'ts': ts, 'to': email,
                            'nome': nome, 'assunto': assunto_final, 'status': 'enviado',
                            'opened': False, 'clicked': False})
            # Atualiza status do contato no CRM
            try:
                contacts_all = load_contacts()
                for c in contacts_all:
                    if c.get('id') == contact.get('id'):
                        c['status'] = 'em_contato'
                        break
                save_contacts(contacts_all)
            except Exception:
                pass
        else:
            job['errors'] += 1
            job['log'].append({'type': 'error', 'text': f'✗ {email}: {msg}'})
            history.append({'id': history_id, 'ts': ts, 'to': email,
                            'nome': nome, 'assunto': assunto_final, 'status': f'erro: {msg[:60]}',
                            'opened': False, 'clicked': False})

        job['progress'] = round(((i + 1) / total) * 100)
        save_email_history(history)

        # Delay entre envios (exceto no último)
        if i < total - 1 and not job.get('cancel'):
            delay = random.randint(delay_min, delay_max)
            job['log'].append({'type': 'info', 'text': f'Aguardando {delay}s...'})
            for _ in range(delay):
                if job.get('cancel'):
                    break
                time.sleep(1)

    if job['status'] == 'running':
        job['status']   = 'done'
        job['progress'] = 100
        job['log'].append({'type': 'success',
                           'text': f'Disparo concluído — {job["sent"]} enviados, {job["errors"]} erros.'})


# ─────────────────────────────────────────────
# E-MAIL — ROTAS DA API
# ─────────────────────────────────────────────

@app.route('/api/email/smtp-config', methods=['GET'])
def email_get_smtp():
    cfg = load_smtp_config()
    # Não retorna a senha por segurança; retorna apenas host/port/user
    safe = {k: v for k, v in cfg.items() if k != 'pass'}
    safe['has_pass'] = bool(cfg.get('pass'))
    return jsonify({"success": True, "config": safe})


@app.route('/api/email/smtp-config', methods=['POST'])
@limiter.limit("20 per minute")
def email_save_smtp():
    data = request.json or {}
    cfg  = load_smtp_config()
    cfg['host'] = data.get('host', cfg.get('host', 'smtp.gmail.com')).strip()
    cfg['port'] = int(data.get('port', cfg.get('port', 587)))
    cfg['user'] = data.get('user', cfg.get('user', '')).strip()
    if data.get('pass'):
        cfg['pass'] = data['pass']
    ok = save_smtp_config(cfg)
    return jsonify({"success": ok, "message": "Configuração salva." if ok else "Erro ao salvar."})


@app.route('/api/email/test-smtp', methods=['POST'])
@limiter.limit("5 per minute")
def email_test_smtp():
    data = request.json or {}
    cfg  = {
        'host': data.get('host', '').strip(),
        'port': int(data.get('port', 587)),
        'user': data.get('user', '').strip(),
        'pass': data.get('pass', '').strip(),
    }
    if not cfg['host'] or not cfg['user'] or not cfg['pass']:
        return jsonify({"success": False, "error": "Preencha host, usuário e senha."}), 400
    try:
        server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=10)
        server.starttls()
        server.login(cfg['user'], cfg['pass'])
        server.quit()
        return jsonify({"success": True, "message": f"Conexão com {cfg['host']} bem-sucedida!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route('/track/open/<history_id>', methods=['GET'])
def email_track_open(history_id):
    """Pixel de rastreamento de abertura — retorna GIF 1x1 transparente."""
    try:
        history = load_email_history()
        for entry in history:
            if entry.get('id') == history_id:
                entry['opened'] = True
                entry['opened_at'] = int(time.time())
                break
        save_email_history(history)
    except Exception as e:
        print(f"[Track] Erro ao registrar abertura {history_id}: {e}")
    # GIF 1x1 transparente
    gif = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00'
           b'!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01'
           b'\x00\x00\x02\x02D\x01\x00;')
    return Response(gif, mimetype='image/gif',
                    headers={'Cache-Control': 'no-store, no-cache, must-revalidate',
                             'Pragma': 'no-cache'})


@app.route('/track/click/<history_id>', methods=['GET'])
def email_track_click(history_id):
    """Rastreamento de clique — registra e redireciona para a URL original."""
    dest = request.args.get('url', '/')
    try:
        history = load_email_history()
        for entry in history:
            if entry.get('id') == history_id:
                entry['clicked'] = True
                entry['clicked_at'] = int(time.time())
                break
        save_email_history(history)
    except Exception as e:
        print(f"[Track] Erro ao registrar clique {history_id}: {e}")
    return redirect(dest)


def _inject_tracking(body: str, history_id: str, base_url: str) -> str:
    """Injeta pixel de abertura e reescreve links para rastreamento de cliques."""
    # Pixel de abertura — insere antes do </body> ou no final
    pixel = f'<img src="{base_url}/track/open/{history_id}" width="1" height="1" style="display:none;" alt="">'
    if '</body>' in body.lower():
        body = re.sub(r'</body>', pixel + '</body>', body, flags=re.IGNORECASE)
    else:
        body = body + pixel

    # Reescreve hrefs para passar pelo /track/click
    def rewrite_link(m):
        href = m.group(1)
        if href.startswith('#') or href.startswith('mailto:') or '/track/' in href:
            return m.group(0)
        encoded = urllib.parse.quote(href, safe='')
        return f'href="{base_url}/track/click/{history_id}?url={encoded}"'

    body = re.sub(r'href="([^"]+)"', rewrite_link, body)
    return body


@app.route('/api/email/send-batch', methods=['POST'])
@limiter.limit("10 per minute")
def email_send_batch():
    # Pega dados do form (multipart por causa do anexo)
    assunto      = (request.form.get('assunto') or '').strip()
    corpo        = (request.form.get('corpo') or '').strip()
    # AGORA RECEBEMOS OS DADOS COMPLETOS DO CONTATO (NÃO SÓ O ID)
    targets_json = request.form.get('targets')
    delay_min    = int(request.form.get('delay_min') or 30)
    delay_max    = int(request.form.get('delay_max') or 90)
    agendamento  = request.form.get('agendamento')   # timestamp Unix (str) ou None
    arte_id      = (request.form.get('arte_id') or '').strip()

    if not assunto:
        return jsonify({"success": False, "error": "Assunto é obrigatório."}), 400
    if not corpo:
        return jsonify({"success": False, "error": "Corpo do e-mail é obrigatório."}), 400
    if not targets_json:
        return jsonify({"success": False, "error": "Selecione ao menos um contato."}), 400

    # Tenta pegar SMTP do Directus/local; aceita também os dados enviados pelo front como fallback
    smtp_cfg = load_smtp_config()
    if not (smtp_cfg.get('host') and smtp_cfg.get('user') and smtp_cfg.get('pass')):
        # fallback: front pode enviar smtp_* no FormData
        smtp_host = (request.form.get('smtp_host') or '').strip()
        smtp_user = (request.form.get('smtp_user') or '').strip()
        smtp_pass = (request.form.get('smtp_pass') or '').strip()
        smtp_port = int(request.form.get('smtp_port') or 587)
        if smtp_host and smtp_user and smtp_pass:
            smtp_cfg = {'host': smtp_host, 'port': smtp_port, 'user': smtp_user, 'pass': smtp_pass}
        else:
            return jsonify({"success": False, "error": "Configure o SMTP antes de disparar (aba Configurações)."}), 400

    try:
        contacts = json.loads(targets_json)
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "Erro ao ler a lista de destinatários."}), 400

    if not contacts:
        return jsonify({"success": False, "error": "Nenhum contato válido selecionado."}), 400

    # Lida com anexo opcional
    anexo_bytes = None
    anexo_nome  = None
    anexo_mime  = None
    if 'anexo' in request.files:
        f = request.files['anexo']
        if f and f.filename:
            anexo_bytes = f.read()
            anexo_nome  = secure_filename(f.filename)
            anexo_mime  = f.content_type or 'application/octet-stream'

    # Resolve timestamp de agendamento
    agendamento_ts = None
    if agendamento:
        try:
            agendamento_ts = int(agendamento)
        except (ValueError, TypeError):
            pass

    job_id = str(uuid.uuid4())[:12]
    job_status_inicial = 'agendado' if agendamento_ts and agendamento_ts > int(time.time()) else 'starting'
    email_jobs[job_id] = {
        'status':        job_status_inicial,
        'progress':      0,
        'total':         len(contacts),
        'sent':          0,
        'errors':        0,
        'log':           [],
        'cancel':        False,
        'created':       int(time.time()),
        'agendamento_ts': agendamento_ts,
    }

    # Determina URL base para tracking (usa REQUEST_HOST ou HOST padrão)
    base_url = os.environ.get("APP_BASE_URL", "").rstrip('/')
    if not base_url:
        base_url = request.host_url.rstrip('/')

    t = threading.Thread(
        target=_disparo_email_worker,
        args=(job_id, contacts, assunto, corpo, smtp_cfg, delay_min, delay_max,
              anexo_bytes, anexo_nome, anexo_mime, agendamento_ts, base_url),
        daemon=True
    )
    t.start()

    return jsonify({"success": True, "job_id": job_id, "total": len(contacts)})

@app.route('/api/email/job-status/<job_id>', methods=['GET'])
def email_job_status(job_id):
    job = email_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job não encontrado."}), 404
    # Retorna apenas os últimos 50 logs para não sobrecarregar
    return jsonify({
        "success":        True,
        "status":         job['status'],
        "progress":       job['progress'],
        "total":          job['total'],
        "sent":           job['sent'],
        "errors":         job['errors'],
        "log":            job['log'][-50:],
        "agendamento_ts": job.get('agendamento_ts'),
    })


@app.route('/api/email/job-cancel/<job_id>', methods=['POST'])
def email_job_cancel(job_id):
    job = email_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job não encontrado."}), 404
    job['cancel'] = True
    return jsonify({"success": True, "message": "Cancelamento solicitado."})


@app.route('/api/email/history', methods=['GET'])
def email_history():
    history = load_email_history()
    return jsonify({"success": True, "history": list(reversed(history)), "total": len(history)})


@app.route('/api/email/history', methods=['DELETE'])
def email_clear_history():
    save_email_history([])
    return jsonify({"success": True, "message": "Histórico limpo."})


@app.route('/api/email/generate-copy', methods=['POST'])
@limiter.limit("20 per minute")
def email_generate_copy():
    if not groq_client:
        return jsonify({"success": False, "error": "GROQ_API_KEY não configurada."}), 400

    data      = request.json or {}
    nicho     = data.get('nicho', 'negócios B2B').strip()
    empresa   = data.get('empresa', '').strip()
    descricao = data.get('descricao', '').strip()

    contexto = ''
    if empresa:   contexto += f' Minha empresa é {empresa}.'
    if descricao: contexto += f' O que vendemos: {descricao}.'

    prompt = (
        f"Você é um copywriter especialista em e-mail marketing B2B. "
        f"Crie um e-mail de prospecção fria para o nicho: {nicho}.{contexto} "
        f"Retorne um JSON com os campos 'assunto' (máx 60 chars, direto e curioso) "
        f"e 'corpo' (HTML simples, máx 3 parágrafos curtos, tom pessoal e direto, "
        f"use {{nome}} para personalizar). Sem markdown, apenas JSON puro."
    )

    try:
        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Retorne APENAS JSON válido, sem markdown, sem explicações."},
                {"role": "user", "content": prompt}
            ],
            model=BEST_FREE_MODEL,
            temperature=0.7,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        raw  = response.choices[0].message.content.strip()
        data = json.loads(raw)
        return jsonify({"success": True, "assunto": data.get('assunto', ''), "corpo": data.get('corpo', '')})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



# ─────────────────────────────────────────────
# CALENDÁRIO
# ─────────────────────────────────────────────
@app.route('/calendario')
def calendario():
    return render_template('calendario.html',
        directus_url=DIRECTUS_URL,
        directus_token=DIRECTUS_TOKEN,
        directus_table=DIRECTUS_TABLE,
    )


# ─────────────────────────────────────────────
# GOALS / PROGRESSO
# ─────────────────────────────────────────────
@app.route('/api/goals/progresso', methods=['GET'])
def goals_progresso():
    tipo   = request.args.get('tipo', '').strip()
    mes    = request.args.get('mes', '').strip()

    if not tipo or not mes:
        return jsonify({"success": False, "error": "Parâmetros 'tipo' e 'mes' são obrigatórios."}), 400

    try:
        ano_str, mes_str = mes.split('-')
        ano = int(ano_str)
        num_mes = int(mes_str)
    except Exception:
        return jsonify({"success": False, "error": "Formato de mês inválido. Use YYYY-MM."}), 400

    hdrs = {"Authorization": f"Bearer {DIRECTUS_TOKEN}", "Content-Type": "application/json"}
    valor = 0

    try:
        if tipo == 'posts':
            data_inicio = f"{ano}-{mes_str:>02}-01"
            data_fim    = f"{ano}-{mes_str:>02}-31"
            url = (
                f"{DIRECTUS_URL}/items/content_cards"
                f"?filter[workspace_id][_eq]={DIRECTUS_TABLE}"
                f"&filter[status_kanban][_in]=pronto,publicado"
                f"&filter[data_publicacao][_between]={data_inicio},{data_fim}"
                f"&aggregate[count]=id"
            )
            res  = requests.get(url, headers=hdrs, timeout=10)
            data = res.json()
            valor = data.get('data', [{}])[0].get('count', {}).get('id', 0) or 0

        elif tipo == 'disparos':
            url = (
                f"{DIRECTUS_URL}/items/campaigns"
                f"?filter[workspace_id][_eq]={DIRECTUS_TABLE}"
                f"&aggregate[sum]=enviados"
            )
            try:
                res  = requests.get(url, headers=hdrs, timeout=10)
                data = res.json()
                directus_enviados = int(data.get('data', [{}])[0].get('sum', {}).get('enviados', 0) or 0)
            except Exception:
                directus_enviados = 0

            url_email = f"{DIRECTUS_URL}/items/email_history?filter[workspace_id][_eq]={DIRECTUS_TABLE}&limit=-1"
            try:
                res_e = requests.get(url_email, headers=hdrs, timeout=10)
                data_e = res_e.json().get('data', [])
                email_enviados = sum(1 for h in data_e if h.get('status', '').startswith('enviado'))
            except Exception:
                email_enviados = 0

            valor = email_enviados + directus_enviados

        elif tipo == 'leads':
            url = f"{DIRECTUS_URL}/items/contacts?filter[workspace_id][_eq]={DIRECTUS_TABLE}&aggregate[count]=id"
            try:
                res  = requests.get(url, headers=hdrs, timeout=10)
                data = res.json()
                valor = int(data.get('data', [{}])[0].get('count', {}).get('id', 0) or 0)
            except Exception:
                valor = 0

        else:
            valor = 0

    except Exception as e:
        print(f"[goals/progresso] Erro ao calcular '{tipo}': {e}")
        valor = 0

    return jsonify({"success": True, "tipo": tipo, "mes": mes, "valor": valor})


@app.route('/api/goals/relatorio', methods=['GET'])
def goals_relatorio():
    mes = request.args.get('mes', datetime.datetime.now().strftime('%Y-%m'))
    if not weasyprint:
        return jsonify({"success": False, "error": "WeasyPrint não instalado no servidor."}), 501
    try:
        html = f"<html><head><style>body{{font-family:sans-serif;}} h1{{color:#2563eb;}}</style></head><body><h1>Relatório {mes}</h1><p>Gerado automaticamente.</p></body></html>"
        pdf_bytes = weasyprint.HTML(string=html).write_pdf()
        return Response(pdf_bytes, mimetype='application/pdf', headers={'Content-Disposition': f'attachment; filename=relatorio_{mes}.pdf'})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# MÉTRICAS — GOOGLE OAUTH2 + GSC + GA4
# ─────────────────────────────────────────────
SCOPES_ALL = [
    'https://www.googleapis.com/auth/webmasters.readonly',
    'https://www.googleapis.com/auth/analytics.readonly'
]
TOKENS_FILE = os.path.join(BASE_DIR, 'data', 'metricas_tokens.json')


def _load_tokens():
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_tokens(data: dict):
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, 'w') as f:
        json.dump(data, f)


def _get_creds(workspace_id: str):
    tokens = _load_tokens()
    tok = tokens.get(workspace_id)
    if not tok:
        return None
    creds = Credentials(
        token         = tok.get('token'),
        refresh_token = tok.get('refresh_token'),
        token_uri     = 'https://oauth2.googleapis.com/token',
        client_id     = GOOGLE_CLIENT_ID,
        client_secret = GOOGLE_CLIENT_SECRET,
        scopes        = SCOPES_ALL,
    )
    if creds.expired and creds.refresh_token:
        try:
            from google.auth.transport.requests import Request as GRequest
            creds.refresh(GRequest())
            tok['token'] = creds.token
            tokens[workspace_id] = tok
            _save_tokens(tokens)
        except Exception as e:
            print(f"[metricas] Falha ao renovar token: {e}")
            return None
    return creds


@app.route('/metricas')
def metricas():
    return render_template('metricas.html',
        directus_url   = DIRECTUS_URL,
        directus_token = DIRECTUS_TOKEN,
        directus_table = DIRECTUS_TABLE,
    )


@app.route('/api/metricas/status', methods=['GET'])
def metricas_status():
    workspace_id = DIRECTUS_TABLE
    creds = _get_creds(workspace_id)
    tokens = _load_tokens()
    tok = tokens.get(workspace_id, {})
    return jsonify({
        "success"       : True,
        "gsc_conectado" : creds is not None and creds.valid,
        "ga4_conectado" : creds is not None and creds.valid and bool(GA4_PROPERTY_ID),
        "gsc_site"      : tok.get('gsc_site', ''),
        "ga4_property"  : GA4_PROPERTY_ID,
    })


@app.route('/api/metricas/oauth/start')
def metricas_oauth_start():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "Erro: GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET não configurados.", 400

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id"    : GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri"     : "https://accounts.google.com/o/oauth2/auth",
                "token_uri"    : "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=SCOPES_ALL,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
    )
    session['oauth_state'] = state
    # NOVA LINHA: Salva o código de verificação para usar na volta
    session['code_verifier'] = getattr(flow, 'code_verifier', None) 
    
    return redirect(auth_url)


@app.route('/api/metricas/oauth/callback')
def metricas_oauth_callback():
    state = session.get('oauth_state', '')
    code_verifier = session.get('code_verifier') # <--- RECUPERA AQUI

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "Erro de configuração OAuth.", 400

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id"    : GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri"     : "https://accounts.google.com/o/oauth2/auth",
                "token_uri"    : "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=SCOPES_ALL,
        state=state,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )

    try:
        # Corrige o http para https que fizemos antes
        auth_response = request.url.replace('http://', 'https://')
        
        # Cria os argumentos para enviar ao Google
        fetch_kwargs = {'authorization_response': auth_response}
        if code_verifier:
            fetch_kwargs['code_verifier'] = code_verifier
            
        # Pega o token final enviando o verifier junto
        flow.fetch_token(**fetch_kwargs)
        creds = flow.credentials

        gsc_site = ""
        try:
            svc = build('searchconsole', 'v1', credentials=creds, cache_discovery=False)
            sites = svc.sites().list().execute()
            entries = sites.get('siteEntry', [])
            if entries:
                gsc_site = entries[0].get('siteUrl', '')
        except Exception:
            pass

        workspace_id = DIRECTUS_TABLE
        tokens = _load_tokens()
        tokens[workspace_id] = {
            'token'        : creds.token,
            'refresh_token': creds.refresh_token,
            'gsc_site'     : gsc_site,
        }
        _save_tokens(tokens)
        return redirect('/metricas?conectado=1')

    except Exception as e:
        return f"Erro no callback OAuth: {e}", 400


@app.route('/api/metricas/gsc', methods=['GET'])
def metricas_gsc():
    dias = int(request.args.get('dias', 28))
    workspace_id = DIRECTUS_TABLE
    creds = _get_creds(workspace_id)

    if not creds or not creds.valid:
        return jsonify({"success": False, "error": "Search Console não conectado."}), 401

    tokens = _load_tokens()
    gsc_site = tokens.get(workspace_id, {}).get('gsc_site', '')
    if not gsc_site:
        return jsonify({"success": False, "error": "Nenhum site encontrado no Search Console."}), 400

    try:
        svc = build('searchconsole', 'v1', credentials=creds, cache_discovery=False)
        end   = datetime.date.today() - datetime.timedelta(days=3)
        start = end - datetime.timedelta(days=dias)

        resp_dia = svc.searchanalytics().query(siteUrl=gsc_site, body={
            'startDate' : start.strftime('%Y-%m-%d'),
            'endDate'   : end.strftime('%Y-%m-%d'),
            'dimensions': ['date'],
            'rowLimit'  : 90,
        }).execute()

        por_dia = []
        total_cliques = total_impressoes = total_ctr_sum = total_pos_sum = 0
        rows_dia = resp_dia.get('rows', [])
        for row in rows_dia:
            cl = row.get('clicks', 0)
            im = row.get('impressions', 0)
            ct = round(row.get('ctr', 0) * 100, 2)
            po = round(row.get('position', 0), 1)
            por_dia.append({'data': row['keys'][0], 'cliques': cl, 'impressoes': im, 'ctr': ct, 'posicao': po})
            total_cliques    += cl
            total_impressoes += im
            total_ctr_sum    += ct
            total_pos_sum    += po

        n = len(rows_dia) or 1
        totais = {
            'cliques'   : total_cliques,
            'impressoes': total_impressoes,
            'ctr'       : round(total_ctr_sum / n, 1),
            'posicao'   : round(total_pos_sum / n, 1),
        }

        resp_pg = svc.searchanalytics().query(siteUrl=gsc_site, body={
            'startDate' : start.strftime('%Y-%m-%d'),
            'endDate'   : end.strftime('%Y-%m-%d'),
            'dimensions': ['page'],
            'rowLimit'  : 10,
            'orderBy'   : [{'fieldName': 'clicks', 'sortOrder': 'DESCENDING'}],
        }).execute()

        top_paginas = [{
            'pagina'    : r['keys'][0],
            'cliques'   : r.get('clicks', 0),
            'impressoes': r.get('impressions', 0),
            'ctr'       : round(r.get('ctr', 0) * 100, 2),
            'posicao'   : round(r.get('position', 0), 1),
        } for r in resp_pg.get('rows', [])]

        resp_kw = svc.searchanalytics().query(siteUrl=gsc_site, body={
            'startDate' : start.strftime('%Y-%m-%d'),
            'endDate'   : end.strftime('%Y-%m-%d'),
            'dimensions': ['query'],
            'rowLimit'  : 10,
            'orderBy'   : [{'fieldName': 'clicks', 'sortOrder': 'DESCENDING'}],
        }).execute()

        top_keywords = [{
            'query'     : r['keys'][0],
            'cliques'   : r.get('clicks', 0),
            'impressoes': r.get('impressions', 0),
            'ctr'       : round(r.get('ctr', 0) * 100, 2),
            'posicao'   : round(r.get('position', 0), 1),
        } for r in resp_kw.get('rows', [])]

        return jsonify({
            "success"     : True,
            "totais"      : totais,
            "por_dia"     : por_dia,
            "top_paginas" : top_paginas,
            "top_keywords": top_keywords,
            "site"        : gsc_site,
            "periodo_dias": dias,
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/metricas/ga4', methods=['GET'])
def metricas_ga4():
    dias = int(request.args.get('dias', 28))
    workspace_id = DIRECTUS_TABLE
    creds = _get_creds(workspace_id)

    if not creds or not creds.valid:
        return jsonify({"success": False, "error": "Google Analytics não conectado."}), 401
    if not GA4_PROPERTY_ID:
        return jsonify({"success": False, "error": "GA4_PROPERTY_ID não configurado no .env"}), 400

    try:
        svc = build('analyticsdata', 'v1beta', credentials=creds, cache_discovery=False)
        end   = datetime.date.today() - datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=dias)

        resp_dia = svc.properties().runReport(property=GA4_PROPERTY_ID, body={
            'dateRanges': [{'startDate': start.strftime('%Y-%m-%d'), 'endDate': end.strftime('%Y-%m-%d')}],
            'dimensions': [{'name': 'date'}],
            'metrics'   : [{'name': 'sessions'}, {'name': 'activeUsers'}, {'name': 'screenPageViews'}],
            'orderBys'  : [{'dimension': {'dimensionName': 'date'}}],
        }).execute()

        por_dia = []
        for row in resp_dia.get('rows', []):
            dt = row['dimensionValues'][0]['value']
            dt_fmt = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
            por_dia.append({
                'data'     : dt_fmt,
                'sessoes'  : int(row['metricValues'][0]['value']),
                'usuarios' : int(row['metricValues'][1]['value']),
                'pageviews': int(row['metricValues'][2]['value']),
            })

        resp_tot = svc.properties().runReport(property=GA4_PROPERTY_ID, body={
            'dateRanges': [{'startDate': start.strftime('%Y-%m-%d'), 'endDate': end.strftime('%Y-%m-%d')}],
            'metrics'   : [
                {'name': 'sessions'},
                {'name': 'activeUsers'},
                {'name': 'screenPageViews'},
                {'name': 'averageSessionDuration'},
            ],
        }).execute()

        totais = {'sessoes': 0, 'usuarios': 0, 'pageviews': 0, 'tempo_medio': 0}
        if resp_tot.get('rows'):
            mv = resp_tot['rows'][0]['metricValues']
            totais = {
                'sessoes'    : int(mv[0]['value']),
                'usuarios'   : int(mv[1]['value']),
                'pageviews'  : int(mv[2]['value']),
                'tempo_medio': round(float(mv[3]['value']), 0),
            }

        resp_cn = svc.properties().runReport(property=GA4_PROPERTY_ID, body={
            'dateRanges': [{'startDate': start.strftime('%Y-%m-%d'), 'endDate': end.strftime('%Y-%m-%d')}],
            'dimensions': [{'name': 'sessionDefaultChannelGroup'}],
            'metrics'   : [{'name': 'sessions'}, {'name': 'activeUsers'}],
            'orderBys'  : [{'metric': {'metricName': 'sessions'}, 'desc': True}],
            'limit'     : 8,
        }).execute()

        canais = [{
            'canal'   : r['dimensionValues'][0]['value'],
            'sessoes' : int(r['metricValues'][0]['value']),
            'usuarios': int(r['metricValues'][1]['value']),
        } for r in resp_cn.get('rows', [])]

        resp_pg = svc.properties().runReport(property=GA4_PROPERTY_ID, body={
            'dateRanges': [{'startDate': start.strftime('%Y-%m-%d'), 'endDate': end.strftime('%Y-%m-%d')}],
            'dimensions': [{'name': 'pagePath'}],
            'metrics'   : [{'name': 'screenPageViews'}, {'name': 'activeUsers'}],
            'orderBys'  : [{'metric': {'metricName': 'screenPageViews'}, 'desc': True}],
            'limit'     : 10,
        }).execute()

        top_paginas = [{
            'pagina'   : r['dimensionValues'][0]['value'],
            'pageviews': int(r['metricValues'][0]['value']),
            'usuarios' : int(r['metricValues'][1]['value']),
        } for r in resp_pg.get('rows', [])]

        return jsonify({
            "success"     : True,
            "totais"      : totais,
            "por_dia"     : por_dia,
            "canais"      : canais,
            "top_paginas" : top_paginas,
            "periodo_dias": dias,
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/metricas/sync', methods=['POST'])
@limiter.limit("10 per minute")
def metricas_sync():
    workspace_id = DIRECTUS_TABLE
    creds = _get_creds(workspace_id)
    if not creds or not creds.valid:
        return jsonify({"success": False, "error": "Nenhuma conexão ativa. Conecte o Google primeiro."}), 401
    return jsonify({"success": True, "message": "Dados atualizados com sucesso."})


@app.route('/api/metricas/ia-analise', methods=['POST'])
@limiter.limit("10 per minute")
def metricas_ia_analise():
    if not groq_client:
        return jsonify({"success": False, "error": "GROQ_API_KEY não configurada."}), 400

    data    = request.json or {}
    gsc     = data.get('gsc', {})
    ga4     = data.get('ga4', {})
    periodo = data.get('periodo', 28)

    partes = []
    if gsc:
        partes.append(
            f"GSC ({periodo} dias): {gsc.get('cliques',0)} cliques, "
            f"{gsc.get('impressoes',0)} impressões, CTR {gsc.get('ctr',0)}%, "
            f"posição média {gsc.get('posicao',0)}."
        )
    if ga4:
        seg = ga4.get('tempo_medio', 0)
        partes.append(
            f"GA4 ({periodo} dias): {ga4.get('sessoes',0)} sessões, "
            f"{ga4.get('usuarios',0)} usuários, {ga4.get('pageviews',0)} visualizações, "
            f"tempo médio {int(seg//60)}m{int(seg%60)}s."
        )

    if not partes:
        return jsonify({"success": False, "error": "Sem dados para analisar."}), 400

    prompt = (
        "Você é um especialista em marketing digital e SEO. "
        "Analise os dados abaixo e dê um diagnóstico direto e acionável em 3-4 frases. "
        "Aponte o ponto mais crítico, o que está bom e 1 ação concreta para melhorar. "
        "Seja objetivo, sem jargões desnecessários.\n\n"
        "Dados: " + " ".join(partes)
    )

    try:
        resp = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Você é um analista de marketing digital direto e prático. Responda em português do Brasil."},
                {"role": "user", "content": prompt}
            ],
            model=BEST_FREE_MODEL,
            temperature=0.5,
            max_tokens=300,
        )
        analise = resp.choices[0].message.content.strip()
        return jsonify({"success": True, "analise": analise})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# INICIALIZAÇÃO
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"🚀 Motor Dark Studio — rodando na porta {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
