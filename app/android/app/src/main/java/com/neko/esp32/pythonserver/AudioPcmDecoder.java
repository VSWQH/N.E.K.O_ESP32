package com.neko.esp32.pythonserver;

import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;

import java.io.ByteArrayOutputStream;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;

public final class AudioPcmDecoder {
    private AudioPcmDecoder() {}

    public static String decodeToPcm(String inputPath, String outputPath, int targetRate) throws Exception {
        MediaExtractor extractor = new MediaExtractor();
        MediaCodec codec = null;
        FileOutputStream out = null;
        try {
            extractor.setDataSource(inputPath);
            int track = -1;
            MediaFormat format = null;
            String mime = null;
            for (int i = 0; i < extractor.getTrackCount(); i++) {
                MediaFormat candidate = extractor.getTrackFormat(i);
                String candidateMime = candidate.getString(MediaFormat.KEY_MIME);
                if (candidateMime != null && candidateMime.startsWith("audio/")) {
                    track = i;
                    format = candidate;
                    mime = candidateMime;
                    break;
                }
            }
            if (track < 0 || format == null || mime == null) {
                throw new IllegalArgumentException("没有找到音频轨道");
            }
            extractor.selectTrack(track);
            int sourceRate = format.containsKey(MediaFormat.KEY_SAMPLE_RATE) ? format.getInteger(MediaFormat.KEY_SAMPLE_RATE) : targetRate;
            int channels = format.containsKey(MediaFormat.KEY_CHANNEL_COUNT) ? format.getInteger(MediaFormat.KEY_CHANNEL_COUNT) : 1;

            codec = MediaCodec.createDecoderByType(mime);
            codec.configure(format, null, null, 0);
            codec.start();
            out = new FileOutputStream(outputPath);
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            PcmResampler resampler = new PcmResampler(sourceRate, targetRate, channels);
            boolean inputDone = false;
            boolean outputDone = false;
            int total = 0;

            while (!outputDone) {
                if (!inputDone) {
                    int inputIndex = codec.dequeueInputBuffer(10000);
                    if (inputIndex >= 0) {
                        ByteBuffer input = codec.getInputBuffer(inputIndex);
                        if (input == null) {
                            continue;
                        }
                        input.clear();
                        int size = extractor.readSampleData(input, 0);
                        if (size < 0) {
                            codec.queueInputBuffer(inputIndex, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                            inputDone = true;
                        } else {
                            codec.queueInputBuffer(inputIndex, 0, size, extractor.getSampleTime(), 0);
                            extractor.advance();
                        }
                    }
                }

                int outputIndex = codec.dequeueOutputBuffer(info, 10000);
                if (outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    MediaFormat newFormat = codec.getOutputFormat();
                    sourceRate = newFormat.containsKey(MediaFormat.KEY_SAMPLE_RATE) ? newFormat.getInteger(MediaFormat.KEY_SAMPLE_RATE) : sourceRate;
                    channels = newFormat.containsKey(MediaFormat.KEY_CHANNEL_COUNT) ? newFormat.getInteger(MediaFormat.KEY_CHANNEL_COUNT) : channels;
                    resampler = new PcmResampler(sourceRate, targetRate, channels);
                } else if (outputIndex >= 0) {
                    ByteBuffer output = codec.getOutputBuffer(outputIndex);
                    if (output != null && info.size > 0) {
                        output.position(info.offset);
                        output.limit(info.offset + info.size);
                        byte[] pcm = new byte[info.size];
                        output.get(pcm);
                        byte[] converted = resampler.convert(pcm);
                        out.write(converted);
                        total += converted.length;
                    }
                    boolean eos = (info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
                    codec.releaseOutputBuffer(outputIndex, false);
                    if (eos) {
                        outputDone = true;
                    }
                }
            }
            return "decoded " + total + " bytes";
        } finally {
            try {
                if (out != null) out.close();
            } catch (Exception ignored) {}
            try {
                if (codec != null) {
                    codec.stop();
                    codec.release();
                }
            } catch (Exception ignored) {}
            try {
                extractor.release();
            } catch (Exception ignored) {}
        }
    }

    private static final class PcmResampler {
        private int sourceRate;
        private int targetRate;
        private int channels;
        private double nextFrame = 0.0;
        private long sourceFrameBase = 0;

        PcmResampler(int sourceRate, int targetRate, int channels) {
            this.sourceRate = Math.max(1, sourceRate);
            this.targetRate = Math.max(1, targetRate);
            this.channels = Math.max(1, channels);
        }

        byte[] convert(byte[] pcm) {
            int frameSize = channels * 2;
            int frames = pcm.length / frameSize;
            if (frames <= 0) return new byte[0];
            double step = sourceRate / (double) targetRate;
            long chunkEnd = sourceFrameBase + frames;
            ByteArrayOutputStream out = new ByteArrayOutputStream(Math.max(2, (int)(frames / step) * 2));
            while (nextFrame < chunkEnd) {
                int localFrame = (int)(nextFrame - sourceFrameBase);
                if (localFrame >= 0 && localFrame < frames) {
                    int mixed = 0;
                    for (int ch = 0; ch < channels; ch++) {
                        int pos = (localFrame * channels + ch) * 2;
                        int lo = pcm[pos] & 0xff;
                        int hi = pcm[pos + 1];
                        short sample = (short)((hi << 8) | lo);
                        mixed += sample;
                    }
                    short mono = (short)(mixed / channels);
                    out.write(mono & 0xff);
                    out.write((mono >> 8) & 0xff);
                }
                nextFrame += step;
            }
            sourceFrameBase = chunkEnd;
            return out.toByteArray();
        }
    }
}
