package cn.idi.camera;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.util.Base64;

/** Bounded, ordered transfer of the vendor's original Base64 JPEG; no image conversion. */
final class H5PhotoTransfer {
  static final int MAX_BYTES = 20 * 1024 * 1024;
  static final int CHUNK_CHARS = 65536;
  private final int expectedChars;
  private final ByteArrayOutputStream bytes;
  private int receivedChars, next;
  private boolean finished;

  H5PhotoTransfer(int chars) throws IOException {
    if (chars <= 0 || chars % 4 != 0 || chars > ((MAX_BYTES + 2) / 3) * 4)
      throw new IOException("SDK 原片大小无效");
    expectedChars = chars;
    bytes = new ByteArrayOutputStream(Math.min(chars / 4 * 3, MAX_BYTES));
  }

  synchronized void append(int sequence, String chunk) throws IOException {
    if (finished || sequence != next || chunk == null || chunk.isEmpty()
        || chunk.length() > CHUNK_CHARS || chunk.length() % 4 != 0
        || receivedChars + chunk.length() > expectedChars
        || !chunk.matches("[A-Za-z0-9+/]*={0,2}")
        || chunk.contains("=") && receivedChars + chunk.length() != expectedChars)
      throw new IOException("SDK 原片传输不完整，请重新拍摄");
    try {
      byte[] decoded = Base64.getDecoder().decode(chunk);
      if (bytes.size() + decoded.length >= MAX_BYTES) throw new IOException("SDK 原片超过支持大小");
      bytes.write(decoded);
    } catch (IllegalArgumentException error) {
      throw new IOException("SDK 原片编码无效", error);
    }
    receivedChars += chunk.length();
    next++;
  }

  synchronized byte[] finish() throws IOException {
    if (finished || receivedChars != expectedChars) throw new IOException("SDK 原片传输尚未完成");
    finished = true;
    return bytes.toByteArray();
  }
}
