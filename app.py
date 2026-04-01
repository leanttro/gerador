import os
import json
import time
import requests
from flask import Flask, request, jsonify, render_template
from groq import Groq
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

def generate_script_and_prompts(niche):
    prompt = f"Crie um roteiro curto de 30 segundos sobre {niche}. Retorne estritamente um JSON com a chave 'script' contendo o texto falado e a chave 'prompts' contendo uma lista de 4 prompts em ingles detalhados para gerar imagens em IA."
    
    response = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama3-70b-8192",
        response_format={"type": "json_object"}
    )
    
    return json.loads(response.choices[0].message.content)

def generate_audio(text, filename="output_audio.mp3"):
    safe_text = text.replace('"', '').replace("'", "")
    command = f'edge-tts --text "{safe_text}" --voice pt-BR-AntonioNeural --write-media {filename}'
    os.system(command)
    return filename

def generate_images_leonardo(prompts):
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {LEONARDO_API_KEY}"
    }
    
    downloaded_files = []
    
    for index, prompt in enumerate(prompts):
        payload = {
            "height": 1024,
            "width": 576,
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
            
        downloaded_files.append(img_filename)
        
    return downloaded_files

def compile_video(audio_file, image_files, output_filename="final_video.mp4"):
    audio = AudioFileClip(audio_file)
    duration_per_image = audio.duration / len(image_files)
    
    clips = []
    for img in image_files:
        clip = ImageClip(img).set_duration(duration_per_image)
        clips.append(clip)
        
    video = concatenate_videoclips(clips, method="compose")
    video = video.set_audio(audio)
    video.write_videofile(output_filename, fps=24, codec="libx264", audio_codec="aac")
    
    return output_filename

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate_video():
    data = request.json
    niche = data.get('niche')
    
    if not niche:
        return jsonify({"success": False, "error": "Nicho ausente"}), 400
        
    try:
        content = generate_script_and_prompts(niche)
        script = content.get("script")
        prompts = content.get("prompts")
        
        audio_path = generate_audio(script)
        
        image_paths = generate_images_leonardo(prompts)
        
        video_path = compile_video(audio_path, image_paths)
        
        return jsonify({"success": True, "file": video_path})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
