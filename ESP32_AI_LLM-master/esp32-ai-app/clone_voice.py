"""语音克隆工具 — 上传音频到百炼，克隆音色用于 Qwen-Omni"""
import base64, pathlib, json, os, requests

CFG_FILE = os.path.join(os.path.dirname(__file__),"esp32中转站","config.json")
CFG = json.load(open(CFG_FILE, encoding="utf-8"))
KEY = CFG["api_key"]

AUDIO = input("音频文件路径(10~20s清晰人声): ").strip().strip('"')
NAME  = input("音色名称前缀: ").strip() or "my_voice"

fp = pathlib.Path(AUDIO)
b64 = base64.b64encode(fp.read_bytes()).decode()

url = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
payload = {
    "model": "qwen-voice-enrollment",
    "input": {
        "action": "create",
        "target_model": "qwen3.5-omni-flash-realtime",
        "preferred_name": NAME,
        "audio": {"data": f"data:audio/wav;base64,{b64}"}
    }
}
headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
resp = requests.post(url, json=payload, headers=headers)

if resp.status_code == 200:
    vid = resp.json()["output"]["voice"]
    print(f"\n✅ 克隆成功! voice_id: {vid}")
    CFG["voice"] = vid
    json.dump(CFG, open(CFG_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"已更新 config.json → 可在 APP 自定义音色ID 里填入: {vid}")
else:
    print(f"❌ 失败: {resp.status_code} {resp.text}")
