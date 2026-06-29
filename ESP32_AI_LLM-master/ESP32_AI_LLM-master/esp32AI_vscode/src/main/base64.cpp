/* base64.cpp - 轻量级Base64编解码实现 */
#include "base64.h"

static const char base64_chars[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789+/";

namespace base64 {

String encode(const uint8_t* data, size_t len) {
    String result;
    result.reserve((len + 2) / 3 * 4 + 1);

    for (size_t i = 0; i < len; i += 3) {
        uint32_t val = (uint32_t)data[i] << 16;
        if (i + 1 < len) val |= (uint32_t)data[i + 1] << 8;
        if (i + 2 < len) val |= (uint32_t)data[i + 2];

        result += base64_chars[(val >> 18) & 0x3F];
        result += base64_chars[(val >> 12) & 0x3F];
        result += (i + 1 < len) ? base64_chars[(val >> 6) & 0x3F] : '=';
        result += (i + 2 < len) ? base64_chars[val & 0x3F] : '=';
    }
    return result;
}

size_t decode(const char* input, uint8_t* output, size_t maxOutputLen) {
    static const uint8_t lookup[256] = {
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,62,0,0,0,63,52,53,54,55,56,57,58,59,60,61,0,0,0,0,0,0,
        0,0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,0,0,0,0,0,
        0,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
    };

    size_t outIdx = 0;
    size_t inIdx = 0;
    size_t inLen = strlen(input);
    uint32_t val = 0;
    int bits = 0;
    uint8_t b;

    while (inIdx < inLen && outIdx < maxOutputLen) {
        b = lookup[(uint8_t)input[inIdx++]];
        if (b == 0 && input[inIdx - 1] != 'A' && input[inIdx - 1] != 'a' && input[inIdx - 1] != '0') {
            continue; // 跳过非法字符
        }
        val = (val << 6) | b;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            output[outIdx++] = (val >> bits) & 0xFF;
        }
    }
    return outIdx;
}

size_t encodedLength(size_t rawLen) {
    return ((rawLen + 2) / 3) * 4;
}

size_t decodedMaxLength(const char* input) {
    size_t len = strlen(input);
    return (len / 4) * 3;
}

} // namespace base64
