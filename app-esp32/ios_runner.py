from __future__ import annotations

import struct
from pathlib import Path

from rubicon.objc import ObjCClass
from rubicon.objc.runtime import load_library


def _mixed_sample(channel_data, channels: int, index: int) -> float:
    if channels <= 1:
        return float(channel_data[0][index])
    total = 0.0
    for channel in range(channels):
        total += float(channel_data[channel][index])
    return total / channels


def decode_audio_to_pcm(audio_path: str, pcm_path: str, sample_rate: int = 16000) -> str:
    """Decode an iOS-supported audio file to mono signed 16-bit PCM.

    AVFoundation handles the compressed audio decode. Python handles the
    lightweight mono mixdown and linear resample so we don't need ffmpeg in
    the iOS bundle.
    """
    source = Path(audio_path)
    target = Path(pcm_path)
    if not source.exists():
        raise FileNotFoundError(str(source))

    load_library("AVFoundation")
    NSURL = ObjCClass("NSURL")
    AVAudioFile = ObjCClass("AVAudioFile")
    AVAudioPCMBuffer = ObjCClass("AVAudioPCMBuffer")

    audio_url = NSURL.fileURLWithPath_(str(source))
    audio_file = AVAudioFile.alloc().initForReading_error_(audio_url, None)
    if not audio_file:
        raise RuntimeError("AVAudioFile 打开音频失败")

    input_format = audio_file.processingFormat
    frame_capacity = int(audio_file.length)
    if frame_capacity <= 0:
        raise RuntimeError("音频长度无效")

    buffer = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(input_format, frame_capacity)
    ok = audio_file.readIntoBuffer_error_(buffer, None)
    if not ok:
        raise RuntimeError("AVAudioFile 读取音频失败")

    frame_count = int(buffer.frameLength)
    channels = int(input_format.channelCount)
    source_rate = float(input_format.sampleRate)
    channel_data = buffer.floatChannelData
    if frame_count <= 0 or channels <= 0 or source_rate <= 0 or not channel_data:
        raise RuntimeError("AVFoundation 没有输出可用 PCM 数据")

    output_frames = max(1, int(frame_count * sample_rate / source_rate))
    step = source_rate / float(sample_rate)
    pcm = bytearray(output_frames * 2)

    for out_index in range(output_frames):
        src_pos = out_index * step
        src_index = int(src_pos)
        if src_index >= frame_count - 1:
            value = _mixed_sample(channel_data, channels, frame_count - 1)
        else:
            frac = src_pos - src_index
            a = _mixed_sample(channel_data, channels, src_index)
            b = _mixed_sample(channel_data, channels, src_index + 1)
            value = a + (b - a) * frac
        if value > 1.0:
            value = 1.0
        elif value < -1.0:
            value = -1.0
        struct.pack_into("<h", pcm, out_index * 2, int(value * 32767))

    target.write_bytes(pcm)
    return f"iOS AVFoundation decoded {frame_count} frames to {output_frames} frames"
