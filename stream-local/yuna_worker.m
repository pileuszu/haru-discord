#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

// stdin: uint32 byte length + UTF-8 phrase. stdout: uint32 lengths + PCM
// blocks, with a zero length marking completion. macOS hosts are little-endian.
static AVSpeechSynthesizer *synth;
static AVSpeechSynthesisVoice *voice;

static void writeBlock(const void *data, uint32_t size) {
    if (fwrite(&size, sizeof(size), 1, stdout) != 1 ||
        (size && fwrite(data, 1, size, stdout) != size) || fflush(stdout)) {
        exit(1);
    }
}

static void writePCM(AVAudioPCMBuffer *pcm) {
    if (pcm.format.sampleRate != 22050 || pcm.format.channelCount != 1) {
        fprintf(stderr, "Expected mono 22050 Hz Yuna audio\n");
        exit(2);
    }
    uint32_t frames = pcm.frameLength;
    if (pcm.format.commonFormat == AVAudioPCMFormatInt16 && pcm.stride == 1) {
        writeBlock(pcm.int16ChannelData[0], frames * sizeof(int16_t));
        return;
    }
    NSMutableData *data = [NSMutableData dataWithLength:frames * sizeof(int16_t)];
    int16_t *samples = data.mutableBytes;
    for (uint32_t i = 0; i < frames; ++i) {
        NSUInteger index = i * pcm.stride;
        switch (pcm.format.commonFormat) {
            case AVAudioPCMFormatFloat32: {
                float scaled = pcm.floatChannelData[0][index] * 32768.0f;
                samples[i] = (int16_t)lrintf(fmaxf(-32768.0f, fminf(scaled, 32767.0f)));
                break;
            }
            case AVAudioPCMFormatInt16:
                samples[i] = pcm.int16ChannelData[0][index];
                break;
            case AVAudioPCMFormatInt32:
                samples[i] = (int16_t)(pcm.int32ChannelData[0][index] >> 16);
                break;
            default:
                fprintf(stderr, "Unsupported Yuna sample format\n");
                exit(2);
        }
    }
    writeBlock(data.bytes, (uint32_t)data.length);
}

static void serve(void) {
    uint32_t length;
    while (fread(&length, sizeof(length), 1, stdin) == 1) {
        @autoreleasepool {
            if (!length || length > 8192) exit(2);
            NSMutableData *data = [NSMutableData dataWithLength:length];
            if (fread(data.mutableBytes, 1, length, stdin) != length) exit(2);
            NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
            if (!text) exit(2);
            dispatch_semaphore_t finished = dispatch_semaphore_create(0);
            dispatch_async(dispatch_get_main_queue(), ^{
                AVSpeechUtterance *utterance = [AVSpeechUtterance speechUtteranceWithString:text];
                utterance.voice = voice;
                __block BOOL ended = NO;
                [synth writeUtterance:utterance toBufferCallback:^(AVAudioBuffer *buffer) {
                    @autoreleasepool {
                        if (ended) return;
                        AVAudioPCMBuffer *pcm = (AVAudioPCMBuffer *)buffer;
                        if (pcm.frameLength) {
                            writePCM(pcm);
                        } else {
                            ended = YES;
                            writeBlock(NULL, 0);
                            dispatch_semaphore_signal(finished);
                        }
                    }
                }];
            });
            dispatch_semaphore_wait(finished, DISPATCH_TIME_FOREVER);
        }
    }
    exit(0);
}

int main(void) {
    @autoreleasepool {
        voice = [AVSpeechSynthesisVoice voiceWithIdentifier:@"com.apple.voice.compact.ko-KR.Yuna"];
        if (!voice || ![voice.language isEqualToString:@"ko-KR"]) {
            fprintf(stderr, "Korean Yuna voice is unavailable\n");
            return 2;
        }
        synth = [AVSpeechSynthesizer new];
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{ serve(); });
        [[NSRunLoop mainRunLoop] run];
    }
    return 0;
}
