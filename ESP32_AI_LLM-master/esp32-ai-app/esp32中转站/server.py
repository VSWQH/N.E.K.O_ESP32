"""ESP32中转站 — 音频中继+Web配置"""
import asyncio, json, base64, ssl, sys, websockets, struct, os
if sys.platform=='win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

CFG_FILE = os.path.join(os.path.dirname(__file__),"config.json")
CFG = json.load(open(CFG_FILE))
KEY = CFG["api_key"]
URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
MODEL = CFG.get("model","qwen3.5-omni-flash-realtime")
VOICE = CFG.get("voice","Tina")
PORT = 8765
CFG_PORT = 8766

def downsample_24k_to_16k(data):
    samples = struct.unpack(f'<{len(data)//2}h', data)
    out = [samples[i] for i in range(len(samples)) if i%3!=2]
    return struct.pack(f'<{len(out)}h', *out)

def ssl_ctx():
    c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE
    return c

async def session(esp32):
    omni = await websockets.connect(URL+f"?model={MODEL}",
        extra_headers={"Authorization":f"Bearer {KEY}"}, ssl=ssl_ctx(), ping_interval=20)
    await omni.send(json.dumps({"type":"session.update","session":{
        "modalities":["text","audio"], "voice":VOICE,
        "input_audio_format":"pcm", "output_audio_format":"pcm",
        "instructions":CFG.get("instructions","你是一个语音助手，回答简短自然。"),
        "turn_detection":{"type":"server_vad","threshold":0.6,"silence_duration_ms":800,"prefix_padding_ms":500}
    }}))
    for _ in range(2): json.loads(await asyncio.wait_for(omni.recv(),timeout=10))
    print("[Omni]就绪")

    audio_queue = asyncio.Queue(maxsize=100)  # 生产者消费者队列
    wav_buf = bytearray()  # 仅用于保存WAV

    # ESP32→Omni
    async def from_esp32():
        cnt=0
        while True:
            try: msg=await asyncio.wait_for(esp32.recv(),timeout=0.5)
            except asyncio.TimeoutError: continue
            except websockets.ConnectionClosed: break
            if isinstance(msg,bytes):
                await omni.send(json.dumps({"type":"input_audio_buffer.append",
                    "audio":base64.b64encode(msg).decode()}))
                cnt+=1
                if cnt%50==1: print(f"[→Omni]×{cnt}")

    # Omni→队列(生产者)
    async def from_omni():
        nonlocal wav_buf
        while True:
            try: raw=await asyncio.wait_for(omni.recv(),timeout=0.5)
            except asyncio.TimeoutError: continue
            except websockets.ConnectionClosed: break
            data=json.loads(raw); t=data.get("type","")

            # 打断：取消+清队列+通知ESP32
            if t=="input_audio_buffer.speech_started":
                await omni.send(json.dumps({"type":"response.cancel"}))
                await esp32.send(b"CLEAR")
                while not audio_queue.empty():
                    try: audio_queue.get_nowait()
                    except asyncio.QueueEmpty: break
                wav_buf.clear()
                print("[打断]")

            # 文字转发
            if "transcript" in t or t in ("response.done","input_audio_buffer.speech_started",
                    "input_audio_buffer.speech_stopped","error"):
                await esp32.send(json.dumps({"type":t,"data":data}))
                if t=="conversation.item.input_audio_transcription.completed":
                    print(f"[识别] {data.get('transcript','')}")
                elif t=="response.audio_transcript.delta":
                    print(f"[LLM] {data.get('delta','')}", end='', flush=True)
                elif t=="response.audio_transcript.done":
                    print(f"\n[LLM完成] {data.get('transcript','')}")

            # 音频：只收集，不发
            elif t=="response.audio.delta":
                wav_buf.extend(base64.b64decode(data.get("delta","")))

            elif t=="response.audio.done":
                pcm = downsample_24k_to_16k(wav_buf)
                print(f"[→ESP] {len(pcm)}字节")
                await esp32.send(b"STOP_REC")
                for i in range(0, len(pcm), 4096):
                    await esp32.send(pcm[i:i+4096])
                    await asyncio.sleep(0.12)
                await esp32.send(b"START_REC")
                wav_buf.clear()

    await asyncio.gather(from_esp32(), from_omni())

async def handle_http(reader, writer):
    try:
        data = await asyncio.wait_for(reader.read(4096), timeout=5)
        req = data.decode()
        if req.startswith("GET /config "):
            body = json.dumps(CFG, ensure_ascii=False)
            resp = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(body)}\r\n\r\n{body}"
        elif req.startswith("POST /save "):
            body_start = req.find("\r\n\r\n") + 4
            new_cfg = json.loads(req[body_start:])
            CFG.update(new_cfg)
            json.dump(CFG, open(CFG_FILE,'w'), ensure_ascii=False, indent=2)
            resp = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 2\r\n\r\nOK"
        else:
            html = open(os.path.join(os.path.dirname(__file__),"config.html"),'rb').read()
            resp = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(html)}\r\n\r\n"
            writer.write(resp.encode()[:len(resp)]); await writer.drain()
            writer.write(html)
            await writer.drain(); writer.close(); return
        writer.write(resp.encode()); await writer.drain(); writer.close()
    except: pass

async def http_server():
    srv = await asyncio.start_server(handle_http, "0.0.0.0", CFG_PORT)
    print(f"🔧 配置页: http://localhost:{CFG_PORT}")
    async with srv: await srv.serve_forever()

async def main():
    print(f"🚀 中继 ws://0.0.0.0:{PORT}")
    await asyncio.gather(
        websockets.serve(session,"0.0.0.0",PORT,ping_interval=None,ping_timeout=None),
        http_server()
    )
asyncio.run(main())
