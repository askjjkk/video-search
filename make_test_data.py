"""Делает тестовые ролики для проверки программы (озвучка Windows, без чужих файлов)."""
import math
import os
import shutil
import struct
import subprocess
import sys
import wave

import av

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "test_data")


def tts(text, voice, wav_path):
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SelectVoice('{voice}');$s.Rate=0;"
          f"$s.SetOutputToWaveFile('{wav_path}');$s.Speak([IO.File]::ReadAllText('{wav_path}.txt',[Text.Encoding]::UTF8));$s.Dispose()")
    with open(wav_path + ".txt", "w", encoding="utf-8") as f:
        f.write(text)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    os.remove(wav_path + ".txt")


def music(wav_path, sec=12):
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        frames = bytearray()
        for i in range(16000 * sec):
            t = i / 16000
            note = (220, 277, 330, 440)[int(t * 2) % 4]
            frames += struct.pack("<h", int(6000 * math.sin(2 * math.pi * note * t)))
        w.writeframes(bytes(frames))


def to_mp4(wav_path, mp4_path):
    inp = av.open(wav_path)
    out = av.open(mp4_path, "w")
    s_out = out.add_stream("aac", rate=16000)
    s_in = inp.streams.audio[0]
    for frame in inp.decode(s_in):
        frame.pts = None
        for p in s_out.encode(frame):
            out.mux(p)
    for p in s_out.encode(None):
        out.mux(p)
    out.close(); inp.close()


os.makedirs(os.path.join(OUT, "вложенная"), exist_ok=True)
tmp = os.path.join(OUT, "_tmp.wav")
cases = [
    ("ru1.mp4", "Microsoft Irina Desktop", "Сегодня мы расскажем про тёмную сторону силы и про кошек, которые живут во дворе."),
    ("вложенная/ru2.mp4", "Microsoft Irina Desktop", "Мой кот любит спать на диване, а соседские кошки гуляют на улице целыми днями."),
    ("вложенная/ru3.mp4", "Microsoft Irina Desktop", "Завтра будет хорошая погода, поэтому мы поедем на дачу и посадим картошку."),
    ("en1.mp4", "Microsoft Zira Desktop", "Today we will talk about the weather, the garden and how to cook a good dinner for friends."),
]
for name, voice, text in cases:
    tts(text, voice, tmp)
    to_mp4(tmp, os.path.join(OUT, name))
music(tmp)
to_mp4(tmp, os.path.join(OUT, "music1.mp4"))
os.remove(tmp)
shutil.copy2(os.path.join(OUT, "ru1.mp4"), os.path.join(OUT, "вложенная", "ru1_копия.mp4"))
print("Готово:", OUT)

