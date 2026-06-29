/* base64.h - 轻量级Base64编解码，用于音频数据传输 */
#ifndef BASE64_H
#define BASE64_H

#include <Arduino.h>

namespace base64 {
    // 编码：二进制 → Base64字符串
    String encode(const uint8_t* data, size_t len);
    // 解码：Base64字符串 → 二进制，返回实际解码字节数
    size_t decode(const char* input, uint8_t* output, size_t maxOutputLen);
    // 计算base64编码后的长度
    size_t encodedLength(size_t rawLen);
    // 计算base64解码后的最大长度
    size_t decodedMaxLength(const char* input);
}

#endif // BASE64_H
