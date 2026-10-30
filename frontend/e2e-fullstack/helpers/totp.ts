import { createHmac } from "node:crypto";

/**
 * RFC 6238 TOTP（30 秒 / 6 位 / SHA-1），参数与后端 `app/security/totp.py` 一致。
 * secret 是种子账号文件里的 Base32 明文（git 忽略，仅测试域）。
 */

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const STEP_SECONDS = 30;
const DIGITS = 6;

export function base32Decode(secret: string): Buffer {
  const normalized = secret.toUpperCase().replace(/[=\s]/g, "");
  let bits = 0;
  let value = 0;
  const bytes: number[] = [];
  for (const char of normalized) {
    const index = BASE32_ALPHABET.indexOf(char);
    if (index < 0) throw new Error(`TOTP secret 含非法 Base32 字符：${char}`);
    value = ((value << 5) | index) & 0xffffffff;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((value >>> bits) & 0xff);
    }
  }
  return Buffer.from(bytes);
}

export function totpCode(secret: string, atMs = Date.now()): string {
  const step = Math.floor(atMs / 1000 / STEP_SECONDS);
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", base32Decode(secret)).update(message).digest();
  const offset = (digest[digest.length - 1] ?? 0) & 0x0f;
  const binary = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(binary % 10 ** DIGITS).padStart(DIGITS, "0");
}
